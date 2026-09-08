from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('routes', '0006_remove_route_cache_ttl_seconds'),
    ]

    operations = [
        migrations.AddField(
            model_name='route',
            name='auth_policy',
            field=models.CharField(
                choices=[
                    ('api_key_only', 'API key only'),
                    ('api_key_and_jwt', 'API key and JWT'),
                ],
                default='api_key_only',
                max_length=20,
            ),
        ),
    ]
