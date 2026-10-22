"""Prevent silently unreachable nested prefixes after switching to direct route lookup."""
from django.db import migrations


def validate_paths(apps, schema_editor):
    Route = apps.get_model('routes', 'Route')
    invalid = []
    for route_id, path in Route.objects.values_list('pk', 'base_path').iterator():
        if not path or not path.startswith('/') or path == '/' or path.count('/') != 1:
            invalid.append((route_id, path))
            if len(invalid) == 10:
                break
    if invalid:
        raise RuntimeError(
            'Single-segment route cache requires base paths such as /doctors. '
            f'Update or remove nested/invalid routes before migrating: {invalid}'
        )


class Migration(migrations.Migration):
    dependencies = [('routes', '0009_backendservice_base_url')]
    operations = [migrations.RunPython(validate_paths, migrations.RunPython.noop)]
