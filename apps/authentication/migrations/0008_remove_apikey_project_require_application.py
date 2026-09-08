from django.db import migrations, models
import django.db.models.deletion


def backfill_application(apps, schema_editor):
    APIKey = apps.get_model('authentication', 'APIKey')
    Application = apps.get_model('authentication', 'Application')
    for api_key in APIKey.objects.filter(application__isnull=True).select_related('project'):
        application, _ = Application.objects.get_or_create(
            project=api_key.project,
            slug='default-application',
            defaults={
                'name': 'Default Application',
                'plan': 'normal',
                'is_active': True,
            },
        )
        api_key.application = application
        api_key.save(update_fields=['application'])


class Migration(migrations.Migration):

    dependencies = [
        ('authentication', '0007_remove_apikey_authenticat_prefix_7e7b84_idx_and_more'),
    ]

    operations = [
        migrations.RunPython(backfill_application, migrations.RunPython.noop),
        migrations.RemoveIndex(
            model_name='apikey',
            name='authenticat_project_056759_idx',
        ),
        migrations.AlterField(
            model_name='apikey',
            name='application',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='api_keys', to='authentication.application'),
        ),
        migrations.RemoveField(
            model_name='apikey',
            name='project',
        ),
    ]
