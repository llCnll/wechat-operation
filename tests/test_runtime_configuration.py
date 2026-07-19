from config.runtime_config import affected_services, public_runtime_config, update_runtime_config


def test_runtime_configuration_preserves_blank_secrets(tmp_path):
    path = tmp_path / ".env"
    path.write_text("AI_API_KEY=existing-key\nAI_MODEL=old\n", encoding="utf-8")

    changed = update_runtime_config({"AI_API_KEY": "", "AI_MODEL": "new"}, path)

    assert changed == {"AI_MODEL"}
    assert "existing-key" not in str(public_runtime_config(path))
    assert public_runtime_config(path)["ai_api_key_configured"] is True
    assert "AI_API_KEY=existing-key" in path.read_text(encoding="utf-8")


def test_runtime_configuration_maps_restarts_to_affected_services():
    assert affected_services({"AI_MODEL"}) == {"review"}
    assert affected_services({"ENABLE_BLOCK_WRITES"}) == {"web"}


def test_runtime_configuration_explicitly_disables_unchecked_write_switches(tmp_path):
    path = tmp_path / ".env"
    path.write_text("ENABLE_BLOCK_WRITES=true\n", encoding="utf-8")

    changed = update_runtime_config({"ENABLE_BLOCK_WRITES": "false"}, path)

    assert changed == {"ENABLE_BLOCK_WRITES"}
    assert "ENABLE_BLOCK_WRITES=false" in path.read_text(encoding="utf-8")
