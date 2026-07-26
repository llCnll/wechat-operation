from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("jobs", "0005_scheduled_producers"),
        ("posts", "0016_originalpost_author_nick_name"),
        ("reporting", "0005_gamecirclemetricsnapshot_publication_state"),
    ]

    operations = [
        migrations.CreateModel(
            name="PublicOpinionReport",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("source_appid", models.CharField(max_length=128)),
                ("start_day", models.DateField()),
                ("end_day", models.DateField()),
                ("status_rule_version", models.CharField(max_length=32)),
                ("analysis_version", models.CharField(max_length=64)),
                ("template_version", models.CharField(max_length=64)),
                ("source_locked_at", models.DateTimeField(blank=True, null=True)),
                ("retention_until", models.DateTimeField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="public_opinion_reports",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ("-created_at", "-pk"),
                "verbose_name": "舆情报告",
                "verbose_name_plural": "舆情报告",
            },
        ),
        migrations.CreateModel(
            name="PublicOpinionReportSource",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                (
                    "source_kind",
                    models.CharField(
                        choices=[("CURRENT", "本期来源"), ("BASELINE", "对比基线")], max_length=16
                    ),
                ),
                ("topic_id", models.BigIntegerField()),
                ("sort_index", models.PositiveIntegerField()),
                ("publish_time", models.DateTimeField(blank=True, null=True)),
                ("title", models.CharField(blank=True, max_length=500)),
                ("content_text", models.TextField(blank=True)),
                ("ballot_text", models.TextField(blank=True)),
                ("content_fingerprint", models.CharField(max_length=64)),
                ("has_ai_assessment", models.BooleanField(default=False)),
                ("ai_category", models.CharField(blank=True, max_length=32)),
                ("ai_sentiment", models.CharField(blank=True, max_length=32)),
                ("ai_feedback_value", models.CharField(blank=True, max_length=32)),
                ("ai_risk_summary", models.JSONField(default=list)),
                ("ai_model_version", models.CharField(blank=True, max_length=200)),
                ("ai_prompt_version", models.CharField(blank=True, max_length=100)),
                ("ai_assessed_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "original_post",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="public_opinion_report_sources",
                        to="posts.originalpost",
                    ),
                ),
                (
                    "report",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="sources",
                        to="reporting.publicopinionreport",
                    ),
                ),
            ],
            options={
                "ordering": ("source_kind", "sort_index"),
                "verbose_name": "舆情报告来源",
                "verbose_name_plural": "舆情报告来源",
            },
        ),
        migrations.CreateModel(
            name="PublicOpinionReportRun",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("run_number", models.PositiveIntegerField()),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("PENDING", "待执行"),
                            ("RUNNING", "执行中"),
                            ("SUCCEEDED", "成功"),
                            ("FAILED", "失败"),
                        ],
                        default="PENDING",
                        max_length=16,
                    ),
                ),
                ("metrics_as_of", models.DateTimeField(blank=True, null=True)),
                ("input_summary", models.JSONField(default=dict)),
                ("data_quality", models.JSONField(default=dict)),
                ("result_payload", models.JSONField(default=dict)),
                ("error_summary", models.CharField(blank=True, max_length=500)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "job",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="public_opinion_run",
                        to="jobs.backgroundjob",
                    ),
                ),
                (
                    "report",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="runs",
                        to="reporting.publicopinionreport",
                    ),
                ),
            ],
            options={
                "ordering": ("-run_number",),
                "verbose_name": "舆情报告运行版本",
                "verbose_name_plural": "舆情报告运行版本",
            },
        ),
        migrations.AddField(
            model_name="publicopinionreport",
            name="current_successful_run",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="current_for_reports",
                to="reporting.publicopinionreportrun",
            ),
        ),
        migrations.AddConstraint(
            model_name="publicopinionreport",
            constraint=models.CheckConstraint(
                condition=models.Q(("start_day__lte", models.F("end_day"))),
                name="public_opinion_report_valid_days",
            ),
        ),
        migrations.AddIndex(
            model_name="publicopinionreport",
            index=models.Index(
                fields=["source_appid", "created_at"], name="reporting_p_source__0d44b5_idx"
            ),
        ),
        migrations.AddIndex(
            model_name="publicopinionreport",
            index=models.Index(fields=["retention_until"], name="reporting_p_retenti_38588c_idx"),
        ),
        migrations.AddConstraint(
            model_name="publicopinionreportsource",
            constraint=models.UniqueConstraint(
                fields=("report", "source_kind", "topic_id"),
                name="public_opinion_report_source_topic",
            ),
        ),
        migrations.AddConstraint(
            model_name="publicopinionreportsource",
            constraint=models.UniqueConstraint(
                fields=("report", "source_kind", "sort_index"),
                name="public_opinion_report_source_order",
            ),
        ),
        migrations.AddIndex(
            model_name="publicopinionreportsource",
            index=models.Index(
                fields=["report", "source_kind", "sort_index"],
                name="reporting_p_report__65d3cb_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="publicopinionreportsource",
            index=models.Index(
                fields=["report", "topic_id"], name="reporting_p_report__d3b2fa_idx"
            ),
        ),
        migrations.AddConstraint(
            model_name="publicopinionreportrun",
            constraint=models.UniqueConstraint(
                fields=("report", "run_number"), name="public_opinion_report_run_number"
            ),
        ),
        migrations.AddIndex(
            model_name="publicopinionreportrun",
            index=models.Index(
                fields=["report", "status", "run_number"], name="reporting_p_report__df9df8_idx"
            ),
        ),
    ]
