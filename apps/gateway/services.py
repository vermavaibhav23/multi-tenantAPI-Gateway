import base64
import json
import threading
import time

import redis
from django.conf import settings
from django.utils import timezone

from .route_cache import get_route, route_from_snapshot

from .redis_client import get_redis_client


class RateLimitBackendUnavailable(Exception):
    """The shared Redis rate limiter cannot enforce the global limit."""


class RedisRateLimiter:
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
        # Fail closed when the cluster cannot be reached/discovered.
        try:
            self.client = get_redis_client(redis_url)
        except (redis.RedisError, OSError) as exc:
            raise RateLimitBackendUnavailable('Redis rate limiter unavailable') from exc

    def allow_request(self, key, limit, window_seconds):
        # All gateway replicas use the same aligned time window and Redis key.
        now = int(time.time())
        window_key = f'{key}:{now // window_seconds}'
        ttl_seconds = window_seconds - (now % window_seconds)
        try:
            allowed = self.client.eval(self._FIXED_WINDOW_SCRIPT, 1, window_key, limit, ttl_seconds)
            return int(allowed) == 1
        except (redis.RedisError, OSError) as exc:
            # Do NOT bypass globally enforced limits with replica-local counters.
            raise RateLimitBackendUnavailable('Redis rate limiter unavailable') from exc


class RouteResolver:
    @staticmethod
    def normalize_path(path):
        return '/' + path.strip('/') if path else '/'

    @staticmethod
    def get_route_for_request(project, method, path):
        normalized = RouteResolver.normalize_path(path)
        # Only one-segment route base paths are supported. /doctors/456 -> /doctors.
        base_path = '/' + normalized.strip('/').split('/', 1)[0]
        if base_path == '/':
            return None
        entry = get_route(project.id, base_path)
        return route_from_snapshot(project.id, entry) if entry is not None else None


class GatewayRequestPreview:
    """Build the exact planned destination and safe-to-display request metadata."""

    @staticmethod
    def build_target_url(route, route_path):
        normalized_route = RouteResolver.normalize_path(route.base_path)
        normalized_request = RouteResolver.normalize_path(route_path)
        if normalized_request != normalized_route and not normalized_request.startswith(f'{normalized_route}/'):
            raise ValueError('Request path does not match the configured public route.')
        return route.backend_service.base_url.rstrip('/') + normalized_request

    @staticmethod
    def build_payload(request, project, application, route, target_url, auth_policy):
        # Preserve the original query-string encoding and parameter ordering.
        query_string = request.META.get('QUERY_STRING', '')
        full_target_url = f'{target_url}?{query_string}' if query_string else target_url
        payload = {
            'status': 'validated',
            'mode': 'preview',
            'forwarded': False,
            'target_url': full_target_url,
            'method': request.method,
            'path': RouteResolver.normalize_path(request.path.removeprefix('/gateway')),
            'query_params': {key: request.GET.getlist(key) for key in request.GET},
            'application_id': application.pk,
            'project_id': project.pk,
            'route_id': route.pk,
            'backend_service_id': route.backend_service_id,
            'auth_policy': auth_policy,
            'client_user_id': getattr(getattr(request, 'client_user', None), 'pk', None),
        }
        body = request.body
        if body:
            content_type = request.content_type or ''
            payload['content_type'] = content_type
            if content_type == 'application/json' or content_type.endswith('+json'):
                try:
                    payload['body'] = json.loads(body)
                    payload['body_encoding'] = 'json'
                    return payload
                except (UnicodeDecodeError, ValueError):
                    pass
            try:
                payload['body'] = body.decode('utf-8')
                payload['body_encoding'] = 'utf-8'
            except UnicodeDecodeError:
                payload['body'] = base64.b64encode(body).decode('ascii')
                payload['body_encoding'] = 'base64'
        # Never echo API keys, bearer tokens, cookies, or arbitrary headers.
        # These responses contain user-supplied bodies, so never cache them.
        return payload


class ProductResponseCache:
    _memory_store = {}
    _lock = threading.Lock()

    def __init__(self, redis_url=None):
        # Redis Cluster needs topology discovery only once per worker.
        # If Redis is initially unavailable, preserve the existing local fallback.
        try:
            self.client = get_redis_client(redis_url)
        except (redis.RedisError, OSError):
            self.client = None

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
        # A Redis Cluster SCAN on one node does not cover other primary shards.
        # Scan each primary explicitly and route DELETE through the cluster client.
        try:
            if self.client is None:
                raise redis.ConnectionError('Redis unavailable')
            if settings.REDIS_CLUSTER_ENABLED:
                for node in self.client.get_primaries():
                    for key in node.redis_connection.scan_iter(match=f'{prefix}*', count=100):
                        self.client.delete(key)
            else:
                for key in self.client.scan_iter(match=f'{prefix}*', count=100):
                    self.client.delete(key)
        except Exception:
            # Local cache invalidation is best-effort; it is not distributed.
            pass
        finally:
            with self._lock:
                for key in list(self._memory_store):
                    if key.startswith(prefix):
                        self._memory_store.pop(key, None)
