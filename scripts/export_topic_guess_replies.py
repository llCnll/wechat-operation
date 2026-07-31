# ruff: noqa: E402, I001
"""Export matching top-level topic replies through the fixed local session IPC.

This is a read-only, operator-run script.  It neither writes platform state nor
persists comments in the application database.  The Supervisor IPC capability
is read only in memory for this invocation and is never printed or written to
the export.  Minimum verification is the first page: the fixed IPC validates
the topic, page number, and 1--500 page size before any CSV is written.
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

DISPLAY_TIMEZONE = ZoneInfo("Asia/Shanghai")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="导出命中关键词与游戏 ID 的帖子顶层评论")
    parser.add_argument("--topic-id", type=int, required=True)
    parser.add_argument("--topic-openid", required=True, help="帖子作者 OpenID")
    parser.add_argument("--keyword", required=True, help="评论必须包含的关键词")
    parser.add_argument("--limit", type=int, default=500, choices=range(1, 501))
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def game_id_from(content: str) -> str | None:
    """Return the first numeric run from a keyword-matching reply."""
    match = re.search(r"\d+", content)
    return match.group(0) if match else None


def format_create_time(value: object) -> str:
    """Format a source Unix timestamp for the operator's configured timezone."""
    return datetime.fromtimestamp(int(value), tz=DISPLAY_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")


def fail(message: str) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(1)


def main() -> None:
    args = parse_args()
    if args.topic_id <= 0:
        fail("帖子 ID 必须为正整数")
    if not args.topic_openid.strip() or not args.keyword.strip():
        fail("帖子作者 OpenID 与关键词不能为空")

    django.setup()
    from django.conf import settings

    from config.game_circle_ipc import (
        GameCircleIpcClient,
        GameCircleIpcError,
        GameCirclePostClient,
    )
    from posts.game_catalog import current_execution_scope
    from service_supervisor.runtime import RuntimeStore

    output = args.output.resolve()
    if output.exists():
        fail("导出文件已存在；请指定新的输出路径，避免覆盖现有文件")
    output.parent.mkdir(parents=True, exist_ok=True)
    capability = RuntimeStore(settings.RUNTIME_DIR).game_circle_ipc_token()
    scope = current_execution_scope(persist_default=False)
    source = GameCirclePostClient(scope=scope, ipc=GameCircleIpcClient(capability=capability))

    page_number = 0
    seen_page_numbers: set[int] = set()
    seen_reply_ids: set[str] = set()
    pages = 0
    read_count = 0
    matches: list[dict[str, Any]] = []
    try:
        while True:
            if page_number in seen_page_numbers:
                fail("平台评论分页页码重复，已停止以避免重复读取")
            if page_number >= 10_000:
                fail("评论分页超过安全上限，已停止以避免持续读取")
            seen_page_numbers.add(page_number)
            reply_page = source.list_topic_replies(
                topic_id=args.topic_id,
                topic_openid=args.topic_openid.strip(),
                page=page_number,
                limit=args.limit,
            )
            pages += 1
            items = reply_page["items"]
            for item in items:
                reply_id = item["reply_id"]
                if reply_id and reply_id in seen_reply_ids:
                    continue
                if reply_id:
                    seen_reply_ids.add(reply_id)
                read_count += 1
                content = item["content"]
                game_id = game_id_from(content)
                if args.keyword in content and game_id:
                    matches.append(
                        {
                            "openid": item["openid"],
                            "nick_name": item["nick_name"],
                            "create_time": format_create_time(item["create_time"]),
                            "content": content,
                            "game_id": game_id,
                        }
                    )
            if not reply_page["has_next"]:
                break
            # The API names this field next_offset, but live verification shows
            # cur_page is a zero-based page number: page 0 -> next_offset 100,
            # page 1 -> next_offset 200 for limit 100.  Do not feed the offset
            # back as cur_page or the platform returns an empty page.
            page_number += 1
    except GameCircleIpcError as exc:
        fail(f"读取失败（{exc.category}）：{exc}")

    with output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["openid", "nick_name", "create_time", "content", "game_id"],
        )
        writer.writeheader()
        writer.writerows(matches)
    print(
        f"完成：页数 {pages}，读取评论 {read_count}，去重后评论 {len(seen_reply_ids)}，"
        f"命中 {len(matches)}，导出文件 {output}"
    )


if __name__ == "__main__":
    main()
