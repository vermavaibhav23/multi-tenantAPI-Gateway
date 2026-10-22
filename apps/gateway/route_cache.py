"""Per-route Redis cache. Method policies and permissions remain in MySQL.

Public routes use a single-segment base path, e.g. /doctors. Each project +
base path has a distinct Redis key. A per-route generation key prevents a
concurrent stale MySQL read from repopulating a just-invalidated route.
Both Lua keys share a Redis Cluster hash slot.
"""
import json
import logging

import redis
from django.db.models import Q

from routes.models import BackendService, Route
from .redis_client import get_redis_client


logger = logging.getLogger(__name__)
ROUTE_CACHE_TTL_SECONDS = 60

_PUBLISH_IF_CURRENT = """
local generation = redis.call('GET', KEYS[1]) or '0'
if generation ~= ARGV[1] then return 0 end
redis.call('SET', KEYS[2], ARGV[2], 'EX', tonumber(ARGV[3]))
return 1
"""

_INVALIDATE = """
redis.call('INCR', KEYS[1])
redis.call('DEL', KEYS[2])
return 1
"""


def _keys(project_id, base_path):
    # {project_id} pins both script keys to the same Redis Cluster slot.
    suffix = base_path.lstrip('/')
    if not suffix or '/' in suffix:
        raise ValueError('Route base path must have exactly one segment.')
    return (
        f'routeversion:{{{project_id}}}:{suffix}',
        f'route:{{{project_id}}}:{suffix}',
    )


def _read_route(project_id, base_path):
    route = Route.objects.select_related('backend_service').filter(
        Q(backend_service__isnull=True) |
        Q(backend_service__project_id=project_id, backend_service__is_active=True),
        project_id=project_id, base_path=base_path, is_active=True,
    ).first()
    if route is None:
        return None
    service = route.backend_service
    return {
        'id': route.pk,
        'path': route.base_path,
        'service': None if service is None else {
            'id': service.pk,
            'base_url': service.base_url,
        },
    }


def get_route(project_id, base_path):
    """Resolve a single route from Redis, falling back to MySQL on cache errors.

    Missing routes are not cached; this keeps new route creation straightforward.
    """
    try:
        version_key, data_key = _keys(project_id, base_path)
        client = get_redis_client()
        for _ in range(3):
            cached = client.get(data_key)
            if cached is not None:
                return json.loads(cached)
            generation = client.get(version_key) or '0'
            route = _read_route(project_id, base_path)
            if route is None:
                return None
            published = client.eval(
                _PUBLISH_IF_CURRENT, 2, version_key, data_key,
                generation, json.dumps(route), ROUTE_CACHE_TTL_SECONDS,
            )
            if int(published) == 1:
                return route
        return _read_route(project_id, base_path)
    except (redis.RedisError, OSError, ValueError, TypeError):
        return _read_route(project_id, base_path)


def invalidate_route(project_id, base_path):
    """Invalidate after a committed route/service configuration change."""
    if project_id is None or not base_path:
        return
    try:
        version_key, data_key = _keys(project_id, base_path)
        get_redis_client().eval(_INVALIDATE, 2, version_key, data_key)
    except (redis.RedisError, OSError, ValueError):
        # A failed invalidation may leave stale routing information until TTL.
        logger.warning('Route-cache invalidation failed for %s:%s', project_id, base_path)


def invalidate_project_routes(project_id):
    """Explicitly invalidate all known routes after bulk updates/raw SQL.

    Normal model save/delete uses more precise per-route invalidation.
    Call this only AFTER transaction commit for database bulk updates.
    """
    if project_id is None:
        return
    paths = Route.objects.filter(project_id=project_id).values_list('base_path', flat=True)
    for path in paths:
        invalidate_route(project_id, path)


def route_from_snapshot(project_id, entry):
    """Create lightweight model objects for existing permission/URL code.

    RoutePermission still uses the real route ID against primary MySQL.
    """
    service_data = entry['service']
    service = (None if service_data is None else BackendService(
        id=service_data['id'],
        project_id=project_id,
        base_url=service_data['base_url'],
        is_active=True,
    ))
    return Route(
        id=entry['id'],
        project_id=project_id,
        base_path=entry['path'],
        backend_service=service,
        is_active=True,
    )
