from django.contrib.auth import get_user_model
from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from authentication.models import APIKey, Application
from .models import Project
from .serializers import APIKeySerializer, ApplicationSerializer, ProjectSerializer, UserRegisterSerializer

User = get_user_model()


class RegisterView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = UserRegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        return Response({'id': user.id, 'email': user.email, 'username': user.username}, status=status.HTTP_201_CREATED)


class ProjectListCreateView(generics.ListCreateAPIView):
    serializer_class = ProjectSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return Project.objects.filter(owner=self.request.user).order_by('-created_at')

    def perform_create(self, serializer):
        serializer.save(owner=self.request.user)


class ApplicationListCreateView(generics.ListCreateAPIView):
    serializer_class = ApplicationSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return Application.objects.filter(project__owner=self.request.user).order_by('-created_at')

    def _get_project_for_create(self):
        project_id = self.request.data.get('project')
        project = self.request.user.projects.filter(id=project_id).first() if project_id else self.request.user.projects.first()
        return project

    def create(self, request, *args, **kwargs):
        project = self._get_project_for_create()
        if not project:
            return Response({'detail': 'Project not found or not owned by this user.'}, status=status.HTTP_404_NOT_FOUND)

        data = request.data.copy()
        data['project'] = project.id
        serializer = self.get_serializer(data=data)
        serializer.is_valid(raise_exception=True)
        application = serializer.save(project=project)
        api_key, raw_key = APIKey.create_for_application(application, f'{application.name} primary key')
        response_data = serializer.data
        response_data['api_key'] = {
            'id': api_key.id,
            'key': raw_key,
            'expires_at': api_key.expires_at,
        }
        return Response(response_data, status=status.HTTP_201_CREATED)


class APIKeyCreateView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        application_id = request.data.get('application')
        if not application_id:
            return Response({'detail': 'Application is required.'}, status=status.HTTP_400_BAD_REQUEST)
        application = Application.objects.filter(id=application_id, project__owner=request.user).first()
        if not application:
            return Response({'detail': 'Application not found or not owned by this user.'}, status=status.HTTP_404_NOT_FOUND)
        name = request.data.get('name', 'Primary API key')
        api_key, raw_key = APIKey.create_for_application(application, name)
        return Response({
            'id': api_key.id,
            'application': api_key.application_id,
            'name': api_key.name,
            'key': raw_key,
            'expires_at': api_key.expires_at,
        }, status=status.HTTP_201_CREATED)


class APIKeyRevokeView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, key_id):
        api_key = APIKey.objects.filter(id=key_id, application__project__owner=request.user).first()
        if not api_key:
            return Response({'detail': 'API key not found.'}, status=status.HTTP_404_NOT_FOUND)
        api_key.revoke()
        serializer = APIKeySerializer(api_key)
        return Response(serializer.data, status=status.HTTP_200_OK)


class DashboardView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        projects = Project.objects.filter(owner=request.user)
        payload = []
        for project in projects:
            payload.append({
                'id': project.id,
                'name': project.name,
                'route_count': project.routes.count(),
                'api_key_count': APIKey.objects.filter(application__project=project).count(),
                'requests': project.request_logs.count(),
            })
        return Response({'projects': payload})
