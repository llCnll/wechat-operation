from django.core.management.base import BaseCommand

from posts.models import OriginalPost, is_post_deleted
from reviews.services import enqueue_evaluation_pipeline


class Command(BaseCommand):
    help = "为已有原创帖补建 OCR 与 AI 审核流水线任务。"

    def add_arguments(self, parser):
        parser.add_argument("--topic-id", type=int)
        parser.add_argument("--limit", type=int, default=100)
        parser.add_argument("--force", action="store_true")

    def handle(self, *args, **options):
        posts = OriginalPost.objects.all().order_by("-updated_at")
        if options["topic_id"]:
            posts = posts.filter(topic_id=options["topic_id"])
        queued = skipped = 0
        for post in posts[: options["limit"]]:
            if is_post_deleted(post.status):
                skipped += 1
                continue
            try:
                enqueue_evaluation_pipeline(post, force=options["force"])
            except ValueError:
                skipped += 1
            else:
                queued += 1
        self.stdout.write(f"已请求 {queued} 条评估流水线，跳过 {skipped} 条。")
