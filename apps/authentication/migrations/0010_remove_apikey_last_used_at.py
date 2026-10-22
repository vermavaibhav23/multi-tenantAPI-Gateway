from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ('authentication', '0009_one_api_key_per_application'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='apikey',
            name='last_used_at',
        ),
    ]
