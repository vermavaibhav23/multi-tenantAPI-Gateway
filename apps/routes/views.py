from django.utils.text import slugify
from django.db import IntegrityError, transaction
from rest_framework import generics, permissions
from rest_framework.exceptions import ValidationError

from .models import BackendService, Route, RouteMethod
from .serializers import BackendServiceSerializer, RouteSerializer, RouteMethodSerializer


def get_owned_project(request):
    project_id = request.data.get('project')
    if project_id is not None:
        try:
            project_id = int(project_id)
        except (TypeError, ValueError):
            raise ValidationError({'project': 'A numeric project ID is required.'})
        if project_id <= 0:
            raise ValidationError({'project': 'A positive project ID is required.'})
    project = (request.user.projects.filter(id=project_id).first()
               if project_id else request.user.projects.first())
    if not project:
        raise ValidationError({'project': 'Project not found or not owned by this administrator.'})
    return project


class BackendServiceListCreateView(generics.ListCreateAPIView):
    serializer_class = BackendServiceSerializer
    permission_classes = [permissions.IsAdminUser]

    def get_queryset(self):
        return BackendService.objects.filter(
            project__owner=self.request.user
        ).order_by('-created_at')

    @transaction.atomic
    def perform_create(self, serializer):
        project = get_owned_project(self.request)
        slug = (serializer.validated_data.get('slug')
                or slugify(serializer.validated_data['name']) or 'backend-service')
        if BackendService.objects.filter(project=project, slug=slug).exists():
            raise ValidationError({'slug': 'This service slug already exists in the project.'})
        if BackendService.objects.filter(
            project=project, name=serializer.validated_data['name']
        ).exists():
            raise ValidationError({'name': 'This service name already exists in the project.'})
        serializer.save(project=project, slug=slug)


class RouteListCreateView(generics.ListCreateAPIView):
    serializer_class = RouteSerializer
    permission_classes = [permissions.IsAdminUser]

    def get_queryset(self):
        return Route.objects.select_related('backend_service').prefetch_related(
            'method_policies'
        ).filter(project__owner=self.request.user).order_by('-created_at')

    def _get_backend_service(self, project):
        service_id = self.request.data.get('backend_service')
        if service_id:
            service = BackendService.objects.filter(
                id=service_id, project=project, is_active=True
            ).first()
            if service is None:
                raise ValidationError({
                    'backend_service': 'Service must be active and belong to this project.'
                })
            return service

        service_name = (self.request.data.get('backend_service_name')
                        or self.request.data.get('downstream_service'))
        service_slug = self.request.data.get('backend_service_slug')
        if not service_name and not service_slug:
            return None
        slug = service_slug or slugify(service_name)
        service, _ = BackendService.objects.get_or_create(
            project=project, slug=slug,
            defaults={'name': service_name or slug.replace('-', ' ').title()},
        )
        if not service.is_active:
            raise ValidationError({'backend_service': 'Service must be active.'})
        return service

    @transaction.atomic
    def perform_create(self, serializer):
        project = get_owned_project(self.request)
        base_path = serializer.validated_data['base_path']
        if Route.objects.filter(project=project, base_path=base_path).exists():
            raise ValidationError({'base_path': 'This project already has that public base path.'})
        backend_service = self._get_backend_service(project)
        if backend_service is None:
            raise ValidationError({'backend_service': 'An active backend service is required.'})
        serializer.save(project=project, backend_service=backend_service)


class RouteMethodListCreateView(generics.ListCreateAPIView):
    serializer_class = RouteMethodSerializer
    permission_classes = [permissions.IsAdminUser]

    def get_queryset(self):
        return RouteMethod.objects.select_related('route').filter(
            route__project__owner=self.request.user
        ).order_by('route_id', 'method')

    @transaction.atomic
    def perform_create(self, serializer):
        route = serializer.validated_data['route']
        if route.project.owner_id != self.request.user.id:
            raise ValidationError({'route': 'Route is not owned by this administrator.'})
        method = serializer.validated_data['method']
        if RouteMethod.objects.filter(route=route, method=method).exists():
            raise ValidationError({'method': 'A policy already exists for this route and method.'})
        serializer.save()
