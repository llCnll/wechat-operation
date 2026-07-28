"""Word rendering from one already-completed public-opinion run snapshot."""

from __future__ import annotations

from io import BytesIO

from docx import Document

from posts.official_authors import OFFICIAL_IDENTITY_LABELS

from .public_opinion_analysis import feedback_value_display
from .public_opinion_conclusion import conclusion_failure_label
from .public_opinion_presentation import present_conclusion, present_daily_metrics, present_observations


def _table(document: Document, headings: tuple[str, ...], rows: list[tuple[object, ...]]) -> None:
    table = document.add_table(rows=1, cols=len(headings))
    table.style = "Table Grid"
    for cell, value in zip(table.rows[0].cells, headings, strict=True):
        cell.text = str(value)
    for row in rows:
        cells = table.add_row().cells
        for cell, value in zip(cells, row, strict=True):
            cell.text = str(value)


def render_public_opinion_word(*, report, run, game_name: str) -> bytes:
    """Render solely from the immutable result payload supplied by the caller."""
    payload = run.result_payload
    document = Document()
    document.add_heading("游戏圈舆情分析报告", level=0)
    document.add_paragraph("仅供运营参考，需人工核验；不构成审核或处置结论。")
    document.add_heading("范围与版本", level=1)
    document.add_paragraph(
        "\n".join(
            (
                f"游戏：{game_name}",
                f"统计范围：{report.start_day.isoformat()} 至 {report.end_day.isoformat()}（上海时区）",
                f"报告编号：{report.pk}，运行版本：{run.run_number}",
                f"来源锁定时间：{report.source_locked_at.isoformat() if report.source_locked_at else '未记录'}",
                f"互动指标时点：{run.metrics_as_of.isoformat() if run.metrics_as_of else '未记录'}",
                f"分析版本：{report.analysis_version}，模板版本：{report.template_version}",
            )
        )
    )
    quality = payload["data_quality"]
    document.add_heading("数据质量与限制", level=1)
    _table(
        document,
        ("项目", "数值"),
        [
            ("锁定来源帖子", quality["source_count"]),
            ("对比基线帖子", quality["baseline_count"]),
            ("文本缺口", quality["text_missing_count"]),
            ("AI 评估覆盖", f"{quality['ai_assessment_count']}/{quality['source_count']}"),
            ("情绪覆盖", f"{quality['sentiment_covered_count']}/{quality['source_count']}"),
            ("互动快照覆盖", f"{quality['interaction_snapshot_count']}/{quality['source_count']}"),
        ],
    )
    for limitation in payload["limitations"]:
        document.add_paragraph(str(limitation), style="List Bullet")
    if payload.get("player_topics"):
        document.add_heading("玩家话题", level=1)
        _table(document, ("话题", "数量"), [(f"#{item['label']}", item["count"]) for item in payload["player_topics"]])
    daily_metrics = present_daily_metrics(payload.get("daily_metrics"))
    if daily_metrics:
        document.add_heading("日指标证据", level=1)
        _table(
            document,
            ("日期", "来源", "指标", "采集时间"),
            [
                (
                    item.get("day", ""),
                    item["source_label"],
                    "\n".join(
                        f"{metric['label']}：{metric['value']}"
                        for metric in item["metric_items"]
                    )
                    or "未提供可展示指标",
                    item["captured_at_label"],
                )
                for item in daily_metrics
            ],
        )
    for title, key in (
        ("内容类别", "categories"),
        ("情绪", "sentiments"),
        ("风险", "risks"),
        ("议题", "topics"),
    ):
        document.add_heading(title, level=1)
        _table(
            document,
            ("项目", "数量"),
            [(item["label"], item["count"]) for item in payload[key]],
        )
    document.add_paragraph("议题计数可重叠，不能与帖子总数直接比较。")
    document.add_heading("反馈价值", level=1)
    _table(
        document,
        ("项目", "数量"),
        [
            (feedback_value_display(item["label"]), item["count"])
            for item in payload["summary"]["feedback_values"]
        ],
    )
    document.add_heading("热词", level=1)
    hotwords = payload["hotwords"]
    document.add_paragraph(
        "已使用固定上期基线对比。"
        if hotwords["baseline_available"]
        else "没有可用上期基线，仅展示当前词频。"
    )
    _table(
        document,
        ("词语", "本期", "基线", "差异"),
        [
            (
                item["term"],
                item["current_count"],
                item["baseline_count"] or "-",
                item["difference"] or "-",
            )
            for item in hotwords["items"]
        ],
    )
    document.add_heading("发帖趋势与时段", level=1)
    _table(
        document,
        ("日期", "帖子", "情绪覆盖", "议题命中"),
        [
            (item["day"], item["post_count"], item["sentiment_covered"], item["topic_hits"])
            for item in payload["daily_trend"]
        ],
    )
    _table(
        document,
        ("小时", "发帖数"),
        [(item["label"], item["count"]) for item in payload["hour_distribution"]],
    )
    document.add_heading("高互动代表帖", level=1)
    _table(
        document,
        ("帖子 ID", "标题", "身份", "互动总量", "互动率", "快照时间"),
        [
            (
                item["topic_id"],
                item["title"],
                OFFICIAL_IDENTITY_LABELS.get(item.get("official_identity"), "玩家内容"),
                item["interaction_total"],
                f"{item['engagement_rate']:.2%}",
                item["captured_at"],
            )
            for item in payload["representative_posts"]["by_total"]
        ],
    )
    document.add_heading("运营观察", level=1)
    observations = present_observations(payload)
    for item in observations:
        suffix = " 需人工核验。" if item.get("verify") else ""
        document.add_paragraph(f"{item['message']}{suffix}", style="List Bullet")
    document.add_heading("模型结论", level=1)
    conclusion = present_conclusion(payload.get("conclusion"), payload)
    if conclusion.get("status") == "AVAILABLE":
        for item in conclusion.get("items", []):
            document.add_paragraph(f"{item['kind_label']}：{item['text']}", style="List Bullet")
            for evidence in item["evidence"]:
                document.add_paragraph(
                    f"引用证据｜{evidence['label']}：{evidence['summary']}", style="List Bullet 2"
                )
    else:
        error_label = conclusion_failure_label(conclusion.get("error_code"))
        document.add_paragraph(
            f"模型结论暂不可用（{error_label}）；确定性统计仍可作为运营参考。"
        )
    if conclusion.get("prompt_version"):
        document.add_paragraph(
            " · ".join(
                (
                    f"模型：{conclusion.get('model_version', '未记录')}",
                    f"提示词：{conclusion['prompt_version']}",
                    f"证据 Schema：{conclusion.get('evidence_schema_version', '未记录')}",
                )
            )
        )
    output = BytesIO()
    document.save(output)
    return output.getvalue()
