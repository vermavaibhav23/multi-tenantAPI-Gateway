from django.contrib.auth.password_validation import validate_password
from rest_framework import serializers

from authentication.models import Application
from .models import ApplicationUser


class ApplicationUserSerializer(serializers.ModelSerializer):
    class Meta:
        model = ApplicationUser
        fields = ('id', 'application', 'email', 'is_active', 'created_at')
        read_only_fields = ('id', 'is_active', 'created_at')


class ApplicationUserCreateSerializer(ApplicationUserSerializer):
    password = serializers.CharField(
        write_only=True, trim_whitespace=False, validators=[validate_password]
    )

    class Meta(ApplicationUserSerializer.Meta):
        fields = ApplicationUserSerializer.Meta.fields + ('password',)

    def validate_email(self, value):
        return value.strip().lower()

    def validate_application(self, value):
        request = self.context['request']
        if not Application.objects.filter(
            pk=value.pk, project__owner=request.user
        ).exists():
            raise serializers.ValidationError('Application is not owned by this administrator.')
        return value

    def create(self, validated_data):
        password = validated_data.pop('password')
        user = ApplicationUser(**validated_data)
        user.set_password(password)
        user.save()
        return user


class ClientLoginSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(trim_whitespace=False)

    def validate_email(self, value):
        return value.strip().lower()


class ClientRefreshSerializer(serializers.Serializer):
    refresh = serializers.CharField(trim_whitespace=False)
