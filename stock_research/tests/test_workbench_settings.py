"""API contracts for local workbench settings and credential handling."""

from pathlib import Path
import stat

import pytest
from fastapi.testclient import TestClient

from stock_research.api.app import create_app
from stock_research.config import get_settings

SETTINGS_URL = "/api/workbench/settings"


def test_saved_settings_override_environment_without_returning_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STOCK_RESEARCH_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://default.example/v1")
    monkeypatch.setenv("DEEPSEEK_MODEL", "environment-model")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "environment-model-key")
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    client = TestClient(create_app(), base_url="http://127.0.0.1", client=("127.0.0.1", 50000))

    blocked = client.patch(SETTINGS_URL, json={"deepseek_base_url": "https://other.example/v1"})
    assert blocked.status_code == 422
    assert not (tmp_path / "web-settings.json").exists()

    saved = client.patch(SETTINGS_URL, json={
        "deepseek_base_url": "https://other.example/v1/",
        "deepseek_model": "saved-model",
        "deepseek_api_key": "fixture-model-key",
        "finnhub_api_key": "fixture-data-key",
        "llm_threads": 7,
    })
    assert saved.status_code == 200
    body = saved.json()
    assert body["has_deepseek_api_key"] and body["has_finnhub_api_key"]
    assert "fixture-" not in saved.text
    assert "fixture-" not in client.get(SETTINGS_URL).text
    assert stat.S_IMODE((tmp_path / "web-settings.json").stat().st_mode) & 0o077 == 0
    assert get_settings(ensure_dirs=False).finnhub_api_key == "fixture-data-key"
    assert get_settings(ensure_dirs=False).llm_threads == 7
    assert client.get("/api/agent/settings").json() == {
        "base_url": "https://other.example/v1", "model": "saved-model", "has_api_key": True,
    }

    restored = client.patch(SETTINGS_URL, json={"deepseek_api_key": None, "finnhub_api_key": None})
    assert restored.status_code == 200
    assert not restored.json()["has_deepseek_api_key"]
    assert not restored.json()["has_finnhub_api_key"]
    client.patch(SETTINGS_URL, json={"deepseek_base_url": None, "deepseek_model": None}).raise_for_status()
    assert get_settings(ensure_dirs=False).deepseek_api_key == "environment-model-key"


def test_settings_enforce_loopback_and_do_not_echo_invalid_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STOCK_RESEARCH_RUNTIME_DIR", str(tmp_path))
    app = create_app()
    local = TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000))
    remote = TestClient(app, base_url="http://127.0.0.1", client=("192.0.2.5", 50000))
    assert remote.get(SETTINGS_URL).status_code == 403
    assert remote.patch(SETTINGS_URL, json={"llm_threads": 5}).status_code == 403
    assert local.get(SETTINGS_URL, headers={"Host": "research.example"}).status_code == 403
    assert local.patch(
        SETTINGS_URL, json={"llm_threads": 5}, headers={"Origin": "https://research.example"},
    ).status_code == 403

    invalid = local.patch(SETTINGS_URL, json={"deepseek_api_key": "fixture invalid key"})
    assert invalid.status_code == 422
    assert "fixture invalid key" not in invalid.text
    assert not (tmp_path / "web-settings.json").exists()
