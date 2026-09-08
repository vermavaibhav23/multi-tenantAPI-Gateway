import hashlib
import secrets
from datetime import datetime

from django.db import models
from django.utils import timezone
from django.utils.text import slugify


class APIKey(models.Model):
    application = models.ForeignKey('Application', on_delete=models.CASCADE, related_name='api_keys')
    name = models.CharField(max_length=120)
    key_hash = models.CharField(max_length=128, unique=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    last_used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=['application', 'revoked_at', 'expires_at']),
        ]

    @property
    def is_active(self):
        if self.revoked_at is not None:
            return False
        if self.expires_at and self.expires_at < timezone.now():
            return False
        return True

    @staticmethod
    def hash_key(raw_key):
        return hashlib.sha256(raw_key.encode('utf-8')).hexdigest()

    @staticmethod
    def create_for_project(project, name, expires_days=365):
        application = project.applications.first()
        if application is None:
            application = Application.objects.create(project=project, name='Default Application', slug='default-application')
        return APIKey.create_for_application(application, name, expires_days=expires_days)

    @staticmethod
    def create_for_application(application, name, expires_days=365):
        raw_key = f'ak_live_{secrets.token_urlsafe(32)}'
        api_key = APIKey.objects.create(
            application=application,
            name=name,
            key_hash=APIKey.hash_key(raw_key),
            expires_at=timezone.now() + timezone.timedelta(days=expires_days),
        )
        return api_key, raw_key

    @classmethod
    def validate_key(cls, raw_key):
        if not raw_key:
            return None
        hashed = cls.hash_key(raw_key)
        api_key = cls.objects.select_related('application__project').filter(key_hash=hashed).first()
        if not api_key or api_key.revoked_at is not None:
            return None
        if not api_key.application.is_active:
            return None
        if api_key.expires_at and api_key.expires_at < timezone.now():
            return None
        api_key.last_used_at = timezone.now()
        api_key.save(update_fields=['last_used_at'])
        return api_key

    def can_access(self, route):
        return self.application.has_permission(route)

    def revoke(self):
        self.revoked_at = timezone.now()
        self.save(update_fields=['revoked_at'])

    def __str__(self):
        return self.name


class Application(models.Model):
    PLAN_NORMAL = 'normal'
    PLAN_PREMIUM = 'premium'
    PLAN_CHOICES = [
        (PLAN_NORMAL, 'Normal'),
        (PLAN_PREMIUM, 'Premium'),
    ]
    PLAN_LIMITS = {
        PLAN_NORMAL: (100, 60),
        PLAN_PREMIUM: (1000, 60),
    }

    project = models.ForeignKey('users.Project', on_delete=models.CASCADE, related_name='applications')
    name = models.CharField(max_length=120)
    slug = models.SlugField(max_length=120, blank=True)
    plan = models.CharField(max_length=20, choices=PLAN_CHOICES, default=PLAN_NORMAL)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [('project', 'slug')]
        indexes = [
            models.Index(fields=['project', 'is_active']),
            models.Index(fields=['slug']),
        ]

    def has_permission(self, route):
        permissions = self.permissions.filter(is_active=True)
        if not permissions.exists():
            return True
        return permissions.filter(route=route).exists()

    def resolved_rate_limit(self):
        return self.PLAN_LIMITS.get(self.plan, self.PLAN_LIMITS[self.PLAN_NORMAL])

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name) or 'application'
        super().save(*args, **kwargs)

    def __str__(self):
        return f'{self.name} ({self.project.name})'


class RoutePermission(models.Model):
    application = models.ForeignKey(Application, on_delete=models.CASCADE, related_name='permissions')
    route = models.ForeignKey('routes.Route', on_delete=models.CASCADE, related_name='application_permissions')
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [('application', 'route')]
        indexes = [
            models.Index(fields=['application', 'is_active']),
            models.Index(fields=['route', 'is_active']),
        ]

    def __str__(self):
        return f'{self.application} -> {self.route}'
