import sys
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi.testclient import TestClient

LINKER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LINKER))
USER = "test-admin"
PASSWORD = "Correct-Horse-Example-42!"


@pytest.fixture
def isolated_app(monkeypatch, tmp_path):
    monkeypatch.chdir(LINKER)
    monkeypatch.setenv("LINKER_USER", USER)
    monkeypatch.setenv("LINKER_PASS", PASSWORD)
    monkeypatch.setenv("PROJECT_DIR", str(tmp_path))
    monkeypatch.setenv("HOST_HOME", str(tmp_path))
    # Settings load at import time; never inspect the host's /data files.
    with patch.object(Path, "exists", return_value=False):
        from app import cleanup, linking, main, updater
    monkeypatch.setattr(cleanup, "SETTINGS_PATH", tmp_path / "cleanup.json")
    monkeypatch.setattr(updater, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(main, "STATE", linking.LinkerState())
    monkeypatch.setattr(main, "SETTINGS", updater.Settings())
    monkeypatch.setattr(main, "CLEANUP_SETTINGS", cleanup.CleanupSettings())
    for name in ("run_linking", "auto_update_loop", "cleanup_loop"):
        monkeypatch.setattr(main, name, AsyncMock())
    for name, value in {
        "run_cleanup_now": (True, "synthetic cleanup"),
        "update_cleanup_settings": cleanup.CleanupSettings().as_dict(),
        "use_drive": {"ok": False, "error": "synthetic"},
        "set_plex_claim": {"ok": False, "error": "synthetic"},
        "list_drives": [],
        "get_connection_info": {},
    }.items():
        monkeypatch.setattr(main, name, Mock(return_value=value))
    monkeypatch.setattr(main, "trigger_update", AsyncMock(return_value=(True, "synthetic")))
    monkeypatch.setattr(main, "render_name_map", AsyncMock(return_value="synthetic_metric 1"))
    monkeypatch.setattr(main, "recreate_services", AsyncMock(return_value=(True, "synthetic")))
    monkeypatch.setattr(main.SETTINGS, "save", Mock())
    return main


@pytest.fixture
def client(isolated_app):
    with TestClient(isolated_app.app) as client:
        yield client
