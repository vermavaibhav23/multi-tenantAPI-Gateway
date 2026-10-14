from rest_framework import serializers

from .models import BackendService, Route
from .destinations import validate_target_url


class BackendServiceSerializer(serializers.ModelSerializer):
    slug = serializers.SlugField(max_length=120, required=False, allow_blank=True)

    class Meta:
        model = BackendService
        fields = ('id', 'project', 'name', 'slug', 'description', 'is_active', 'created_at')
        read_only_fields = ('id', 'project', 'created_at')


class RouteSerializer(serializers.ModelSerializer):
    # Accept Docker DNS names; validate the full origin using operator configuration.
    target_url = serializers.CharField(max_length=500, required=False, allow_blank=True,
                                       validators=[validate_target_url])
    backend_service_detail = BackendServiceSerializer(source='backend_service', read_only=True)
    backend_service_name = serializers.CharField(write_only=True, required=False)
    backend_service_slug = serializers.SlugField(write_only=True, required=False)
    downstream_service = serializers.SerializerMethodField()

    class Meta:
        model = Route
        fields = (
            'id',
            'name',
            'backend_service',
            'backend_service_detail',
            'backend_service_name',
            'backend_service_slug',
            'downstream_service',
            'method',
            'path',
            'target_url',
            'auth_policy',
            'is_active',
            'created_at',
        )
        read_only_fields = ('id', 'created_at', 'downstream_service')

    def create(self, validated_data):
        validated_data.pop('backend_service_name', None)
        validated_data.pop('backend_service_slug', None)
        return super().create(validated_data)

    def get_downstream_service(self, obj):
        return obj.backend_service.slug if obj.backend_service else ''
