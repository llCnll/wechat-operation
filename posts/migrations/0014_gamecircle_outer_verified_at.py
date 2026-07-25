from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("posts", "0013_gamecircle_frame_scope_version")]

    operations = [migrations.AddField(model_name="gamecircleloginruntimestate", name="outer_verified_at", field=models.DateTimeField(blank=True, null=True))]
