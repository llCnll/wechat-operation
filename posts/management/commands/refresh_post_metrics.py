"""Refresh only post-level interaction counters for already-ingested original posts."""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from posts.models import OriginalPost
from posts.services import store_post_metrics
from posts.source_client import CdpGameCircleClient


class Command(BaseCommand):
    help = "通过受控 Chrome 回填原创帖浏览、点赞和评论数量；不保存评论正文。"

    def add_arguments(self, parser):
        parser.add_argument("--appid", required=True, help="目标小游戏 appid")
        parser.add_argument("--limit", type=int, default=100, help="最多刷新条数，默认 100")

    def handle(self, *args, **options):
        appid = options["appid"].strip()
        limit = options["limit"]
        if not appid:
            raise CommandError("appid 不能为空")
        if limit <= 0:
            raise CommandError("limit 必须为正数")
        posts = list(OriginalPost.objects.filter(source_appid=appid).order_by("topic_id")[:limit])
        client = CdpGameCircleClient(appid=appid)
        try:
            for post in posts:
                requested_at = timezone.now()
                store_post_metrics(
                    post,
                    client.get_post_metrics(post.topic_id),
                    source_requested_at=requested_at,
                )
        finally:
            client.close()
        self.stdout.write(f"已刷新 {len(posts)} 条原创帖互动指标。")
