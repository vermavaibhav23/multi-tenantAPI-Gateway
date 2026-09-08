import time

from django.http import HttpResponse, JsonResponse
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError
from rest_framework.views import APIView

from routes.models import Route

from .services import GatewayLogger, GatewayRequestProxy, ProductResponseCache, RedisRateLimiter, RouteResolver


GATEWAY_CACHE_TTL_SECONDS = 60


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
        if route.auth_policy == Route.AUTH_API_KEY_AND_JWT:
            try:
                auth_result = JWTAuthentication().authenticate(request)
            except (InvalidToken, TokenError):
                return JsonResponse({'detail': 'Invalid customer token.'}, status=401)
            if auth_result is None:
                return JsonResponse({'detail': 'Missing customer bearer token.'}, status=401)
            request.user, request.auth = auth_result

        application = getattr(request, 'application', None)
        if not application:
            return JsonResponse({'detail': 'API key is not linked to an active application.'}, status=401)
        limit, window_seconds = application.resolved_rate_limit()
        rate_key = f'gateway_rate:api_key:{request.api_key.id}'
        if not RedisRateLimiter().allow_request(rate_key, limit, window_seconds):
            return JsonResponse({'error': 'Rate limit exceeded'}, status=429)

        cache_client = ProductResponseCache()
        target_url = GatewayRequestProxy.build_target_url(route, route_path)
        cache_key = None
        project_cache_prefix = f'gateway_cache:{project.id}:'
        if request.method == 'GET':
            customer_id = request.user.id if getattr(request, 'user', None) and request.user.is_authenticated else 'anonymous'
            query_string = request.META.get('QUERY_STRING', '')
            cache_target_url = f'{target_url}?{query_string}' if query_string else target_url
            cache_key = f'gateway_cache:{application.id}:{customer_id}:{request.method}:{cache_target_url}'
            cached = cache_client.get(cache_key)
            if cached is not None:
                GatewayLogger.log_request(project, request.api_key, route, route.path, request, 200, 0.0)
                return HttpResponse(cached, status=200, content_type='application/json')
        elif request.method in {'POST', 'PUT', 'PATCH', 'DELETE'}:
            cache_client.invalidate_prefix(project_cache_prefix)

        if not route.target_url:
            return JsonResponse({'detail': 'No healthy backends for this route.'}, status=503)

        started = time.time()
        response = GatewayRequestProxy.proxy(request, target_url)
        elapsed_ms = (time.time() - started) * 1000

        if response is None:
            GatewayLogger.log_request(project, request.api_key, route, route.path, request, 503, elapsed_ms)
            return JsonResponse({'detail': 'Backend unavailable.'}, status=503)

        content_type = response.headers.get('content-type', 'application/json')
        GatewayLogger.log_request(project, request.api_key, route, route.path, request, response.status_code, elapsed_ms)
        if cache_key and response.status_code < 400 and 'application/json' in content_type:
            cache_client.set(cache_key, response.text, GATEWAY_CACHE_TTL_SECONDS)
        return HttpResponse(response.content, status=response.status_code, content_type=content_type)
