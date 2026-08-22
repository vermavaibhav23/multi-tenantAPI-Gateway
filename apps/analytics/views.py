from django.db.models import Avg, Count
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import RequestLog


class AnalyticsSummaryView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        project_ids = list(request.user.projects.values_list('id', flat=True))
        logs = RequestLog.objects.filter(project_id__in=project_ids)
        total_requests = logs.count()
        average_latency = logs.aggregate(avg_latency=Avg('latency_ms')).get('avg_latency') or 0.0
        success_count = logs.filter(status_code__lt=400).count()
        failure_count = total_requests - success_count
        success_percentage = round((success_count / total_requests * 100), 2) if total_requests else 0.0
        failure_percentage = round((failure_count / total_requests * 100), 2) if total_requests else 0.0
        service_counts = logs.values('downstream_service').annotate(count=Count('downstream_service')).order_by('-count')
        endpoint_counts = logs.values('endpoint').annotate(count=Count('endpoint')).order_by('-count')[:5]
        most_used_endpoints = [
            {'endpoint': item['endpoint'], 'requests': item['count']} for item in endpoint_counts
        ]
        return Response({
            'total_requests': total_requests,
            'average_latency': round(float(average_latency), 2),
            'error_rate': failure_percentage,
            'success_percentage': success_percentage,
            'failure_percentage': failure_percentage,
            'services': [
                {'service': item['downstream_service'] or 'unknown', 'requests': item['count']} for item in service_counts
            ],
            'most_used_endpoints': most_used_endpoints,
        })
