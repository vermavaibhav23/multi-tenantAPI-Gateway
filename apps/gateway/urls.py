from django.urls import path, re_path
from client_auth.views import ClientLoginView, ClientRefreshView

from .views import GatewayProxyView

urlpatterns = [
    path('auth/login', ClientLoginView.as_view(), name='client-login'),
    path('auth/refresh', ClientRefreshView.as_view(), name='client-refresh'),
    re_path(r'^(?P<route_path>.+)$', GatewayProxyView.as_view(), name='gateway-proxy'),
]
