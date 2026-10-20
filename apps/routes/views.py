from django.utils.text import slugify
from django.db import transaction
from rest_framework import generics, permissions
from rest_framework.exceptions import ValidationError

from .models import BackendService, Route
from .serializers import BackendServiceSerializer, RouteSerializer


def owned_project(request):
    project_id = request.data.get('project')
    if project_id is not None:
        try:
            project_id = int(project_id)
        except (TypeError, ValueError):
            raise ValidationError({'project': 'A numeric project ID is required.'})
        if project_id <= 0:
            raise ValidationError({'project': 'A positive project ID is required.'})
    project = request.user.projects.filter(id=project_id).first() if project_id else request.user.projects.first()
    if not project:
        raise ValidationError({'project': 'Project not found or not owned by this administrator.'})
    return project


class BackendServiceListCreateView(generics.ListCreateAPIView):
    serializer_class = BackendServiceSerializer
    permission_classes = [permissions.IsAdminUser]

    def get_queryset(self):
        return BackendService.objects.filter(project__owner=self.request.user).order_by('-created_at')

    @transaction.atomic
    def perform_create(self, serializer):
        project = owned_project(self.request)
        slug = serializer.validated_data.get('slug') or slugify(serializer.validated_data['name']) or 'backend-service'
        if BackendService.objects.filter(project=project, slug=slug).exists():
            raise ValidationError({'slug': 'A backend service with this slug already exists in the project.'})
        if BackendService.objects.filter(project=project, base_path=serializer.validated_data['base_path']).exists():
            raise ValidationError({'base_path': 'This public base path is already used within the project.'})
        serializer.save(project=project, slug=slug)


class RouteListCreateView(generics.ListCreateAPIView):
    serializer_class = RouteSerializer
    permission_classes = [permissions.IsAdminUser]

    def get_queryset(self):
        return Route.objects.select_related('backend_service').filter(
            project__owner=self.request.user).order_by('-created_at')

    @transaction.atomic
    def perform_create(self, serializer):
        project = owned_project(self.request)
        service = serializer.validated_data['backend_service']
        if service.project_id != project.id or not service.is_active:
            raise ValidationError({'backend_service': 'Service must be active and belong to the route project.'})
        if Route.objects.filter(project=project, backend_service=service).exists():
            raise ValidationError({'backend_service': 'This service already has a public route in the project.'})
        serializer.save(project=project)
