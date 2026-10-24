"""Run with python -m benchmarks.seed; creates ONLY synthetic benchmark tenants."""
import json
import os
from pathlib import Path

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'benchmarks.settings')


def main():
    import django
    django.setup()
    from django.conf import settings
    from django.db import transaction
    from authentication.models import APIKey, Application, RoutePermission
    from routes.models import BackendService, Route, RouteMethod
    from users.models import Project, User

    if settings.SETTINGS_MODULE != 'benchmarks.settings' or settings.USE_SQLITE:
        raise RuntimeError('Load-test seeding requires isolated benchmark MySQL settings')
    if settings.DATABASES['default']['NAME'] != 'gatescale_benchmark':
        raise RuntimeError('Refusing to seed a non-benchmark database')
    credentials = []
    with transaction.atomic():
        owner, _ = User.objects.get_or_create(
            email='benchmark@example.invalid', defaults={'username': 'benchmark'})
        owner.set_unusable_password()
        owner.save()
        project, _ = Project.objects.get_or_create(name='Benchmark', defaults={'owner': owner})
        backend, _ = BackendService.objects.update_or_create(
            project=project, name='Benchmark backend',
            defaults={'base_url': 'http://benchmark-backend:8000', 'is_active': True})
        route, _ = Route.objects.update_or_create(
            project=project, base_path='/products',
            defaults={'name': 'Products', 'backend_service': backend, 'is_active': True})
        RouteMethod.objects.update_or_create(route=route, method='GET',
                                            defaults={'auth_policy': 'api_key_only'})
        for index in range(100):
            app, _ = Application.objects.get_or_create(
                project=project, slug=f'benchmark-{index:03}',
                defaults={'name': f'Benchmark {index:03}'})
            RoutePermission.objects.update_or_create(application=app, route=route,
                                                     defaults={'is_active': True})
            _, key = APIKey.create_for_application(app, 'Benchmark only')
            credentials.append({'application_id': app.id, 'api_key': key})
    output = Path('/private/credentials.json')
    output.write_text(json.dumps(credentials), encoding='utf-8')
    print(f'Seeded {len(credentials)} isolated tenants; credentials saved privately.')


if __name__ == '__main__':
    main()

