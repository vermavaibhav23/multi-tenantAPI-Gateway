from django.db import migrations, models
from django.db.models import Count
import django.db.models.deletion


def validate_single_key_per_application(apps, schema_editor):
    APIKey = apps.get_model('authentication', 'APIKey')
    duplicates = APIKey.objects.values('application_id').annotate(
        count=Count('id')
    ).filter(count__gt=1)
    if duplicates.exists():
        raise RuntimeError(
            'Multiple API keys exist for one or more applications. '
            'Revoke/delete extra keys deliberately before enforcing one-to-one.'
        )


class Migration(migrations.Migration):
    dependencies = [
        ('authentication', '0008_remove_apikey_project_require_application'),
    ]

    operations = [
        migrations.RunPython(
            validate_single_key_per_application, reverse_code=migrations.RunPython.noop
        ),
        migrations.AlterField(
            model_name='apikey',
            name='application',
            field=models.OneToOneField(
                on_delete=django.db.models.deletion.CASCADE,
                related_name='api_key', to='authentication.application',
            ),
        ),
    ]
