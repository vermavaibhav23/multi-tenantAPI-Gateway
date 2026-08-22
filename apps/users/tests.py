from django.test import TestCase

from authentication.models import APIKey
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
