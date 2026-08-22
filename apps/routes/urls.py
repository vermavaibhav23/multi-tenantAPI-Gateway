from django.urls import path

from .views import BackendServiceListCreateView, RouteListCreateView

urlpatterns = [
    path('services', BackendServiceListCreateView.as_view(), name='backend-service-list-create'),
    path('routes', RouteListCreateView.as_view(), name='route-list-create'),
]
