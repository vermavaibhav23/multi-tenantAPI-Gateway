from django.urls import path

from .views import BackendServiceListCreateView, RouteListCreateView, RouteMethodListCreateView

urlpatterns = [
    path('services', BackendServiceListCreateView.as_view(), name='backend-service-list-create'),
    path('routes', RouteListCreateView.as_view(), name='route-list-create'),
    path('route-methods', RouteMethodListCreateView.as_view(), name='route-method-list-create'),
]
