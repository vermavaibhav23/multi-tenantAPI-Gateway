from django.urls import re_path

from .views import GatewayProxyView

urlpatterns = [
    re_path(r'^(?P<route_path>.+)$', GatewayProxyView.as_view(), name='gateway-proxy'),
]
