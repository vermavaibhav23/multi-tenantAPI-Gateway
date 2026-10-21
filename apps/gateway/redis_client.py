"""Shared Redis connection factory for single-node tests and Docker Redis Cluster.

Cluster clients discover slots/nodes at construction; reuse the client across
requests rather than repeating topology discovery for every HTTP request.
"""
from functools import lru_cache

import redis
from django.conf import settings
from redis.cluster import RedisCluster


@lru_cache(maxsize=8)
def _cached_client(url, clustered):
    options = {
        'decode_responses': True,
        'socket_connect_timeout': 0.5,
        'socket_timeout': 0.5,
    }
    if clustered:
        return RedisCluster.from_url(url, **options)
    return redis.Redis.from_url(url, **options)


def get_redis_client(url=None):
    clustered = settings.REDIS_CLUSTER_ENABLED
    address = url or (settings.REDIS_CLUSTER_URL if clustered else settings.REDIS_URL)
    return _cached_client(address, clustered)
