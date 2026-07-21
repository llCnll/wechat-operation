from django.db import migrations, models


CORE_WEEKLY_METRICS = (
    "avg_quanzi_dau_cur_week",
    "avg_quannzi_cross_game_uv_cur_week",
    "res_expose_cover_new_uv_cur_week",
    "res_expose_cover_back_uv_cur_week",
    "res_expose_cover_deposit_uv_cur_week",
    "avg_publish_uv_rate_cur_week",
    "avg_quality_publish_pv_rate_cur_week",
    "avg_content_expose_cnt_cur_week",
)


def mark_ambiguous_weekly_snapshots_pending(apps, schema_editor):
    snapshot_model = apps.get_model("reporting", "GameCircleMetricSnapshot")
    for snapshot in snapshot_model.objects.filter(source="OPERATION_EFFECT_WEEKLY").iterator():
        metrics = snapshot.metrics if isinstance(snapshot.metrics, dict) else {}
        values = [metrics.get(key) for key in CORE_WEEKLY_METRICS]
        if len(values) == len(CORE_WEEKLY_METRICS) and all(
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and value == 0
            for value in values
        ):
            snapshot.publication_state = "PENDING_RECHECK"
            snapshot.save(update_fields=["publication_state"])


class Migration(migrations.Migration):

    dependencies = [
        ("reporting", "0004_alter_gamecirclemetricsnapshot_source_metricjoblease"),
    ]

    operations = [
        migrations.AddField(
            model_name="gamecirclemetricsnapshot",
            name="publication_state",
            field=models.CharField(
                choices=[("VERIFIED", "已验证"), ("PENDING_RECHECK", "待稳定性复核")],
                default="VERIFIED",
                max_length=24,
            ),
        ),
        migrations.RunPython(mark_ambiguous_weekly_snapshots_pending, migrations.RunPython.noop),
    ]
