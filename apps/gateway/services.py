import threading
import time

import httpx
import redis
from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from analytics.models import RequestLog
from routes.destinations import validate_target_url
from routes.models import Route


class RedisRateLimiter:
    _memory_store = {}
    _lock = threading.Lock()
    _FIXED_WINDOW_SCRIPT = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then
    redis.call('EXPIRE', KEYS[1], tonumber(ARGV[2]))
end
if count <= tonumber(ARGV[1]) then
    return 1
end
return 0
"""

    def __init__(self, redis_url=None):
        self.redis_url = redis_url or settings.REDIS_URL
        self.client = redis.Redis.from_url(self.redis_url, decode_responses=True, socket_connect_timeout=0.5, socket_timeout=0.5)

    def _fallback_allow_request(self, key, limit, window_seconds):
        # Local fallback is best-effort only: it is not shared across replicas.
        window_number = int(time.time()) // window_seconds
        with self._lock:
            saved_window, count = self._memory_store.get(key, (None, 0))
            if saved_window != window_number:
                count = 0
            count += 1
            self._memory_store[key] = (window_number, count)
            return count <= limit

    def allow_request(self, key, limit, window_seconds):
        # All gateway replicas use the same aligned time window and Redis key.
        now = int(time.time())
        window_key = f'{key}:{now // window_seconds}'
        ttl_seconds = window_seconds - (now % window_seconds)
        try:
            allowed = self.client.eval(self._FIXED_WINDOW_SCRIPT, 1, window_key, limit, ttl_seconds)
            return int(allowed) == 1
        except redis.RedisError:
            return self._fallback_allow_request(key, limit, window_seconds)

class RouteResolver:
    @staticmethod
    def normalize_path(path):
        return '/' + path.strip('/') if path else '/'

    @staticmethod
    def get_route_for_request(project, method, path):
        normalized = RouteResolver.normalize_path(path)
        # Read current configuration on every request, across all gateway workers.
        routes = Route.objects.select_related('backend_service').filter(
            Q(backend_service__isnull=True)
            | Q(backend_service__project=project, backend_service__is_active=True),
            project=project, is_active=True,
        )
        # Match the longest full-segment prefix, independent of HTTP method.
        # A disallowed method must not fall through to a shorter public prefix.
        for route in sorted(routes, key=lambda item: len(RouteResolver.normalize_path(item.base_path)), reverse=True):
            prefix = RouteResolver.normalize_path(route.base_path)
            if prefix == normalized or normalized.startswith(f'{prefix}/'):
                return route
        return None


class GatewayRequestProxy:
    @staticmethod
    def build_target_url(route, route_path):
        normalized_route = RouteResolver.normalize_path(route.base_path)
        normalized_request = RouteResolver.normalize_path(route_path)
        if normalized_request != normalized_route and not normalized_request.startswith(f'{normalized_route}/'):
            raise ValueError('Request path does not match the configured public route.')
        # route_path is already the requested path after /gateway.
        # Public and backend paths are intentionally the same in ServLink.
        return route.backend_service.base_url.rstrip('/') + normalized_request

    @staticmethod
    def proxy(request, target_url):
        validate_target_url(target_url)
        excluded = {'host', 'content-length', 'connection', 'keep-alive', 'transfer-encoding',
                    'te', 'trailer', 'upgrade', 'proxy-authorization', 'proxy-authenticate',
                    'x-api-key', 'x-tenant-id', 'x-application-id', 'authorization'}
        excluded.update(h.strip().lower() for h in request.headers.get('Connection', '').split(','))
        headers = {k: v for k, v in request.headers.items()
                   if k.lower().replace('_', '-') not in excluded and not k.lower().replace('_', '-').startswith('x-gatescale-')}
        headers['X-GateScale-Application-ID'] = str(request.application.id)
        if getattr(request, 'client_user', None) is not None:
            headers['X-GateScale-Client-User-ID'] = str(request.client_user.id)
        try:
            with httpx.Client(timeout=10.0, follow_redirects=False, trust_env=False) as client:
                response = client.request(
                    method=request.method,
                    url=target_url,
                    headers=headers,
                    content=request.body if request.body else None,
                    params=request.GET,
                )
            return response
        except httpx.TimeoutException:
            return None
        except httpx.RequestError:
            return None


class GatewayLogger:
    @staticmethod
    def log_request(project, api_key, route, endpoint, request, status_code, latency_ms):
        RequestLog.objects.create(
            project=project,
            api_key=api_key,
            route=route,
            endpoint=endpoint,
            method=request.method,
            status_code=status_code,
            latency_ms=latency_ms,
            ip_address=request.META.get('REMOTE_ADDR'),
            downstream_service=route.backend_service.slug if route and route.backend_service else '',
            application=api_key.application if api_key else None,
            customer=request.user if getattr(request, 'user', None) and request.user.is_authenticated else None,
            client_user=getattr(request, 'client_user', None),
        )


class ProductResponseCache:
    _memory_store = {}
    _lock = threading.Lock()

    def __init__(self, redis_url=None):
        self.redis_url = redis_url or settings.REDIS_URL
        self.client = redis.Redis.from_url(self.redis_url, decode_responses=True, socket_connect_timeout=0.5, socket_timeout=0.5)

    def _memory_get(self, key):
        with self._lock:
            item = self._memory_store.get(key)
            if not item:
                return None
            expires_at, value = item
            if expires_at < timezone.now().timestamp():
                self._memory_store.pop(key, None)
                return None
            return value

    def _memory_set(self, key, value, ttl_seconds):
        with self._lock:
            self._memory_store[key] = (timezone.now().timestamp() + ttl_seconds, value)

    def get(self, key):
        try:
            self.client.ping()
            return self.client.get(key)
        except Exception:
            return self._memory_get(key)

    def set(self, key, value, ttl_seconds):
        if ttl_seconds <= 0:
            return
        try:
            self.client.set(key, value, ex=ttl_seconds)
        except Exception:
            self._memory_set(key, value, ttl_seconds)

    def invalidate_prefix(self, prefix):
        try:
            self.client.ping()
            for key in self.client.scan_iter(f'{prefix}*'):
                self.client.delete(key)
        except Exception:
            with self._lock:
                for key in list(self._memory_store):
                    if key.startswith(prefix):
                        self._memory_store.pop(key, None)
