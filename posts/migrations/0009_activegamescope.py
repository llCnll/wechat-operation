# Generated for the operation-scope domain model.

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("posts", "0008_originalpost_author_openid_authormutestatus"),
    ]

    operations = [
        migrations.CreateModel(
            name="ActiveGameScope",
            fields=[
                ("id", models.PositiveSmallIntegerField(default=1, editable=False, primary_key=True, serialize=False)),
                ("scope_version", models.PositiveBigIntegerField(default=1)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("active_game", models.ForeignKey(db_column="active_appid", on_delete=django.db.models.deletion.PROTECT, related_name="active_scopes", to="posts.managedgame", to_field="appid")),
                ("updated_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="updated_active_game_scopes", to=settings.AUTH_USER_MODEL)),
            ],
            options={"verbose_name": "当前操作游戏", "verbose_name_plural": "当前操作游戏"},
        ),
        migrations.AddConstraint(
            model_name="activegamescope",
            constraint=models.CheckConstraint(condition=models.Q(("id", 1)), name="active_game_scope_one"),
        ),
    ]
