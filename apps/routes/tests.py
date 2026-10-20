from django.db import IntegrityError, transaction
from django.test import override_settings
from rest_framework.test import APITestCase

from routes.models import BackendService, Route, RouteMethod
from users.models import Project, User


@override_settings(GATEWAY_ALLOWED_BACKEND_ORIGINS=['http://fashion-product-service:8000'])
class RouteCreationTests(APITestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username='owner', is_staff=True, email='owner@example.com'
        )
        self.project = Project.objects.create(owner=self.owner, name='Project', slug='project')
        self.client.force_authenticate(self.owner)

    def add_route(self, path='/products', method='GET', auth_policy='api_key_only'):
        service = BackendService.objects.create(
            project=self.project, name='Product Service', slug='product-service',
            base_url='http://fashion-product-service:8000',
        )
        response = self.client.post('/admin-api/routes', {
            'project': self.project.id, 'name': 'Products',
            'base_path': path, 'backend_service': service.id,
        }, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        route = Route.objects.get()
        policy = self.client.post('/admin-api/route-methods', {
            'route': route.id, 'method': method, 'auth_policy': auth_policy,
        }, format='json')
        self.assertEqual(policy.status_code, 201, policy.data)
        return route

    def test_admin_registers_service_origin_and_route_separately(self):
        service_response = self.client.post('/admin-api/services', {
            'project': self.project.id,
            'name': 'Doctor Service',
            'base_url': 'http://fashion-product-service:8000',
        }, format='json')
        self.assertEqual(service_response.status_code, 201, service_response.data)
        self.assertEqual(service_response.data['base_url'],
                         'http://fashion-product-service:8000')

        route_response = self.client.post('/admin-api/routes', {
            'project': self.project.id,
            'name': 'Doctors',
            'backend_service': service_response.data['id'],
            'base_path': '/doctors',
        }, format='json')
        self.assertEqual(route_response.status_code, 201, route_response.data)
        self.assertNotIn('target_url', route_response.data)
        self.assertEqual(route_response.data['backend_service_detail']['base_url'],
                         'http://fashion-product-service:8000')

    def test_route_and_method_policy_created_separately(self):
        route = self.add_route()
        self.assertEqual(route.base_path, '/products')
        self.assertEqual(route.backend_service.name, 'Product Service')
        self.assertEqual(RouteMethod.objects.get(route=route).method, 'GET')
        self.assertEqual(Route.objects.count(), 1)

    def test_multiple_methods_share_one_route(self):
        route = self.add_route()
        response = self.client.post('/admin-api/route-methods', {
            'route': route.id, 'method': 'POST', 'auth_policy': 'api_key_and_jwt',
        }, format='json')
        self.assertEqual(response.status_code, 201)
        self.assertEqual(route.method_policies.count(), 2)
        self.assertEqual(Route.objects.count(), 1)
        duplicate = self.client.post('/admin-api/route-methods', {
            'route': route.id, 'method': 'POST',
        }, format='json')
        self.assertEqual(duplicate.status_code, 400)

    def test_duplicate_base_path_fails_even_with_another_service(self):
        route = self.add_route()
        other = BackendService.objects.create(
            project=self.project, name='Other Service', slug='other-service'
        )
        response = self.client.post('/admin-api/routes', {
            'project': self.project.id, 'name': 'Duplicate',
            'base_path': '/products', 'backend_service': other.id,
        }, format='json')
        self.assertEqual(response.status_code, 400)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Route.objects.create(
                project=self.project, backend_service=other,
                name='Duplicate', base_path='/products',
            )

    def test_service_can_have_multiple_paths(self):
        route = self.add_route()
        response = self.client.post('/admin-api/routes', {
            'project': self.project.id, 'name': 'Catalog',
            'base_path': '/catalog', 'backend_service': route.backend_service_id,
        }, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(Route.objects.filter(backend_service=route.backend_service).count(), 2)

    def test_cross_project_service_or_policy_rejected(self):
        route = self.add_route()
        other_owner = User.objects.create_user(
            username='other', is_staff=True, email='other@example.com'
        )
        self.client.force_authenticate(other_owner)
        bad = self.client.post('/admin-api/route-methods', {
            'route': route.id, 'method': 'DELETE',
        }, format='json')
        self.assertEqual(bad.status_code, 400)
        foreign_project = Project.objects.create(
            owner=other_owner, name='Foreign', slug='foreign'
        )
        foreign_service = BackendService.objects.create(
            project=foreign_project, name='Foreign Service'
        )
        self.client.force_authenticate(self.owner)
        bad = self.client.post('/admin-api/routes', {
            'project': self.project.id, 'name': 'Foreign',
            'base_path': '/foreign', 'backend_service': foreign_service.id,
        }, format='json')
        self.assertEqual(bad.status_code, 400)

    def test_destination_validation_happens_before_creating_service(self):
        response = self.client.post('/admin-api/services', {
            'project': self.project.id, 'name': 'Blocked',
            'base_url': 'http://169.254.169.254',
        }, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Route.objects.exists())
        self.assertFalse(BackendService.objects.exists())

    def test_service_origin_must_not_include_path_query_or_credentials(self):
        for value in (
            'http://fashion-product-service:8000/products',
            'http://fashion-product-service:8000?debug=1',
            'http://user:pass@fashion-product-service:8000',
            'http://fashion-product-service:8000/',
        ):
            with self.subTest(base_url=value):
                response = self.client.post('/admin-api/services', {
                    'project': self.project.id, 'name': 'Bad Origin',
                    'base_url': value,
                }, format='json')
                self.assertEqual(response.status_code, 400)
        self.assertFalse(BackendService.objects.exists())

    def test_unique_backend_service_name_within_project(self):
        BackendService.objects.create(
            project=self.project, name='Doctor Service', slug='doctor-one'
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            BackendService.objects.create(
                project=self.project, name='Doctor Service', slug='doctor-two'
            )

    def test_invalid_base_paths_rejected(self):
        service = BackendService.objects.create(project=self.project, name='Doctor')
        for bad_path in ('doctors', '/doctors/', '//doctors', '/x/../admin', '/x?y=2'):
            with self.subTest(path=bad_path):
                response = self.client.post('/admin-api/routes', {
                    'project': self.project.id, 'name': 'Bad',
                    'base_path': bad_path, 'backend_service': service.id,
                }, format='json')
                self.assertEqual(response.status_code, 400)
