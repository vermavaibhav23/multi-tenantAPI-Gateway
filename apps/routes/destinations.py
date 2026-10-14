"""Validate destinations against origins approved by the gateway operator."""
import ipaddress
import re
from urllib.parse import urlsplit

from django.conf import settings
from django.core.exceptions import ValidationError


def _origin(value):
    if '\\' in value or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError('Invalid URL characters')
    parsed = urlsplit(value)
    if (parsed.scheme not in {'http', 'https'} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None or parsed.fragment):
        raise ValueError('Invalid backend URL')
    host = parsed.hostname.lower()
    if ':' in host:
        ipaddress.IPv6Address(host)
    elif not re.fullmatch(r'[a-z0-9]+(?:[a-z0-9.-]*[a-z0-9])?', host):
        raise ValueError('Invalid hostname')
    port = parsed.port
    return parsed.scheme, host, port if port is not None else (443 if parsed.scheme == 'https' else 80)


def validate_target_url(value):
    if not value:
        return value  # Unconfigured routes retain their existing 503 behavior.
    try:
        origin = _origin(value)
        allowed = {_origin(item) for item in settings.GATEWAY_ALLOWED_BACKEND_ORIGINS}
        if origin not in allowed:
            raise ValueError('Origin not approved')
    except ValueError as exc:
        raise ValidationError('Backend destination must use an operator-approved HTTP(S) origin.') from exc
    return value
