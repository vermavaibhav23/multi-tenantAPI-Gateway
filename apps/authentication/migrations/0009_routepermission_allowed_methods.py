from django.db import migrations, models
import authentication.models


class Migration(migrations.Migration):

    dependencies = [
        ('authentication', '0008_remove_apikey_project_require_application'),
    ]

    operations = [
        migrations.AddField(
            model_name='routepermission',
            name='allowed_methods',
            field=models.JSONField(default=authentication.models.default_permission_methods),
        ),
    ]
