from django.contrib import admin
from django.urls import include, path

from analytics.views import AnalyticsSummaryView
from authentication.views import LoginView

urlpatterns = [
    path('admin/', admin.site.urls),
    path('login', LoginView.as_view(), name='customer-login'),
    path('analytics', AnalyticsSummaryView.as_view(), name='analytics'),
    path('api/', include('authentication.urls')),
    path('api/', include('users.urls')),
    path('api/', include('routes.urls')),
    path('api/', include('analytics.urls')),
    path('api/', include('gateway.urls')),
    path('gateway/', include('gateway.urls')),
]
