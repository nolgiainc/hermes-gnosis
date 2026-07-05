"""Config loading/saving tests: env fallback, gnosis.json overrides, token."""

from __future__ import annotations

import json

from hermes_gnosis._config import load_config, save_config_file


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for var in ("GNOSIS_URL", "GNOSIS_SERVICE_TOKEN", "GNOSIS_USER_ID",
                "GNOSIS_AGENT_ID", "GNOSIS_TENANT_ID", "GNOSIS_TIMEOUT",
                "GNOSIS_ADD_TIMEOUT"):
        monkeypatch.delenv(var, raising=False)


def test_defaults(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    cfg = load_config()
    assert cfg["gnosis_url"] == ""
    assert cfg["gnosis_token"] == ""
    assert cfg["agent_id"] == "hermes"
    assert cfg["tenant_id"] == "bromigos"
    assert cfg["timeout"] == 10.0
    assert cfg["add_timeout"] == 30.0
    assert "user_id" not in cfg  # unset → gateway-native id may flow through


def test_recall_mode_default_and_overrides(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    # Default is the full read-pipeline recall.
    assert load_config()["recall_mode"] == "context"
    # Env var provides the default...
    monkeypatch.setenv("GNOSIS_RECALL_MODE", "search")
    assert load_config()["recall_mode"] == "search"
    # ...and gnosis.json overrides it.
    (tmp_path / "gnosis.json").write_text(json.dumps({"recall_mode": "context"}))
    assert load_config()["recall_mode"] == "context"


def test_json_overrides_env(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("GNOSIS_URL", "https://env.example")
    (tmp_path / "gnosis.json").write_text(json.dumps({
        "gnosis_url": "https://file.example",
        "user_id": "lesse",
        "timeout": 3,
    }))
    cfg = load_config()
    assert cfg["gnosis_url"] == "https://file.example"
    assert cfg["user_id"] == "lesse"
    assert cfg["timeout"] == 3.0


def test_env_token_preferred_over_plaintext(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    (tmp_path / "gnosis.json").write_text(json.dumps({
        "gnosis_token": "plaintext-token",
    }))
    monkeypatch.setenv("GNOSIS_SERVICE_TOKEN", "env-token")
    assert load_config()["gnosis_token"] == "env-token"
    # Without the env var, the file token is still honored as a fallback.
    monkeypatch.delenv("GNOSIS_SERVICE_TOKEN")
    assert load_config()["gnosis_token"] == "plaintext-token"


def test_save_config_merges_and_strips_secret(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    (tmp_path / "gnosis.json").write_text(json.dumps({"user_id": "keepme"}))
    save_config_file(
        {"gnosis_url": "https://g.example", "gnosis_token": "secret"},
        str(tmp_path),
    )
    saved = json.loads((tmp_path / "gnosis.json").read_text())
    assert saved["gnosis_url"] == "https://g.example"
    assert saved["user_id"] == "keepme"
    assert "gnosis_token" not in saved


def test_is_available(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    from hermes_gnosis import GnosisMemoryProvider
    provider = GnosisMemoryProvider()
    assert provider.is_available() is False
    monkeypatch.setenv("GNOSIS_URL", "https://g.example")
    assert provider.is_available() is False
    monkeypatch.setenv("GNOSIS_SERVICE_TOKEN", "t")
    assert provider.is_available() is True


def test_initialize_user_id_resolution(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("GNOSIS_URL", "https://g.example")
    monkeypatch.setenv("GNOSIS_SERVICE_TOKEN", "t")
    from hermes_gnosis import GnosisMemoryProvider

    # Don't hit the network for the startup top-memories warmup.
    monkeypatch.setattr(
        GnosisMemoryProvider, "_start_top_memories_fetch", lambda self: None,
    )

    # Gateway-native id wins when no operator-configured user_id.
    provider = GnosisMemoryProvider()
    provider.initialize("sess-1", user_id="tg-12345", platform="telegram")
    assert provider._user_id == "tg-12345"
    assert provider._channel == "telegram"
    provider.shutdown()

    # The literal default placeholder is treated as unset.
    (tmp_path / "gnosis.json").write_text(json.dumps({"user_id": "hermes-user"}))
    provider = GnosisMemoryProvider()
    provider.initialize("sess-1", user_id="tg-12345")
    assert provider._user_id == "tg-12345"
    provider.shutdown()

    # Operator-configured id wins over gateway-native id.
    (tmp_path / "gnosis.json").write_text(json.dumps({"user_id": "lesse"}))
    provider = GnosisMemoryProvider()
    provider.initialize("sess-1", user_id="tg-12345")
    assert provider._user_id == "lesse"
    provider.shutdown()
