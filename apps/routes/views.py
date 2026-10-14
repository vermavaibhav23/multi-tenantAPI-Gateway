from django.utils.text import slugify
from django.db import transaction
from rest_framework import generics, permissions
from rest_framework.exceptions import ValidationError

from .models import BackendService, Route
from .serializers import BackendServiceSerializer, RouteSerializer


class BackendServiceListCreateView(generics.ListCreateAPIView):
    serializer_class = BackendServiceSerializer
    permission_classes = [permissions.IsAdminUser]

    def get_queryset(self):
        return BackendService.objects.filter(project__owner=self.request.user).order_by('-created_at')

    @transaction.atomic
    def perform_create(self, serializer):
        project_id = self.request.data.get('project')
        if project_id is not None:
            try:
                project_id = int(project_id)
            except (TypeError, ValueError):
                raise ValidationError({'project': 'A numeric project ID is required.'})
            if project_id <= 0:
                raise ValidationError({'project': 'A positive project ID is required.'})
        project = self.request.user.projects.filter(id=project_id).first() if project_id else self.request.user.projects.first()
        if not project:
            raise ValidationError({'project': 'Project not found or not owned by this administrator.'})
        slug = serializer.validated_data.get('slug') or slugify(serializer.validated_data['name']) or 'backend-service'
        if BackendService.objects.filter(project=project, slug=slug).exists():
            raise ValidationError({'slug': 'A backend service with this slug already exists in the project.'})
        serializer.save(project=project, slug=slug)


class RouteListCreateView(generics.ListCreateAPIView):
    serializer_class = RouteSerializer
    permission_classes = [permissions.IsAdminUser]

    def get_queryset(self):
        return Route.objects.select_related('backend_service').filter(project__owner=self.request.user).order_by('-created_at')

    def _get_project(self):
        project_id = self.request.data.get('project')
        if project_id is not None:
            try:
                project_id = int(project_id)
            except (TypeError, ValueError):
                raise ValidationError({'project': 'A numeric project ID is required.'})
            if project_id <= 0:
                raise ValidationError({'project': 'A positive project ID is required.'})
        return self.request.user.projects.filter(id=project_id).first() if project_id else self.request.user.projects.first()

    def _get_backend_service(self, project):
        service_id = self.request.data.get('backend_service')
        if service_id:
            service = BackendService.objects.filter(id=service_id, project=project, is_active=True).first()
            if service is None:
                raise ValidationError({'backend_service': 'Service must be active and belong to the route project.'})
            return service

        service_name = self.request.data.get('backend_service_name') or self.request.data.get('downstream_service')
        service_slug = self.request.data.get('backend_service_slug')
        if not service_name and not service_slug:
            return None

        slug = service_slug or slugify(service_name)
        service, _ = BackendService.objects.get_or_create(
            project=project,
            slug=slug,
            defaults={'name': service_name or slug.replace('-', ' ').title()},
        )
        if not service.is_active:
            raise ValidationError({'backend_service': 'Service must be active.'})
        return service

    @transaction.atomic
    def perform_create(self, serializer):
        project = self._get_project()
        if not project:
            raise ValidationError({'project': 'Project not found or not owned by this administrator.'})
        if Route.objects.filter(project=project, method=serializer.validated_data.get('method', 'GET'),
                                path=serializer.validated_data['path']).exists():
            raise ValidationError({'path': 'A route already exists for this project, method and path.'})
        backend_service = self._get_backend_service(project)
        serializer.save(project=project, backend_service=backend_service)
