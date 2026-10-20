import re

from rest_framework import serializers

from .models import BackendService, Route, HTTP_METHODS
from .destinations import validate_target_url


class BackendServiceSerializer(serializers.ModelSerializer):
    slug = serializers.SlugField(max_length=120, required=False, allow_blank=True)
    target_url = serializers.CharField(max_length=500, required=False, allow_blank=True,
                                       validators=[validate_target_url])

    class Meta:
        model = BackendService
        fields = ('id', 'project', 'name', 'slug', 'description', 'base_path',
                  'target_url', 'is_active', 'created_at')
        read_only_fields = ('id', 'project', 'created_at')

    def validate_base_path(self, value):
        if not re.fullmatch(r'/[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*', value):
            raise serializers.ValidationError('Use a public base path such as /doctors (without a trailing slash).')
        return value


class RouteSerializer(serializers.ModelSerializer):
    backend_service_detail = BackendServiceSerializer(source='backend_service', read_only=True)
    downstream_service = serializers.SerializerMethodField()
    allowed_methods = serializers.ListField(child=serializers.ChoiceField(choices=HTTP_METHODS),
                                             required=False, allow_empty=False)

    class Meta:
        model = Route
        fields = ('id', 'project', 'name', 'backend_service', 'backend_service_detail',
                  'downstream_service', 'allowed_methods', 'auth_policy', 'is_active', 'created_at')
        read_only_fields = ('id', 'project', 'created_at', 'downstream_service')

    def validate_allowed_methods(self, value):
        if len(value) != len(set(value)):
            raise serializers.ValidationError('Each HTTP method must appear only once.')
        return value

    def get_downstream_service(self, obj):
        return obj.backend_service.slug
