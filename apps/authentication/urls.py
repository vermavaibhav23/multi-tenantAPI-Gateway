from django.urls import path

from .views import LoginView, RefreshTokenView

urlpatterns = [
    path('auth/login', LoginView.as_view(), name='login'),
    path('auth/refresh', RefreshTokenView.as_view(), name='refresh-token'),
]
