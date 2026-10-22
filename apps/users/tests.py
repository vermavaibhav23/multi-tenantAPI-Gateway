from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from rest_framework import status
from rest_framework.test import APITestCase

from authentication.models import APIKey, Application
from users.models import Project, User


class APIKeyValidationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='owner', is_staff=True, email='owner@example.com', password='password123')
        self.project = Project.objects.create(owner=self.user, name='Alpha', slug='alpha')

    def test_valid_key(self):
        api_key, raw_key = APIKey.create_for_project(self.project, 'primary')
        self.assertIsNotNone(APIKey.validate_key(raw_key))

    def test_api_key_validation_never_writes_usage_timestamp(self):
        api_key, raw_key = APIKey.create_for_project(self.project, 'primary')
        self.assertNotIn('last_used_at', {field.name for field in APIKey._meta.fields})
        with CaptureQueriesContext(connection) as executed:
            validated = APIKey.validate_key(raw_key)
        self.assertEqual(validated.pk, api_key.pk)
        self.assertFalse(any(
            query['sql'].lstrip().upper().startswith(('UPDATE ', 'INSERT ', 'DELETE '))
            for query in executed.captured_queries
        ), 'API key validation should only read the database')

    def test_expired_key(self):
        api_key, raw_key = APIKey.create_for_project(self.project, 'expired', expires_days=0)
        api_key.expires_at = api_key.expires_at
        self.assertIsNone(APIKey.validate_key(raw_key))

    def test_revoked_key(self):
        api_key, raw_key = APIKey.create_for_project(self.project, 'revoked')
        api_key.revoke()
        self.assertIsNone(APIKey.validate_key(raw_key))


class ApplicationCreateTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='owner', is_staff=True, email='owner@example.com', password='password123')
        self.project = Project.objects.create(owner=self.user, name='Fashion', slug='fashion')
        self.client.force_authenticate(user=self.user)

    def test_dashboard_reports_configuration_without_request_metrics(self):
        response = self.client.get('/admin-api/dashboard')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data['projects']), 1)
        entry = response.data['projects'][0]
        self.assertIn('route_count', entry)
        self.assertIn('api_key_count', entry)
        self.assertNotIn('requests', entry)

    def test_application_create_also_creates_api_key(self):
        response = self.client.post(
            '/admin-api/applications',
            {'project': self.project.id, 'name': 'Nykaa', 'plan': Application.PLAN_NORMAL},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        application = Application.objects.get(name='Nykaa')
        api_key = APIKey.objects.get(application=application)
        self.assertEqual(api_key.application.project, self.project)
        self.assertEqual(response.data['api_key']['id'], api_key.id)
        self.assertTrue(response.data['api_key']['key'].startswith('ak_live_'))
        self.assertEqual(APIKey.validate_key(response.data['api_key']['key']), api_key)
