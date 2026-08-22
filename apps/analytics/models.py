from django.db import models


class RequestLog(models.Model):
    project = models.ForeignKey('users.Project', on_delete=models.CASCADE, related_name='request_logs')
    api_key = models.ForeignKey('authentication.APIKey', on_delete=models.SET_NULL, null=True, blank=True, related_name='request_logs')
    application = models.ForeignKey('authentication.Application', on_delete=models.SET_NULL, null=True, blank=True, related_name='request_logs')
    customer = models.ForeignKey('users.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='gateway_request_logs')
    route = models.ForeignKey('routes.Route', on_delete=models.SET_NULL, null=True, blank=True, related_name='request_logs')
    endpoint = models.CharField(max_length=255)
    method = models.CharField(max_length=10, default='GET')
    status_code = models.PositiveIntegerField(default=200)
    latency_ms = models.FloatField(default=0.0)
    downstream_service = models.CharField(max_length=80, blank=True, default='')
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=['project', 'created_at']),
            models.Index(fields=['application', 'created_at']),
            models.Index(fields=['status_code', 'created_at']),
            models.Index(fields=['endpoint', 'created_at']),
            models.Index(fields=['downstream_service', 'created_at']),
        ]

    def __str__(self):
        return f'{self.method} {self.endpoint} -> {self.status_code}'
