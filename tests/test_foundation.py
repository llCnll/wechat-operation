import importlib
import json
import logging

import pytest
from django.db import connection
from django.test import Client

import config.settings
from config.settings import SensitiveDataFilter


def test_health_check_returns_ok():
    response = Client().get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_sensitive_log_message_is_redacted():
    record = logging.LogRecord("test", logging.INFO, "", 0, "token=private", (), None)
    assert SensitiveDataFilter().filter(record)
    assert record.getMessage() == "[已脱敏的日志消息]"


def test_json_formatter_emits_safe_structured_log():
    formatter = config.settings.JsonFormatter()
    record = logging.LogRecord("test", logging.INFO, "", 0, "completed", (), None)
    record.topic_id = 123
    record.video_stage = "frame_extract"
    record.process_exit_code = 9
    payload = json.loads(formatter.format(record))
    assert payload["message"] == "completed"
    assert payload["topic_id"] == 123
    assert payload["video_stage"] == "frame_extract"
    assert payload["process_exit_code"] == 9


@pytest.mark.django_db(transaction=True)
def test_sqlite_connection_is_initialized_with_required_pragmas():
    with connection.cursor() as cursor:
        cursor.execute("PRAGMA foreign_keys")
        foreign_keys = cursor.fetchone()[0]
        cursor.execute("PRAGMA busy_timeout")
        busy_timeout = cursor.fetchone()[0]
    assert foreign_keys == 1
    assert busy_timeout == config.settings.runtime.sqlite_busy_timeout_ms


def test_invalid_runtime_configuration_is_rejected(monkeypatch):
    monkeypatch.setenv("SQLITE_BUSY_TIMEOUT_MS", "0")
    with pytest.raises(RuntimeError, match="运行时配置无效"):
        importlib.reload(config.settings)
    monkeypatch.delenv("SQLITE_BUSY_TIMEOUT_MS")
