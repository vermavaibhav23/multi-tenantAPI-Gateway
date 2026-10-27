import io
import os
from unittest import skipUnless
from unittest.mock import patch
from uuid import uuid4

import redis
from django.core.management import call_command, CommandError
from django.test import SimpleTestCase, override_settings
from rest_framework.test import APITestCase

from authentication.models import APIKey, Application, RoutePermission
from gateway.services import ProductResponseCache, RedisRateLimiter
from gateway.test_support import FakeRedisClient
from gateway.route_cache import invalidate_project_routes
from routes.demo import INDUSTRY_BUNDLES
from routes.models import BackendService, Route, RouteMethod
from users.models import Project, User


class BaaSIntegrationTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username='admin', email='admin@example.com', is_staff=True
        )
        call_command('seed_demo', admin_email=self.admin.email, stdout=io.StringIO())
        self.project = Project.objects.get(slug='healthcare')
        self.app, self.key = self.create_business(self.project, 'Hospital A', 'premium')
        self.other_app, self.other_key = self.create_business(self.project, 'Clinic B', 'normal')
        self.client.force_authenticate(None)
        ProductResponseCache._memory_store.clear()
        shared_redis = FakeRedisClient()
        mocked_redis = patch('gateway.services.get_redis_client', return_value=shared_redis)
        mocked_redis.start()
        self.addCleanup(mocked_redis.stop)
        route_cache_redis = patch('gateway.route_cache.get_redis_client', return_value=shared_redis)
        route_cache_redis.start()
        self.addCleanup(route_cache_redis.stop)

    def create_business(self, project, name, plan):
        self.client.force_authenticate(self.admin)
        result = self.client.post('/admin-api/applications', {
            'project': project.pk, 'name': name, 'plan': plan,
        }, format='json')
        self.assertEqual(result.status_code, 201, result.data)
        self.client.force_authenticate(None)
        return Application.objects.get(pk=result.data['id']), result.data['api_key']['key']

    def get(self, key=None, path='/doctors', **headers):
        return self.client.get('/gateway' + path, HTTP_X_API_KEY=key or self.key, **headers)

    def doctor_route(self):
        return self.project.routes.get(base_path='/doctors')

    def test_seed_is_idempotent_and_has_one_route_per_service(self):
        ids = list(Route.objects.order_by('id').values_list('id', flat=True))
        self.doctor_route().method_policies.filter(method='DELETE').update(
            auth_policy=RouteMethod.AUTH_API_KEY_AND_JWT
        )
        call_command('seed_demo', admin_email=self.admin.email, stdout=io.StringIO())
        self.assertEqual(ids, list(Route.objects.order_by('id').values_list('id', flat=True)))
        self.assertEqual(Project.objects.count(), 3)
        self.assertEqual(BackendService.objects.count(), 9)
        self.assertEqual(Route.objects.count(), 9)
        self.assertEqual(RouteMethod.objects.count(), 45)
        self.assertEqual(Application.objects.count(), 2)
        self.assertEqual(
            self.doctor_route().method_policies.get(method='DELETE').auth_policy,
            RouteMethod.AUTH_API_KEY_AND_JWT
        )

    def test_seed_rejects_nonstaff_and_owner_collisions(self):
        ordinary = User.objects.create_user(username='ordinary', email='ordinary@example.com')
        with self.assertRaises(CommandError):
            call_command('seed_demo', admin_email=ordinary.email, stdout=io.StringIO())
        other = User.objects.create_user(username='other', email='other@example.com', is_staff=True)
        with self.assertRaises(CommandError):
            call_command('seed_demo', admin_email=other.email, stdout=io.StringIO())

    def test_two_businesses_share_one_backend_but_have_distinct_identity(self):
        for app, key in ((self.app, self.key), (self.other_app, self.other_key)):
            result = self.get(
                key, HTTP_X_GATESCALE_APPLICATION_ID='9999',
                HTTP_CONNECTION='X-GateScale-Application-ID',
            )
            self.assertEqual(result.status_code, 200)
            data = result.json()
            self.assertEqual(data['application_id'], app.id)
            self.assertEqual(data['target_url'],
                             'http://healthcare-doctor-service:8000/doctors')
            self.assertFalse(data['forwarded'])
            self.assertNotIn('headers', data)

    def test_all_nine_backends_accept_five_methods(self):
        for slug, (_, services) in INDUSTRY_BUNDLES.items():
            project = Project.objects.get(slug=slug)
            _, key = self.create_business(project, 'Test client', 'premium')
            for _, host, path in services:
                for method, _ in RouteMethod.METHOD_CHOICES:
                    with self.subTest(host=host, method=method):
                        result = self.client.generic(
                            method, '/gateway' + path, data=b'dummy body',
                            content_type='application/octet-stream',
                            HTTP_X_API_KEY=key,
                        )
                        self.assertEqual(result.status_code, 200)
                        data = result.json()
                        self.assertEqual(data['backend_service_id'],
                                         project.routes.get(base_path=path).backend_service_id)
                        self.assertEqual(data['target_url'], f'http://{host}:8000{path}')
                        self.assertEqual(data['method'], method)
                        self.assertEqual(data['body'], 'dummy body')
                        self.assertEqual(data['body_encoding'], 'utf-8')

    def test_cross_project_and_route_permissions(self):
        self.assertEqual(self.get(path='/accounts').status_code, 404)
        RoutePermission.objects.create(
            application=self.app, route=self.project.routes.get(base_path='/patients')
        )
        self.assertEqual(self.get().status_code, 403)

    def test_disabled_service_or_route_is_rejected(self):
        route = self.doctor_route()
        BackendService.objects.filter(pk=route.backend_service_id).update(is_active=False)
        self.assertEqual(self.get().status_code, 404)
        BackendService.objects.filter(pk=route.backend_service_id).update(is_active=True)
        Route.objects.filter(pk=route.pk).update(is_active=False)
        self.assertEqual(self.get().status_code, 404)

    def test_single_key_rotation_invalidates_previous_key(self):
        _, rotated = APIKey.create_for_application(self.app, 'rotated')
        self.assertEqual(APIKey.objects.filter(application=self.app).count(), 1)
        self.assertEqual(self.get(key=self.key).status_code, 401)
        self.assertEqual(self.get(key=rotated).status_code, 200)

    def test_cache_is_per_application_and_mutations_invalidate(self):
        # The final request preview is regenerated; it is never response-cached.
        first = self.get().json()
        other = self.get(self.other_key).json()
        self.assertNotEqual(first['application_id'], other['application_id'])
        self.assertEqual(first['target_url'], other['target_url'])
        service = self.doctor_route().backend_service
        service.base_url = 'http://healthcare-patient-service:8000'
        with override_settings(GATEWAY_ALLOWED_BACKEND_ORIGINS=[
            'http://healthcare-patient-service:8000',
        ]):
            service.save(update_fields=['base_url'])
            with self.captureOnCommitCallbacks(execute=True):
                invalidate_project_routes(self.project.id)
            updated = self.get()
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json()['target_url'],
                         'http://healthcare-patient-service:8000/doctors')

    def test_jwt_policy_bypasses_cache(self):
        policy = self.doctor_route().method_policies.get(method='GET')
        policy.auth_policy = RouteMethod.AUTH_API_KEY_AND_JWT
        policy.save(update_fields=['auth_policy'])
        self.assertEqual(self.get().status_code, 401)
        self.assertFalse(ProductResponseCache._memory_store)


class ManagementAccessTests(APITestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username='staff', email='staff@example.com', is_staff=True
        )
        self.user = User.objects.create_user(username='user', email='user@example.com')

    def test_nonstaff_cannot_manage_services_or_route_methods(self):
        paths = ('projects', 'services', 'routes', 'route-methods', 'applications', 'keys',
                 'dashboard')
        for user in (None, self.user):
            self.client.force_authenticate(user)
            for path in paths:
                for method in ('get', 'post'):
                    with self.subTest(user=user, path=path, method=method):
                        result = getattr(self.client, method)('/admin-api/' + path)
                        self.assertEqual(result.status_code, 401 if user is None else 403)


@skipUnless(os.getenv('GATESCALE_TEST_REDIS_URL'),
            'Set GATESCALE_TEST_REDIS_URL for integration test with shared Redis.')
class SharedRedisTests(SimpleTestCase):
    def test_independent_clients_share_atomic_fixed_window(self):
        first = RedisRateLimiter(os.environ['GATESCALE_TEST_REDIS_URL'])
        second = RedisRateLimiter(os.environ['GATESCALE_TEST_REDIS_URL'])
        key = f'gateway_test:{uuid4().hex}'
        try:
            self.assertTrue(first.allow_request(key, 1, 600))
            self.assertFalse(second.allow_request(key, 1, 600))
        finally:
            window_keys = list(first.client.scan_iter(f'{key}:*'))
            if window_keys:
                first.client.delete(*window_keys)
