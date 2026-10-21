from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from unittest.mock import patch
from urllib.parse import urlsplit

import httpx
import redis
from django.contrib.auth import get_user_model
from django.test import override_settings
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken
from client_auth.models import ApplicationUser
from client_auth.services import issue_token_pair

from authentication.models import APIKey, Application, RoutePermission
from gateway.services import ProductResponseCache, RedisRateLimiter
from routes.models import BackendService, Route, RouteMethod
from users.models import Project


class GatewayRoutingTests(APITestCase):
    def setUp(self):
        self.origins = ['http://127.0.0.1:9002', 'http://127.0.0.1:1']
        ctx = override_settings(
            GATEWAY_ALLOWED_BACKEND_ORIGINS=self.origins,
            GATEWAY_CACHEABLE_BACKEND_HOSTS=['127.0.0.1'],
        )
        ctx.enable()
        self.addCleanup(ctx.disable)
        offline = patch('redis.Redis.execute_command', side_effect=redis.ConnectionError('test offline'))
        offline.start()
        self.addCleanup(offline.stop)
        self.user = get_user_model().objects.create_user(
            username='owner', email='owner@example.com', password='test-password123'
        )
        self.project = Project.objects.create(owner=self.user, name='Healthcare', slug='healthcare')
        self.service = BackendService.objects.create(
            project=self.project, name='Doctor Service', slug='doctor-service',
        )
        self.api_key, self.key = APIKey.create_for_project(self.project, 'key')
        ProductResponseCache._memory_store.clear()
        RedisRateLimiter._memory_store.clear()

    def route(self, path='/doctors', url='http://127.0.0.1:9002/doctors',
              methods=None, service=None):
        backend = service or self.service
        if url:
            parsed = urlsplit(url)
            backend.base_url = f'{parsed.scheme}://{parsed.netloc}'
        else:
            backend.base_url = ''
        backend.save(update_fields=['base_url'])
        route = Route.objects.create(
            project=self.project, backend_service=backend,
            name=path, base_path=path,
        )
        for method, policy in (methods or [('GET', 'api_key_only')]):
            RouteMethod.objects.create(route=route, method=method, auth_policy=policy)
        return route

    def headers(self, jwt=False, raw_key=None):
        headers = {'HTTP_X_API_KEY': raw_key or self.key}
        if jwt:
            user, _ = ApplicationUser.objects.get_or_create(
                application=self.api_key.application,
                email='doctor@hospital.example',
                defaults={'password_hash': 'unusable-password'},
            )
            headers['HTTP_AUTHORIZATION'] = 'Bearer ' + issue_token_pair(user)['access']
        return headers

    def fake_backend(self):
        seen = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append((self.command, self.path))
                payload = b'{"ok":true}'
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        self.addCleanup(lambda: thread.join(timeout=2))
        origin = f'http://127.0.0.1:{server.server_address[1]}'
        self.origins.append(origin)
        return origin, seen

    def test_dynamic_suffix_and_query_forwarding(self):
        origin, seen = self.fake_backend()
        self.route(url=origin + '/doctors')
        response = self.client.get('/gateway/doctors/456?active=yes', **self.headers())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(seen, [('GET', '/doctors/456?active=yes')])

    def test_two_public_routes_share_one_service_origin(self):
        self.route(path='/doctors', url='http://127.0.0.1:9002/doctors')
        self.route(path='/specialties', url='http://127.0.0.1:9002/specialties')
        self.assertEqual(Route.objects.count(), 2)
        self.assertEqual(
            Route.objects.values_list('backend_service_id', flat=True).distinct().count(), 1
        )
        with patch('gateway.views.GatewayRequestProxy.proxy',
                   return_value=httpx.Response(200, json={'ok': True})) as proxy:
            self.assertEqual(
                self.client.get('/gateway/doctors/435', **self.headers()).status_code, 200
            )
            self.assertEqual(
                self.client.get('/gateway/specialties/8', **self.headers()).status_code, 200
            )
        self.assertEqual(
            [call.args[1] for call in proxy.call_args_list],
            ['http://127.0.0.1:9002/doctors/435',
             'http://127.0.0.1:9002/specialties/8']
        )

    def test_get_post_delete_share_route_with_independent_authentication(self):
        route = self.route(methods=[
            ('GET', 'api_key_only'), ('POST', 'api_key_and_jwt'),
            ('DELETE', 'api_key_and_jwt'),
        ])
        self.assertEqual(Route.objects.count(), 1)
        self.assertEqual(RouteMethod.objects.filter(route=route).count(), 3)
        with patch('gateway.views.GatewayRequestProxy.proxy',
                   return_value=httpx.Response(200, json={'ok': True})) as proxy:
            self.assertEqual(self.client.get('/gateway/doctors', **self.headers()).status_code, 200)
            self.assertEqual(self.client.post('/gateway/doctors', **self.headers()).status_code, 401)
            self.assertEqual(self.client.delete('/gateway/doctors/456', **self.headers()).status_code, 401)
            staff_access = str(RefreshToken.for_user(self.user).access_token)
            self.assertEqual(self.client.post('/gateway/doctors', **self.headers(
                jwt=False), HTTP_AUTHORIZATION='Bearer ' + staff_access).status_code, 401)
            self.assertEqual(self.client.post('/gateway/doctors', **self.headers(jwt=True)).status_code, 200)
            self.assertEqual(proxy.call_count, 2)

    def test_missing_method_policy_denies_without_fallback(self):
        self.route()
        with patch('gateway.views.GatewayRequestProxy.proxy') as proxy:
            response = self.client.delete('/gateway/doctors/456', **self.headers())
            self.assertEqual(response.status_code, 405)
            proxy.assert_not_called()

    def test_matching_is_segment_aware_and_uses_longest_prefix(self):
        self.route()
        second_service = BackendService.objects.create(
            project=self.project, name='Doctor Admin Service'
        )
        self.route(
            path='/doctors/admin', url='http://127.0.0.1:9002/doctors/admin',
            methods=[('GET', 'api_key_and_jwt')], service=second_service,
        )
        self.assertEqual(self.client.get('/gateway/doctors-other', **self.headers()).status_code, 404)
        self.assertEqual(self.client.get('/gateway/doctors/admin', **self.headers()).status_code, 401)
        # DELETE should not fall through to a less restrictive /doctors mapping.
        self.assertEqual(self.client.delete('/gateway/doctors/admin', **self.headers()).status_code, 405)

    def test_cross_project_and_route_permission_denied(self):
        local_route = self.route()
        other = Project.objects.create(owner=self.user, name='Banking', slug='banking')
        foreign_service = BackendService.objects.create(project=other, name='Accounts')
        foreign = Route.objects.create(
            project=other, name='Accounts', backend_service=foreign_service,
            base_path='/accounts',
        )
        RouteMethod.objects.create(route=foreign, method='GET')
        self.assertEqual(self.client.get('/gateway/accounts', **self.headers()).status_code, 404)
        app = self.api_key.application
        RoutePermission.objects.create(application=app, route=foreign)
        self.assertEqual(self.client.get('/gateway/doctors', **self.headers()).status_code, 403)
        self.assertFalse(app.has_permission(local_route))

    def test_empty_destination_returns_503_even_with_suffix(self):
        self.route(url='')
        response = self.client.get('/gateway/doctors/456', **self.headers())
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['detail'], 'No healthy backends for this route.')

    def test_disabled_service_is_not_routed(self):
        self.route()
        self.service.is_active = False
        self.service.save(update_fields=['is_active'])
        self.assertEqual(self.client.get('/gateway/doctors', **self.headers()).status_code, 404)

    def test_rate_limit_shared_across_methods(self):
        self.route(methods=[('GET', 'api_key_only'), ('POST', 'api_key_only')])
        with patch.dict(Application.PLAN_LIMITS, {Application.PLAN_NORMAL: (1, 60)}), patch(
            'gateway.views.GatewayRequestProxy.proxy',
            return_value=httpx.Response(200, json={'ok': True}),
        ):
            self.assertEqual(self.client.get('/gateway/doctors', **self.headers()).status_code, 200)
            self.assertEqual(self.client.post('/gateway/doctors', **self.headers()).status_code, 429)

    def test_response_cache_isolated_by_application_and_query(self):
        self.route()
        other = Application.objects.create(project=self.project, name='Hospital B')
        _, other_key = APIKey.create_for_application(other, 'other-key')
        with patch('gateway.views.GatewayRequestProxy.proxy',
                   return_value=httpx.Response(200, json={'ok': True})) as proxy:
            for _ in range(2):
                self.assertEqual(self.client.get('/gateway/doctors?x=1', **self.headers()).status_code, 200)
            self.assertEqual(self.client.get('/gateway/doctors?x=2', **self.headers()).status_code, 200)
            self.assertEqual(self.client.get(
                '/gateway/doctors?x=1', **self.headers(raw_key=other_key)
            ).status_code, 200)
            self.assertEqual(proxy.call_count, 3)

    def test_get_and_delete_cannot_share_jwt_cache(self):
        self.route(methods=[('GET', 'api_key_only'), ('DELETE', 'api_key_and_jwt')])
        with patch('gateway.views.GatewayRequestProxy.proxy',
                   return_value=httpx.Response(200, json={'ok': True})) as proxy:
            self.assertEqual(self.client.get('/gateway/doctors', **self.headers()).status_code, 200)
            self.assertEqual(self.client.delete('/gateway/doctors', **self.headers()).status_code, 401)
            self.assertEqual(proxy.call_count, 1)

    def test_fixed_window_resets_at_boundary(self):
        limiter = RedisRateLimiter()
        with patch('gateway.services.time.time', side_effect=[0, 1, 59, 60]):
            self.assertTrue(limiter._fallback_allow_request('window:test', 2, 60))
            self.assertTrue(limiter._fallback_allow_request('window:test', 2, 60))
            self.assertFalse(limiter._fallback_allow_request('window:test', 2, 60))
            self.assertTrue(limiter._fallback_allow_request('window:test', 2, 60))

    def test_redis_uses_distinct_keys_for_fixed_windows(self):
        limiter = RedisRateLimiter()
        with patch('gateway.services.time.time', side_effect=[59, 60]), patch.object(
            limiter.client, 'eval', side_effect=[1, 1],
        ) as redis_eval:
            self.assertTrue(limiter.allow_request('window:shared', 1, 60))
            self.assertTrue(limiter.allow_request('window:shared', 1, 60))
        first_call, second_call = redis_eval.call_args_list
        self.assertEqual(first_call.args[2], 'window:shared:0')
        self.assertEqual(second_call.args[2], 'window:shared:1')
        self.assertEqual(first_call.args[4], 1)
        self.assertEqual(second_call.args[4], 60)
