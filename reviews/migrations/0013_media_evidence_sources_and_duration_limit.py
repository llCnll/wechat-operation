from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("reviews", "0012_replydraft_faq_variants")]

    operations = [
        migrations.AddField(
            model_name="riskevidence",
            name="media_source_refs",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name="videoextraction",
            name="duration_limit_seconds",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
    ]
