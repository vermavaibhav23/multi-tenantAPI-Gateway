import threading
import time

import httpx
import redis
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from analytics.models import RequestLog
from routes.models import Route


class RedisRateLimiter:
    _memory_store = {}
    _lock = threading.Lock()
    _TOKEN_BUCKET_SCRIPT = """
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local window_seconds = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local refill_rate = capacity / window_seconds

local bucket = redis.call('HMGET', key, 'tokens', 'updated_at')
local tokens = tonumber(bucket[1])
local updated_at = tonumber(bucket[2])

if tokens == nil or updated_at == nil then
    tokens = capacity
    updated_at = now
else
    local elapsed = math.max(0, now - updated_at)
    tokens = math.min(capacity, tokens + (elapsed * refill_rate))
    updated_at = now
end

local allowed = 0
if tokens >= 1 then
    tokens = tokens - 1
    allowed = 1
end

redis.call('HSET', key, 'tokens', tokens, 'updated_at', updated_at)
redis.call('EXPIRE', key, math.ceil(window_seconds * 2))
return allowed
"""

    def __init__(self, redis_url=None):
        self.redis_url = redis_url or settings.REDIS_URL
        self.client = redis.Redis.from_url(self.redis_url, decode_responses=True)

    def _fallback_allow_request(self, key, limit, window_seconds):
        now = time.time()
        with self._lock:
            tokens, updated_at = self._memory_store.get(key, (float(limit), now))
            refill_rate = float(limit) / float(window_seconds)
            elapsed = max(0, now - updated_at)
            tokens = min(float(limit), tokens + (elapsed * refill_rate))
            if tokens < 1:
                self._memory_store[key] = (tokens, now)
                return False
            self._memory_store[key] = (tokens - 1, now)
            return True

    def allow_request(self, key, limit, window_seconds):
        try:
            self.client.ping()
        except Exception:
            return self._fallback_allow_request(key, limit, window_seconds)
        allowed = self.client.eval(self._TOKEN_BUCKET_SCRIPT, 1, key, limit, window_seconds, time.time())
        return int(allowed) == 1


class RouteResolver:
    @staticmethod
    def normalize_path(path):
        return '/' + path.strip('/') if path else '/'

    @staticmethod
    def get_route_for_request(project, method, path):
        normalized = RouteResolver.normalize_path(path)
        cache_key = f'route:{project.id}:{method}:{normalized}'
        route = cache.get(cache_key)
        if route is not None:
            return route
        routes = Route.objects.filter(project=project, method=method, is_active=True).order_by('-path')
        for route in routes:
            route_path = RouteResolver.normalize_path(route.path)
            if route_path == normalized or normalized.startswith(f'{route_path}/'):
                cache.set(cache_key, route, timeout=300)
                return route
        return None


class GatewayRequestProxy:
    @staticmethod
    def build_target_url(route, route_path):
        normalized_route = RouteResolver.normalize_path(route.path)
        normalized_request = RouteResolver.normalize_path(route_path)
        suffix = normalized_request[len(normalized_route):] if normalized_request.startswith(normalized_route) else ''
        return route.target_url.rstrip('/') + suffix

    @staticmethod
    def proxy(request, target_url):
        headers = {k: v for k, v in request.headers.items() if k.lower() not in {'host', 'content-length', 'connection'}}
        try:
            with httpx.Client(timeout=10.0) as client:
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
        )


class ProductResponseCache:
    _memory_store = {}
    _lock = threading.Lock()

    def __init__(self, redis_url=None):
        self.redis_url = redis_url or settings.REDIS_URL
        self.client = redis.Redis.from_url(self.redis_url, decode_responses=True)

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
