import pytest

from config.runtime_config import (
    affected_services,
    display_service_names,
    public_runtime_config,
    update_runtime_config,
)


def test_runtime_configuration_preserves_blank_secrets(tmp_path):
    path = tmp_path / ".env"
    path.write_text("AI_API_KEY=existing-key\nAI_MODEL=old\n", encoding="utf-8")

    changed = update_runtime_config({"AI_API_KEY": "", "AI_MODEL": "new"}, path)

    assert changed == {"AI_MODEL"}
    assert "existing-key" not in str(public_runtime_config(path))
    assert public_runtime_config(path)["ai_api_key_configured"] is True
    assert "AI_API_KEY=existing-key" in path.read_text(encoding="utf-8")


def test_runtime_configuration_restores_safe_video_defaults_for_blank_numeric_values(tmp_path):
    path = tmp_path / ".env"
    path.write_text("VIDEO_MAX_DURATION_SECONDS=\nVIDEO_DHASH_THRESHOLD=\n", encoding="utf-8")

    config = public_runtime_config(path)
    changed = update_runtime_config(
        {"VIDEO_MAX_DURATION_SECONDS": "", "VIDEO_DHASH_THRESHOLD": ""}, path
    )

    assert config["video_max_duration_seconds"] == "30"
    assert config["video_dhash_threshold"] == "24"
    assert changed == {"VIDEO_MAX_DURATION_SECONDS", "VIDEO_DHASH_THRESHOLD"}
    assert "VIDEO_MAX_DURATION_SECONDS=30" in path.read_text(encoding="utf-8")
    assert "VIDEO_DHASH_THRESHOLD=24" in path.read_text(encoding="utf-8")


def test_runtime_configuration_keeps_asr_disabled_without_an_explicit_opt_in(tmp_path):
    path = tmp_path / ".env"

    assert public_runtime_config(path)["asr_enabled"] == "false"
    assert update_runtime_config({"ASR_ENABLED": "true"}, path) == {"ASR_ENABLED"}
    assert "ASR_ENABLED=true" in path.read_text(encoding="utf-8")


def test_runtime_configuration_rejects_out_of_range_video_values(tmp_path):
    path = tmp_path / ".env"

    with pytest.raises(ValueError, match="VIDEO_MAX_DURATION_SECONDS"):
        update_runtime_config({"VIDEO_MAX_DURATION_SECONDS": "61"}, path)

    assert not path.exists()


def test_runtime_configuration_maps_restarts_to_affected_services():
    assert affected_services({"AI_MODEL"}) == {"public_opinion_reporting", "review"}
    assert affected_services({"ENABLE_BLOCK_WRITES"}) == {"web", "ai_moderation"}
    assert affected_services({"ENABLE_AI_SINK_WRITES"}) == {"web", "ai_moderation"}
    assert affected_services({"SESSION_REPAIR_ENABLED"}) == {"game_circle_session", "scheduler"}
    assert affected_services({"SESSION_KEEPALIVE_ENABLED"}) == set()


def test_runtime_configuration_displays_affected_services_with_console_labels():
    assert display_service_names({"web", "scheduler", "game_circle_session", "ai_moderation"}) == [
        "AI 自动处置 Worker",
        "游戏圈会话服务",
        "自动调度器",
        "Web 服务",
    ]


def test_runtime_configuration_explicitly_disables_unchecked_write_switches(tmp_path):
    path = tmp_path / ".env"
    path.write_text("ENABLE_BLOCK_WRITES=true\n", encoding="utf-8")

    changed = update_runtime_config({"ENABLE_BLOCK_WRITES": "false"}, path)

    assert changed == {"ENABLE_BLOCK_WRITES"}
    assert "ENABLE_BLOCK_WRITES=false" in path.read_text(encoding="utf-8")


def test_ai_automation_switches_are_publicly_visible_but_default_to_disabled(tmp_path):
    config = public_runtime_config(tmp_path / ".env")

    assert config["enable_ai_automated_moderation"] == "false"
    assert config["enable_ai_sink_writes"] == "false"
    assert config["enable_ai_block_writes"] == "false"
