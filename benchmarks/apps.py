from django.apps import AppConfig
from django.conf import settings


class BenchmarkConfig(AppConfig):
    name = 'benchmarks'

    def ready(self):
        if settings.SETTINGS_MODULE != 'benchmarks.settings':
            raise RuntimeError('Benchmark app requires benchmarks.settings')
        from authentication.models import Application
        Application.PLAN_LIMITS = {
            plan: (settings.BENCHMARK_RATE_LIMIT, 60)
            for plan, _ in Application.PLAN_CHOICES
        }

