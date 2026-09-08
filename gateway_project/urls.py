from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path('admin/', admin.site.urls),
    path('admin-api/', include('authentication.urls')),
    path('admin-api/', include('users.urls')),
    path('admin-api/', include('routes.urls')),
    path('admin-api/', include('analytics.urls')),
    path('gateway/', include('gateway.urls')),
]
