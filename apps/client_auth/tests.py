from unittest.mock import patch

import redis
from django.test import override_settings
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from authentication.models import APIKey, Application
from client_auth.models import ApplicationUser
from client_auth.services import issue_token_pair
from gateway.test_support import FakeRedisClient
from routes.models import BackendService, Route, RouteMethod
from users.models import Project, User


@override_settings(
    GATEWAY_ALLOWED_BACKEND_ORIGINS=['http://fashion-product-service:8000'],
    CLIENT_JWT_SIGNING_KEY='test-client-key-distinct-from-staff',
)
class ClientUserAuthenticationTests(APITestCase):
    def setUp(self):
        shared_redis = FakeRedisClient()
        mocked_redis = patch('gateway.services.get_redis_client', return_value=shared_redis)
        mocked_redis.start()
        self.addCleanup(mocked_redis.stop)
        route_cache_redis = patch('gateway.route_cache.get_redis_client', return_value=shared_redis)
        route_cache_redis.start()
        self.addCleanup(route_cache_redis.stop)
        self.admin = User.objects.create_user(
            username='owner', email='owner@example.com',
            password='AdminSecret123!', is_staff=True,
        )
        self.project = Project.objects.create(
            owner=self.admin, name='Healthcare', slug='healthcare'
        )
        self.app_a = Application.objects.create(project=self.project, name='Hospital A')
        self.app_b = Application.objects.create(project=self.project, name='Clinic B')
        _, self.key_a = APIKey.create_for_application(self.app_a, 'hospital-a')
        _, self.key_b = APIKey.create_for_application(self.app_b, 'clinic-b')

        service = BackendService.objects.create(
            project=self.project, name='Doctor Service', slug='doctor-service',
            base_url='http://fashion-product-service:8000',
        )
        self.route = Route.objects.create(
            project=self.project, name='Doctors', backend_service=service,
            base_path='/doctors',
        )
        RouteMethod.objects.create(
            route=self.route, method='GET', auth_policy=RouteMethod.AUTH_API_KEY_AND_JWT
        )
        self.password = 'ApplicationTestPassword123!'
        self.client_user_a = ApplicationUser(
            application=self.app_a, email='doctor@example.com'
        )
        self.client_user_a.set_password(self.password)
        self.client_user_a.save()
        self.client_user_b = ApplicationUser(
            application=self.app_b, email='doctor@example.com'
        )
        self.client_user_b.set_password(self.password)
        self.client_user_b.save()

    def _login(self, key, email='doctor@example.com', password=None):
        return self.client.post('/gateway/auth/login', {
            'email': email, 'password': password or self.password,
        }, format='json', HTTP_X_API_KEY=key)

    def _protected(self, key, access):
        return self.client.get('/gateway/doctors/456', HTTP_X_API_KEY=key,
                               HTTP_AUTHORIZATION='Bearer ' + access)

    def _register(self, key=None, email='newpatient@example.com',
                  password='StrongPatientPassword123!', **extra):
        payload = {'email': email, 'password': password, **extra}
        return self.client.post(
            '/gateway/auth/register', payload,
            format='json', HTTP_X_API_KEY=key or self.key_a,
        )

    def test_self_register_then_login_and_access_protected_route(self):
        registered = self._register()
        self.assertEqual(registered.status_code, 201, registered.data)
        self.assertEqual(registered.data['application'], self.app_a.id)
        self.assertEqual(registered.data['email'], 'newpatient@example.com')
        self.assertTrue(registered.data['is_active'])
        self.assertNotIn('access', registered.data)
        self.assertNotIn('refresh', registered.data)
        self.assertNotIn('password', registered.data)
        self.assertNotIn('password_hash', registered.data)

        user = ApplicationUser.objects.get(pk=registered.data['id'])
        self.assertTrue(user.check_password('StrongPatientPassword123!'))
        self.assertNotEqual(user.password_hash, 'StrongPatientPassword123!')
        login = self._login(
            self.key_a, email='newpatient@example.com', password='StrongPatientPassword123!'
        )
        self.assertEqual(login.status_code, 200, login.data)
        validated = self._protected(self.key_a, login.data['access'])
        self.assertEqual(validated.status_code, 200)
        self.assertEqual(validated.json()['client_user_id'], user.pk)

    def test_register_rejects_missing_invalid_or_revoked_application_api_key(self):
        data = {'email': 'newpatient@example.com', 'password': 'StrongPatientPassword123!'}
        self.assertEqual(self.client.post('/gateway/auth/register', data, format='json').status_code, 401)
        self.assertEqual(
            self.client.post('/gateway/auth/register', data, format='json',
                             HTTP_X_API_KEY='not-a-key').status_code, 401
        )
        self.app_a.is_active = False
        self.app_a.save(update_fields=['is_active'])
        self.assertEqual(self._register().status_code, 401)
        self.assertFalse(ApplicationUser.objects.filter(email='newpatient@example.com').exists())

    def test_registration_uses_api_key_context_not_user_chosen_application_or_role(self):
        for injected in (
            {'application': self.app_b.pk}, {'application_id': self.app_b.pk},
            {'role': 'admin'}, {'is_active': True}, {'permissions': ['doctors:delete']},
        ):
            with self.subTest(payload=injected):
                response = self._register(**injected)
                self.assertEqual(response.status_code, 400, response.data)
        self.assertFalse(ApplicationUser.objects.filter(
            application=self.app_a, email='newpatient@example.com'
        ).exists())

    def test_registration_rejects_duplicate_within_app_but_isolates_other_app(self):
        first = self._register(email='NewPatient@Example.Com')
        self.assertEqual(first.status_code, 201, first.data)
        self.assertEqual(first.data['email'], 'newpatient@example.com')
        duplicate = self._register(email='newpatient@example.com')
        self.assertEqual(duplicate.status_code, 400)
        other = self._register(key=self.key_b, email='newpatient@example.com')
        self.assertEqual(other.status_code, 201, other.data)
        self.assertNotEqual(first.data['id'], other.data['id'])
        self.assertEqual(ApplicationUser.objects.filter(
            email='newpatient@example.com'
        ).count(), 2)
        self.assertEqual(self._login(self.key_a, email='newpatient@example.com',
                                     password='StrongPatientPassword123!').status_code, 200)

    def test_registration_password_validation_and_shape(self):
        for password in ('12345678', 'password', 'short'):
            with self.subTest(password=password):
                self.assertEqual(self._register(password=password).status_code, 400)
        self.assertEqual(self._register(email='invalid-email').status_code, 400)
        self.assertFalse(ApplicationUser.objects.filter(email='newpatient@example.com').exists())

    def test_registration_rate_limit(self):
        for n in range(5):
            result = self._register(email='newpatient@example.com', password='weak')
            self.assertEqual(result.status_code, 400)
        # Validation errors are rejected before the attempt counter.
        self.assertEqual(self._register().status_code, 201)
        for n in range(4):
            self.assertEqual(self._register().status_code, 400)
        self.assertEqual(self._register().status_code, 429)

    def test_app_user_is_not_a_platform_user_and_password_is_hashed(self):
        self.assertNotEqual(self.client_user_a.password_hash, self.password)
        self.assertTrue(self.client_user_a.check_password(self.password))
        self.assertFalse(User.objects.filter(email='doctor@example.com').exists())

    def test_login_requires_app_key_and_correct_application_credentials(self):
        self.assertEqual(self.client.post('/gateway/auth/login', {
            'email': 'doctor@example.com', 'password': self.password,
        }).status_code, 401)
        self.assertEqual(self._login(self.key_a, password='wrong').status_code, 401)
        success = self._login(self.key_a)
        self.assertEqual(success.status_code, 200, success.data)
        self.assertIn('access', success.data)
        self.assertIn('refresh', success.data)
        self.assertEqual(success.data['expires_in'], 900)

    def test_valid_client_token_passes_to_backend(self):
        token = self._login(self.key_a).data['access']
        resp = self._protected(self.key_a, token)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()['client_user_id'], self.client_user_a.pk)
        self.assertFalse(resp.json()['forwarded'])

    def test_client_jwt_cannot_cross_applications_even_if_same_email(self):
        token = self._login(self.key_a).data['access']
        self.assertEqual(self._protected(self.key_b, token).status_code, 401)
        token_b = self._login(self.key_b).data['access']
        result = self._protected(self.key_b, token_b)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['client_user_id'], self.client_user_b.pk)

    def test_staff_jwt_cannot_authenticate_client_endpoint(self):
        staff = str(RefreshToken.for_user(self.admin).access_token)
        self.assertEqual(self._protected(self.key_a, staff).status_code, 401)

    def test_refresh_issues_new_access_and_rejects_access_as_refresh(self):
        pair = self._login(self.key_a).data
        refreshed = self.client.post('/gateway/auth/refresh', {
            'refresh': pair['refresh'],
        }, format='json', HTTP_X_API_KEY=self.key_a)
        self.assertEqual(refreshed.status_code, 200, refreshed.data)
        self.assertIn('access', refreshed.data)
        self.assertEqual(self._protected(self.key_a, pair['refresh']).status_code, 401)
        self.assertEqual(self.client.post('/gateway/auth/refresh', {
            'refresh': pair['access'],
        }, format='json', HTTP_X_API_KEY=self.key_a).status_code, 401)
        self.assertEqual(self.client.post('/gateway/auth/refresh', {
            'refresh': pair['refresh'],
        }, format='json', HTTP_X_API_KEY=self.key_b).status_code, 401)

    def test_staff_only_provisioning_and_deactivation_invalidate_access(self):
        admin_token = str(RefreshToken.for_user(self.admin).access_token)
        response = self.client.post('/admin-api/application-users', {
            'application': self.app_a.pk, 'email': 'New@Example.Com',
            'password': 'AnotherValidPassword123!',
        }, format='json', HTTP_AUTHORIZATION='Bearer ' + admin_token)
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data['email'], 'new@example.com')
        self.assertNotIn('password', response.data)
        self.assertNotIn('password_hash', response.data)
        duplicate = self.client.post('/admin-api/application-users', {
            'application': self.app_a.pk, 'email': 'NEW@EXAMPLE.COM',
            'password': 'AnotherValidPassword123!',
        }, format='json', HTTP_AUTHORIZATION='Bearer ' + admin_token)
        self.assertEqual(duplicate.status_code, 400)
        anonymous = self.client.post('/admin-api/application-users', {
            'application': self.app_a.pk, 'email': 'no@example.com',
            'password': 'AnotherValidPassword123!',
        }, format='json')
        self.assertEqual(anonymous.status_code, 401)

        pair = self._login(self.key_a).data
        disabled = self.client.post(
            f'/admin-api/application-users/{self.client_user_a.pk}/disable',
            HTTP_AUTHORIZATION='Bearer ' + admin_token,
        )
        self.assertEqual(disabled.status_code, 200)
        self.assertEqual(self._protected(self.key_a, pair['access']).status_code, 401)
        self.assertEqual(self.client.post('/gateway/auth/refresh', {
            'refresh': pair['refresh'],
        }, format='json', HTTP_X_API_KEY=self.key_a).status_code, 401)
        self.assertEqual(self._login(self.key_a).status_code, 401)

    def test_stolen_or_modified_token_and_missing_key_are_rejected(self):
        token = self._login(self.key_a).data['access']
        self.assertEqual(self._protected(self.key_a, token + 'tampered').status_code, 401)
        self.assertEqual(self.client.get('/gateway/doctors/456',
            HTTP_AUTHORIZATION='Bearer ' + token).status_code, 401)
        _, rotated = APIKey.create_for_application(self.app_a, 'rotated')
        self.assertEqual(self._protected(self.key_a, token).status_code, 401)
        self.assertEqual(self._protected(rotated, token).status_code, 200)

    def test_gateway_passes_verified_identity_not_bearer_or_spoofed_headers(self):
        token = self._login(self.key_a).data['access']
        result = self.client.get(
            '/gateway/doctors/456',
            HTTP_X_API_KEY=self.key_a,
            HTTP_AUTHORIZATION='Bearer ' + token,
            HTTP_X_GATESCALE_CLIENT_USER_ID='99999',
        )
        self.assertEqual(result.status_code, 200)
        preview = result.json()
        self.assertEqual(preview['application_id'], self.app_a.pk)
        self.assertEqual(preview['client_user_id'], self.client_user_a.pk)
        self.assertNotIn('headers', preview)
        self.assertNotIn('Authorization', str(preview))
        self.assertNotIn(token, str(preview))
        self.assertNotIn(self.key_a, str(preview))

    def test_staff_cannot_create_client_for_other_admin_project(self):
        other_staff = User.objects.create_user(username='other', email='other@example.com',
                                               password='StaffOtherPassword123!', is_staff=True)
        other_token = str(RefreshToken.for_user(other_staff).access_token)
        response = self.client.post('/admin-api/application-users', {
            'application': self.app_a.pk, 'email': 'new@example.com',
            'password': 'AnotherValidPassword123!',
        }, format='json', HTTP_AUTHORIZATION='Bearer ' + other_token)
        self.assertEqual(response.status_code, 400)

    def test_login_rate_limit_blocks_excessive_attempts(self):
        for _ in range(10):
            self.assertEqual(self._login(self.key_a, password='wrong').status_code, 401)
        self.assertEqual(self._login(self.key_a).status_code, 429)

    def test_registration_fails_closed_during_redis_outage(self):
        with patch('gateway.services.get_redis_client', side_effect=redis.ConnectionError('offline')):
            result = self._register(email='outage@example.com')
        self.assertEqual(result.status_code, 503)
        self.assertFalse(ApplicationUser.objects.filter(email='outage@example.com').exists())

    def test_login_fails_closed_during_redis_outage(self):
        with patch('gateway.services.get_redis_client', side_effect=redis.ConnectionError('offline')):
            result = self._login(self.key_a)
        self.assertEqual(result.status_code, 503)
        self.assertNotIn('access', result.data)
