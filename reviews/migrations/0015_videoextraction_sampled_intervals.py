from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("reviews", "0014_operationaudit_mute_trigger_and_system_source")]

    operations = [
        migrations.AddField(
            model_name="videoextraction",
            name="sampled_intervals_ms",
            field=models.JSONField(blank=True, default=list),
        ),
    ]
