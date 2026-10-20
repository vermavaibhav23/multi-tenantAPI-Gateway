from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    initial = True
    dependencies = [
        ('authentication', '0009_one_api_key_per_application'),
    ]
    operations = [
        migrations.CreateModel(
            name='ApplicationUser',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('email', models.EmailField(max_length=254)),
                ('password_hash', models.CharField(max_length=128)),
                ('is_active', models.BooleanField(default=True)),
                ('token_version', models.PositiveIntegerField(default=1)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('application', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='client_users', to='authentication.application',
                )),
            ],
            options={
                'indexes': [models.Index(fields=['application', 'is_active'], name='client_auth_app_active_idx')],
                'constraints': [models.UniqueConstraint(
                    fields=('application', 'email'), name='clientuser_application_email_unique'
                )],
            },
        ),
    ]
