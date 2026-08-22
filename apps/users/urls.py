from django.urls import path

from .views import APIKeyCreateView, APIKeyRevokeView, ApplicationListCreateView, DashboardView, ProjectListCreateView, RegisterView

urlpatterns = [
    path('auth/register', RegisterView.as_view(), name='register-user'),
    path('projects', ProjectListCreateView.as_view(), name='project-list-create'),
    path('applications', ApplicationListCreateView.as_view(), name='application-list-create'),
    path('keys', APIKeyCreateView.as_view(), name='api-key-create'),
    path('keys/<int:key_id>/revoke', APIKeyRevokeView.as_view(), name='api-key-revoke'),
    path('dashboard', DashboardView.as_view(), name='developer-dashboard'),
]
