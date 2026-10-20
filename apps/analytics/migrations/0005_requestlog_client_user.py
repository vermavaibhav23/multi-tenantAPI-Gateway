from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ('analytics', '0004_requestlog_application_requestlog_customer_and_more'),
        ('client_auth', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='requestlog',
            name='client_user',
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name='request_logs', to='client_auth.applicationuser',
            ),
        ),
    ]
