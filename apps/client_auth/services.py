"""Shared client-user token issuer/verifier (distinct from platform staff JWTs).

Client JWTs are HS256 signed with CLIENT_JWT_SIGNING_KEY; platform staff
SimpleJWT tokens MUST NOT be accepted as client-user credentials.
"""
from datetime import timedelta

import jwt
from django.conf import settings
from django.utils import timezone

from .models import ApplicationUser


class ClientTokenError(Exception):
    pass


def _make_token(user, token_type, lifetime):
    now = timezone.now()
    claims = {
        'sub': str(user.pk),
        'application_id': user.application_id,
        'token_version': user.token_version,
        'iss': settings.CLIENT_JWT_ISSUER,
        'aud': settings.CLIENT_JWT_AUDIENCE,
        'token_type': token_type,
        'iat': now,
        'nbf': now,
        'exp': now + lifetime,
    }
    return jwt.encode(claims, settings.CLIENT_JWT_SIGNING_KEY, algorithm='HS256')


def issue_token_pair(user):
    return {
        'access': _make_token(user, 'access', settings.CLIENT_ACCESS_TOKEN_LIFETIME),
        'refresh': _make_token(user, 'refresh', settings.CLIENT_REFRESH_TOKEN_LIFETIME),
        'token_type': 'Bearer',
        'expires_in': int(settings.CLIENT_ACCESS_TOKEN_LIFETIME.total_seconds()),
    }


def verify_client_token(raw_token, application, token_type='access'):
    if not isinstance(raw_token, str) or not raw_token:
        raise ClientTokenError('Missing client token.')
    try:
        claims = jwt.decode(
            raw_token,
            settings.CLIENT_JWT_SIGNING_KEY,
            algorithms=['HS256'],
            issuer=settings.CLIENT_JWT_ISSUER,
            audience=settings.CLIENT_JWT_AUDIENCE,
            options={'require': [
                'sub', 'application_id', 'token_version', 'iss', 'aud',
                'iat', 'nbf', 'exp', 'token_type',
            ]},
        )
    except (jwt.PyJWTError, TypeError, ValueError) as exc:
        raise ClientTokenError('Invalid or expired client token.') from exc

    if claims.get('token_type') != token_type:
        raise ClientTokenError('Incorrect client token type.')

    # Avoid accepting bool or arbitrarily formatted identity claims.
    user_id = claims.get('sub')
    app_id = claims.get('application_id')
    version = claims.get('token_version')
    if (not isinstance(user_id, str) or not user_id.isdecimal()
            or type(app_id) is not int or type(version) is not int
            or app_id != application.pk):
        raise ClientTokenError('Client token application mismatch.')

    user = ApplicationUser.objects.filter(
        pk=int(user_id),
        application=application,
        is_active=True,
        token_version=version,
    ).first()
    if user is None or not application.is_active:
        raise ClientTokenError('Client user is inactive or no longer authorized.')
    return user


def authenticate_client_bearer(authorization, application):
    if not isinstance(authorization, str):
        raise ClientTokenError('Missing customer bearer token.')
    pieces = authorization.strip().split()
    if len(pieces) != 2 or pieces[0].lower() != 'bearer':
        raise ClientTokenError('Missing or malformed customer bearer token.')
    return verify_client_token(pieces[1], application)
