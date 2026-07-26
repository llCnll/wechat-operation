from django.db import connection
from django.utils import timezone

from posts.models import OriginalPost


INCREMENTAL_REQUIRED_COLUMNS = {
    "posts_authormutestatus": {"permanent_mute_enabled"},
    "posts_gamecircleloginruntimestate": {
        "available_actions",
        "frame_state",
        "outer_state",
    },
    "posts_originalpost": {"author_nick_name", "author_openid", "ballot_text"},
    "posts_postmedia": {"media_type"},
    "reporting_gamecirclemetricsnapshot": {"publication_state"},
    "reviews_operationaudit": {"automation_snapshot", "execution_source", "mute_trigger"},
    "reviews_policyrule": {"auto_execution_enabled"},
    "reviews_replydraft": {"faq_rule_id", "faq_version", "selected_style", "style_variants"},
    "reviews_riskevidence": {"media_source_refs", "ocr_source_refs"},
    "reviews_reviewtask": {"focus_reasons"},
    "reviews_videoextraction": {"attempt", "sampled_intervals_ms"},
}


def _sqlite_column_defaults(table_name: str) -> dict[str, object]:
    with connection.cursor() as cursor:
        cursor.execute(f'PRAGMA table_info("{table_name}")')
        return {row[1]: row[4] for row in cursor.fetchall()}


def test_incremental_required_columns_have_database_defaults(db):
    """Old workers may omit a newly added column during a controlled upgrade."""
    assert connection.vendor == "sqlite"
    for table_name, fields in INCREMENTAL_REQUIRED_COLUMNS.items():
        defaults = _sqlite_column_defaults(table_name)
        missing_defaults = sorted(field for field in fields if defaults.get(field) is None)
        assert not missing_defaults, f"{table_name} 缺少数据库默认值：{', '.join(missing_defaults)}"


def test_legacy_ingestion_insert_uses_original_post_database_defaults(db):
    """A pre-migration ingestion worker can write a post without later-added columns."""
    now = timezone.now()
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO posts_originalpost (
                topic_id, content_id, title, content_html, content_text,
                publish_time, status, recommend_status, source_appid, created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            [
                990_001,
                "legacy-worker-content",
                "旧 Worker 帖子",
                "",
                "",
                None,
                2,
                None,
                "wx-legacy",
                now,
                now,
            ],
        )

    post = OriginalPost.objects.get(topic_id=990_001)
    assert post.ballot_text == ""
    assert post.author_openid == ""
    assert post.author_nick_name == ""
