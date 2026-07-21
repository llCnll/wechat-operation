from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

from jobs.models import BackgroundJob
from posts.models import CandidatePost, ManagedGame, OriginalPost, OriginType


@pytest.mark.django_db
def test_live_operations_analysis_requires_login():
    response = Client().get("/reports/")

    assert response.status_code == 302
    assert "/admin/login/" in response.url


@pytest.mark.django_db
def test_live_operations_analysis_defaults_to_seven_days_and_does_not_create_jobs():
    user = get_user_model().objects.create_user(username="report-user", password="test-password")
    post = OriginalPost.objects.create(
        topic_id=701,
        content_id="report-view-701",
        publish_time=datetime(2026, 7, 18, 12, tzinfo=ZoneInfo("Asia/Shanghai")),
    )
    post.interaction_snapshots.create(
        source="report-test", view_count=8, love_count=3, reply_count=2, share_count=1
    )
    jobs_before = BackgroundJob.objects.count()
    games_before = list(ManagedGame.objects.values_list("appid", "name"))
    client = Client()
    client.force_login(user)

    response = client.get("/reports/")

    page = response.content.decode()
    assert response.status_code == 200
    assert "运营分析" in page
    assert "运营指标工作台" in page
    assert "统计周期" in page
    assert "影响整个工作台" in page
    assert "每日运营指标" in page
    assert "游戏圈活跃用户" in page
    assert "非留存率" in page
    end_date = timezone.localdate()
    start_date = end_date - timedelta(days=6)
    assert f'value="{start_date.isoformat()}"' in page
    assert f'value="{end_date.isoformat()}"' in page
    assert "刷新帖子互动" in page
    assert "generate_daily_report" not in page
    assert "CSV" not in page
    assert BackgroundJob.objects.count() == jobs_before
    assert list(ManagedGame.objects.values_list("appid", "name")) == games_before


@pytest.mark.django_db
def test_live_operations_analysis_scopes_the_selected_range_and_game():
    user = get_user_model().objects.create_user(username="report-filter", password="test-password")
    appid = "wx-report-game"
    candidate = CandidatePost.objects.create(
        topic_id=702,
        content_id="report-view-702",
        source_appid=appid,
        origin_type=OriginType.FORWARD,
    )
    CandidatePost.objects.filter(pk=candidate.pk).update(
        created_at=datetime(2026, 7, 18, 9, tzinfo=ZoneInfo("Asia/Shanghai"))
    )
    OriginalPost.objects.create(
        topic_id=703,
        content_id="report-view-703",
        source_appid=appid,
        publish_time=datetime(2026, 7, 18, 12, tzinfo=ZoneInfo("Asia/Shanghai")),
    )
    client = Client()
    client.force_login(user)

    response = client.get(
        "/reports/",
        {"start_date": "2026-07-18", "end_date": "2026-07-18", "appid": appid},
    )

    page = response.content.decode()
    assert response.status_code == 200
    assert "转发排除" in page
    assert "<dd>1</dd>" in page
    assert 'value="wx-report-game" selected' in page


@pytest.mark.django_db
def test_analysis_refresh_post_enqueues_one_job_and_preserves_the_range():
    user = get_user_model().objects.create_user(username="refresh-user", password="test-password")
    client = Client()
    client.force_login(user)
    payload = {
        "start_date": "2026-07-18",
        "end_date": "2026-07-18",
        "appid": "wx6d2454a8afcd9706",
    }

    first = client.post("/reports/refresh-metrics/", payload)
    second = client.post("/reports/refresh-metrics/", payload)

    assert first.status_code == 302
    assert "start_date=2026-07-18" in first.url
    assert second.status_code == 302
    jobs = BackgroundJob.objects.filter(task_type="METRICS_REFRESH_WINDOW")
    assert jobs.count() == 1
    assert jobs.get().parameter_summary["appid"] == "wx6d2454a8afcd9706"


@pytest.mark.django_db
def test_analysis_refresh_requires_csrf_confirmation():
    user = get_user_model().objects.create_user(username="csrf-user", password="test-password")
    client = Client(enforce_csrf_checks=True)
    client.force_login(user)

    response = client.post(
        "/reports/refresh-metrics/",
        {
            "start_date": "2026-07-18",
            "end_date": "2026-07-18",
            "appid": "wx6d2454a8afcd9706",
        },
    )

    assert response.status_code == 403
    assert BackgroundJob.objects.filter(task_type="METRICS_REFRESH_WINDOW").count() == 0
