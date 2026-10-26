"""Opt-in benchmark settings; normal settings never import this module."""
from gateway_project.settings import *  # noqa: F403

DEBUG = False
INSTALLED_APPS = [*INSTALLED_APPS, 'benchmarks.apps.BenchmarkConfig']
BENCHMARK_RATE_LIMIT = int(os.environ.get('BENCHMARK_RATE_LIMIT', '2000'))
if BENCHMARK_RATE_LIMIT < 1:
    raise ValueError('BENCHMARK_RATE_LIMIT must be positive')
for database in DATABASES.values():
    database['NAME'] = (BASE_DIR / 'benchmarks' / 'benchmark.sqlite3'
                        if USE_SQLITE else 'gatescale_benchmark')
    database['CONN_MAX_AGE'] = 60

