from django.db import models
from django.utils.text import slugify


class BackendService(models.Model):
    project = models.ForeignKey('users.Project', on_delete=models.CASCADE, related_name='backend_services')
    name = models.CharField(max_length=120)
    slug = models.SlugField(max_length=120, blank=True)
    description = models.TextField(blank=True, default='')
    # Internal network origin only, e.g. http://healthcare-doctor-service:8000.
    base_url = models.URLField(max_length=500, blank=True, default='')
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [('project', 'slug'), ('project', 'name')]
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
    project = models.ForeignKey('users.Project', on_delete=models.CASCADE, related_name='routes')
    backend_service = models.ForeignKey(
        BackendService, on_delete=models.CASCADE, related_name='routes', null=True, blank=True
    )
    name = models.CharField(max_length=120)
    base_path = models.CharField(max_length=200)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=['is_active']),
        ]
        unique_together = [('project', 'base_path')]

    def save(self, *args, **kwargs):
        from django.core.exceptions import ValidationError
        if (not self.base_path or not self.base_path.startswith('/')
                or self.base_path == '/' or self.base_path.count('/') != 1):
            raise ValidationError(
                'Route base_path must have one segment, e.g. /doctors.'
            )
        super().save(*args, **kwargs)

    def __str__(self):
        return self.base_path


class RouteMethod(models.Model):
    AUTH_API_KEY_ONLY = 'api_key_only'
    AUTH_API_KEY_AND_JWT = 'api_key_and_jwt'
    AUTH_POLICY_CHOICES = [
        (AUTH_API_KEY_ONLY, 'API key only'),
        (AUTH_API_KEY_AND_JWT, 'API key and JWT'),
    ]
    METHOD_CHOICES = [
        ('GET', 'GET'),
        ('POST', 'POST'),
        ('PUT', 'PUT'),
        ('PATCH', 'PATCH'),
        ('DELETE', 'DELETE'),
    ]
    route = models.ForeignKey(Route, on_delete=models.CASCADE, related_name='method_policies')
    method = models.CharField(max_length=10, choices=METHOD_CHOICES)
    auth_policy = models.CharField(
        max_length=20, choices=AUTH_POLICY_CHOICES, default=AUTH_API_KEY_ONLY
    )

    class Meta:
        unique_together = [('route', 'method')]

    def __str__(self):
        return f'{self.method} {self.route.base_path} ({self.auth_policy})'
