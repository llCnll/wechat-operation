from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("jobs", "0003_admin_display_metadata"),
    ]

    operations = [
        migrations.AddField(
            model_name="backgroundjob",
            name="active_coalescing_key",
            field=models.CharField(
                blank=True,
                help_text="仅在待执行或运行中占用；终态自动释放。",
                max_length=255,
                null=True,
                unique=True,
            ),
        ),
    ]
