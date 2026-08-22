from django.db import models
from django.utils.text import slugify


class BackendService(models.Model):
    project = models.ForeignKey('users.Project', on_delete=models.CASCADE, related_name='backend_services')
    name = models.CharField(max_length=120)
    slug = models.SlugField(max_length=120, blank=True)
    description = models.TextField(blank=True, default='')
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [('project', 'slug')]
        indexes = [
            models.Index(fields=['project', 'is_active']),
            models.Index(fields=['slug']),
        ]

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name) or 'backend-service'
        super().save(*args, **kwargs)

    def __str__(self):
        return f'{self.name} ({self.project.name})'


class Route(models.Model):
    METHOD_CHOICES = [
        ('GET', 'GET'),
        ('POST', 'POST'),
        ('PUT', 'PUT'),
        ('PATCH', 'PATCH'),
        ('DELETE', 'DELETE'),
    ]
    project = models.ForeignKey('users.Project', on_delete=models.CASCADE, related_name='routes')
    backend_service = models.ForeignKey(BackendService, on_delete=models.CASCADE, related_name='routes', null=True, blank=True)
    name = models.CharField(max_length=120)
    method = models.CharField(max_length=10, choices=METHOD_CHOICES, default='GET')
    path = models.CharField(max_length=200)
    target_url = models.URLField(max_length=500, blank=True, default='')
    is_active = models.BooleanField(default=True)
    cache_ttl_seconds = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=['project', 'method', 'path']),
            models.Index(fields=['backend_service', 'method']),
            models.Index(fields=['method', 'path']),
            models.Index(fields=['is_active']),
        ]
        unique_together = [('project', 'method', 'path')]

    def __str__(self):
        return f'{self.method} {self.path}'
