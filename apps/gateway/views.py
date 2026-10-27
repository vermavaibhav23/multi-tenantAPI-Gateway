import json
from urllib.parse import urlsplit

from django.core.exceptions import ValidationError
from django.http import JsonResponse
from client_auth.services import ClientTokenError, authenticate_client_bearer
from rest_framework.views import APIView

from routes.destinations import validate_backend_base_url, validate_target_url
from routes.models import RouteMethod

from .services import GatewayRequestPreview, RedisRateLimiter, RateLimitBackendUnavailable, RouteResolver


class GatewayProxyView(APIView):
    """Validate gateway requests, then describe the destination without forwarding."""

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

        def reject(detail, status_code, error_key='detail'):
            return JsonResponse({error_key: detail}, status=status_code)

        route = RouteResolver.get_route_for_request(project, request.method, route_path)
        if not route:
            return reject('Route not found.', 404)
        # Method and JWT policies are read from MySQL, never from the route cache.
        method_policy = RouteMethod.objects.filter(
            route_id=route.pk, method=request.method
        ).values_list('auth_policy', flat=True).first()
        if method_policy is None:
            return reject('Method not allowed for this route.', 405)
        if not request.api_key.can_access(route):
            return reject('Application is not allowed to access this route.', 403)
        if method_policy == RouteMethod.AUTH_API_KEY_AND_JWT:
            try:
                request.client_user = authenticate_client_bearer(
                    request.headers.get('Authorization'), request.application
                )
            except ClientTokenError:
                return reject('Invalid or missing application-user token.', 401)

        application = getattr(request, 'application', None)
        if not application:
            return reject('API key is not linked to an active application.', 401)
        limit, window_seconds = application.resolved_rate_limit()
        rate_key = f'gateway_rate:application:{application.id}'
        try:
            allowed = RedisRateLimiter().allow_request(rate_key, limit, window_seconds)
        except RateLimitBackendUnavailable:
            return reject('Rate limiting temporarily unavailable. Please retry later.', 503)
        if not allowed:
            return reject('Rate limit exceeded', 429, error_key='error')

        # Service rows remain as virtual destination metadata. No HTTP client
        # is created and no destination is contacted in preview mode.
        if not route.backend_service or not route.backend_service.base_url:
            return reject('No destination configured for this route.', 503)
        try:
            validate_backend_base_url(route.backend_service.base_url)
            target_url = GatewayRequestPreview.build_target_url(route, route_path)
            validate_target_url(target_url)
        except (ValidationError, ValueError):
            return reject('Backend destination is not allowed.', 403)

        return JsonResponse(
            GatewayRequestPreview.build_payload(
                request=request,
                project=project,
                application=application,
                route=route,
                target_url=target_url,
                auth_policy=method_policy,
            ),
            status=200,
        )
