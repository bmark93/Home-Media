"""
Scheduled downloads-folder cleanup.

Sonarr/Radarr import completed downloads into /movies and /tv (hardlink or
copy), but the original stays behind in DOWNLOADS_PATH - so every finished
download exists on disk twice until something removes the original. This
module runs on a user-configured schedule (daily / weekly / monthly, with a
time of day) and simply empties the contents of DOWNLOADS_PATH.

This intentionally breaks seeding for anything it deletes: qBittorrent keeps
listing the torrent as seeding, but once its file is gone from disk it can no
longer serve it to peers - qBittorrent has no way to detect a file deleted
out from under it and won't pause/remove the torrent on its own. The UI
surfaces this warning; this module does not try to talk to qBittorrent about
it (e.g. to stop the torrent first), since that would still leave the data
gone, just with extra API calls in front of it.
"""
from __future__ import annotations

import asyncio
import calendar
import json
import shutil
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path

from app.linking import STATE
from app.storage import resolve_downloads_path

SETTINGS_PATH = Path("/data/cleanup_settings.json")
CHECK_INTERVAL_SECONDS = 5 * 60


@dataclass
class CleanupSettings:
    enabled: bool = False
    frequency: str = "weekly"  # "daily" | "weekly" | "monthly"
    time: str = "03:00"  # HH:MM, in the container's TZ (see docker-compose.yml)
    weekly_day: int = 0  # 0=Monday .. 6=Sunday
    monthly_mode: str = "day_of_month"  # "day_of_month" | "nth_weekday" | "last_day"
    monthly_day: int = 1  # 1-31, clamped to the last day of shorter months
    monthly_nth: int = 1  # 1-4, or -1 for "last"
    monthly_weekday: int = 1  # 0=Monday .. 6=Sunday
    last_run: str | None = None
    next_run: str | None = None

    @classmethod
    def load(cls) -> "CleanupSettings":
        if SETTINGS_PATH.exists():
            try:
                data = json.loads(SETTINGS_PATH.read_text())
                known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
                return cls(**known)
            except (json.JSONDecodeError, OSError, TypeError):
                pass
        return cls()

    def save(self) -> None:
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_PATH.write_text(json.dumps(self.__dict__))

    def as_dict(self) -> dict:
        return dict(self.__dict__)


SETTINGS = CleanupSettings.load()

EDITABLE_FIELDS = (
    "enabled",
    "frequency",
    "time",
    "weekly_day",
    "monthly_mode",
    "monthly_day",
    "monthly_nth",
    "monthly_weekday",
)


def _parse_time(value: str) -> dtime:
    try:
        h, m = value.split(":")
        return dtime(hour=int(h), minute=int(m))
    except (ValueError, AttributeError):
        return dtime(hour=3, minute=0)


def _date_matches(d: date, s: CleanupSettings) -> bool:
    if s.frequency == "daily":
        return True
    if s.frequency == "weekly":
        return d.weekday() == s.weekly_day
    if s.frequency == "monthly":
        days_in_month = calendar.monthrange(d.year, d.month)[1]
        if s.monthly_mode == "last_day":
            return d.day == days_in_month
        if s.monthly_mode == "day_of_month":
            return d.day == min(s.monthly_day, days_in_month)
        if s.monthly_mode == "nth_weekday":
            if d.weekday() != s.monthly_weekday:
                return False
            if s.monthly_nth == -1:
                return d.day + 7 > days_in_month
            return (d.day - 1) // 7 + 1 == s.monthly_nth
    return False


def compute_next_run(s: CleanupSettings, after: datetime) -> datetime:
    """First matching schedule instant strictly after `after`."""
    t = _parse_time(s.time)
    d = after.date()
    if datetime.combine(d, t) <= after:
        d += timedelta(days=1)
    for _ in range(400):  # covers any monthly rule at least once
        if _date_matches(d, s):
            return datetime.combine(d, t)
        d += timedelta(days=1)
    return after + timedelta(days=1)  # unreachable in practice


def _refresh_next_run() -> None:
    SETTINGS.next_run = compute_next_run(SETTINGS, datetime.now()).isoformat() if SETTINGS.enabled else None
    SETTINGS.save()


def update_settings(patch: dict) -> dict:
    for key in EDITABLE_FIELDS:
        if key in patch:
            setattr(SETTINGS, key, patch[key])
    _refresh_next_run()
    return SETTINGS.as_dict()


def run_cleanup_now() -> tuple[bool, str]:
    downloads = resolve_downloads_path()
    if downloads is None:
        msg = "could not resolve DOWNLOADS_PATH from .env"
        STATE.note(f"Cleanup: {msg}, skipping")
        return False, msg
    if not downloads.is_dir():
        msg = f"{downloads} is not a directory"
        STATE.note(f"Cleanup: {msg}, skipping")
        return False, msg

    removed = 0
    errors: list[str] = []
    for entry in downloads.iterdir():
        try:
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink()
            removed += 1
        except OSError as exc:
            errors.append(f"{entry.name}: {exc}")

    SETTINGS.last_run = datetime.now().isoformat()
    _refresh_next_run()

    if errors:
        msg = f"removed {removed} item(s) from {downloads}, {len(errors)} error(s): {'; '.join(errors[:5])}"
        STATE.note(f"Cleanup: {msg}")
        return False, msg

    msg = f"removed {removed} item(s) from {downloads}"
    STATE.note(f"Cleanup: {msg}. Seeding stops for anything deleted here - qBittorrent won't notice.")
    return True, msg


async def cleanup_loop() -> None:
    await asyncio.sleep(30)  # let the initial linking pass settle first
    if SETTINGS.enabled and not SETTINGS.next_run:
        _refresh_next_run()
    while True:
        if SETTINGS.enabled and SETTINGS.next_run:
            if datetime.now() >= datetime.fromisoformat(SETTINGS.next_run):
                run_cleanup_now()
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)
