"""Preserve method-specific authentication while consolidating public base routes."""
from collections import defaultdict

from django.db import migrations, models
import django.db.models.deletion


def consolidate_routes(apps, schema_editor):
    Route = apps.get_model('routes', 'Route')
    RouteMethod = apps.get_model('routes', 'RouteMethod')
    RoutePermission = apps.get_model('authentication', 'RoutePermission')
    RequestLog = apps.get_model('analytics', 'RequestLog')

    groups = defaultdict(list)
    for route in Route.objects.order_by('id'):
        groups[(route.project_id, route.path)].append(route)

    # Validate first: never silently widen permissions or pick an arbitrary target.
    for (project_id, path), rows in groups.items():
        if len({row.target_url for row in rows}) > 1:
            raise RuntimeError(
                f'Conflicting backend target URLs for {project_id}:{path}. '
                'Resolve them before running this migration.'
            )
        if len({row.backend_service_id for row in rows}) > 1:
            raise RuntimeError(
                f'Conflicting backend services for {project_id}:{path}. '
                'Resolve them before running this migration.'
            )

        active_methods = {row.method for row in rows if row.is_active}
        row_ids = [row.id for row in rows]
        grants = defaultdict(set)
        all_permission_apps = set()
        for grant in RoutePermission.objects.filter(route_id__in=row_ids):
            all_permission_apps.add(grant.application_id)
            original = next(row for row in rows if row.id == grant.route_id)
            if grant.is_active and original.is_active:
                grants[grant.application_id].add(original.method)

        # An application-level RoutePermission would otherwise give all methods
        # when only e.g. GET was granted before. Fail safely instead.
        for app_id in all_permission_apps:
            if grants[app_id] and grants[app_id] != active_methods:
                raise RuntimeError(
                    f'Application {app_id} has method-specific grants for {project_id}:{path}. '
                    'Review/normalize permissions before collapsing this route.'
                )

    for rows in groups.values():
        rows.sort(key=lambda item: item.id)
        survivor = rows[0]
        enabled_rows = [row for row in rows if row.is_active]

        for original in enabled_rows:
            RouteMethod.objects.create(
                route_id=survivor.id, method=original.method,
                auth_policy=original.auth_policy,
            )

        # Preserve disabled groups, but hide disabled individual methods.
        new_active = bool(enabled_rows)
        if survivor.is_active != new_active:
            Route.objects.filter(pk=survivor.pk).update(is_active=new_active)

        duplicate_ids = [row.id for row in rows[1:]]
        if not duplicate_ids:
            continue

        # Request logs must keep referring to the surviving route.
        RequestLog.objects.filter(route_id__in=duplicate_ids).update(route_id=survivor.id)

        grants_by_app = defaultdict(list)
        for grant in RoutePermission.objects.filter(route_id__in=[row.id for row in rows]):
            grants_by_app[grant.application_id].append(grant)
        for permission_rows in grants_by_app.values():
            selected = next(
                (entry for entry in permission_rows if entry.route_id == survivor.id),
                permission_rows[0],
            )
            should_be_active = any(entry.is_active for entry in permission_rows)
            for entry in permission_rows:
                if entry.pk != selected.pk:
                    entry.delete()
            if selected.route_id != survivor.id or selected.is_active != should_be_active:
                selected.route_id = survivor.id
                selected.is_active = should_be_active
                selected.save(update_fields=['route', 'is_active'])

        Route.objects.filter(pk__in=duplicate_ids).delete()


class Migration(migrations.Migration):
    dependencies = [
        ('routes', '0007_route_auth_policy'),
        ('authentication', '0008_remove_apikey_project_require_application'),
        ('analytics', '0004_requestlog_application_requestlog_customer_and_more'),
    ]

    operations = [
        migrations.CreateModel(
            name='RouteMethod',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('method', models.CharField(max_length=10, choices=[
                    ('GET', 'GET'), ('POST', 'POST'), ('PUT', 'PUT'),
                    ('PATCH', 'PATCH'), ('DELETE', 'DELETE')
                ])),
                ('auth_policy', models.CharField(
                    max_length=20, default='api_key_only',
                    choices=[('api_key_only', 'API key only'), ('api_key_and_jwt', 'API key and JWT')],
                )),
                ('route', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='method_policies', to='routes.route',
                )),
            ],
        ),
        migrations.AlterUniqueTogether(
            name='routemethod', unique_together={('route', 'method')},
        ),
        migrations.RunPython(consolidate_routes, reverse_code=migrations.RunPython.noop),
        migrations.AlterUniqueTogether(name='route', unique_together=set()),
        migrations.RemoveIndex(
            model_name='route', name='routes_rout_project_68cb6d_idx',
        ),
        migrations.RemoveIndex(
            model_name='route', name='routes_rout_method_8dac9a_idx',
        ),
        migrations.RemoveIndex(
            model_name='route', name='routes_rout_backend_373903_idx',
        ),
        migrations.RemoveField(model_name='route', name='method'),
        migrations.RemoveField(model_name='route', name='auth_policy'),
        migrations.RenameField(model_name='route', old_name='path', new_name='base_path'),
        migrations.AlterUniqueTogether(
            name='route', unique_together={('project', 'base_path')},
        ),
        migrations.AlterUniqueTogether(
            name='backendservice',
            unique_together={('project', 'slug'), ('project', 'name')},
        ),
    ]
