"""Move internal destinations to BackendService without changing route behavior.

This design assumes that the backend path after the host is identical to the
public base path, and all Routes of a BackendService share the same origin.
Abort on data that cannot be represented without rewriting or losing meaning.
"""
from collections import defaultdict
from urllib.parse import urlsplit

from django.db import migrations, models


def migrate_service_origins(apps, schema_editor):
    BackendService = apps.get_model('routes', 'BackendService')
    Route = apps.get_model('routes', 'Route')
    routes_by_service = defaultdict(list)

    for route in Route.objects.all().order_by('id'):
        if route.backend_service_id is None:
            if route.target_url:
                raise RuntimeError(
                    f'Route {route.pk} has a configured target_url but no BackendService. '
                    'Assign a service before this migration.'
                )
            continue
        service = BackendService.objects.get(pk=route.backend_service_id)
        if route.project_id != service.project_id:
            raise RuntimeError(
                f'Route {route.pk} references a BackendService in another project.'
            )
        routes_by_service[service.pk].append(route)

    for service_id, routes in routes_by_service.items():
        origins = set()
        has_empty_target = False
        for route in routes:
            if not route.target_url:
                has_empty_target = True
                continue
            # Origin + identical public path was a valid old forward target.
            raw = route.target_url
            parts = urlsplit(raw)
            if (parts.scheme not in ('http', 'https') or not parts.hostname
                    or parts.username or parts.password or parts.fragment or parts.query):
                raise RuntimeError(
                    f'Route {route.pk} has an invalid destination; update it before migrating.'
                )
            expected_path = '/' + route.base_path.strip('/')
            if (parts.path.rstrip('/') or '/') != expected_path:
                raise RuntimeError(
                    f'Route {route.pk} maps public {route.base_path!r} to {parts.path!r}. '
                    'The new forwarding scheme requires public and backend paths to match.'
                )
            origins.add(f'{parts.scheme}://{parts.netloc}')
        if len(origins) > 1:
            raise RuntimeError(
                f'BackendService {service_id} has multiple origins. '
                'Split the service or consolidate destination URLs before migrating.'
            )
        if has_empty_target and origins:
            raise RuntimeError(
                f'BackendService {service_id} mixes configured and unconfigured routes; '
                'resolve those settings before migrating.'
            )
        if origins:
            BackendService.objects.filter(pk=service_id).update(base_url=next(iter(origins)))


class Migration(migrations.Migration):
    dependencies = [('routes', '0008_route_methods_and_unique_base_paths')]

    operations = [
        migrations.AddField(
            model_name='backendservice',
            name='base_url',
            field=models.URLField(blank=True, default='', max_length=500),
        ),
        migrations.RunPython(migrate_service_origins),
        migrations.RemoveField(model_name='route', name='target_url'),
    ]
