import time

from django.http import HttpResponse, JsonResponse
from rest_framework.views import APIView

from .services import GatewayLogger, GatewayRequestProxy, ProductResponseCache, RedisRateLimiter, RouteResolver


class GatewayProxyView(APIView):
    authentication_classes = []
    permission_classes = []

    def get(self, request, route_path):
        return self.forward(request, route_path)

    def post(self, request, route_path):
        return self.forward(request, route_path)

    def put(self, request, route_path):
        return self.forward(request, route_path)

    def patch(self, request, route_path):
        return self.forward(request, route_path)

    def delete(self, request, route_path):
        return self.forward(request, route_path)

    def forward(self, request, route_path):
        project = getattr(request, 'project', None)
        if not project:
            return JsonResponse({'detail': 'No project associated with the API key.'}, status=401)

        route = RouteResolver.get_route_for_request(project, request.method, route_path)
        if not route:
            return JsonResponse({'detail': 'Route not found.'}, status=404)
        if not request.api_key.can_access(route):
            return JsonResponse({'detail': 'Application is not allowed to access this route.'}, status=403)

        application = getattr(request, 'application', None)
        if application:
            limit, window_seconds = application.resolved_rate_limit()
        else:
            limit, window_seconds = project.rate_limit, project.window_seconds
        rate_key = f'gateway_rate:api_key:{request.api_key.id}'
        if not RedisRateLimiter().allow_request(rate_key, limit, window_seconds):
            return JsonResponse({'error': 'Rate limit exceeded'}, status=429)

        cache_client = ProductResponseCache()
        cache_key = None
        service_slug = route.backend_service.slug if route.backend_service else 'unassigned-service'
        service_cache_prefix = f'service_cache:{project.id}:{service_slug}:'
        if request.method == 'GET' and route.cache_ttl_seconds > 0:
            cache_key = f'{service_cache_prefix}{request.get_full_path()}'
            cached = cache_client.get(cache_key)
            if cached is not None:
                GatewayLogger.log_request(project, request.api_key, route, route.path, request, 200, 0.0)
                return HttpResponse(cached, status=200, content_type='application/json')
        elif request.method in {'POST', 'PUT', 'PATCH', 'DELETE'}:
            cache_client.invalidate_prefix(service_cache_prefix)

        if not route.target_url:
            return JsonResponse({'detail': 'No healthy backends for this route.'}, status=503)

        started = time.time()
        target_url = GatewayRequestProxy.build_target_url(route, route_path)
        response = GatewayRequestProxy.proxy(request, target_url)
        elapsed_ms = (time.time() - started) * 1000

        if response is None:
            GatewayLogger.log_request(project, request.api_key, route, route.path, request, 503, elapsed_ms)
            return JsonResponse({'detail': 'Backend unavailable.'}, status=503)

        content_type = response.headers.get('content-type', 'application/json')
        GatewayLogger.log_request(project, request.api_key, route, route.path, request, response.status_code, elapsed_ms)
        if cache_key and response.status_code < 400 and 'application/json' in content_type:
            cache_client.set(cache_key, response.text, route.cache_ttl_seconds)
        return HttpResponse(response.content, status=response.status_code, content_type=content_type)
