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
from rest_framework_simplejwt.tokens import RefreshToken

from analytics.models import RequestLog
from authentication.models import APIKey, Application, RoutePermission
from dummy_backend.server import DummyBackendHandler
from gateway.services import ProductResponseCache, RedisRateLimiter
from routes.demo import INDUSTRY_BUNDLES
from routes.models import BackendService, Route
from users.models import Project, User


class BaaSIntegrationTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username='admin', email='admin@example.com', is_staff=True)
        call_command('seed_demo', admin_email=self.admin.email, stdout=io.StringIO())
        self.project = Project.objects.get(slug='healthcare')
        self.app, self.key = self.create_business(self.project, 'Hospital A', 'premium')
        self.other_app, self.other_key = self.create_business(self.project, 'Clinic B', 'normal')
        self.client.force_authenticate(None)
        ProductResponseCache._memory_store.clear()
        RedisRateLimiter._memory_store.clear()
        offline = patch('redis.Redis.execute_command', side_effect=redis.ConnectionError('test offline'))
        offline.start()
        self.addCleanup(offline.stop)

    def create_business(self, project, name, plan):
        self.client.force_authenticate(self.admin)
        response = self.client.post('/admin-api/applications', {
            'project': project.pk, 'name': name, 'plan': plan,
        }, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        self.client.force_authenticate(None)
        return Application.objects.get(pk=response.data['id']), response.data['api_key']['key']

    def backend(self, name='healthcare-doctor-service'):
        received = []
        class Handler(DummyBackendHandler):
            service_name = name

            def _send_response(self):
                received.append(dict(self.headers))
                super()._send_response()

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def cleanup():
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.addCleanup(cleanup)
        origin = f'http://127.0.0.1:{server.server_address[1]}'
        return origin, received

    def get(self, key=None, path='/doctors', **headers):
        return self.client.get('/gateway' + path, HTTP_X_API_KEY=key or self.key, **headers)

    def doctor_service(self):
        return BackendService.objects.get(project=self.project, base_path='/doctors')

    def test_seed_is_idempotent_and_preserves_edits(self):
        ids = list(Route.objects.order_by('id').values_list('id', flat=True))
        route = self.project.routes.get(backend_service=self.doctor_service())
        Route.objects.filter(pk=route.pk).update(is_active=False)
        call_command('seed_demo', admin_email=self.admin.email, stdout=io.StringIO())
        self.assertEqual(ids, list(Route.objects.order_by('id').values_list('id', flat=True)))
        self.assertFalse(Route.objects.get(pk=route.pk).is_active)
        self.assertEqual(Project.objects.count(), 3)
        self.assertEqual(BackendService.objects.count(), 9)
        self.assertEqual(Route.objects.count(), 9)
        self.assertEqual(Application.objects.count(), 2)
        for slug, (_, services) in INDUSTRY_BUNDLES.items():
            project = Project.objects.get(slug=slug)
            for _, host, path in services:
                service = project.backend_services.get(slug=host)
                self.assertEqual(service.base_path, path)
                self.assertEqual(service.target_url, f'http://{host}:8000{path}')
                self.assertEqual(service.routes.count(), 1)

    def test_seed_rejects_nonstaff_and_owner_collision(self):
        ordinary = User.objects.create_user(username='ordinary', email='ordinary@example.com')
        with self.assertRaises(CommandError):
            call_command('seed_demo', admin_email=ordinary.email, stdout=io.StringIO())
        other = User.objects.create_user(username='other', email='other@example.com', is_staff=True)
        with self.assertRaises(CommandError):
            call_command('seed_demo', admin_email=other.email, stdout=io.StringIO())

    def test_two_businesses_reach_same_server_with_trusted_identity(self):
        origin, received = self.backend()
        service = self.doctor_service()
        service.target_url = origin + '/doctors'
        service.save(update_fields=['target_url'])
        with override_settings(GATEWAY_ALLOWED_BACKEND_ORIGINS=[origin]):
            for app, key in ((self.app, self.key), (self.other_app, self.other_key)):
                response = self.get(key, HTTP_X_GATESCALE_APPLICATION_ID='9999',
                                    HTTP_X_GATESCALE_TENANT_ID='spoof', HTTP_CONNECTION='X-GateScale-Application-ID')
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), {'response': 'success', 'backend_service': 'healthcare-doctor-service',
                    'application_id': app.id, 'method': 'GET', 'path': '/doctors'})
        self.assertEqual(len(received), 2)
        for headers in received:
            headers = {k.lower(): v for k, v in headers.items()}
            self.assertNotIn('x-api-key', headers)
            self.assertNotIn('x-gatescale-tenant-id', headers)
            self.assertNotEqual(headers['x-gatescale-application-id'], '9999')

    def test_all_nine_services_echo_five_methods_with_one_route_each(self):
        for slug, (_, services) in INDUSTRY_BUNDLES.items():
            project = Project.objects.get(slug=slug)
            _, key = self.create_business(project, 'Routing checks', 'premium')
            for _, host, path in services:
                origin, received = self.backend(host)
                project.backend_services.filter(base_path=path).update(target_url=origin + path)
                with override_settings(GATEWAY_ALLOWED_BACKEND_ORIGINS=[origin]):
                    for method, _ in Route.METHOD_CHOICES:
                        with self.subTest(host=host, method=method):
                            response = self.client.generic(method, '/gateway' + path, data=b'not JSON',
                                content_type='application/octet-stream', HTTP_X_API_KEY=key)
                            self.assertEqual(response.status_code, 200)
                            self.assertEqual(response.json()['backend_service'], host)
                            self.assertEqual(response.json()['method'], method)
                            self.assertEqual(response.json()['path'], path)
                self.assertEqual(len(received), 5)
        self.assertEqual(Route.objects.count(), 9)

    def test_cross_project_and_route_permission_denial(self):
        with patch('gateway.views.GatewayRequestProxy.proxy') as proxy:
            self.assertEqual(self.get(path='/accounts').status_code, 404)
            route = self.project.routes.get(backend_service__base_path='/patients')
            RoutePermission.objects.create(application=self.app, route=route, allowed_methods=['GET'])
            self.assertEqual(self.get().status_code, 403)
            proxy.assert_not_called()

    def test_inactive_or_inconsistent_service_is_not_routed(self):
        route = self.project.routes.get(backend_service=self.doctor_service())
        foreign = BackendService.objects.get(slug='banking-account-service')
        Route.objects.filter(pk=route.pk).update(backend_service=foreign)
        self.assertEqual(self.get().status_code, 404)
        Route.objects.filter(pk=route.pk).update(backend_service=self.doctor_service())
        BackendService.objects.filter(pk=route.backend_service_id).update(is_active=False)
        self.assertEqual(self.get().status_code, 404)

    def test_rate_limit_is_application_scoped_across_keys(self):
        _, second_key = APIKey.create_for_application(self.app, 'rotation')
        with patch.dict(Application.PLAN_LIMITS, {'normal': (1, 600), 'premium': (1, 600)}), patch(
                'gateway.views.GatewayRequestProxy.proxy', return_value=httpx.Response(200, json={'ok': True})):
            self.assertEqual(self.get().status_code, 200)
            self.assertEqual(self.get(second_key).status_code, 429)
            self.assertEqual(self.get(self.other_key).status_code, 200)

    def test_cache_is_application_isolated_and_invalidated(self):
        origin, received = self.backend('fashion-product-service')
        self.doctor_service().target_url = origin + '/doctors'
        self.doctor_service().save(update_fields=['target_url'])
        with override_settings(GATEWAY_ALLOWED_BACKEND_ORIGINS=[origin], GATEWAY_CACHEABLE_BACKEND_HOSTS=['127.0.0.1']):
            for _ in range(2):
                self.assertEqual(self.get().json()['application_id'], self.app.id)
                self.assertEqual(self.get(self.other_key).json()['application_id'], self.other_app.id)
            self.assertEqual(len(received), 2)
            self.client.post('/gateway/doctors', HTTP_X_API_KEY=self.key)
            self.get()
            self.get(self.other_key)
            self.assertEqual(len(received), 5)

    def test_sensitive_and_jwt_responses_bypass_cache(self):
        route = self.project.routes.get(backend_service=self.doctor_service())
        with patch('gateway.views.GatewayRequestProxy.proxy',
                   return_value=httpx.Response(200, json={'ok': True})) as proxy:
            for host in ('healthcare-doctor-service', 'banking-account-service'):
                self.doctor_service().target_url = f'http://{host}:8000/doctors'
                self.doctor_service().save(update_fields=['target_url'])
                self.assertEqual(self.get().status_code, 200)
                self.assertEqual(self.get().status_code, 200)
            self.assertEqual(proxy.call_count, 4)
            route.auth_policy = Route.AUTH_API_KEY_AND_JWT
            route.save(update_fields=['auth_policy'])
            self.doctor_service().target_url = 'http://fashion-product-service:8000/doctors'
            self.doctor_service().save(update_fields=['target_url'])
            token = str(RefreshToken.for_user(self.admin).access_token)
            for _ in range(2):
                self.assertEqual(self.get(HTTP_AUTHORIZATION=f'Bearer {token}').status_code, 200)
            self.assertEqual(proxy.call_count, 6)
        self.assertFalse(ProductResponseCache._memory_store)

    def test_private_response_headers_prevent_caching(self):
        self.doctor_service().target_url = 'http://fashion-product-service:8000/doctors'
        self.doctor_service().save(update_fields=['target_url'])
        for headers in ({'cache-control': 'private'}, {'cache-control': 'no-store'},
                        {'set-cookie': 'session=value'}, {'vary': 'Authorization'}):
            with patch('gateway.views.GatewayRequestProxy.proxy',
                       return_value=httpx.Response(200, json={'ok': True}, headers=headers)) as proxy:
                self.assertEqual(self.get().status_code, 200)
                self.assertEqual(self.get().status_code, 200)
                self.assertEqual(proxy.call_count, 2)

    def test_dummy_head_and_options(self):
        origin, _ = self.backend()
        with httpx.Client(trust_env=False, timeout=5) as client:
            self.assertEqual(client.head(origin + '/doctors').status_code, 200)
            self.assertEqual(client.options(origin + '/doctors').json()['method'], 'OPTIONS')

    def test_analytics_filter_is_scoped_to_admin(self):
        with patch('gateway.views.GatewayRequestProxy.proxy',
                   return_value=httpx.Response(200, json={'ok': True})):
            self.get()
            self.get(self.other_key)
        self.client.force_authenticate(self.admin)
        summary = self.client.get('/admin-api/analytics/summary', {'application_id': self.app.id})
        self.assertEqual(summary.data['total_requests'], 1)
        self.assertEqual(self.client.get('/admin-api/analytics/summary').data['total_requests'], 2)
        outsider = User.objects.create_user(username='outsider', email='outsider@example.com', is_staff=True)
        self.client.force_authenticate(outsider)
        self.assertEqual(self.client.get('/admin-api/analytics/summary', {'application_id': self.app.id}).status_code, 404)


class ManagementAccessTests(APITestCase):
    def setUp(self):
        self.staff = User.objects.create_user(username='staff', email='staff@example.com', is_staff=True)
        self.user = User.objects.create_user(username='user', email='user@example.com', password='test-password-123')

    def test_management_endpoints_reject_nonstaff_and_anonymous(self):
        for user in (None, self.user):
            self.client.force_authenticate(user)
            for endpoint in ('projects', 'services', 'routes', 'applications', 'keys', 'dashboard', 'analytics/summary'):
                for method in ('get', 'post'):
                    with self.subTest(user=user, endpoint=endpoint, method=method):
                        response = getattr(self.client, method)('/admin-api/' + endpoint)
                        self.assertEqual(response.status_code, 401 if user is None else 403)

    def test_staff_cannot_link_foreign_project_service(self):
        self.client.force_authenticate(self.staff)
        project = self.client.post('/admin-api/projects', {'name': 'E-commerce'})
        self.assertEqual(project.status_code, 201)
        service = self.client.post('/admin-api/services', {
            'project': project.data['id'], 'name': 'Product Service', 'base_path': '/products',
        })
        self.assertEqual(service.status_code, 201, service.data)
        other = Project.objects.create(owner=self.staff, name='Other', slug='other')
        bad_route = self.client.post('/admin-api/routes', {
            'project': other.id, 'name': 'Bad', 'backend_service': service.data['id'],
        })
        self.assertEqual(bad_route.status_code, 400)
        self.assertFalse(Route.objects.exists())

    def test_inactive_user_cannot_log_in(self):
        self.user.is_active = False
        self.user.save()
        response = self.client.post('/admin-api/auth/login', {
            'email': self.user.email, 'password': 'test-password-123',
        })
        self.assertEqual(response.status_code, 400)


@skipUnless(os.getenv('GATESCALE_TEST_REDIS_URL'), 'Set GATESCALE_TEST_REDIS_URL to test a real shared Redis instance.')
class SharedRedisTests(SimpleTestCase):
    def test_independent_clients_share_atomic_bucket(self):
        first = RedisRateLimiter(os.environ['GATESCALE_TEST_REDIS_URL'])
        second = RedisRateLimiter(os.environ['GATESCALE_TEST_REDIS_URL'])
        key = f'gateway_test:{uuid4().hex}'
        try:
            with patch.object(first, '_fallback_allow_request', side_effect=AssertionError('Redis unavailable')), patch.object(
                    second, '_fallback_allow_request', side_effect=AssertionError('Redis unavailable')):
                self.assertTrue(first.allow_request(key, 1, 600))
                self.assertFalse(second.allow_request(key, 1, 600))
        finally:
            first.client.delete(key)
