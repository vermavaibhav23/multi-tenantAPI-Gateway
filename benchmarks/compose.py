"""Generate four standalone Compose files without editing the real deployment."""
from copy import deepcopy
from pathlib import Path
import json
import yaml

ROOT = Path(__file__).resolve().parent.parent
CASES = {
    'baseline': (False, False),
    'redis-cluster': (True, False),
    'mysql-replicas': (False, True),
    'combined': (True, True),
}


def generate(case):
    redis_cluster, mysql_cluster = CASES[case]
    original = yaml.safe_load((ROOT / 'docker-compose.yml').read_text())
    generated = ROOT / 'benchmarks' / 'generated' / case
    private = generated / 'private'
    private.mkdir(parents=True, exist_ok=True)
    services = {}
    selected = ['mysql-1']
    if mysql_cluster:
        selected += ['mysql-2', 'mysql-3', 'mysql-cluster-init', 'mysql-router']
    if redis_cluster:
        selected += [f'redis-node-{i}' for i in range(1, 7)] + ['redis-cluster-init']
    for name in selected:
        service = deepcopy(original['services'][name])
        service['restart'] = 'no'
        services[name] = service
        if name in ('mysql-1', 'mysql-2', 'mysql-3'):
            service['environment'] = {'MYSQL_ROOT_PASSWORD': 'benchmark-root', 'MYSQL_ROOT_HOST': '%'}
            if name == 'mysql-1':
                service['environment'].update(MYSQL_DATABASE='gatescale_benchmark',
                                              MYSQL_USER='benchmark', MYSQL_PASSWORD='benchmark')
            service['command'] += ['--innodb-buffer-pool-size=128M', '--max-connections=150']
            service.update(mem_limit='1g', cpus=0.75)
        elif name == 'mysql-cluster-init':
            service['environment'] = {'MYSQL_ROOT_PASSWORD': 'benchmark-root'}
        elif name == 'mysql-router':
            service['environment']['MYSQL_PASSWORD'] = 'benchmark-root'
            service.update(mem_limit='256m', cpus=0.25)
        elif name.startswith('redis-node-'):
            service.update(mem_limit='256m', cpus=0.25)
    if not redis_cluster:
        services['redis-single'] = {
            'image': 'redis:7-alpine', 'command': ['redis-server', '--appendonly', 'yes'],
            'healthcheck': {'test': ['CMD', 'redis-cli', 'ping'], 'interval': '3s', 'retries': 30},
            'volumes': ['redis_single_data:/data'], 'mem_limit': '256m', 'cpus': 0.25,
        }
    env = {
        'DJANGO_SETTINGS_MODULE': 'benchmarks.settings', 'USE_SQLITE': 'False',
        'MYSQL_DATABASE': 'gatescale_benchmark', 'MYSQL_USER': 'benchmark',
        'MYSQL_PASSWORD': 'benchmark', 'MYSQL_ROOT_PASSWORD': 'benchmark-root',
        'MYSQL_HOST': 'mysql-router' if mysql_cluster else 'mysql-1',
        'MYSQL_PORT': '6446' if mysql_cluster else '3306', 'MYSQL_READ_PORT': '6447',
        'MYSQL_CLUSTER_ENABLED': str(mysql_cluster), 'REDIS_CLUSTER_ENABLED': str(redis_cluster),
        'REDIS_CLUSTER_URL': 'redis://redis-node-1:6379',
        'REDIS_URL': 'redis://redis-single:6379/0', 'BENCHMARK_RATE_LIMIT': '2000',
        'DJANGO_SECRET_KEY': 'isolated-benchmark-only-0123456789abcdef',
        'GATEWAY_ALLOWED_BACKEND_ORIGINS': 'http://benchmark-backend:8000',
        'GATEWAY_CACHEABLE_BACKEND_HOSTS': 'benchmark-backend',
    }
    dependencies = {
        ('mysql-router' if mysql_cluster else 'mysql-1'): {
            'condition': 'service_started' if mysql_cluster else 'service_healthy'},
        ('redis-cluster-init' if redis_cluster else 'redis-single'): {
            'condition': 'service_completed_successfully' if redis_cluster else 'service_healthy'},
    }
    gateway = {
        'image': 'gatescale-benchmark:local', 'environment': env,
        'networks': ['default'] + (['redis-cluster'] if redis_cluster else []),
        'depends_on': dependencies,
        'volumes': [f'{private.as_posix()}:/private'],
        'mem_limit': '512m', 'cpus': 1,
    }
    services['setup'] = {**deepcopy(gateway), 'command': ['sh', '-c',
        'i=0; until python manage.py migrate --noinput; do i=$$((i+1)); '
        'if [ "$$i" -ge 30 ]; then exit 1; fi; sleep 3; done; python -m benchmarks.seed']}
    gateway_instance = {**gateway,
        'command': ['gunicorn', 'gateway_project.wsgi:application', '--bind', '0.0.0.0:8000',
                    '--workers', '2', '--threads', '4', '--timeout', '30'],
        'depends_on': {'setup': {'condition': 'service_completed_successfully'},
                       'benchmark-backend': {'condition': 'service_started'}},
        'healthcheck': {'test': ['CMD', 'python', '-c',
            "import http.client; c=http.client.HTTPConnection('localhost',8000); "
            "c.request('GET','/gateway/products'); assert c.getresponse().status == 401"],
            'interval': '5s', 'timeout': '3s', 'retries': 30},
    }
    for index in range(1, 4):
        services[f'gateway-{index}'] = deepcopy(gateway_instance)
    services['load-balancer'] = {
        'image': 'nginx:1.27-alpine',
        'volumes': ['./benchmarks/load-balancer.conf:/etc/nginx/nginx.conf:ro'],
        'ports': ['127.0.0.1:18080:8000'],
        'depends_on': {f'gateway-{i}': {'condition': 'service_healthy'} for i in range(1, 4)},
        'healthcheck': {'test': ['CMD', 'wget', '-q', '-O', '-', 'http://127.0.0.1:8000/healthz'],
                        'interval': '5s', 'timeout': '3s', 'retries': 30},
        'mem_limit': '128m', 'cpus': 0.5,
    }
    services['benchmark-backend'] = {
        'image': 'nginx:1.27-alpine',
        'volumes': ['./benchmarks/backend.conf:/etc/nginx/nginx.conf:ro'],
        'mem_limit': '128m', 'cpus': 0.5,
    }
    services['load'] = {
        'image': 'grafana/k6:0.54.0', 'user': '0:0', 'profiles': ['load'],
        'entrypoint': ['k6'], 'mem_limit': '512m', 'cpus': 1,
        'volumes': ['./benchmarks/load.js:/scripts/load.js:ro',
                    f'{private.as_posix()}:/private:ro'],
    }
    volumes = {}
    for service in services.values():
        for mount in service.get('volumes', []):
            source = mount.split(':')[0]
            if source in original.get('volumes', {}) or source == 'redis_single_data':
                volumes[source] = None
    result = {'name': f'gatescale-bench-{case}', 'services': services, 'volumes': volumes}
    if redis_cluster:
        result['networks'] = original['networks']
    target = generated / 'compose.json'
    target.write_text(json.dumps(result, indent=2), encoding='utf-8')
    return target


if __name__ == '__main__':
    for case in CASES:
        print(generate(case))

