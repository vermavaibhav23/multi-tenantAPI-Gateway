"""Exercise the real NGINX configs with lightweight gateway stand-ins, not a load test."""
import json
import subprocess
import httpx

from benchmarks.compose import ROOT
from benchmarks.preflight import verify


def main():
    folder = ROOT / 'benchmarks' / 'generated' / 'smoke'
    folder.mkdir(parents=True, exist_ok=True)
    services = {}
    for name in ('gateway-1', 'gateway-2', 'gateway-3', 'backend'):
        services[name] = {
            'image': 'nginx:1.27-alpine',
            'volumes': ['./benchmarks/backend.conf:/etc/nginx/nginx.conf:ro'],
            'healthcheck': {'test': ['CMD', 'wget', '-q', '-O', '-', 'http://127.0.0.1:8000/products/1'],
                            'interval': '1s', 'timeout': '2s', 'retries': 30},
        }
    services['backend']['ports'] = ['127.0.0.1:18082:8000']
    services['load-balancer'] = {
        'image': 'nginx:1.27-alpine', 'ports': ['127.0.0.1:18081:8000'],
        'volumes': ['./benchmarks/load-balancer.conf:/etc/nginx/nginx.conf:ro'],
        'depends_on': {f'gateway-{i}': {'condition': 'service_healthy'} for i in range(1, 4)},
        'healthcheck': {'test': ['CMD', 'wget', '-q', '-O', '-', 'http://127.0.0.1:8000/healthz'],
                        'interval': '1s', 'timeout': '2s', 'retries': 30},
    }
    config = folder / 'compose.json'
    config.write_text(json.dumps({'services': services}))
    compose = ['docker', 'compose', '--project-directory', str(ROOT), '-p', 'gatescale-bench-smoke',
               '-f', str(config)]
    def run(args, **kwargs):
        return subprocess.run(compose + args, check=True, timeout=180, **kwargs)
    if run(['ps', '-a', '-q'], capture_output=True, text=True).stdout.strip():
        raise RuntimeError('Existing smoke-test containers found; inspect them before rerunning')
    try:
        run(['up', '-d', '--wait', '--wait-timeout', '90'])
        for service in ('backend', 'load-balancer'):
            run(['exec', '-T', service, 'nginx', '-t'])
        with httpx.Client(timeout=10, trust_env=False) as client:
            print(json.dumps(verify(client, 'http://127.0.0.1:18082', 'http://127.0.0.1:18081')))
    finally:
        subprocess.run(compose + ['down'], timeout=120)


if __name__ == '__main__':
    main()
