from datetime import timedelta
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from posts.content import normalize_topic_label, split_trailing_topics
from posts.game_catalog import set_active_game
from posts.models import CandidatePost, ManagedGame, OfficialAuthor, OriginalPost
from posts.official_authors import (
    disable_official_author,
    enable_official_author,
    identity_for_values,
)
from posts.services import classify_content_card


def test_split_trailing_topics_keeps_body_hash_and_orders_unique_topics():
    body, topics = split_trailing_topics("正文含 #号\n#城主攻略\n#城主攻略\n#活动")
    assert body == "正文含 #号"
    assert topics == ("城主攻略", "活动")
    assert split_trailing_topics("正文\n# 空标签") == ("正文\n# 空标签", ())
    assert normalize_topic_label("#攻略") == "攻略"
    with pytest.raises(ValueError, match="话题格式无效"):
        normalize_topic_label("##攻略")


@pytest.mark.django_db
def test_official_author_is_scoped_and_disable_immediately_stops_match():
    user = get_user_model().objects.create_user(username="official-author")
    ManagedGame.objects.create(appid="game-a", name="游戏A")
    ManagedGame.objects.create(appid="game-b", name="游戏B")
    author = enable_official_author(appid="game-a", openid="openid-a", nick_name="官方", user=user)
    assert identity_for_values(author_openid="openid-a", author_nick_name="任意", game_name="游戏A", verified_openids={"openid-a"}).key == "VERIFIED_OFFICIAL"
    assert identity_for_values(author_openid="openid-a", author_nick_name="任意", game_name="游戏B", verified_openids=set()).key == "PLAYER"
    disable_official_author(author=author, user=user)
    author.refresh_from_db()
    assert author.disabled_at is not None
    assert identity_for_values(author_openid="openid-a", author_nick_name="游戏A", game_name="游戏A", verified_openids=set()).key == "GAME_NAME_MATCH"


@pytest.mark.django_db
def test_official_author_requires_a_managed_game():
    user = get_user_model().objects.create_user(username="official-author-scope")
    with pytest.raises(ValueError, match="只能选择已同步的可管理游戏"):
        enable_official_author(appid="unmanaged-game", openid="openid-a", nick_name="官方", user=user)


@pytest.mark.django_db
def test_official_author_management_lists_synced_games(client):
    user = get_user_model().objects.create_user(username="official-author-management")
    client.force_login(user)
    current = ManagedGame.objects.create(appid="game-current", name="当前游戏")
    other = ManagedGame.objects.create(appid="game-other", name="另一款游戏")
    set_active_game(appid=current.appid)

    response = client.get("/official-authors/")

    assert response.status_code == 200
    assert response.context["appid"] == current.appid
    assert {game.appid for game in response.context["managed_games"]} == {
        current.appid,
        other.appid,
    }
    assert other.name in response.content.decode()


def test_game_name_match_normalizes_only_whitespace():
    identity = identity_for_values(
        author_openid="",
        author_nick_name="  星海\t远征  ",
        game_name="星海 远征",
        verified_openids=set(),
    )

    assert identity.key == "GAME_NAME_MATCH"


@pytest.mark.django_db
def test_official_author_preview_binds_ticket_and_confirmation_needs_no_second_source_read(client):
    user = get_user_model().objects.create_user(username="official-view")
    client.force_login(user)
    ManagedGame.objects.create(appid="game-a", name="游戏A")
    set_active_game(appid="game-a")
    response_payload = {"data": {"user": {"openid": "openid-a", "nick_name": "官方"}, "total": 1, "topic_list": []}}
    with patch("posts.views.GameCirclePostClient.get_author_detail", return_value=response_payload) as read:
        response = client.post("/official-authors/preview/", {"appid": "game-a", "openid": "openid-a"})
    assert response.status_code == 302
    assert read.call_count == 1
    with patch("posts.views.GameCirclePostClient.get_author_detail") as read:
        response = client.post("/official-authors/confirm/", {"appid": "game-a"})
    assert response.status_code == 302
    assert read.call_count == 0
    assert OfficialAuthor.objects.filter(source_appid="game-a", openid="openid-a", disabled_at__isnull=True).exists()


@pytest.mark.django_db
def test_official_author_rejects_mismatched_response_and_expired_or_cross_scope_ticket(client):
    user = get_user_model().objects.create_user(username="official-view-errors")
    client.force_login(user)
    ManagedGame.objects.create(appid="game-a", name="游戏A")
    ManagedGame.objects.create(appid="game-b", name="游戏B")
    set_active_game(appid="game-a")
    mismatched = {"data": {"user": {"openid": "other", "nick_name": "不应保存"}}}
    with patch("posts.views.GameCirclePostClient.get_author_detail", return_value=mismatched):
        client.post("/official-authors/preview/", {"appid": "game-a", "openid": "openid-a"})
    assert not OfficialAuthor.objects.exists()

    session = client.session
    session["official-author-preview:game-a"] = {
        "user_id": user.pk,
        "openid": "openid-a",
        "nick_name": "官方",
        "verified_at": (timezone.now() - timedelta(minutes=11)).isoformat(),
    }
    session.save()
    client.post("/official-authors/confirm/", {"appid": "game-a"})
    client.post("/official-authors/confirm/", {"appid": "game-b"})
    assert not OfficialAuthor.objects.exists()


@pytest.mark.django_db
def test_topic_backfill_is_local_idempotent_and_has_no_identity_checkpoint():
    from jobs.once.backfill_post_topics import (
        enqueue_post_topic_backfill,
        post_topic_backfill_handler,
    )

    post = OriginalPost.objects.create(topic_id=900, content_id="topic-backfill", content_text="正文\n#攻略")
    job = enqueue_post_topic_backfill()
    post_topic_backfill_handler(job)
    post.refresh_from_db()
    job.refresh_from_db()
    assert post.content_text == "正文"
    assert post.topics == ["攻略"]
    assert job.checkpoint == {"last_post_id": post.pk, "processed": 1, "updated": 1, "complete": True}
    post_topic_backfill_handler(job)
    assert OriginalPost.objects.get(pk=post.pk).topics == ["攻略"]


@pytest.mark.django_db
def test_content_card_and_backfill_share_the_trailing_topic_boundary():
    candidate = CandidatePost.objects.create(topic_id=901, content_id="card-topic", source_appid="game-a")
    post = classify_content_card(candidate, {"base_content": {"content": "<p>正文 #不抽离</p><p>#攻略</p><p>#活动</p>", "status": 2}})
    assert post.content_text == "正文 #不抽离"
    assert post.topics == ["攻略", "活动"]


@pytest.mark.django_db
def test_content_queue_and_post_detail_show_dynamic_official_identity(client):
    user = get_user_model().objects.create_user(username="official-display")
    client.force_login(user)
    game = ManagedGame.objects.create(appid="game-display", name="显示游戏")
    set_active_game(appid=game.appid)
    post = OriginalPost.objects.create(topic_id=902, content_id="official-display", source_appid=game.appid, author_openid="official", author_nick_name="任意")
    enable_official_author(appid=game.appid, openid="official", nick_name="官方", user=user)
    queue = client.get(f"/?appid={game.appid}&topic={post.topic_id}")
    detail = client.get(f"/posts/{post.topic_id}/")
    queue_html = queue.content.decode()
    detail_html = detail.content.decode()
    assert 'class="official-identity-badge"' in queue_html
    assert 'class="official-identity-badge"' in detail_html
    assert "所属游戏：显示游戏" in queue_html
    assert "所属游戏：显示游戏" in detail_html
    assert "· game-display" not in queue_html
    assert "· game-display" not in detail_html
