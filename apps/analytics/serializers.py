from rest_framework import serializers


class AnalyticsSummarySerializer(serializers.Serializer):
    total_requests = serializers.IntegerField()
    average_latency = serializers.FloatField()
    error_rate = serializers.FloatField()
    success_percentage = serializers.FloatField()
    failure_percentage = serializers.FloatField()
    services = serializers.ListField(child=serializers.DictField())
    most_used_endpoints = serializers.ListField(child=serializers.DictField())
