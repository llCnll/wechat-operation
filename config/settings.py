"""Django settings for the internal content operations service."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Literal

from pydantic import Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from config import database  # noqa: F401  Registers SQLite connection initialization.

BASE_DIR = Path(__file__).resolve().parent.parent
RUNTIME_DIR = BASE_DIR / "runtime"


class RuntimeSettings(BaseSettings):
    """Environment-derived runtime configuration without secret persistence."""

    model_config = SettingsConfigDict(env_file=RUNTIME_DIR / ".env", extra="ignore")

    django_secret_key: str = Field(min_length=32, default="development-only-secret-key-change-me")
    django_debug: bool = False
    django_allowed_hosts: str = "localhost,127.0.0.1,testserver"
    database_path: Path = RUNTIME_DIR / "operations.sqlite3"
    sqlite_busy_timeout_ms: int = Field(default=5000, ge=100, le=60000)
    log_level: str = "INFO"
    ai_base_url: str = ""
    ai_api_key: str = ""
    ai_model: str = ""
    ai_timeout_seconds: float = Field(default=30, ge=1, le=120)
    ocr_base_url: str = ""
    ocr_api_key: str = ""
    ocr_model: str = ""
    enable_upstream_writes: bool = False
    enable_sink_writes: bool = False
    enable_block_writes: bool = False
    enable_mute_writes: bool = False
    enable_unmute_writes: bool = False
    enable_restore_writes: bool = False
    scheduled_ingestion_appid: Literal["wx6d2454a8afcd9706"] = "wx6d2454a8afcd9706"
    scheduled_ingestion_interval_minutes: Literal[15] = 15
    scheduled_ingestion_lookback_minutes: Literal[30] = 30
    scheduled_ingestion_page_size: Literal[20] = 20


try:
    runtime = RuntimeSettings()
except ValidationError as exc:
    raise RuntimeError("运行时配置无效；请检查非敏感配置值。") from exc

if not runtime.database_path.is_absolute():
    runtime.database_path = BASE_DIR / runtime.database_path

RUNTIME_DIR.mkdir(parents=True, exist_ok=True)

SECRET_KEY = runtime.django_secret_key
DEBUG = runtime.django_debug
ALLOWED_HOSTS = [host.strip() for host in runtime.django_allowed_hosts.split(",") if host.strip()]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "jobs",
    "posts",
    "reviews",
    "reporting",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]
WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": runtime.database_path,
        "OPTIONS": {"timeout": runtime.sqlite_busy_timeout_ms / 1000},
    },
}

AUTH_PASSWORD_VALIDATORS: list[dict[str, str]] = []
LANGUAGE_CODE = "zh-hans"
TIME_ZONE = "Asia/Shanghai"
USE_I18N = True
USE_TZ = True
STATIC_URL = "static/"
STATIC_ROOT = RUNTIME_DIR / "static"
STORAGES = {
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}
WHITENOISE_MANIFEST_STRICT = False
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
LOGIN_URL = "/admin/login/"
SCHEDULED_INGESTION_APPID = runtime.scheduled_ingestion_appid
SCHEDULED_INGESTION_INTERVAL_MINUTES = runtime.scheduled_ingestion_interval_minutes
SCHEDULED_INGESTION_LOOKBACK_MINUTES = runtime.scheduled_ingestion_lookback_minutes
SCHEDULED_INGESTION_PAGE_SIZE = runtime.scheduled_ingestion_page_size


class SensitiveDataFilter(logging.Filter):
    """Replace sensitive tokens before a record is serialized by any handler."""

    sensitive_terms = ("token=", "cookie", "authorization", "openid")

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if any(term in message.lower() for term in self.sensitive_terms):
            record.msg = "[已脱敏的日志消息]"
            record.args = ()
        return True


class JsonFormatter(logging.Formatter):
    """Emit stable JSON logs suitable for collection by the deployment platform."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in ("correlation_id", "task_id", "topic_id", "adapter", "result_code"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        return json.dumps(payload, ensure_ascii=False)


LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {"sensitive": {"()": SensitiveDataFilter}},
    "formatters": {
        "structured": {"()": JsonFormatter},
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "filters": ["sensitive"],
            "formatter": "structured",
        },
    },
    "root": {"handlers": ["console"], "level": os.getenv("LOG_LEVEL", runtime.log_level)},
}
