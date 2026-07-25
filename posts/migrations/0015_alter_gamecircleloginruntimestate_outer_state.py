from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("posts", "0014_gamecircle_outer_verified_at")]

    operations = [
        migrations.AlterField(
            model_name="gamecircleloginruntimestate",
            name="outer_state",
            field=models.CharField(
                choices=[
                    ("UNKNOWN", "待检查"),
                    ("BROWSER_UNAVAILABLE", "Chrome 不可用"),
                    ("BOOTSTRAPPING", "正在检查"),
                    ("LOGIN_PENDING", "需要扫码"),
                    ("LOGIN_REQUIRED", "需要重新登录"),
                    ("OUTER_READY", "外层会话有效"),
                ],
                default="UNKNOWN",
                max_length=24,
            ),
        ),
    ]
