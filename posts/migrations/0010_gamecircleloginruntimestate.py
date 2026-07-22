from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("posts", "0009_activegamescope")]

    operations = [
        migrations.CreateModel(
            name="GameCircleLoginRuntimeState",
            fields=[
                ("id", models.PositiveSmallIntegerField(default=1, editable=False, primary_key=True, serialize=False)),
                ("login_state", models.CharField(choices=[("READY", "已就绪"), ("SCAN_REQUIRED", "需要扫码"), ("UNAVAILABLE", "Chrome 不可用"), ("UNKNOWN", "待检查")], default="UNKNOWN", max_length=20)),
                ("checked_at", models.DateTimeField(blank=True, null=True)),
                ("last_ready_at", models.DateTimeField(blank=True, null=True)),
                ("error_category", models.CharField(choices=[("NONE", "无"), ("SCAN_REQUIRED", "需要扫码"), ("CHROME_UNAVAILABLE", "Chrome 不可用"), ("OUTER_SESSION_UNREADY", "外层会话未就绪"), ("PROBE_FAILED", "探测失败")], default="NONE", max_length=32)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"verbose_name": "游戏圈登录运行态", "verbose_name_plural": "游戏圈登录运行态"},
        ),
        migrations.AddConstraint(
            model_name="gamecircleloginruntimestate",
            constraint=models.CheckConstraint(condition=models.Q(("id", 1)), name="game_circle_login_state_one"),
        ),
    ]
