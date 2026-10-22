from django.urls import path

from .views import (
    BackendServiceListCreateView, BackendServiceDetailView,
    RouteListCreateView, RouteDetailView,
    RouteMethodListCreateView, RouteMethodDetailView,
    RoutePermissionListCreateView, RoutePermissionDetailView,
)

urlpatterns = [
    path('services', BackendServiceListCreateView.as_view(), name='backend-service-list-create'),
    path('services/<int:pk>', BackendServiceDetailView.as_view(), name='backend-service-detail'),
    path('routes', RouteListCreateView.as_view(), name='route-list-create'),
    path('routes/<int:pk>', RouteDetailView.as_view(), name='route-detail'),
    path('route-methods', RouteMethodListCreateView.as_view(), name='route-method-list-create'),
    path('route-methods/<int:pk>', RouteMethodDetailView.as_view(), name='route-method-detail'),
    path('route-permissions', RoutePermissionListCreateView.as_view(), name='route-permissions'),
    path('route-permissions/<int:pk>', RoutePermissionDetailView.as_view(), name='route-permission-detail'),
]
