from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ('analytics', '0005_requestlog_client_user'),
        # Keep historical route-consolidation migrations that read RequestLog
        # before deleting the legacy table on a brand-new database.
        ('routes', '0009_backendservice_base_url'),
    ]

    operations = [
        migrations.DeleteModel(name='RequestLog'),
    ]
