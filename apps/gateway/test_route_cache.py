"""Direct per-route Redis cache: hit/miss, invalidation and SQL-only policies."""
from unittest.mock import patch

import redis
from django.core.exceptions import ValidationError
from django.test import override_settings
from rest_framework.test import APITestCase

from authentication.models import APIKey, Application, RoutePermission
from gateway.route_cache import _keys, _read_route, get_route, invalidate_route
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
            project=self.project, name='Doctors', base_url='http://healthcare-doctor-service:8000',
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

    def test_route_key_is_project_plus_base_path_with_60_second_ttl(self):
        with patch.object(self.redis, 'eval', wraps=self.redis.eval) as redis_eval:
            found = RouteResolver.get_route_for_request(self.project, 'GET', '/doctors/456')
        self.assertEqual(found.id, self.route.id)
        self.assertEqual(redis_eval.call_args.args[-1], 60)
        version_key, route_key = _keys(self.project.id, '/doctors')
        self.assertEqual(route_key, f'route:{{{self.project.id}}}:doctors')
        self.assertIsNotNone(self.redis.get(route_key))
        self.assertIn(f'{{{self.project.id}}}', version_key)

    def test_repeated_requests_skip_route_mysql_lookup(self):
        with patch('gateway.route_cache._read_route', wraps=_read_route) as loader:
            first = RouteResolver.get_route_for_request(self.project, 'GET', '/doctors/456')
            second = RouteResolver.get_route_for_request(self.project, 'POST', '/doctors/789')
            self.assertEqual(loader.call_count, 1)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(second.backend_service.base_url, self.service.base_url)
        self.assertFalse(hasattr(second, '_cached_method_policies'))

    def test_different_base_paths_have_different_cache_entries(self):
        patients = Route.objects.create(
            project=self.project, name='Patients', base_path='/patients',
            backend_service=self.service,
        )
        self.assertEqual(
            RouteResolver.get_route_for_request(self.project, 'GET', '/patients/22').pk, patients.pk
        )
        self.assertEqual(
            RouteResolver.get_route_for_request(self.project, 'GET', '/doctors/22').pk, self.route.pk
        )
        self.assertIsNotNone(self.redis.get(_keys(self.project.id, '/patients')[1]))
        self.assertIsNotNone(self.redis.get(_keys(self.project.id, '/doctors')[1]))
        with self.captureOnCommitCallbacks(execute=True):
            patients.is_active = False
            patients.save(update_fields=['is_active'])
        self.assertIsNone(self.redis.get(_keys(self.project.id, '/patients')[1]))
        self.assertIsNotNone(self.redis.get(_keys(self.project.id, '/doctors')[1]))

    def test_same_route_path_isolated_across_projects(self):
        other = Project.objects.create(owner=self.owner, name='Other')
        other_service = BackendService.objects.create(
            project=other, name='Patients', base_url='http://healthcare-patient-service:8000',
        )
        Route.objects.create(
            project=other, backend_service=other_service, name='Doctors', base_path='/doctors',
        )
        one = RouteResolver.get_route_for_request(self.project, 'GET', '/doctors/1')
        two = RouteResolver.get_route_for_request(other, 'GET', '/doctors/1')
        self.assertNotEqual(one.backend_service.base_url, two.backend_service.base_url)

    def test_method_policy_is_always_checked_against_mysql(self):
        self.assertEqual(self.client.get('/gateway/doctors/1', HTTP_X_API_KEY=self.key).status_code, 200)
        # Bulk update bypasses route signals, but the JWT change is still seen.
        RouteMethod.objects.filter(route=self.route, method='GET').update(
            auth_policy=RouteMethod.AUTH_API_KEY_AND_JWT,
        )
        self.assertEqual(self.client.get('/gateway/doctors/1', HTTP_X_API_KEY=self.key).status_code, 401)

    def test_service_update_and_route_disable_invalidate_cache(self):
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

    def test_renaming_route_invalidates_old_and_new_key(self):
        RouteResolver.get_route_for_request(self.project, 'GET', '/doctors/12')
        with self.captureOnCommitCallbacks(execute=True):
            self.route.base_path = '/physicians'
            self.route.save(update_fields=['base_path'])
        self.assertIsNone(RouteResolver.get_route_for_request(self.project, 'GET', '/doctors/12'))
        self.assertEqual(RouteResolver.get_route_for_request(self.project, 'GET', '/physicians/12').pk,
                         self.route.pk)

    def test_cache_fill_does_not_restore_stale_value_after_invalidation(self):
        calls = [0]

        def race(project_id, base_path):
            data = _read_route(project_id, base_path)
            calls[0] += 1
            if calls[0] == 1:
                invalidate_route(project_id, base_path)
            return data

        with patch('gateway.route_cache._read_route', side_effect=race):
            result = get_route(self.project.id, '/doctors')
        self.assertEqual(result['id'], self.route.id)
        self.assertEqual(calls[0], 2)

    def test_permission_check_remains_fresh(self):
        self.assertEqual(self.client.get('/gateway/doctors/2', HTTP_X_API_KEY=self.key).status_code, 200)
        other_route = Route.objects.create(
            project=self.project, name='Patients', base_path='/patients',
            backend_service=self.service,
        )
        RoutePermission.objects.create(application=self.app, route=other_route)
        self.assertEqual(self.client.get('/gateway/doctors/2', HTTP_X_API_KEY=self.key).status_code, 403)

    def test_nested_base_paths_are_rejected(self):
        with self.assertRaises(ValidationError):
            Route.objects.create(
                project=self.project, name='Nested', base_path='/doctors/appointments',
                backend_service=self.service,
            )
        self.assertIsNone(RouteResolver.get_route_for_request(self.project, 'GET', '/unknown/1'))

    def test_redis_route_cache_failure_falls_back_to_mysql(self):
        with patch('gateway.route_cache.get_redis_client', side_effect=redis.ConnectionError('offline')):
            route = RouteResolver.get_route_for_request(self.project, 'GET', '/doctors/2')
        self.assertEqual(route.pk, self.route.pk)
