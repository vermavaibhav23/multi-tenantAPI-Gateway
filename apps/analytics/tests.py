from django.contrib.auth import get_user_model
from rest_framework.test import APIClient, APITestCase

from analytics.models import RequestLog
from authentication.models import APIKey
from users.models import Project


class AnalyticsTests(APITestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username='analytics-owner', email='analytics@example.com', password='password123')
        self.project = Project.objects.create(owner=self.user, name='Stats', slug='stats')
        self.api_key, self.raw_key = APIKey.create_for_project(self.project, 'analytics-key')

    def test_summary_logs_generated_correctly(self):
        RequestLog.objects.create(project=self.project, api_key=self.api_key, endpoint='/gateway/users', method='GET', status_code=200, latency_ms=120.5, ip_address='127.0.0.1')
        RequestLog.objects.create(project=self.project, api_key=self.api_key, endpoint='/gateway/users', method='GET', status_code=500, latency_ms=200.0, ip_address='127.0.0.1')
        client = APIClient()
        login_response = client.post('/api/auth/login', {'email': 'analytics@example.com', 'password': 'password123'}, format='json')
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {login_response.data['access']}")
        response = client.get('/api/analytics/summary')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['total_requests'], 2)
        self.assertGreaterEqual(response.data['average_latency'], 160.0)
