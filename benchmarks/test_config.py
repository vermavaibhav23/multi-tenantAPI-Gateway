import os
import subprocess
import sys
import unittest
import json

from benchmarks.compose import CASES, ROOT, generate


class BenchmarkConfigurationTests(unittest.TestCase):
    def test_topologies_are_isolated_and_change_only_selected_infrastructure(self):
        for case, (redis_cluster, mysql_cluster) in CASES.items():
            config = json.loads(generate(case).read_text())
            services = config['services']
            gateway = services['gateway-1']
            self.assertEqual(config['name'], f'gatescale-bench-{case}')
            self.assertEqual(services['load-balancer']['ports'], ['127.0.0.1:18080:8000'])
            self.assertEqual(set(services['load-balancer']['depends_on']),
                             {'gateway-1', 'gateway-2', 'gateway-3'})
            for index in range(1, 4):
                self.assertEqual(services[f'gateway-{index}'], gateway)
            self.assertNotIn('gateway', services)
            self.assertEqual(gateway['environment']['MYSQL_DATABASE'], 'gatescale_benchmark')
            self.assertEqual(gateway['environment']['REDIS_CLUSTER_ENABLED'], str(redis_cluster))
            self.assertEqual(gateway['environment']['MYSQL_CLUSTER_ENABLED'], str(mysql_cluster))
            self.assertEqual(gateway['environment']['BENCHMARK_RATE_LIMIT'], '2000')
            self.assertEqual('mysql-2' in services, mysql_cluster)
            self.assertEqual('redis-node-6' in services, redis_cluster)
            self.assertEqual(sum(name.startswith('api-gateway') for name in services), 0)
            self.assertEqual(gateway['cpus'], 1)
            self.assertNotIn('build', gateway)
            for name, service in services.items():
                if name != 'load-balancer':
                    self.assertNotIn('ports', service)
            self.assertTrue(all(value is None for value in config['volumes'].values()))

    def test_benchmark_settings_do_not_change_production_limits_or_urlconf(self):
        script = (
            'import django,json; django.setup(); '
            'from authentication.models import Application; '
            'from django.conf import settings; '
            'print(json.dumps([Application.PLAN_LIMITS,settings.ROOT_URLCONF,str(settings.DATABASES["default"]["NAME"])]))'
        )
        for module, limit in [('gateway_project.settings', 100), ('benchmarks.settings', 2000)]:
            env = {**os.environ, 'DJANGO_SETTINGS_MODULE': module, 'USE_SQLITE': 'True',
                   'PYTHONDONTWRITEBYTECODE': '1', 'BENCHMARK_RATE_LIMIT': '2000'}
            result = subprocess.run([sys.executable, '-c', script], cwd=ROOT, env=env,
                                    capture_output=True, text=True, check=True)
            limits, urls, database = json.loads(result.stdout)
            self.assertEqual(limits['normal'][0], limit)
            self.assertEqual(urls, 'gateway_project.urls')
            self.assertEqual('benchmark.sqlite3' in database, module == 'benchmarks.settings')


if __name__ == '__main__':
    unittest.main()
