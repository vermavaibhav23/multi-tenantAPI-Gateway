from django.contrib.auth.hashers import check_password, make_password
from django.db import models


class ApplicationUser(models.Model):
    """An end user of one subscribing Application, never a ServLink staff User."""
    application = models.ForeignKey(
        'authentication.Application', on_delete=models.CASCADE, related_name='client_users'
    )
    email = models.EmailField()
    password_hash = models.CharField(max_length=128)
    is_active = models.BooleanField(default=True)
    token_version = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['application', 'email'], name='clientuser_application_email_unique'
            )
        ]
        indexes = [models.Index(fields=['application', 'is_active'])]

    def set_password(self, raw_password):
        self.password_hash = make_password(raw_password)

    def check_password(self, raw_password):
        return check_password(raw_password, self.password_hash)

    def save(self, *args, **kwargs):
        self.email = self.email.strip().lower()
        return super().save(*args, **kwargs)

    def __str__(self):
        return f'{self.email} (app {self.application_id})'
