from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("posts", "0010_gamecircleloginruntimestate")]

    operations = [
        migrations.AddField(
            model_name="gamecircleloginruntimestate",
            name="available_actions",
            field=models.JSONField(default=list),
        ),
        migrations.AddField(
            model_name="gamecircleloginruntimestate",
            name="frame_state",
            field=models.CharField(
                choices=[
                    ("UNKNOWN", "未知"),
                    ("UNREADY", "未就绪"),
                    ("BUILDING", "建立中"),
                    ("READY", "有效"),
                ],
                default="UNKNOWN",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="gamecircleloginruntimestate",
            name="login_wait_until",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="gamecircleloginruntimestate",
            name="outer_state",
            field=models.CharField(
                choices=[
                    ("UNKNOWN", "待检查"),
                    ("BROWSER_UNAVAILABLE", "Chrome 不可用"),
                    ("BOOTSTRAPPING", "正在检查"),
                    ("LOGIN_PENDING", "需要扫码"),
                    ("OUTER_READY", "外层会话有效"),
                ],
                default="UNKNOWN",
                max_length=24,
            ),
        ),
        migrations.AlterField(
            model_name="gamecircleloginruntimestate",
            name="error_category",
            field=models.CharField(
                choices=[
                    ("NONE", "无"),
                    ("SCAN_REQUIRED", "需要扫码"),
                    ("CHROME_UNAVAILABLE", "Chrome 不可用"),
                    ("OUTER_SESSION_UNREADY", "外层会话未就绪"),
                    ("FRAME_UNREADY", "Frame 未就绪"),
                    ("TARGET_UNAVAILABLE", "受控页面不可用"),
                    ("SOURCE_VERIFICATION_FAILED", "来源验证失败"),
                    ("GAME_NOT_SELECTABLE", "当前账号无法切换目标游戏"),
                    ("SERVICE_STOPPING", "服务正在停止"),
                ],
                default="NONE",
                max_length=32,
            ),
        ),
    ]
