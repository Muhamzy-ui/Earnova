from django.db import migrations, models
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='PlatformSettings',
            fields=[
                ('doc_id', models.CharField(max_length=50, primary_key=True, serialize=False, unique=True)),
                ('data', models.JSONField(blank=True, default=dict)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'db_table': 'platform_settings',
            },
        ),
    ]
