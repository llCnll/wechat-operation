from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("posts", "0012_authormutestatus_permanent_mute")]

    operations = [
        migrations.AddField(
            model_name="gamecircleloginruntimestate",
            name="frame_scope_version",
            field=models.PositiveBigIntegerField(blank=True, null=True),
        ),
    ]
