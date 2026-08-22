from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from authentication.models import APIKey, Application
from routes.models import BackendService, Route
from users.models import Project
from gateway.services import ProductResponseCache, RedisRateLimiter


class GatewayRoutingTests(APITestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='gateway-owner',
            email='gateway@example.com',
            password='password123',
        )
        self.project = Project.objects.create(owner=self.user, name='Orders', slug='orders')
        self.product_service = BackendService.objects.create(project=self.project, name='Product Service', slug='product-service')
        self.api_key, self.raw_key = APIKey.create_for_project(self.project, 'gateway-key')
        cache.clear()
        ProductResponseCache._memory_store.clear()
        RedisRateLimiter._memory_store.clear()

    def _auth_headers(self):
        token = RefreshToken.for_user(self.user).access_token
        return {'X-API-Key': self.raw_key, 'Authorization': f'Bearer {token}'}

    def _make_backend_server(self, payload='ok', status=200):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = payload.encode('utf-8')
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread

    def test_successful_forwarding(self):
        server, thread = self._make_backend_server(payload='{"status": "ok"}')
        port = server.server_address[1]
        Route.objects.create(
            project=self.project,
            backend_service=self.product_service,
            name='Users route',
            method='GET',
            path='/users',
            target_url=f'http://127.0.0.1:{port}/users',
        )
        response = self.client.get('/gateway/users', headers={'X-API-Key': self.raw_key})
        self.assertEqual(response.status_code, 200)
        self.assertIn('status', response.json())
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    def test_customer_api_requires_jwt(self):
        Route.objects.create(
            project=self.project,
            backend_service=self.product_service,
            name='Products',
            method='GET',
            path='/products',
            target_url='http://127.0.0.1:9002/products',
        )
        response = self.client.get('/api/products', headers={'X-API-Key': self.raw_key})
        self.assertEqual(response.status_code, 401)

    def test_customer_api_routes_to_configured_service(self):
        server, thread = self._make_backend_server(payload='[{"id": 1, "name": "Laptop", "price": 50000}]')
        port = server.server_address[1]
        route = Route.objects.create(
            project=self.project,
            backend_service=self.product_service,
            name='Products',
            method='GET',
            path='/products',
            target_url=f'http://127.0.0.1:{port}/products',
        )
        response = self.client.get('/api/products', headers=self._auth_headers())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()[0]['name'], 'Laptop')
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    def test_project_can_use_custom_banking_backend_service(self):
        banking_project = Project.objects.create(owner=self.user, name='Banking API', slug='banking-api')
        account_service = BackendService.objects.create(project=banking_project, name='Account Service', slug='account-service')
        banking_key, raw_key = APIKey.create_for_project(banking_project, 'phonepe-key')
        server, thread = self._make_backend_server(payload='{"account_id": 42, "balance": 1000}')
        port = server.server_address[1]
        route = Route.objects.create(
            project=banking_project,
            backend_service=account_service,
            name='Accounts',
            method='GET',
            path='/accounts',
            target_url=f'http://127.0.0.1:{port}/accounts',
        )

        response = self.client.get('/gateway/accounts', headers={'X-API-Key': raw_key})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['account_id'], 42)
        self.assertEqual(banking_key.project, banking_project)
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    def test_route_without_target_returns_no_healthy_backend(self):
        transaction_service = BackendService.objects.create(
            project=self.project,
            name='Transaction Service',
            slug='transaction-service',
        )
        Route.objects.create(
            project=self.project,
            backend_service=transaction_service,
            name='Transactions',
            method='GET',
            path='/transactions',
        )

        response = self.client.get('/gateway/transactions', headers={'X-API-Key': self.raw_key})

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['detail'], 'No healthy backends for this route.')

    def test_invalid_route(self):
        response = self.client.get('/gateway/missing', headers={'X-API-Key': self.raw_key})
        self.assertEqual(response.status_code, 404)

    def test_backend_unavailable(self):
        Route.objects.create(
            project=self.project,
            backend_service=self.product_service,
            name='Broken route',
            method='GET',
            path='/broken',
            target_url='http://127.0.0.1:1/broken',
        )
        response = self.client.get('/gateway/broken', headers={'X-API-Key': self.raw_key})
        self.assertEqual(response.status_code, 503)

    def test_rate_limiting_blocks_by_api_key_across_routes(self):
        application = self.api_key.application
        application.plan = Application.PLAN_NORMAL
        application.rate_limit = 1
        application.window_seconds = 60
        application.save(update_fields=['plan', 'rate_limit', 'window_seconds'])
        Route.objects.create(
            project=self.project,
            backend_service=self.product_service,
            name='Limited',
            method='GET',
            path='/limited',
            target_url='http://127.0.0.1:9002/products',
        )
        Route.objects.create(
            project=self.project,
            backend_service=self.product_service,
            name='Other limited',
            method='GET',
            path='/other-limited',
            target_url='http://127.0.0.1:9002/other-products',
        )
        with patch('gateway.views.GatewayRequestProxy.proxy') as proxy:
            proxy.return_value = type('Response', (), {
                'headers': {'content-type': 'application/json'},
                'status_code': 200,
                'text': '{"ok": true}',
                'content': b'{"ok": true}',
            })()
            first = self.client.get('/gateway/limited', headers={'X-API-Key': self.raw_key})
            second = self.client.get('/gateway/other-limited', headers={'X-API-Key': self.raw_key})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 429)

    def test_product_cache_hit_avoids_second_backend_call(self):
        route = Route.objects.create(
            project=self.project,
            backend_service=self.product_service,
            name='Cached products',
            method='GET',
            path='/products',
            target_url='http://127.0.0.1:9002/products',
            cache_ttl_seconds=60,
        )
        with patch('gateway.views.GatewayRequestProxy.proxy') as proxy:
            proxy.return_value = type('Response', (), {
                'headers': {'content-type': 'application/json'},
                'status_code': 200,
                'text': '[{"id": 1}]',
                'content': b'[{"id": 1}]',
            })()
            first = self.client.get('/gateway/products', headers={'X-API-Key': self.raw_key})
            second = self.client.get('/gateway/products', headers={'X-API-Key': self.raw_key})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(proxy.call_count, 1)
