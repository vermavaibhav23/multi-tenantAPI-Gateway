from django.http import JsonResponse

from authentication.models import APIKey


class GatewayAuthenticationMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def _is_gateway_request(self, request):
        return request.path.startswith('/gateway/')

    def __call__(self, request):
        if self._is_gateway_request(request):
            api_key_value = request.headers.get('X-API-Key')
            if not api_key_value:
                return JsonResponse({'detail': 'Missing X-API-Key header.'}, status=401)
            api_key = APIKey.validate_key(api_key_value)
            if not api_key:
                return JsonResponse({'detail': 'Invalid or expired API key.'}, status=401)
            request.api_key = api_key
            request.application = api_key.application
            request.project = api_key.application.project
        return self.get_response(request)
