from django.http import JsonResponse
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError

from authentication.models import APIKey


class GatewayAuthenticationMiddleware:
    PORTAL_PREFIXES = (
        '/api/auth/',
        '/api/projects',
        '/api/applications',
        '/api/keys',
        '/api/routes',
        '/api/dashboard',
        '/api/analytics',
    )

    def __init__(self, get_response):
        self.get_response = get_response

    def _is_gateway_request(self, request):
        if request.path.startswith('/gateway/'):
            return True
        if request.path.startswith('/api/') and not request.path.startswith(self.PORTAL_PREFIXES):
            return True
        return False

    def __call__(self, request):
        if self._is_gateway_request(request):
            api_key_value = request.headers.get('X-API-Key')
            if not api_key_value:
                return JsonResponse({'detail': 'Missing X-API-Key header.'}, status=401)
            api_key = APIKey.validate_key(api_key_value)
            if not api_key:
                return JsonResponse({'detail': 'Invalid or expired API key.'}, status=401)
            request.project = api_key.project
            request.api_key = api_key
            request.application = api_key.application

            if request.path.startswith('/api/'):
                try:
                    auth_result = JWTAuthentication().authenticate(request)
                except (InvalidToken, TokenError):
                    return JsonResponse({'detail': 'Invalid customer token.'}, status=401)
                if auth_result is None:
                    return JsonResponse({'detail': 'Missing customer bearer token.'}, status=401)
                request.user, request.auth = auth_result
        return self.get_response(request)
