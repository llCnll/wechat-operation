from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("posts", "0011_gamecircle_session_projection"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="authormutestatus",
            name="permanent_mute_enabled",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="authormutestatus",
            name="permanent_mute_set_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="authormutestatus",
            name="permanent_mute_set_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=models.SET_NULL,
                related_name="permanent_mute_statuses",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddIndex(
            model_name="authormutestatus",
            index=models.Index(
                fields=["source_appid", "permanent_mute_enabled"],
                name="posts_autho_source__3f490d_idx",
            ),
        ),
    ]
