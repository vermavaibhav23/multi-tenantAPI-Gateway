from rest_framework import serializers

from .models import BackendService, Route, RouteMethod
from .destinations import validate_target_url


class BackendServiceSerializer(serializers.ModelSerializer):
    slug = serializers.SlugField(max_length=120, required=False, allow_blank=True)

    class Meta:
        model = BackendService
        fields = ('id', 'project', 'name', 'slug', 'description', 'is_active', 'created_at')
        read_only_fields = ('id', 'project', 'created_at')


class RouteMethodSerializer(serializers.ModelSerializer):
    class Meta:
        model = RouteMethod
        fields = ('id', 'route', 'method', 'auth_policy')
        read_only_fields = ('id',)


class RouteSerializer(serializers.ModelSerializer):
    target_url = serializers.CharField(
        max_length=500, required=False, allow_blank=True, validators=[validate_target_url]
    )
    backend_service_detail = BackendServiceSerializer(source='backend_service', read_only=True)
    backend_service_name = serializers.CharField(write_only=True, required=False)
    backend_service_slug = serializers.SlugField(write_only=True, required=False)
    downstream_service = serializers.SerializerMethodField()
    method_policies = RouteMethodSerializer(many=True, read_only=True)

    class Meta:
        model = Route
        fields = (
            'id', 'name', 'backend_service', 'backend_service_detail',
            'backend_service_name', 'backend_service_slug', 'downstream_service',
            'base_path', 'target_url', 'is_active', 'method_policies', 'created_at',
        )
        read_only_fields = ('id', 'created_at', 'downstream_service', 'method_policies')

    def validate_base_path(self, value):
        if not value.startswith('/') or '//' in value or '?' in value or '#' in value:
            raise serializers.ValidationError('Use a clean absolute API prefix such as /doctors.')
        if value != '/' and value.endswith('/'):
            raise serializers.ValidationError('Do not include a trailing slash.')
        if '\\' in value or any(ord(char) < 33 or ord(char) == 127 for char in value):
            raise serializers.ValidationError('Invalid path characters.')
        if any(part in {'.', '..'} for part in value.split('/')):
            raise serializers.ValidationError('Dot segments are not allowed.')
        return value

    def create(self, validated_data):
        validated_data.pop('backend_service_name', None)
        validated_data.pop('backend_service_slug', None)
        return super().create(validated_data)

    def get_downstream_service(self, obj):
        return obj.backend_service.slug if obj.backend_service else ''
