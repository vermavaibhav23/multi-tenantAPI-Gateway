from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from unittest.mock import patch

import httpx
import redis
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import override_settings
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from authentication.models import APIKey, Application, RoutePermission
from routes.models import BackendService, Route
from users.models import Project
from gateway.services import ProductResponseCache, RedisRateLimiter


class GatewayRoutingTests(APITestCase):
    def setUp(self):
        self.allowed_origins = ['http://127.0.0.1:9002', 'http://127.0.0.1:1']
        origins = override_settings(GATEWAY_ALLOWED_BACKEND_ORIGINS=self.allowed_origins,
                                    GATEWAY_CACHEABLE_BACKEND_HOSTS=['127.0.0.1'])
        origins.enable()
        self.addCleanup(origins.disable)
        offline = patch('redis.Redis.execute_command', side_effect=redis.ConnectionError('test offline'))
        offline.start()
        self.addCleanup(offline.stop)
        self.user = get_user_model().objects.create_user(username='owner', email='gateway@example.com',
                                                          password='password123')
        self.project = Project.objects.create(owner=self.user, name='Orders', slug='orders')
        self.api_key, self.raw_key = APIKey.create_for_project(self.project, 'gateway-key')
        cache.clear()
        ProductResponseCache._memory_store.clear()
        RedisRateLimiter._memory_store.clear()

    def add_route(self, path, target_url='', project=None, methods=None, auth_policy=Route.AUTH_API_KEY_ONLY):
        project = project or self.project
        service = BackendService.objects.create(project=project, name=path, slug='service-' + path.strip('/').replace('/', '-'),
                                                base_path=path, target_url=target_url)
        route = Route.objects.create(project=project, backend_service=service, name=path, auth_policy=auth_policy)
        if methods is not None:
            route.allowed_methods = methods
            route.save(update_fields=['allowed_methods'])
        return route

    def headers(self, jwt=False):
        headers = {'HTTP_X_API_KEY': self.raw_key}
        if jwt:
            headers['HTTP_AUTHORIZATION'] = f'Bearer {RefreshToken.for_user(self.user).access_token}'
        return headers

    def fake_backend(self, payload='{"ok": true}'):
        requests = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(self.path)
                body = payload.encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        self.addCleanup(lambda: thread.join(timeout=2))
        origin = f'http://127.0.0.1:{server.server_address[1]}'
        self.allowed_origins.append(origin)
        return origin, requests

    def test_forwards_dynamic_suffix_and_query(self):
        origin, received = self.fake_backend()
        self.add_route('/doctors', origin + '/internal/doctors')
        response = self.client.get('/gateway/doctors/456?active=1', **self.headers())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(received, ['/internal/doctors/456?active=1'])

    def test_get_and_post_reuse_one_route_and_preserve_method(self):
        self.add_route('/doctors', 'http://127.0.0.1:9002/doctors')
        with patch('gateway.views.GatewayRequestProxy.proxy', return_value=httpx.Response(200, json={'ok': True})) as proxy:
            self.assertEqual(self.client.get('/gateway/doctors/456', **self.headers()).status_code, 200)
            self.assertEqual(self.client.post('/gateway/doctors', data=b'hello',
                                              content_type='application/octet-stream', **self.headers()).status_code, 200)
        self.assertEqual(Route.objects.count(), 1)
        self.assertEqual(proxy.call_args.args[0].method, 'POST')
        self.assertEqual(proxy.call_args.args[1], 'http://127.0.0.1:9002/doctors')

    def test_jwt_protected_route(self):
        self.add_route('/doctors', 'http://127.0.0.1:9002/doctors',
                       auth_policy=Route.AUTH_API_KEY_AND_JWT)
        self.assertEqual(self.client.get('/gateway/doctors', **self.headers()).status_code, 401)
        with patch('gateway.views.GatewayRequestProxy.proxy', return_value=httpx.Response(200, json={'ok': True})):
            self.assertEqual(self.client.get('/gateway/doctors', **self.headers(jwt=True)).status_code, 200)

    def test_unconfigured_route_returns_503(self):
        self.add_route('/doctors')
        response = self.client.get('/gateway/doctors/456', **self.headers())
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['detail'], 'No healthy backends for this route.')

    def test_missing_or_prefix_collision_returns_404(self):
        self.add_route('/doctors', 'http://127.0.0.1:9002/doctors')
        self.assertEqual(self.client.get('/gateway/missing', **self.headers()).status_code, 404)
        self.assertEqual(self.client.get('/gateway/doctors-other', **self.headers()).status_code, 404)

    def test_service_must_match_project_and_be_active(self):
        route = self.add_route('/doctors', 'http://127.0.0.1:9002/doctors')
        other = Project.objects.create(owner=self.user, name='Other', slug='other')
        foreign = BackendService.objects.create(project=other, name='Foreign', base_path='/doctors')
        Route.objects.filter(pk=route.pk).update(backend_service=foreign)
        self.assertEqual(self.client.get('/gateway/doctors', **self.headers()).status_code, 404)
        Route.objects.filter(pk=route.pk).update(backend_service=route.backend_service)
        route.backend_service.is_active = False
        route.backend_service.save(update_fields=['is_active'])
        self.assertEqual(self.client.get('/gateway/doctors', **self.headers()).status_code, 404)

    def test_disallowed_route_method_never_reaches_backend(self):
        self.add_route('/doctors', 'http://127.0.0.1:9002/doctors', methods=['GET'])
        with patch('gateway.views.GatewayRequestProxy.proxy') as proxy:
            self.assertEqual(self.client.delete('/gateway/doctors/456', **self.headers()).status_code, 404)
            proxy.assert_not_called()

    def test_method_scoped_application_grant(self):
        route = self.add_route('/doctors', 'http://127.0.0.1:9002/doctors')
        RoutePermission.objects.create(application=self.api_key.application, route=route,
                                       allowed_methods=['GET'])
        with patch('gateway.views.GatewayRequestProxy.proxy', return_value=httpx.Response(200, json={'ok': True})) as proxy:
            self.assertEqual(self.client.get('/gateway/doctors', **self.headers()).status_code, 200)
            self.assertEqual(self.client.delete('/gateway/doctors/456', **self.headers()).status_code, 403)
            self.assertEqual(proxy.call_count, 1)

    def test_unavailable_backend_returns_503(self):
        self.add_route('/broken', 'http://127.0.0.1:1/broken')
        self.assertEqual(self.client.get('/gateway/broken', **self.headers()).status_code, 503)

    def test_rate_limit_applies_across_http_methods(self):
        self.add_route('/doctors', 'http://127.0.0.1:9002/doctors')
        self.api_key.application.plan = Application.PLAN_NORMAL
        self.api_key.application.save(update_fields=['plan'])
        with patch.dict(Application.PLAN_LIMITS, {Application.PLAN_NORMAL: (1, 60)}), patch(
                'gateway.views.GatewayRequestProxy.proxy', return_value=httpx.Response(200, json={'ok': True})):
            self.assertEqual(self.client.get('/gateway/doctors', **self.headers()).status_code, 200)
            self.assertEqual(self.client.post('/gateway/doctors', **self.headers()).status_code, 429)

    def test_cache_is_query_and_application_scoped(self):
        self.add_route('/products', 'http://127.0.0.1:9002/products')
        other = Application.objects.create(project=self.project, name='Other application')
        _, other_key = APIKey.create_for_application(other, 'key')
        with patch('gateway.views.GatewayRequestProxy.proxy', return_value=httpx.Response(200, json={'ok': True})) as proxy:
            self.assertEqual(self.client.get('/gateway/products?page=1', **self.headers()).status_code, 200)
            self.assertEqual(self.client.get('/gateway/products?page=1', **self.headers()).status_code, 200)
            self.assertEqual(self.client.get('/gateway/products?page=2', **self.headers()).status_code, 200)
            self.assertEqual(self.client.get('/gateway/products?page=1', HTTP_X_API_KEY=other_key).status_code, 200)
            self.assertEqual(proxy.call_count, 3)

    def test_token_bucket_fallback_refills_over_time(self):
        limiter = RedisRateLimiter()
        with patch('gateway.services.time.time', side_effect=[0, 0, 29, 30]):
            self.assertTrue(limiter._fallback_allow_request('bucket:test', 2, 60))
            self.assertTrue(limiter._fallback_allow_request('bucket:test', 2, 60))
            self.assertFalse(limiter._fallback_allow_request('bucket:test', 2, 60))
            self.assertTrue(limiter._fallback_allow_request('bucket:test', 2, 60))
