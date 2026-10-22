"""Route config cache correctness: tenant isolation, longest prefix, invalidation."""
from unittest.mock import patch

import httpx
import redis
from django.test import override_settings
from rest_framework.test import APITestCase

from authentication.models import APIKey, Application, RoutePermission
from gateway.route_cache import (
    _read_project_routes, get_project_routes, invalidate_project_routes,
)
from gateway.services import RouteResolver
from gateway.test_support import FakeRedisClient
from routes.models import BackendService, Route, RouteMethod
from users.models import Project, User


@override_settings(
    GATEWAY_ALLOWED_BACKEND_ORIGINS=[
        'http://healthcare-doctor-service:8000',
        'http://healthcare-patient-service:8000',
    ],
    GATEWAY_CACHEABLE_BACKEND_HOSTS=[],
)
class RouteCacheTests(APITestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username='cache-owner', email='cache@example.com')
        self.project = Project.objects.create(owner=self.owner, name='Hospital')
        self.app = Application.objects.create(project=self.project, name='App A')
        _, self.key = APIKey.create_for_application(self.app, 'key')
        self.service = BackendService.objects.create(
            project=self.project, name='Doctors', base_url='http://healthcare-doctor-service:8000'
        )
        self.route = Route.objects.create(
            project=self.project, name='Doctors', base_path='/doctors', backend_service=self.service,
        )
        RouteMethod.objects.create(route=self.route, method='GET')
        self.redis = FakeRedisClient()
        for target in ('gateway.route_cache.get_redis_client', 'gateway.services.get_redis_client'):
            mocked = patch(target, return_value=self.redis)
            mocked.start()
            self.addCleanup(mocked.stop)

    def test_second_lookup_uses_redis_without_route_or_method_db_queries(self):
        with patch('gateway.route_cache._read_project_routes', wraps=_read_project_routes) as loader:
            first = RouteResolver.get_route_for_request(self.project, 'GET', '/doctors/456')
            second = RouteResolver.get_route_for_request(self.project, 'GET', '/doctors/789')
            self.assertEqual(loader.call_count, 1)
        self.assertEqual(first.pk, self.route.pk)
        self.assertEqual(second.backend_service.base_url, self.service.base_url)
        self.assertEqual(second._cached_method_policies['GET'], RouteMethod.AUTH_API_KEY_ONLY)

    def test_longest_prefix_and_full_segment_matching(self):
        specific = Route.objects.create(
            project=self.project, name='Doctor Notes', base_path='/doctors/notes',
            backend_service=self.service,
        )
        RouteMethod.objects.create(route=specific, method='GET')
        matched = RouteResolver.get_route_for_request(self.project, 'GET', '/doctors/notes/55')
        self.assertEqual(matched.pk, specific.pk)
        self.assertIsNone(RouteResolver.get_route_for_request(self.project, 'GET', '/doctors-other'))

    def test_project_cache_keys_are_isolated(self):
        other_project = Project.objects.create(owner=self.owner, name='Other Hospital')
        other_service = BackendService.objects.create(
            project=other_project, name='Other Doctors',
            base_url='http://healthcare-patient-service:8000',
        )
        other_route = Route.objects.create(
            project=other_project, backend_service=other_service, name='Doctors', base_path='/doctors',
        )
        RouteMethod.objects.create(route=other_route, method='GET')
        first = RouteResolver.get_route_for_request(self.project, 'GET', '/doctors/1')
        second = RouteResolver.get_route_for_request(other_project, 'GET', '/doctors/1')
        self.assertNotEqual(first.backend_service.base_url, second.backend_service.base_url)

    def test_method_policy_change_invalidates_after_commit(self):
        before = RouteResolver.get_route_for_request(self.project, 'GET', '/doctors/1')
        self.assertEqual(before._cached_method_policies['GET'], 'api_key_only')
        method = self.route.method_policies.get(method='GET')
        with self.captureOnCommitCallbacks(execute=True):
            method.auth_policy = RouteMethod.AUTH_API_KEY_AND_JWT
            method.save(update_fields=['auth_policy'])
        after = RouteResolver.get_route_for_request(self.project, 'GET', '/doctors/1')
        self.assertEqual(after._cached_method_policies['GET'], 'api_key_and_jwt')

    def test_service_update_and_route_disable_invalidate_after_commit(self):
        RouteResolver.get_route_for_request(self.project, 'GET', '/doctors/1')
        with self.captureOnCommitCallbacks(execute=True):
            self.service.base_url = 'http://healthcare-patient-service:8000'
            self.service.save(update_fields=['base_url'])
        updated = RouteResolver.get_route_for_request(self.project, 'GET', '/doctors/1')
        self.assertEqual(updated.backend_service.base_url, self.service.base_url)
        with self.captureOnCommitCallbacks(execute=True):
            self.route.is_active = False
            self.route.save(update_fields=['is_active'])
        self.assertIsNone(RouteResolver.get_route_for_request(self.project, 'GET', '/doctors/1'))

    def test_cache_fill_does_not_overwrite_concurrent_invalidation(self):
        original = _read_project_routes
        count = [0]

        def invalidate_during_load(project_id):
            result = original(project_id)
            count[0] += 1
            if count[0] == 1:
                invalidate_project_routes(project_id)
            return result

        with patch('gateway.route_cache._read_project_routes', side_effect=invalidate_during_load):
            result = get_project_routes(self.project.id)
        self.assertTrue(result)
        self.assertEqual(count[0], 2)

    def test_permission_check_remains_live_even_on_route_cache_hit(self):
        with patch('gateway.views.GatewayRequestProxy.proxy',
                   return_value=httpx.Response(200, json={'ok': True})) as proxy:
            self.assertEqual(self.client.get('/gateway/doctors/2', HTTP_X_API_KEY=self.key).status_code, 200)
            alternate_route = Route.objects.create(
                project=self.project, name='Patient', base_path='/patients', backend_service=self.service,
            )
            RoutePermission.objects.create(application=self.app, route=alternate_route)
            # No cached permission list: authorization still queries MySQL.
            self.assertEqual(self.client.get('/gateway/doctors/2', HTTP_X_API_KEY=self.key).status_code, 403)
            proxy.assert_called_once()

    def test_cache_backend_failure_falls_back_to_mysql(self):
        with patch('gateway.route_cache.get_redis_client', side_effect=redis.ConnectionError('offline')):
            route = RouteResolver.get_route_for_request(self.project, 'GET', '/doctors/2')
        self.assertEqual(route.pk, self.route.pk)
