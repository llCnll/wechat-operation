from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("reviews", "0008_videoextraction_attempt"),
    ]

    operations = [
        migrations.AddField(
            model_name="policyrule",
            name="auto_execution_enabled",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="aiassessment",
            name="deciding_policy_rule",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="deciding_assessments", to="reviews.policyrule"),
        ),
        migrations.AlterField(
            model_name="reviewtask",
            name="status",
            field=models.CharField(choices=[("PENDING", "待审核"), ("AUTO_EXECUTED_PENDING_REVIEW", "AI 已执行待复审"), ("AUTO_REVIEW_ESCALATED", "AI 复审异常待跟进"), ("REVIEWED", "已审核"), ("CANCELED", "已取消")], default="PENDING", max_length=32),
        ),
        migrations.AddField(
            model_name="operationaudit",
            name="execution_source",
            field=models.CharField(choices=[("HUMAN", "人工"), ("AI_AUTOMATED", "AI 自动")], default="HUMAN", max_length=16),
        ),
        migrations.AddField(
            model_name="operationaudit",
            name="automation_snapshot",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.CreateModel(
            name="AiOperationReview",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("status", models.CharField(choices=[("PENDING", "待复审"), ("CONFIRMED", "已确认正确"), ("ESCALATED", "已升级人工跟进"), ("RESTORED", "已人工恢复")], default="PENDING", max_length=16)),
                ("reviewed_at", models.DateTimeField(blank=True, null=True)),
                ("reason", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("audit", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name="ai_operation_review", to="reviews.operationaudit")),
                ("reviewer", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL)),
            ],
            options={"verbose_name": "AI 操作复审", "verbose_name_plural": "AI 操作复审"},
        ),
        migrations.AddIndex(
            model_name="aioperationreview",
            index=models.Index(fields=["status", "created_at"], name="reviews_aio_status_43c5da_idx"),
        ),
    ]
