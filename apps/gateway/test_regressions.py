from unittest.mock import patch

import httpx
import redis
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.test import RequestFactory, override_settings
from rest_framework.test import APITestCase

from authentication.models import APIKey, Application, RoutePermission
from gateway.services import GatewayRequestProxy, ProductResponseCache, RedisRateLimiter, RateLimitBackendUnavailable, RouteResolver
from gateway.test_support import FakeRedisClient
from routes.destinations import validate_target_url
from routes.models import BackendService, Route, RouteMethod
from users.models import Project, User


@override_settings(
    GATEWAY_ALLOWED_BACKEND_ORIGINS=['http://fashion-product-service:8000'],
    GATEWAY_CACHEABLE_BACKEND_HOSTS=['fashion-product-service'],
)
class GatewayRegressionTests(APITestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username='owner', email='owner@example.com')
        self.project = Project.objects.create(owner=self.owner, name='Project', slug='project')
        self.app = Application.objects.create(project=self.project, name='Client')
        _, self.key = APIKey.create_for_application(self.app, 'key')
        self.service = BackendService.objects.create(
            project=self.project, name='Echo Service', slug='echo-service',
            base_url='http://fashion-product-service:8000',
        )
        self.route = Route.objects.create(
            project=self.project, backend_service=self.service, name='Echo',
            base_path='/echo',
        )
        RouteMethod.objects.create(route=self.route, method='GET')
        RouteMethod.objects.create(route=self.route, method='POST')
        cache.clear()
        ProductResponseCache._memory_store.clear()
        shared_redis = FakeRedisClient()
        mocked_redis = patch('gateway.services.get_redis_client', return_value=shared_redis)
        mocked_redis.start()
        self.addCleanup(mocked_redis.stop)
        route_cache_redis = patch('gateway.route_cache.get_redis_client', return_value=shared_redis)
        route_cache_redis.start()
        self.addCleanup(route_cache_redis.stop)

    def get(self, key=None):
        return self.client.get('/gateway/echo', HTTP_X_API_KEY=key or self.key)

    def test_revoked_permission_denies_route(self):
        self.assertTrue(self.app.has_permission(self.route))
        permission = RoutePermission.objects.create(application=self.app, route=self.route)
        permission.is_active = False
        permission.save()
        self.assertFalse(self.app.has_permission(self.route))
        self.assertEqual(self.get().status_code, 403)

    def test_cross_project_permission_never_allowed(self):
        other = Project.objects.create(owner=self.owner, name='Other', slug='other')
        service = BackendService.objects.create(project=other, name='Other Service')
        route = Route.objects.create(
            project=other, backend_service=service, name='Other', base_path='/echo',
        )
        RoutePermission.objects.create(application=self.app, route=route)
        self.assertFalse(self.app.has_permission(route))

    def test_runtime_policy_change_works_without_route_cache(self):
        RouteResolver.get_route_for_request(self.project, 'GET', 'echo')
        cache.set(f'route:{self.project.id}:GET:/echo', self.route, 300)
        RouteMethod.objects.filter(route=self.route, method='GET').update(
            auth_policy=RouteMethod.AUTH_API_KEY_AND_JWT
        )
        self.assertEqual(self.get().status_code, 401)
        Route.objects.filter(pk=self.route.pk).update(is_active=False)
        self.assertEqual(self.get().status_code, 404)
        self.route.delete()
        self.assertEqual(self.get().status_code, 404)

    def test_cache_isolated_by_project_and_invalidated_on_write(self):
        sibling = Application.objects.create(project=self.project, name='Sibling')
        _, sibling_key = APIKey.create_for_application(sibling, 'key')
        other_project = Project.objects.create(owner=self.owner, name='Other', slug='other')
        other_app = Application.objects.create(project=other_project, name='Other client')
        _, other_key = APIKey.create_for_application(other_app, 'key')
        other_service = BackendService.objects.create(
            project=other_project, name='Other Echo', base_url=self.service.base_url,
        )
        other_route = Route.objects.create(
            project=other_project, backend_service=other_service,
            name='Echo', base_path='/echo',
        )
        RouteMethod.objects.create(route=other_route, method='GET')
        with patch('gateway.views.GatewayRequestProxy.proxy',
                   return_value=httpx.Response(200, json={'ok': True})) as proxy:
            for key in (self.key, sibling_key, other_key):
                self.assertEqual(self.get(key).status_code, 200)
            self.assertEqual(proxy.call_count, 3)
            self.assertEqual(
                self.client.post('/gateway/echo', HTTP_X_API_KEY=self.key).status_code, 200
            )
            self.assertEqual(self.get(other_key).status_code, 200)
            self.assertEqual(proxy.call_count, 4)
            for key in (self.key, sibling_key):
                self.assertEqual(self.get(key).status_code, 200)
            self.assertEqual(proxy.call_count, 6)

    def test_destination_checked_before_cache_and_network(self):
        with patch('gateway.views.GatewayRequestProxy.proxy',
                   return_value=httpx.Response(200, json={'ok': True})) as proxy:
            self.assertEqual(self.get().status_code, 200)
            with override_settings(GATEWAY_ALLOWED_BACKEND_ORIGINS=[]):
                self.assertEqual(self.get().status_code, 403)
            BackendService.objects.filter(pk=self.service.pk).update(
                base_url='http://169.254.169.254'
            )
            self.assertEqual(self.get().status_code, 403)
            self.assertEqual(proxy.call_count, 1)

    def test_destination_parser_rejects_origin_bypasses(self):
        for url in [
            'http://169.254.169.254/latest/', 'http://127.0.0.1/',
            'http://fashion-product-service:8001/',
            'https://fashion-product-service:8000/',
            'http://fashion-product-service.evil:8000/',
            'http://fashion-product-service:8000@evil.test/',
            'http://evil.test\\@fashion-product-service:8000/',
            'http://fashion-product-service:8000/#fragment',
            'file:///etc/passwd', 'http://fashion-product-service:8000/\n',
        ]:
            with self.subTest(url=url), self.assertRaises(ValidationError):
                validate_target_url(url)

    def test_proxy_does_not_follow_redirects_or_environment_proxies(self):
        with patch('gateway.services.httpx.Client') as client:
            client.return_value.__enter__.return_value.request.return_value = httpx.Response(
                302, headers={'location': 'http://169.254.169.254/'}
            )
            request = RequestFactory().get('/echo')
            request.application = self.app
            response = GatewayRequestProxy.proxy(request, self.service.base_url + '/echo')
            self.assertEqual(response.status_code, 302)
            self.assertFalse(client.call_args.kwargs['follow_redirects'])
            self.assertFalse(client.call_args.kwargs['trust_env'])

    def test_redis_eval_failure_is_fail_closed(self):
        limiter = RedisRateLimiter()
        with patch.object(limiter.client, 'eval', side_effect=redis.ConnectionError('offline')):
            with self.assertRaises(RateLimitBackendUnavailable):
                limiter.allow_request('test', 1, 600)

    def test_gateway_returns_503_and_does_not_proxy_if_redis_goes_down(self):
        with patch('gateway.services.get_redis_client', side_effect=redis.ConnectionError('offline')), patch(
            'gateway.views.GatewayRequestProxy.proxy',
        ) as proxy:
            response = self.get()
            self.assertEqual(response.status_code, 503)
            self.assertIn('Rate limiting temporarily unavailable', response.json()['detail'])
            proxy.assert_not_called()

    def test_gateway_recovers_when_shared_redis_recovers(self):
        shared_redis = FakeRedisClient()
        with patch('gateway.services.get_redis_client', return_value=shared_redis), patch(
            'gateway.views.GatewayRequestProxy.proxy',
            return_value=httpx.Response(200, json={'ok': True}),
        ) as proxy:
            with patch.object(shared_redis, 'eval', side_effect=redis.ConnectionError('offline')):
                self.assertEqual(self.get().status_code, 503)
            self.assertEqual(self.get().status_code, 200)
            proxy.assert_called_once()

    def test_actual_limit_exceeded_is_still_429(self):
        with patch.dict(Application.PLAN_LIMITS, {Application.PLAN_NORMAL: (1, 60)}), patch(
            'gateway.views.GatewayRequestProxy.proxy',
            return_value=httpx.Response(200, json={'ok': True}),
        ) as proxy:
            self.assertEqual(self.get().status_code, 200)
            self.assertEqual(self.get().status_code, 429)
            proxy.assert_called_once()

    def test_redis_eval_success_is_enforced(self):
        limiter = RedisRateLimiter()
        with patch.object(limiter.client, 'eval', side_effect=[1, 0]):
            self.assertTrue(limiter.allow_request('test', 1, 60))
            self.assertFalse(limiter.allow_request('test', 1, 60))
