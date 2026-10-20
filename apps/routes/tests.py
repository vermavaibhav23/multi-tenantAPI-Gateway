from django.db import IntegrityError, transaction
from django.test import override_settings
from rest_framework.test import APITestCase

from routes.models import BackendService, Route
from users.models import Project, User


@override_settings(GATEWAY_ALLOWED_BACKEND_ORIGINS=['http://fashion-product-service:8000'])
class RouteCreationRegressionTests(APITestCase):
    def setUp(self):
        owner = User.objects.create_user(username='owner', is_staff=True, email='owner@example.com')
        self.project = Project.objects.create(owner=owner, name='Project', slug='project')
        self.client.force_authenticate(owner)

    def create_service(self, name, path, url=''):
        return self.client.post('/admin-api/services', {
            'project': self.project.id, 'name': name,
            'base_path': path, 'target_url': url,
        }, format='json')

    def test_one_route_per_service_handles_all_methods(self):
        service = self.create_service('Product Service', '/products',
                                      'http://fashion-product-service:8000/products')
        self.assertEqual(service.status_code, 201, service.data)
        response = self.client.post('/admin-api/routes', {
            'project': self.project.id, 'name': 'Product API',
            'backend_service': service.data['id'],
        }, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        route = Route.objects.get()
        self.assertEqual(route.backend_service.base_path, '/products')
        self.assertEqual(route.allowed_methods, ['GET', 'POST', 'PUT', 'PATCH', 'DELETE'])

        duplicate = self.client.post('/admin-api/routes', {
            'project': self.project.id, 'name': 'Duplicate',
            'backend_service': service.data['id'],
        }, format='json')
        self.assertEqual(duplicate.status_code, 400)

    def test_duplicate_base_path_rejected_in_same_project(self):
        self.assertEqual(self.create_service('Doctor', '/doctors').status_code, 201)
        duplicate = self.create_service('Other', '/doctors')
        self.assertEqual(duplicate.status_code, 400)
        self.assertEqual(BackendService.objects.count(), 1)

    def test_unapproved_destination_rejected_without_service_side_effect(self):
        response = self.create_service('Blocked', '/blocked', 'http://169.254.169.254/latest/meta-data/')
        self.assertEqual(response.status_code, 400)
        self.assertFalse(BackendService.objects.exists())

    def test_invalid_base_path_rejected(self):
        for path in ('doctors', '/', '/doctors/', '/doctors-other/../admin', '/doctors?x=1'):
            with self.subTest(path=path):
                self.assertEqual(self.create_service('Invalid', path).status_code, 400)
        self.assertFalse(BackendService.objects.exists())

    def test_service_must_belong_to_the_route_project(self):
        other = Project.objects.create(owner=self.project.owner, name='Other', slug='other')
        service = BackendService.objects.create(project=other, name='Doctor', base_path='/doctors')
        response = self.client.post('/admin-api/routes', {
            'project': self.project.id, 'name': 'Cross project', 'backend_service': service.id,
        })
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Route.objects.exists())

    def test_allowed_methods_are_validated(self):
        service = BackendService.objects.create(project=self.project, name='Doctor', base_path='/doctors')
        bad = self.client.post('/admin-api/routes', {
            'project': self.project.id, 'name': 'Bad', 'backend_service': service.id,
            'allowed_methods': ['GET', 'INVALID'],
        }, format='json')
        self.assertEqual(bad.status_code, 400)
        good = self.client.post('/admin-api/routes', {
            'project': self.project.id, 'name': 'Read only', 'backend_service': service.id,
            'allowed_methods': ['GET'],
        }, format='json')
        self.assertEqual(good.status_code, 201, good.data)

    def test_database_constraints_enforce_uniqueness(self):
        service = BackendService.objects.create(project=self.project, name='Doctor', base_path='/doctors')
        with self.assertRaises(IntegrityError), transaction.atomic():
            BackendService.objects.create(project=self.project, name='Other', base_path='/doctors')
        Route.objects.create(project=self.project, backend_service=service, name='Doctor')
        with self.assertRaises(IntegrityError), transaction.atomic():
            Route.objects.create(project=self.project, backend_service=service, name='Duplicate')
