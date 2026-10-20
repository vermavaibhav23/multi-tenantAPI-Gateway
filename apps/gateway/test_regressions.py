from unittest.mock import patch

import httpx
import redis
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.test import RequestFactory, override_settings
from rest_framework.test import APITestCase

from authentication.models import APIKey, Application, RoutePermission
from gateway.services import GatewayRequestProxy, ProductResponseCache, RedisRateLimiter, RouteResolver
from routes.destinations import validate_target_url
from routes.models import BackendService, Route
from users.models import Project, User


@override_settings(GATEWAY_ALLOWED_BACKEND_ORIGINS=['http://fashion-product-service:8000'])
class GatewayRegressionTests(APITestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username='owner', email='owner@example.com')
        self.project = Project.objects.create(owner=self.owner, name='Project', slug='project')
        self.app = Application.objects.create(project=self.project, name='Client')
        _, self.key = APIKey.create_for_application(self.app, 'key')
        self.service = BackendService.objects.create(
            project=self.project, name='Echo', base_path='/echo',
            target_url='http://fashion-product-service:8000/echo')
        self.route = Route.objects.create(project=self.project, backend_service=self.service, name='Echo')
        cache.clear()
        ProductResponseCache._memory_store.clear()
        RedisRateLimiter._memory_store.clear()
        offline = patch('redis.Redis.execute_command', side_effect=redis.ConnectionError('offline'))
        offline.start()
        self.addCleanup(offline.stop)

    def get(self, key=None):
        return self.client.get('/gateway/echo', HTTP_X_API_KEY=key or self.key)

    def test_revoked_permission_denies_only_granted_methods(self):
        self.assertTrue(self.app.has_permission(self.route, 'GET'))
        permission = RoutePermission.objects.create(
            application=self.app, route=self.route, allowed_methods=['GET'])
        self.assertTrue(self.app.has_permission(self.route, 'GET'))
        self.assertFalse(self.app.has_permission(self.route, 'DELETE'))
        permission.is_active = False
        permission.save()
        self.assertFalse(self.app.has_permission(self.route, 'GET'))
        self.assertEqual(self.get().status_code, 403)

    def test_cross_project_permission_never_allowed(self):
        other = Project.objects.create(owner=self.owner, name='Other', slug='other')
        service = BackendService.objects.create(project=other, name='Echo', base_path='/echo')
        other_route = Route.objects.create(project=other, backend_service=service, name='Other')
        RoutePermission.objects.create(application=self.app, route=other_route)
        self.assertFalse(self.app.has_permission(other_route, 'GET'))

    def test_disabled_service_or_route_takes_effect_immediately(self):
        self.assertIsNotNone(RouteResolver.get_route_for_request(self.project, 'GET', '/echo'))
        Route.objects.filter(pk=self.route.pk).update(auth_policy=Route.AUTH_API_KEY_AND_JWT)
        self.assertEqual(self.get().status_code, 401)
        Route.objects.filter(pk=self.route.pk).update(is_active=False)
        self.assertEqual(self.get().status_code, 404)
        self.route.delete()
        self.assertEqual(self.get().status_code, 404)

    def test_cache_invalidation_is_project_scoped(self):
        sibling = Application.objects.create(project=self.project, name='Sibling')
        _, sibling_key = APIKey.create_for_application(sibling, 'key')
        other_project = Project.objects.create(owner=self.owner, name='Other', slug='other')
        other_app = Application.objects.create(project=other_project, name='Other client')
        _, other_key = APIKey.create_for_application(other_app, 'key')
        other_service = BackendService.objects.create(project=other_project, name='Echo',
            base_path='/echo', target_url=self.service.target_url)
        Route.objects.create(project=other_project, backend_service=other_service, name='Echo')
        with patch('gateway.views.GatewayRequestProxy.proxy', return_value=httpx.Response(200, json={'ok': True})) as proxy:
            for key in (self.key, sibling_key, other_key):
                self.assertEqual(self.get(key).status_code, 200)
            self.assertEqual(proxy.call_count, 3)
            self.assertEqual(self.client.post('/gateway/echo', HTTP_X_API_KEY=self.key).status_code, 200)
            self.assertEqual(self.get(other_key).status_code, 200)
            self.assertEqual(proxy.call_count, 4)
            for key in (self.key, sibling_key):
                self.assertEqual(self.get(key).status_code, 200)
            self.assertEqual(proxy.call_count, 6)

    def test_destination_checked_before_cache_or_network(self):
        with patch('gateway.views.GatewayRequestProxy.proxy', return_value=httpx.Response(200, json={'ok': True})) as proxy:
            self.assertEqual(self.get().status_code, 200)
            with override_settings(GATEWAY_ALLOWED_BACKEND_ORIGINS=[]):
                self.assertEqual(self.get().status_code, 403)
            BackendService.objects.filter(pk=self.service.pk).update(target_url='http://169.254.169.254/latest/meta-data/')
            self.assertEqual(self.get().status_code, 403)
            self.assertEqual(proxy.call_count, 1)

    def test_destination_parser_rejects_origin_bypasses(self):
        for url in ['http://169.254.169.254/latest/', 'http://127.0.0.1/',
                    'http://fashion-product-service:8001/', 'https://fashion-product-service:8000/',
                    'http://fashion-product-service.evil:8000/', 'http://fashion-product-service:8000@evil.test/',
                    'http://evil.test\\@fashion-product-service:8000/',
                    'http://fashion-product-service:8000/#fragment', 'file:///etc/passwd',
                    'http://fashion-product-service:8000/\n']:
            with self.subTest(url=url), self.assertRaises(ValidationError):
                validate_target_url(url)

    def test_proxy_does_not_follow_redirects_or_environment_proxies(self):
        with patch('gateway.services.httpx.Client') as client:
            client.return_value.__enter__.return_value.request.return_value = httpx.Response(
                302, headers={'location': 'http://169.254.169.254/'})
            request = RequestFactory().get('/echo')
            request.application = self.app
            response = GatewayRequestProxy.proxy(request, self.service.target_url)
            self.assertEqual(response.status_code, 302)
            self.assertFalse(client.call_args.kwargs['follow_redirects'])
            self.assertFalse(client.call_args.kwargs['trust_env'])

    def test_redis_eval_failure_uses_existing_fallback(self):
        limiter = RedisRateLimiter()
        with patch.object(limiter.client, 'ping', return_value=True), patch.object(
                limiter.client, 'eval', side_effect=redis.ConnectionError('connection lost')):
            self.assertTrue(limiter.allow_request('test', 1, 600))
            self.assertFalse(limiter.allow_request('test', 1, 600))

    def test_redis_eval_success_is_enforced(self):
        limiter = RedisRateLimiter()
        with patch.object(limiter.client, 'eval', side_effect=[1, 0]):
            self.assertTrue(limiter.allow_request('test', 1, 60))
            self.assertFalse(limiter.allow_request('test', 1, 60))

    def test_prefix_matches_whole_segments_and_prefers_specific_route(self):
        other = BackendService.objects.create(project=self.project, name='Specific',
            base_path='/echo/admin', target_url='http://fashion-product-service:8000/admin')
        specific_route = Route.objects.create(project=self.project, backend_service=other, name='Specific',
                                             allowed_methods=['GET'])
        self.assertEqual(RouteResolver.get_route_for_request(self.project, 'GET', '/echo/admin/123'), specific_route)
        self.assertIsNone(RouteResolver.get_route_for_request(self.project, 'DELETE', '/echo/admin/123'))
        self.assertIsNone(RouteResolver.get_route_for_request(self.project, 'GET', '/echo-extra'))
