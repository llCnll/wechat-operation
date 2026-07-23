from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("reviews", "0013_media_evidence_sources_and_duration_limit")]

    operations = [
        migrations.AddField(
            model_name="operationaudit",
            name="mute_trigger",
            field=models.CharField(
                blank=True,
                choices=[
                    ("HUMAN_INITIAL", "人工首次禁言"),
                    ("HUMAN_PERMANENT", "人工永久管理"),
                    ("SYSTEM_RENEWAL", "系统续期"),
                    ("SYSTEM_REPAIR", "系统续期修复"),
                ],
                default="",
                help_text="仅用于禁言相关操作的来源投影，不保存认证材料或上游响应。",
                max_length=24,
            ),
        ),
        migrations.AlterField(
            model_name="operationaudit",
            name="execution_source",
            field=models.CharField(
                choices=[
                    ("HUMAN", "人工"),
                    ("AI_AUTOMATED", "AI 自动"),
                    ("SYSTEM_MAINTENANCE", "系统维护"),
                ],
                default="HUMAN",
                max_length=24,
            ),
        ),
    ]
