"""Django settings for the internal content operations service."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from pydantic import Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from config import database  # noqa: F401  Registers SQLite connection initialization.

BASE_DIR = Path(__file__).resolve().parent.parent
RUNTIME_DIR = BASE_DIR / "runtime"
FIXED_INGESTION_PAGE_SIZE = 100


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
    public_opinion_conclusion_enabled: bool = False
    ocr_base_url: str = ""
    ocr_api_key: str = ""
    ocr_model: str = ""
    asr_base_url: str = ""
    asr_api_key: str = ""
    asr_model: str = ""
    asr_timeout_seconds: float = Field(default=60, ge=1, le=300)
    asr_enabled: bool = False
    video_enabled: bool = False
    video_preflight_verified: bool = False
    video_media_tools_dir: str = ""
    video_max_bytes: int = Field(default=50 * 1024 * 1024, ge=1_048_576, le=200 * 1024 * 1024)
    video_max_duration_seconds: int = Field(default=30, ge=1, le=60)
    video_max_per_post: int = Field(default=3, ge=1, le=10)
    video_max_frame_edge: int = Field(default=960, ge=240, le=1920)
    video_dhash_threshold: int = Field(default=24, ge=0, le=32)
    video_ocr_frame_budget: int = Field(default=60, ge=1, le=120)
    video_connect_timeout_seconds: float = Field(default=10, ge=1, le=60)
    video_read_timeout_seconds: float = Field(default=30, ge=1, le=180)
    video_processing_timeout_seconds: float = Field(default=120, ge=10, le=600)
    enable_upstream_writes: bool = False
    enable_sink_writes: bool = False
    enable_block_writes: bool = False
    enable_mute_writes: bool = False
    enable_unmute_writes: bool = False
    enable_restore_writes: bool = False
    enable_ai_automated_moderation: bool = False
    enable_ai_sink_writes: bool = False
    enable_ai_block_writes: bool = False
    default_game_appid: str = ""
    controlled_chrome_bin: str = ""
    controlled_chrome_user_data_dir: Path = RUNTIME_DIR / "controlled-chrome-profile"
    controlled_chrome_debug_port: int = Field(default=9222, ge=1024, le=65535)
    game_circle_ipc_port: int = Field(default=9364, ge=1024, le=65535)
    game_circle_ipc_timeout_seconds: float = Field(default=30, ge=1, le=120)
    game_circle_session_retire_seconds: int = Field(default=10, ge=1, le=60)
    # Zero means "unlimited" for the current verified read-only rollout.  The
    # fields are retained so a future upstream rate-limit finding can be applied
    # per service deployment without redesigning the lease contract.
    game_circle_max_concurrent_accesses: int = Field(default=0, ge=0, le=1000)
    game_circle_max_requests_per_second: float = Field(default=0, ge=0, le=1000)
    scheduled_ingestion_interval_minutes: int = Field(default=15, ge=1, le=1440)
    scheduled_ingestion_lookback_minutes: int = Field(default=30, ge=1, le=24 * 60)
    scheduled_ingestion_page_size: int = Field(default=FIXED_INGESTION_PAGE_SIZE, ge=1, le=100)


try:
    runtime = RuntimeSettings()
except ValidationError as exc:
    raise RuntimeError("运行时配置无效；请检查非敏感配置值。") from exc

if not runtime.database_path.is_absolute():
    runtime.database_path = BASE_DIR / runtime.database_path
if not runtime.controlled_chrome_user_data_dir.is_absolute():
    runtime.controlled_chrome_user_data_dir = BASE_DIR / runtime.controlled_chrome_user_data_dir

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
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "config.context_processors.admin_console_overview",
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
STATIC_URL = "/static/"
STATIC_ROOT = RUNTIME_DIR / "static"
STORAGES = {
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}
WHITENOISE_MANIFEST_STRICT = False
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
LOGIN_URL = "/admin/login/"
SCHEDULED_INGESTION_INTERVAL_MINUTES = runtime.scheduled_ingestion_interval_minutes
SCHEDULED_INGESTION_LOOKBACK_MINUTES = runtime.scheduled_ingestion_lookback_minutes
SCHEDULED_INGESTION_PAGE_SIZE = runtime.scheduled_ingestion_page_size
DEFAULT_GAME_APPID = runtime.default_game_appid
CONTROLLED_CHROME_BIN = runtime.controlled_chrome_bin
CONTROLLED_CHROME_USER_DATA_DIR = runtime.controlled_chrome_user_data_dir
CONTROLLED_CHROME_DEBUG_PORT = runtime.controlled_chrome_debug_port
CONTROLLED_CHROME_CDP_URL = f"http://127.0.0.1:{CONTROLLED_CHROME_DEBUG_PORT}"
GAME_CIRCLE_IPC_PORT = runtime.game_circle_ipc_port
GAME_CIRCLE_IPC_TIMEOUT_SECONDS = runtime.game_circle_ipc_timeout_seconds
GAME_CIRCLE_SESSION_RETIRE_SECONDS = runtime.game_circle_session_retire_seconds
GAME_CIRCLE_MAX_CONCURRENT_ACCESSES = runtime.game_circle_max_concurrent_accesses
GAME_CIRCLE_MAX_REQUESTS_PER_SECOND = runtime.game_circle_max_requests_per_second


class SensitiveDataFilter(logging.Filter):
    """Replace sensitive tokens before a record is serialized by any handler."""

    sensitive_terms = ("token=", "cookie", "authorization", "openid", "dis_k=", "dis_t=")

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if (
            any(term in message.lower() for term in self.sensitive_terms)
            or "http" in message.lower()
        ):
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
        for key in (
            "correlation_id",
            "task_id",
            "topic_id",
            "adapter",
            "result_code",
            "video_stage",
            "process_exit_code",
        ):
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
