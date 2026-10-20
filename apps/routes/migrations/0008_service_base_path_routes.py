"""Consolidate legacy method-specific routes into one base route per backend service.

This migration deliberately aborts rather than silently changing routing when
existing method records disagree about their backend, target or auth policy.
"""
import re
from collections import defaultdict

from django.db import migrations, models
import django.db.models.deletion
import routes.models


METHOD_ORDER = ['GET', 'POST', 'PUT', 'PATCH', 'DELETE']
BASE_PATH_RE = re.compile(r'/[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*\Z')


def migrate_routes(apps, schema_editor):
    BackendService = apps.get_model('routes', 'BackendService')
    Route = apps.get_model('routes', 'Route')
    RoutePermission = apps.get_model('authentication', 'RoutePermission')

    groups = defaultdict(list)
    for route in Route.objects.all().order_by('id'):
        groups[(route.project_id, route.path)].append(route)

    service_paths = {}
    project_paths = {}

    # Validate the entire legacy configuration before changing any records.
    for (project_id, path), routes in groups.items():
        if not BASE_PATH_RE.fullmatch(path):
            raise RuntimeError(f'Invalid legacy route base path {path!r}; fix it before migrating.')
        service_ids = {route.backend_service_id for route in routes if route.backend_service_id}
        targets = {route.target_url for route in routes}
        policies = {route.auth_policy for route in routes}
        if len(service_ids) > 1 or len(targets) > 1 or len(policies) > 1:
            raise RuntimeError(
                f'Cannot combine legacy routes for project {project_id}, path {path}: '
                'different backends, targets or JWT policies. Resolve them before migrating.'
            )
        if service_ids:
            service_id = next(iter(service_ids))
            service = BackendService.objects.get(pk=service_id)
            if service.project_id != project_id:
                raise RuntimeError(f'Route {path} references a service in another project.')
            if service_id in service_paths and service_paths[service_id] != path:
                raise RuntimeError(
                    f'BackendService {service_id} exposes more than one base path. '
                    'Split or redesign the service registration before migrating.'
                )
            service_paths[service_id] = path
        project_paths[(project_id, path)] = next(iter(service_ids)) if service_ids else None

    for (project_id, path), rows in groups.items():
        rows.sort(key=lambda obj: obj.id)
        current = rows[0]
        service = BackendService.objects.get(pk=current.backend_service_id) if current.backend_service_id else None
        if service is None:
            # Old routes could have no service association.
            slug = f'legacy-route-{current.id}'
            service = BackendService.objects.create(
                project_id=project_id, name=current.name, slug=slug,
                base_path=path, target_url=current.target_url,
            )
        else:
            service.base_path = path
            service.target_url = current.target_url
            service.save(update_fields=['base_path', 'target_url'])

        # Retain only the methods actually active in the old configuration.
        active_methods = {item.method for item in rows if item.is_active}
        enabled = [method for method in METHOD_ORDER if method in active_methods]
        route_by_id = {item.id: item for item in rows}
        ids = list(route_by_id)

        # Merge application grants without widening their former method scope.
        permissions_by_application = defaultdict(list)
        for permission in RoutePermission.objects.filter(route_id__in=ids).order_by('id'):
            permissions_by_application[permission.application_id].append(permission)
        for permissions in permissions_by_application.values():
            granted = {route_by_id[permission.route_id].method for permission in permissions
                       if permission.is_active and route_by_id[permission.route_id].is_active}
            selected = next((permission for permission in permissions if permission.route_id == current.id),
                            permissions[0])
            for permission in permissions:
                if permission.pk != selected.pk:
                    permission.delete()
            selected.route_id = current.pk
            selected.allowed_methods = [method for method in METHOD_ORDER if method in granted]
            selected.is_active = bool(granted)
            selected.save(update_fields=['route', 'allowed_methods', 'is_active'])

        current.backend_service_id = service.id
        current.allowed_methods = enabled
        current.is_active = bool(enabled)
        current.save(update_fields=['backend_service', 'allowed_methods', 'is_active'])
        Route.objects.filter(pk__in=[item.id for item in rows[1:]]).delete()

    # A service registered without a route still needs a reserved base path.
    for service in BackendService.objects.filter(base_path='').order_by('id'):
        candidate = '/' + service.slug
        if not BASE_PATH_RE.fullmatch(candidate):
            raise RuntimeError(f'Invalid slug for unconfigured BackendService {service.id}.')
        if BackendService.objects.filter(project_id=service.project_id, base_path=candidate).exclude(pk=service.pk).exists():
            raise RuntimeError(f'Base path {candidate} is already registered; configure service {service.id} first.')
        service.base_path = candidate
        service.save(update_fields=['base_path'])


class Migration(migrations.Migration):

    dependencies = [
        ('routes', '0007_route_auth_policy'),
        ('authentication', '0009_routepermission_allowed_methods'),
    ]

    operations = [
        migrations.AddField(
            model_name='backendservice',
            name='base_path',
            field=models.CharField(blank=True, default='', max_length=200),
        ),
        migrations.AddField(
            model_name='backendservice',
            name='target_url',
            field=models.URLField(blank=True, default='', max_length=500),
        ),
        migrations.AddField(
            model_name='route',
            name='allowed_methods',
            field=models.JSONField(default=routes.models.default_http_methods),
        ),
        migrations.RunPython(migrate_routes),
        migrations.AlterUniqueTogether(
            name='route',
            unique_together=set(),
        ),
        migrations.RemoveIndex(
            model_name='route',
            name='routes_rout_project_68cb6d_idx',
        ),
        migrations.RemoveIndex(
            model_name='route',
            name='routes_rout_method_8dac9a_idx',
        ),
        migrations.RemoveIndex(
            model_name='route',
            name='routes_rout_backend_373903_idx',
        ),
        migrations.RemoveField(model_name='route', name='method'),
        migrations.RemoveField(model_name='route', name='path'),
        migrations.RemoveField(model_name='route', name='target_url'),
        migrations.AlterField(
            model_name='backendservice',
            name='base_path',
            field=models.CharField(max_length=200),
        ),
        migrations.AlterField(
            model_name='route',
            name='backend_service',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                    related_name='routes', to='routes.backendservice'),
        ),
        migrations.AlterUniqueTogether(
            name='backendservice',
            unique_together={('project', 'slug'), ('project', 'base_path')},
        ),
        migrations.AlterUniqueTogether(
            name='route',
            unique_together={('project', 'backend_service')},
        ),
        migrations.AddIndex(
            model_name='route',
            index=models.Index(fields=['project', 'is_active'], name='route_proj_active_idx'),
        ),
    ]
