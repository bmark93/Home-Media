"""
Container auto-update toggle, backed by Watchtower's HTTP API mode.

Watchtower itself runs with no polling interval (WATCHTOWER_HTTP_API_UPDATE=true
and no --interval), so it never updates anything on its own - it only checks
for updates when its HTTP endpoint is called. This module owns that trigger:

- the desired on/off state is persisted to a small JSON file so it survives
  a container restart
- a background loop periodically calls Watchtower's endpoint, but only if
  the toggle is currently on
- a manual "update now" trigger works regardless of the toggle, for a one-off
  check from the UI
"""
from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from pathlib import Path

import httpx

from app.linking import STATE

SETTINGS_PATH = Path("/data/settings.json")
WATCHTOWER_URL = "http://watchtower:8080"
CHECK_INTERVAL_SECONDS = 6 * 60 * 60  # re-check the toggle every 6 hours


@dataclass
class Settings:
    auto_update: bool = False

    @classmethod
    def load(cls) -> "Settings":
        if SETTINGS_PATH.exists():
            try:
                data = json.loads(SETTINGS_PATH.read_text())
                return cls(auto_update=bool(data.get("auto_update", False)))
            except (json.JSONDecodeError, OSError):
                pass
        return cls()

    def save(self) -> None:
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_PATH.write_text(json.dumps({"auto_update": self.auto_update}))


SETTINGS = Settings.load()


async def trigger_update() -> tuple[bool, str]:
    token = os.environ.get("WATCHTOWER_API_TOKEN", "")
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(f"{WATCHTOWER_URL}/v1/update", headers=headers)
        if resp.status_code == 200:
            return True, "update check triggered"
        return False, f"error ({resp.status_code}): {resp.text[:200]}"
    except httpx.HTTPError as exc:
        return False, f"could not reach Watchtower: {exc}"


async def auto_update_loop() -> None:
    # Small initial delay so this doesn't race the very first linking run.
    await asyncio.sleep(60)
    while True:
        if SETTINGS.auto_update:
            ok, msg = await trigger_update()
            STATE.note(f"Auto-update: {msg}" if ok else f"Auto-update failed: {msg}")
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)
