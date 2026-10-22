"""Unit tests for Redis Cluster wiring, with optional live-cluster smoke tests."""
import os
import time
from unittest import skipUnless
from unittest.mock import MagicMock, call, patch
from uuid import uuid4

import redis
from django.test import SimpleTestCase, override_settings
from rest_framework.test import APITestCase

from gateway.redis_client import _cached_client, get_redis_client
from gateway.services import ProductResponseCache, RedisRateLimiter, RateLimitBackendUnavailable
from gateway.route_cache import (
    _read_route, invalidate_route,
)
from gateway.services import RouteResolver
from routes.models import BackendService, Route, RouteMethod
from users.models import Project, User


class RedisClusterClientTests(SimpleTestCase):
    def setUp(self):
        _cached_client.cache_clear()
        self.addCleanup(_cached_client.cache_clear)
        ProductResponseCache._memory_store.clear()

    @override_settings(REDIS_CLUSTER_ENABLED=True, REDIS_CLUSTER_URL='redis://redis-node-1:6379')
    @patch('gateway.redis_client.RedisCluster.from_url')
    def test_cluster_discovery_reused_across_gateway_components(self, from_url):
        limiter = RedisRateLimiter()
        cache = ProductResponseCache()
        another_limiter = RedisRateLimiter()
        self.assertIs(limiter.client, cache.client)
        self.assertIs(limiter.client, another_limiter.client)
        from_url.assert_called_once()
        self.assertEqual(from_url.call_args.args[0], 'redis://redis-node-1:6379')

    @override_settings(REDIS_CLUSTER_ENABLED=True)
    def test_cache_invalidation_scans_every_primary_shard(self):
        first = MagicMock()
        second = MagicMock()
        third = MagicMock()
        first.redis_connection.scan_iter.return_value = ['gateway_cache:v2:42:a']
        second.redis_connection.scan_iter.return_value = ['gateway_cache:v2:42:b']
        third.redis_connection.scan_iter.return_value = ['gateway_cache:v2:42:c']
        cluster = MagicMock()
        cluster.get_primaries.return_value = [first, second, third]
        with patch('gateway.services.get_redis_client', return_value=cluster):
            cache = ProductResponseCache()
            cache._memory_set('gateway_cache:v2:42:local', 'old', 60)
            cache.invalidate_prefix('gateway_cache:v2:42:')
        self.assertEqual(cluster.delete.call_args_list, [
            call('gateway_cache:v2:42:a'),
            call('gateway_cache:v2:42:b'),
            call('gateway_cache:v2:42:c'),
        ])
        for node in (first, second, third):
            node.redis_connection.scan_iter.assert_called_once_with(
                match='gateway_cache:v2:42:*', count=100
            )
        self.assertNotIn('gateway_cache:v2:42:local', cache._memory_store)

    @override_settings(REDIS_CLUSTER_ENABLED=True)
    def test_fixed_window_uses_one_atomic_key_in_cluster(self):
        cluster = MagicMock()
        cluster.eval.side_effect = [1, 0]
        with patch('gateway.services.get_redis_client', return_value=cluster), patch(
            'gateway.services.time.time', return_value=120,
        ):
            limiter = RedisRateLimiter()
            self.assertTrue(limiter.allow_request('gateway_rate:application:3', 1, 60))
            self.assertFalse(limiter.allow_request('gateway_rate:application:3', 1, 60))
        self.assertEqual(cluster.eval.call_count, 2)
        self.assertEqual(cluster.eval.call_args.args[2], 'gateway_rate:application:3:2')

    @override_settings(REDIS_CLUSTER_ENABLED=True)
    def test_initial_cluster_outage_fails_closed(self):
        with patch('gateway.services.get_redis_client', side_effect=redis.ConnectionError):
            with self.assertRaises(RateLimitBackendUnavailable):
                RedisRateLimiter()

    @override_settings(REDIS_CLUSTER_ENABLED=True)
    def test_primary_unreachable_rejects_instead_of_using_local_counter(self):
        cluster = MagicMock()
        cluster.eval.side_effect = redis.ConnectionError('primary unreachable')
        with patch('gateway.services.get_redis_client', return_value=cluster):
            limiter = RedisRateLimiter()
            with self.assertRaises(RateLimitBackendUnavailable):
                limiter.allow_request('gateway_rate:application:12', 100, 60)


@skipUnless(os.getenv('GATESCALE_TEST_REDIS_CLUSTER_URL'),
            'Set GATESCALE_TEST_REDIS_CLUSTER_URL to run tests against six-node Redis Cluster.')
class LiveRedisClusterTests(SimpleTestCase):
    @override_settings(REDIS_CLUSTER_ENABLED=True)
    def test_rate_limit_and_cache_invalidation_across_shards(self):
        url = os.environ['GATESCALE_TEST_REDIS_CLUSTER_URL']
        cache = ProductResponseCache(url)
        limiter_a = RedisRateLimiter(url)
        limiter_b = RedisRateLimiter(url)
        self.assertIsNotNone(cache.client)
        key = f'servlink_cluster_test:{uuid4().hex}'
        prefix = f'gateway_cache:cluster_smoke:{uuid4().hex}:'
        keys = [f'{prefix}{index}' for index in range(60)]
        try:
            self.assertTrue(limiter_a.allow_request(key, 1, 60))
            self.assertFalse(limiter_b.allow_request(key, 1, 60))
            for item in keys:
                cache.set(item, 'value', 60)
            self.assertTrue(all(cache.client.get(item) == 'value' for item in keys))
            cache.invalidate_prefix(prefix)
            self.assertTrue(all(cache.client.get(item) is None for item in keys))
        finally:
            # The rate limiter uses fixed, aligned 60-second windows.
            window = int(time.time()) // 60
            for offset in (-1, 0, 1):
                cache.client.delete(f'{key}:{window + offset}')
            for item in keys:
                cache.client.delete(item)


@skipUnless(os.getenv('GATESCALE_TEST_REDIS_CLUSTER_URL'),
            'Set GATESCALE_TEST_REDIS_CLUSTER_URL to test route snapshots against Redis Cluster.')
@override_settings(REDIS_CLUSTER_ENABLED=True)
class LiveRouteSnapshotTests(APITestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username='route-cache-admin')
        self.project = Project.objects.create(owner=self.owner, name='Cache Cluster Test')
        service = BackendService.objects.create(
            project=self.project, name='Doctor Service',
            base_url='http://healthcare-doctor-service:8000',
        )
        self.route = Route.objects.create(
            project=self.project, name='Doctors', base_path='/doctors',
            backend_service=service,
        )
        RouteMethod.objects.create(route=self.route, method='GET')
        invalidate_route(self.project.id, '/doctors')
        self.addCleanup(invalidate_route, self.project.id, '/doctors')

    def test_redis_cluster_route_hit_and_committed_route_disable(self):
        with patch('gateway.route_cache._read_route', wraps=_read_route) as loader:
            first = RouteResolver.get_route_for_request(self.project, 'GET', '/doctors/12')
            second = RouteResolver.get_route_for_request(self.project, 'GET', '/doctors/34')
            self.assertEqual(loader.call_count, 1)
        self.assertEqual(first.id, self.route.id)
        self.assertEqual(second.id, self.route.id)
        with self.captureOnCommitCallbacks(execute=True):
            self.route.is_active = False
            self.route.save(update_fields=['is_active'])
        self.assertIsNone(RouteResolver.get_route_for_request(self.project, 'GET', '/doctors/12'))
