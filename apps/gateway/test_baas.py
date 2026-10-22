import io
import os
from http.server import ThreadingHTTPServer
from threading import Thread
from unittest import skipUnless
from unittest.mock import patch
from uuid import uuid4

import httpx
import redis
from django.core.management import call_command, CommandError
from django.test import SimpleTestCase, override_settings
from rest_framework.test import APITestCase

from analytics.models import RequestLog
from authentication.models import APIKey, Application, RoutePermission
from dummy_backend.server import DummyBackendHandler
from gateway.services import ProductResponseCache, RedisRateLimiter
from gateway.test_support import FakeRedisClient
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

    def create_business(self, project, name, plan):
        self.client.force_authenticate(self.admin)
        result = self.client.post('/admin-api/applications', {
            'project': project.pk, 'name': name, 'plan': plan,
        }, format='json')
        self.assertEqual(result.status_code, 201, result.data)
        self.client.force_authenticate(None)
        return Application.objects.get(pk=result.data['id']), result.data['api_key']['key']

    def backend(self, service_name='healthcare-doctor-service'):
        received = []
        class Handler(DummyBackendHandler):
            def _send_response(self):
                received.append(dict(self.headers))
                super()._send_response()

        Handler.service_name = service_name
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def cleanup():
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.addCleanup(cleanup)
        return f'http://127.0.0.1:{server.server_address[1]}', received

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
        origin, received = self.backend()
        BackendService.objects.filter(pk=self.doctor_route().backend_service_id).update(base_url=origin)
        with override_settings(GATEWAY_ALLOWED_BACKEND_ORIGINS=[origin]):
            for app, key in ((self.app, self.key), (self.other_app, self.other_key)):
                result = self.get(
                    key, HTTP_X_GATESCALE_APPLICATION_ID='9999',
                    HTTP_CONNECTION='X-GateScale-Application-ID'
                )
                self.assertEqual(result.status_code, 200)
                self.assertEqual(result.json()['application_id'], app.id)
                self.assertEqual(result.json()['backend_service'], 'healthcare-doctor-service')
        self.assertEqual(len(received), 2)
        for headers in received:
            lower = {key.lower(): value for key, value in headers.items()}
            self.assertNotIn('x-api-key', lower)
            self.assertNotEqual(lower.get('x-gatescale-application-id'), '9999')
        self.assertEqual(
            set(RequestLog.objects.values_list('application_id', flat=True)),
            {self.app.id, self.other_app.id},
        )

    def test_all_nine_backends_accept_five_methods(self):
        for slug, (_, services) in INDUSTRY_BUNDLES.items():
            project = Project.objects.get(slug=slug)
            _, key = self.create_business(project, 'Test client', 'premium')
            for _, host, path in services:
                origin, received = self.backend(host)
                BackendService.objects.filter(project=project, slug=host).update(base_url=origin)
                with override_settings(GATEWAY_ALLOWED_BACKEND_ORIGINS=[origin]):
                    for method, _ in RouteMethod.METHOD_CHOICES:
                        with self.subTest(host=host, method=method):
                            result = self.client.generic(
                                method, '/gateway' + path, data=b'dummy body',
                                content_type='application/octet-stream',
                                HTTP_X_API_KEY=key,
                            )
                            self.assertEqual(result.status_code, 200)
                            self.assertEqual(result.json()['backend_service'], host)
                            self.assertEqual(result.json()['method'], method)
                self.assertEqual(len(received), 5)

    def test_cross_project_and_route_permissions(self):
        with patch('gateway.views.GatewayRequestProxy.proxy') as proxy:
            self.assertEqual(self.get(path='/accounts').status_code, 404)
            RoutePermission.objects.create(
                application=self.app, route=self.project.routes.get(base_path='/patients')
            )
            self.assertEqual(self.get().status_code, 403)
            proxy.assert_not_called()

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
        with patch('gateway.views.GatewayRequestProxy.proxy',
                   return_value=httpx.Response(200, json={'ok': True})):
            self.assertEqual(self.get(key=rotated).status_code, 200)

    def test_cache_is_per_application_and_mutations_invalidate(self):
        origin, received = self.backend('fashion-product-service')
        BackendService.objects.filter(pk=self.doctor_route().backend_service_id).update(base_url=origin)
        with override_settings(
            GATEWAY_ALLOWED_BACKEND_ORIGINS=[origin],
            GATEWAY_CACHEABLE_BACKEND_HOSTS=['127.0.0.1'],
        ):
            for _ in range(2):
                self.assertEqual(self.get().json()['application_id'], self.app.id)
                self.assertEqual(self.get(self.other_key).json()['application_id'], self.other_app.id)
            self.assertEqual(len(received), 2)
            self.client.post('/gateway/doctors', HTTP_X_API_KEY=self.key)
            self.get()
            self.get(self.other_key)
            self.assertEqual(len(received), 5)

    def test_jwt_policy_bypasses_cache(self):
        policy = self.doctor_route().method_policies.get(method='GET')
        policy.auth_policy = RouteMethod.AUTH_API_KEY_AND_JWT
        policy.save(update_fields=['auth_policy'])
        self.assertEqual(self.get().status_code, 401)
        self.assertFalse(ProductResponseCache._memory_store)

    def test_analytics_scoped_to_owned_apps(self):
        with patch('gateway.views.GatewayRequestProxy.proxy',
                   return_value=httpx.Response(200, json={'ok': True})):
            self.get()
            self.get(self.other_key)
        self.client.force_authenticate(self.admin)
        summary = self.client.get('/admin-api/analytics/summary', {'application_id': self.app.id})
        self.assertEqual(summary.status_code, 200)
        self.assertEqual(summary.data['total_requests'], 1)
        outsider = User.objects.create_user(
            username='outsider', email='outsider@example.com', is_staff=True
        )
        self.client.force_authenticate(outsider)
        self.assertEqual(
            self.client.get('/admin-api/analytics/summary', {'application_id': self.app.id}).status_code,
            404,
        )


class ManagementAccessTests(APITestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username='staff', email='staff@example.com', is_staff=True
        )
        self.user = User.objects.create_user(username='user', email='user@example.com')

    def test_nonstaff_cannot_manage_services_or_route_methods(self):
        paths = ('projects', 'services', 'routes', 'route-methods', 'applications', 'keys',
                 'dashboard', 'analytics/summary')
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
