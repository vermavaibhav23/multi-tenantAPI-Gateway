from django.urls import path

from .views import ApplicationUserListCreateView, ApplicationUserDisableView

urlpatterns = [
    path('application-users', ApplicationUserListCreateView.as_view(), name='application-users'),
    path('application-users/<int:user_id>/disable',
         ApplicationUserDisableView.as_view(), name='application-user-disable'),
]
