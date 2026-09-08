from django.test import TestCase
from rest_framework import status
from rest_framework.test import APITestCase

from authentication.models import APIKey, Application
from users.models import Project, User


class APIKeyValidationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='owner', email='owner@example.com', password='password123')
        self.project = Project.objects.create(owner=self.user, name='Alpha', slug='alpha')

    def test_valid_key(self):
        api_key, raw_key = APIKey.create_for_project(self.project, 'primary')
        self.assertIsNotNone(APIKey.validate_key(raw_key))

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
        self.user = User.objects.create_user(username='owner', email='owner@example.com', password='password123')
        self.project = Project.objects.create(owner=self.user, name='Fashion', slug='fashion')
        self.client.force_authenticate(user=self.user)

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
