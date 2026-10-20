import hashlib

from django.db import IntegrityError, transaction
from rest_framework import generics, permissions, status
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from gateway.services import RedisRateLimiter
from .models import ApplicationUser
from .serializers import (
    ApplicationUserCreateSerializer, ApplicationUserSerializer,
    ClientLoginSerializer, ClientRefreshSerializer,
)
from .services import (
    ClientTokenError, _make_token, authenticate_client_bearer,
    issue_token_pair, verify_client_token,
)
from django.conf import settings


class ApplicationUserListCreateView(generics.ListCreateAPIView):
    permission_classes = [permissions.IsAdminUser]

    def get_queryset(self):
        return ApplicationUser.objects.filter(
            application__project__owner=self.request.user
        ).order_by('application_id', 'id')

    def get_serializer_class(self):
        return ApplicationUserCreateSerializer if self.request.method == 'POST' else ApplicationUserSerializer

    @transaction.atomic
    def perform_create(self, serializer):
        app = serializer.validated_data['application']
        email = serializer.validated_data['email']
        if ApplicationUser.objects.filter(application=app, email=email).exists():
            raise ValidationError({'email': 'This email already exists in the application.'})
        serializer.save()


class ApplicationUserDisableView(APIView):
    permission_classes = [permissions.IsAdminUser]

    def post(self, request, user_id):
        with transaction.atomic():
            user = ApplicationUser.objects.select_for_update().filter(
                pk=user_id, application__project__owner=request.user
            ).first()
            if not user:
                return Response({'detail': 'Application user not found.'}, status=404)
            if user.is_active:
                user.is_active = False
                user.token_version += 1
                user.save(update_fields=['is_active', 'token_version', 'updated_at'])
        return Response({'id': user.pk, 'is_active': False})


class ClientLoginView(APIView):
    # API-key authentication happens in GatewayAuthenticationMiddleware.
    authentication_classes = []
    permission_classes = []

    def post(self, request):
        application = getattr(request, 'application', None)
        if application is None:
            return Response({'detail': 'A valid application API key is required.'}, status=401)
        serializer = ClientLoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data['email']
        # Cap attempts for each application and login identity. Prevents basic
        # password guessing without revealing whether the email exists.
        hashed_email = hashlib.sha256(email.encode('utf-8')).hexdigest()
        key = f'client_login:{application.pk}:{hashed_email}'
        if not RedisRateLimiter().allow_request(key, 10, 300):
            return Response({'detail': 'Too many login attempts.'}, status=429)
        user = ApplicationUser.objects.filter(
            application=application, email=email, is_active=True
        ).first()
        if user is None or not user.check_password(serializer.validated_data['password']):
            return Response({'detail': 'Invalid email or password.'}, status=401)
        return Response(issue_token_pair(user), status=200)


class ClientRefreshView(APIView):
    authentication_classes = []
    permission_classes = []

    def post(self, request):
        application = getattr(request, 'application', None)
        if application is None:
            return Response({'detail': 'A valid application API key is required.'}, status=401)
        serializer = ClientRefreshSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            user = verify_client_token(
                serializer.validated_data['refresh'], application, token_type='refresh'
            )
        except ClientTokenError:
            return Response({'detail': 'Invalid or expired refresh token.'}, status=401)
        from .services import _make_token
        return Response({
            'access': _make_token(user, 'access', settings.CLIENT_ACCESS_TOKEN_LIFETIME),
            'token_type': 'Bearer',
            'expires_in': int(settings.CLIENT_ACCESS_TOKEN_LIFETIME.total_seconds()),
        })
