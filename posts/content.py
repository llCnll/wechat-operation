"""Canonical, local-only decomposition of original-post text."""

from __future__ import annotations

import re

_TRAILING_TOPIC = re.compile(r"^#([^\s#]{1,80})$")


def normalize_topic_label(value: str) -> str:
    """Normalize an optional operator-supplied topic label for exact lookup."""
    candidate = value.strip()
    if candidate.startswith("#"):
        candidate = candidate[1:].strip()
    if not candidate:
        return ""
    if _TRAILING_TOPIC.fullmatch(f"#{candidate}") is None:
        raise ValueError("话题格式无效")
    return candidate


def split_trailing_topics(text: str) -> tuple[str, tuple[str, ...]]:
    """Return body text and ordered unique, final-line topic labels."""
    lines = text.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    collected: list[str] = []
    while lines:
        match = _TRAILING_TOPIC.fullmatch(lines[-1].strip())
        if match is None:
            break
        collected.append(match.group(1))
        lines.pop()
    topics: list[str] = []
    for value in reversed(collected):
        if value not in topics:
            topics.append(value)
    return "\n".join(lines).strip(), tuple(topics)
