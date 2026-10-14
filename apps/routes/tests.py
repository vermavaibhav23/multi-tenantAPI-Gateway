from django.test import override_settings
from rest_framework.test import APITestCase

from routes.models import BackendService, Route
from users.models import Project, User


@override_settings(GATEWAY_ALLOWED_BACKEND_ORIGINS=['http://fashion-product-service:8000'])
class RouteCreationRegressionTests(APITestCase):
    def setUp(self):
        owner = User.objects.create_user(username='owner', email='owner@example.com')
        self.project = Project.objects.create(owner=owner, name='Project', slug='project')
        self.client.force_authenticate(owner)

    def test_named_service_and_docker_url_create_route(self):
        response = self.client.post('/admin-api/routes', {
            'project': self.project.id, 'name': 'Echo', 'path': '/echo',
            'backend_service_name': 'Product Service', 'backend_service_slug': 'product-service',
            'target_url': 'http://fashion-product-service:8000/products',
        }, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        route = Route.objects.get()
        self.assertEqual(route.backend_service.name, 'Product Service')
        self.assertEqual(route.backend_service.slug, 'product-service')
        self.assertEqual(route.backend_service.project, self.project)

    def test_unapproved_destination_rejected_without_service_side_effect(self):
        response = self.client.post('/admin-api/routes', {
            'project': self.project.id, 'name': 'Blocked', 'path': '/meta',
            'backend_service_name': 'Blocked', 'target_url': 'http://169.254.169.254/latest/meta-data/',
        }, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Route.objects.exists())
        self.assertFalse(BackendService.objects.exists())

    def test_existing_service_and_empty_target_remain_supported(self):
        service = BackendService.objects.create(project=self.project, name='Existing')
        response = self.client.post('/admin-api/routes', {
            'project': self.project.id, 'name': 'Empty', 'path': '/empty', 'backend_service': service.id,
        }, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(Route.objects.get().backend_service, service)
