import time
from urllib.parse import urlsplit

from django.conf import settings
from django.core.exceptions import ValidationError
from django.http import HttpResponse, JsonResponse
from client_auth.services import ClientTokenError, authenticate_client_bearer
from rest_framework.views import APIView

from routes.destinations import validate_target_url
from routes.models import RouteMethod

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

        def reject(detail, status_code, route=None, error_key='detail'):
            GatewayLogger.log_request(project, request.api_key, route,
                                      route.base_path if route else route_path[:255], request, status_code, 0.0)
            return JsonResponse({error_key: detail}, status=status_code)

        route = RouteResolver.get_route_for_request(project, request.method, route_path)
        if not route:
            return reject('Route not found.', 404)
        method_policy = RouteMethod.objects.filter(route=route, method=request.method).first()
        if method_policy is None:
            return reject('Method not allowed for this route.', 405, route)
        if not request.api_key.can_access(route):
            return reject('Application is not allowed to access this route.', 403, route)
        if method_policy.auth_policy == RouteMethod.AUTH_API_KEY_AND_JWT:
            try:
                request.client_user = authenticate_client_bearer(
                    request.headers.get('Authorization'), request.application
                )
            except ClientTokenError:
                return reject('Invalid or missing application-user token.', 401, route)

        application = getattr(request, 'application', None)
        if not application:
            return reject('API key is not linked to an active application.', 401, route)
        limit, window_seconds = application.resolved_rate_limit()
        rate_key = f'gateway_rate:application:{application.id}'
        if not RedisRateLimiter().allow_request(rate_key, limit, window_seconds):
            return reject('Rate limit exceeded', 429, route, error_key='error')

        if not route.target_url:
            return reject('No healthy backends for this route.', 503, route)
        cache_client = ProductResponseCache()
        target_url = GatewayRequestProxy.build_target_url(route, route_path)
        try:
            validate_target_url(target_url)
        except ValidationError:
            return reject('Backend destination is not allowed.', 403, route)
        cache_key = None
        project_cache_prefix = f'gateway_cache:v2:{project.id}:'
        cache_allowed = (
            request.method == 'GET' and method_policy.auth_policy == RouteMethod.AUTH_API_KEY_ONLY
            and urlsplit(target_url).hostname in settings.GATEWAY_CACHEABLE_BACKEND_HOSTS
            and not request.headers.get('Authorization') and not request.headers.get('Cookie')
        )
        if cache_allowed:
            customer_id = request.user.id if getattr(request, 'user', None) and request.user.is_authenticated else 'anonymous'
            query_string = request.META.get('QUERY_STRING', '')
            cache_target_url = f'{target_url}?{query_string}' if query_string else target_url
            cache_key = f'{project_cache_prefix}{application.id}:{customer_id}:{request.method}:{cache_target_url}'
            cached = cache_client.get(cache_key)
            if cached is not None:
                GatewayLogger.log_request(project, request.api_key, route, route.base_path, request, 200, 0.0)
                return HttpResponse(cached, status=200, content_type='application/json')
        elif request.method in {'POST', 'PUT', 'PATCH', 'DELETE'}:
            cache_client.invalidate_prefix(project_cache_prefix)

        started = time.time()
        response = GatewayRequestProxy.proxy(request, target_url)
        elapsed_ms = (time.time() - started) * 1000

        if response is None:
            GatewayLogger.log_request(project, request.api_key, route, route.base_path, request, 503, elapsed_ms)
            return JsonResponse({'detail': 'Backend unavailable.'}, status=503)

        content_type = response.headers.get('content-type', 'application/json')
        GatewayLogger.log_request(project, request.api_key, route, route.base_path, request, response.status_code, elapsed_ms)
        cache_control = response.headers.get('cache-control', '').lower()
        if (cache_key and response.status_code == 200 and 'application/json' in content_type
                and not response.headers.get('set-cookie') and not response.headers.get('vary')
                and not any(directive in cache_control for directive in ('no-store', 'no-cache', 'private'))):
            cache_client.set(cache_key, response.text, GATEWAY_CACHE_TTL_SECONDS)
        return HttpResponse(response.content, status=response.status_code, content_type=content_type)
