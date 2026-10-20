from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('users', '0003_remove_project_users_proje_owner_i_4385fa_idx_and_more'),
    ]

    operations = [
        migrations.AlterField(
            model_name='project',
            name='name',
            field=models.CharField(max_length=120, unique=True),
        ),
    ]
