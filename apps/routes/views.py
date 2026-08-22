from django.utils.text import slugify
from rest_framework import generics, permissions

from .models import BackendService, Route
from .serializers import BackendServiceSerializer, RouteSerializer


class BackendServiceListCreateView(generics.ListCreateAPIView):
    serializer_class = BackendServiceSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return BackendService.objects.filter(project__owner=self.request.user).order_by('-created_at')

    def perform_create(self, serializer):
        project_id = self.request.data.get('project')
        project = self.request.user.projects.filter(id=project_id).first() if project_id else self.request.user.projects.first()
        if not project:
            raise ValueError('Project not found or not owned by this user.')
        serializer.save(project=project)


class RouteListCreateView(generics.ListCreateAPIView):
    serializer_class = RouteSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return Route.objects.select_related('backend_service').filter(project__owner=self.request.user).order_by('-created_at')

    def _get_project(self):
        project_id = self.request.data.get('project')
        return self.request.user.projects.filter(id=project_id).first() if project_id else self.request.user.projects.first()

    def _get_backend_service(self, project):
        service_id = self.request.data.get('backend_service')
        if service_id:
            return BackendService.objects.filter(id=service_id, project=project, is_active=True).first()

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
        return service

    def perform_create(self, serializer):
        project = self._get_project()
        if not project:
            raise ValueError('Project not found or not owned by this user.')
        backend_service = self._get_backend_service(project)
        serializer.save(project=project, backend_service=backend_service)
