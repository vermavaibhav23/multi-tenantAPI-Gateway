"""Shared Redis Cluster cache for project routes (not API keys or app permissions).

Cache a project route snapshot once, then match longest prefix in Python.
Explicit JSON prevents unpickling arbitrary data. Two keys share a Redis
Cluster hash slot so scripts can atomically publish/invalidate snapshots.
"""
import json
import logging

import redis
from django.conf import settings
from django.db.models import Q

from routes.models import BackendService, Route

from .redis_client import get_redis_client


logger = logging.getLogger(__name__)
ROUTE_CACHE_TTL_SECONDS = 30

# Abort stale cache fills if route configuration changed during DB loading.
_PUBLISH_IF_CURRENT = """
local generation = redis.call('GET', KEYS[1]) or '0'
if generation ~= ARGV[1] then return 0 end
redis.call('SET', KEYS[2], ARGV[2], 'EX', tonumber(ARGV[3]))
return 1
"""

# Bump generation + delete the cached snapshot as one Redis operation.
_INVALIDATE = """
redis.call('INCR', KEYS[1])
redis.call('DEL', KEYS[2])
return 1
"""


def _keys(project_id):
    tag = '{servlink-routes-' + str(project_id) + '}'
    return f'routecfg:{tag}:version', f'routecfg:{tag}:data'


def _read_project_routes(project_id):
    rows = Route.objects.select_related('backend_service').prefetch_related(
        'method_policies'
    ).filter(
        Q(backend_service__isnull=True) |
        Q(backend_service__project_id=project_id, backend_service__is_active=True),
        project_id=project_id, is_active=True,
    )
    result = []
    for route in rows:
        service = route.backend_service
        result.append({
            'id': route.id,
            'path': route.base_path,
            'service': None if service is None else {
                'id': service.id,
                'base_url': service.base_url,
            },
            'methods': {policy.method: policy.auth_policy for policy in route.method_policies.all()},
        })
    # Longest full-segment prefix must win, regardless of method policy.
    return sorted(result, key=lambda entry: len(entry['path'].strip('/')), reverse=True)


def get_project_routes(project_id):
    """Use cached route snapshot, falling back to primary MySQL on Redis errors."""
    try:
        client = get_redis_client()
        version_key, data_key = _keys(project_id)
        for _ in range(3):
            cached = client.get(data_key)
            if cached is not None:
                return json.loads(cached)
            generation = client.get(version_key) or '0'
            fresh = _read_project_routes(project_id)
            saved = client.eval(
                _PUBLISH_IF_CURRENT, 2, version_key, data_key,
                generation, json.dumps(fresh), ROUTE_CACHE_TTL_SECONDS,
            )
            if int(saved) == 1:
                return fresh
        # Concurrent config changes: do not serve an older cached snapshot.
        return _read_project_routes(project_id)
    except (redis.RedisError, OSError, ValueError, TypeError):
        return _read_project_routes(project_id)


def invalidate_project_routes(project_id):
    """Invalidate following a committed route/service/method change.

    For bulk QuerySet.update()/raw SQL (which bypass Django model signals),
    callers must explicitly invalidate affected projects after commit.
    """
    if project_id is None:
        return
    try:
        version_key, data_key = _keys(project_id)
        get_redis_client().eval(_INVALIDATE, 2, version_key, data_key)
    except (redis.RedisError, OSError):
        # A short TTL bounds stale configuration if an invalidation cannot
        # reach Redis. Don't make an already committed DB change error.
        logger.warning('Failed to invalidate route cache for project %s', project_id)


def route_from_snapshot(project_id, entry):
    """Hydrate lightweight Django model instances for permission FK comparisons.

    Models are never saved. API key and RoutePermission queries still hit MySQL.
    """
    service_data = entry['service']
    service = (None if service_data is None else BackendService(
        id=service_data['id'],
        project_id=project_id,
        base_url=service_data['base_url'],
        is_active=True,
    ))
    route = Route(
        id=entry['id'],
        project_id=project_id,
        base_path=entry['path'],
        backend_service=service,
        is_active=True,
    )
    route._cached_method_policies = entry['methods']
    return route
