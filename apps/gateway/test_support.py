"""Minimal, per-test Redis double for routing tests without running Redis.

This is only used by unit tests. Production rate limiting never falls back
to a process-local counter.
"""
import fnmatch
import threading
import time


class FakeRedisClient:
    def __init__(self):
        self._data = {}
        self._expires = {}
        self._lock = threading.Lock()

    def _read(self, key):
        expires = self._expires.get(key)
        if expires is not None and expires <= time.time():
            self._expires.pop(key, None)
            self._data.pop(key, None)
        return self._data.get(key)

    def ping(self):
        return True

    def get(self, key):
        with self._lock:
            return self._read(key)

    def set(self, key, value, ex=None):
        with self._lock:
            self._data[key] = value
            if ex is not None:
                self._expires[key] = time.time() + ex
            else:
                self._expires.pop(key, None)
            return True

    def delete(self, *keys):
        with self._lock:
            removed = 0
            for key in keys:
                if key in self._data:
                    del self._data[key]
                    removed += 1
                self._expires.pop(key, None)
            return removed

    def scan_iter(self, match='*', count=None):
        with self._lock:
            return iter([
                key for key in list(self._data)
                if self._read(key) is not None and fnmatch.fnmatchcase(key, match)
            ])

    def eval(self, script, numkeys, *args):
        with self._lock:
            if numkeys == 1:
                key, limit, ttl_seconds = args
                value = self._read(key)
                counter = int(value) + 1 if value is not None else 1
                self._data[key] = str(counter)
                if counter == 1:
                    self._expires[key] = time.time() + int(ttl_seconds)
                return int(counter <= int(limit))
            if numkeys == 2:
                generation_key, data_key = args[:2]
                if "redis.call('INCR', KEYS[1])" in script:
                    generation = int(self._read(generation_key) or '0') + 1
                    self._data[generation_key] = str(generation)
                    self._data.pop(data_key, None)
                    self._expires.pop(data_key, None)
                    return 1
                expected_generation, payload, ttl_seconds = args[2:]
                current_generation = str(self._read(generation_key) or '0')
                if current_generation != str(expected_generation):
                    return 0
                self._data[data_key] = payload
                self._expires[data_key] = time.time() + int(ttl_seconds)
                return 1
            raise ValueError('Unsupported number of keys in Redis test double')
