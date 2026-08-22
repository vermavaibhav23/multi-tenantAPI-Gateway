from rest_framework import serializers

from .models import BackendService, Route


class BackendServiceSerializer(serializers.ModelSerializer):
    class Meta:
        model = BackendService
        fields = ('id', 'project', 'name', 'slug', 'description', 'is_active', 'created_at')
        read_only_fields = ('id', 'project', 'created_at')


class RouteSerializer(serializers.ModelSerializer):
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
            'is_active',
            'cache_ttl_seconds',
            'created_at',
        )
        read_only_fields = ('id', 'created_at', 'downstream_service')

    def get_downstream_service(self, obj):
        return obj.backend_service.slug if obj.backend_service else ''
