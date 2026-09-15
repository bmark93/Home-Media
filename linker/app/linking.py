"""
The actual auto-linking logic:
- reads the Sonarr / Radarr / Prowlarr API key from their config.xml
- registers qBittorrent as a download client in Sonarr and Radarr
- sets each app's root folder (/tv, /movies - already mounted in the containers)
- registers Sonarr and Radarr as an "Application" in Prowlarr, so Prowlarr
  automatically syncs its indexers to them

Every step is idempotent: if a link already exists, it will not be recreated.

Seerr (https://github.com/seerr-team/seerr, the Jellyseerr/Overseerr
successor) is only monitored for reachability here, not auto-configured:
its admin account setup requires a real Plex sign-in (same limitation as
Plex itself), and its settings API is locked to session-cookie auth even
with a valid API key - there is no safe way to script that from outside.
See README.md for the one-time manual steps.
"""
from __future__ import annotations

import asyncio
import json
import xml.etree.ElementTree as ET
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import httpx

SONARR_URL = "http://sonarr:8989"
RADARR_URL = "http://radarr:7878"
PROWLARR_URL = "http://prowlarr:9696"
PROWLARR_INTERNAL_URL = "http://prowlarr:9696"
QBIT_HOST = "qbittorrent"
QBIT_PORT = 8080
PLEX_URL = "http://plex:32400"
SEERR_URL = "http://seerr:5055"

SONARR_TV_PATH = "/tv"
RADARR_MOVIES_PATH = "/movies"

API_KEY_PATHS = {
    "sonarr": Path("/configs/sonarr/config.xml"),
    "radarr": Path("/configs/radarr/config.xml"),
    "prowlarr": Path("/configs/prowlarr/config.xml"),
}


@dataclass
class ServiceState:
    reachable: bool = False
    linked: dict[str, bool] = field(default_factory=dict)
    error: str | None = None


@dataclass
class LinkerState:
    services: dict[str, ServiceState] = field(
        default_factory=lambda: {
            "sonarr": ServiceState(),
            "radarr": ServiceState(),
            "prowlarr": ServiceState(),
            "qbittorrent": ServiceState(),
            "plex": ServiceState(),
            "seerr": ServiceState(),
        }
    )
    last_run: str | None = None
    running: bool = False
    log: list[str] = field(default_factory=list)

    def note(self, msg: str) -> None:
        stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
        self.log.append(f"[{stamp}] {msg}")
        self.log[:] = self.log[-50:]


STATE = LinkerState()


CONNECTION_INFO = {
    "sonarr": {"host": "sonarr", "port": 8989},
    "radarr": {"host": "radarr", "port": 7878},
    "prowlarr": {"host": "prowlarr", "port": 9696},
}


def get_connection_info() -> dict:
    """
    Host/port/API key for each *arr app, exactly as needed when manually
    filling in a "Hostname", "Port", "API Key" form (e.g. in Seerr's Add
    Server dialog, or Prowlarr's Add Application). The hostname is the
    docker network service name - NOT "localhost", which from inside
    another container's network namespace refers to that container itself.
    """
    result = {}
    for svc, info in CONNECTION_INFO.items():
        result[svc] = {
            "host": info["host"],
            "port": info["port"],
            "api_key": read_api_key(API_KEY_PATHS[svc]),
        }
    return result


def read_api_key(path: Path) -> str | None:
    if not path.exists():
        return None
    try:
        tree = ET.parse(path)
        node = tree.getroot().find("ApiKey")
        return node.text.strip() if node is not None and node.text else None
    except ET.ParseError:
        return None


async def wait_until_up(client: httpx.AsyncClient, url: str, timeout_s: int = 180) -> bool:
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        try:
            r = await client.get(f"{url}/ping", timeout=5)
            if r.status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        await asyncio.sleep(3)
    return False


async def wait_for_api_key(path: Path, timeout_s: int = 180) -> str | None:
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        key = read_api_key(path)
        if key:
            return key
        await asyncio.sleep(3)
    return None


async def link_qbittorrent_to_arr(
    client: httpx.AsyncClient, base_url: str, api_key: str, category: str, qbit_user: str, qbit_pass: str
) -> tuple[bool, str]:
    headers = {"X-Api-Key": api_key}

    existing = await client.get(f"{base_url}/api/v3/downloadclient", headers=headers)
    existing.raise_for_status()
    for dc in existing.json():
        if dc.get("implementation") == "QBittorrent":
            return True, "already linked"

    schema_resp = await client.get(f"{base_url}/api/v3/downloadclient/schema", headers=headers)
    schema_resp.raise_for_status()
    template = next(s for s in schema_resp.json() if s["implementation"] == "QBittorrent")

    fields = deepcopy(template["fields"])
    for f in fields:
        name = f["name"].lower()
        if name == "host":
            f["value"] = QBIT_HOST
        elif name == "port":
            f["value"] = QBIT_PORT
        elif name == "username":
            f["value"] = qbit_user
        elif name == "password":
            f["value"] = qbit_pass
        elif "category" in name:
            f["value"] = category

    payload = {
        "enable": True,
        "protocol": "torrent",
        "priority": 1,
        "name": "qBittorrent",
        "fields": fields,
        "implementationName": template["implementationName"],
        "implementation": template["implementation"],
        "configContract": template["configContract"],
        "tags": [],
    }

    resp = await client.post(f"{base_url}/api/v3/downloadclient", headers=headers, json=payload)
    if resp.status_code >= 400:
        return False, f"error ({resp.status_code}): {resp.text[:200]}"
    return True, "linked successfully"


async def link_arr_to_prowlarr(
    client: httpx.AsyncClient, name: str, base_url: str, arr_api_key: str, prowlarr_api_key: str
) -> tuple[bool, str]:
    headers = {"X-Api-Key": prowlarr_api_key}

    existing = await client.get(f"{PROWLARR_URL}/api/v1/applications", headers=headers)
    existing.raise_for_status()
    for app in existing.json():
        if app.get("implementation", "").lower() == name.lower():
            return True, "already linked"

    schema_resp = await client.get(f"{PROWLARR_URL}/api/v1/applications/schema", headers=headers)
    schema_resp.raise_for_status()
    template = next(s for s in schema_resp.json() if s["implementation"].lower() == name.lower())

    fields = deepcopy(template["fields"])
    for f in fields:
        fname = f["name"].lower()
        if fname == "baseurl":
            f["value"] = base_url
        elif fname == "apikey":
            f["value"] = arr_api_key
        elif fname == "prowlarrurl":
            f["value"] = PROWLARR_INTERNAL_URL

    payload = deepcopy(template)
    payload.pop("id", None)
    payload["name"] = name
    payload["fields"] = fields
    payload["syncLevel"] = "fullSync"
    payload["tags"] = []

    resp = await client.post(f"{PROWLARR_URL}/api/v1/applications", headers=headers, json=payload)
    if resp.status_code >= 400:
        return False, f"error ({resp.status_code}): {resp.text[:200]}"
    return True, "linked successfully"


async def ensure_qbit_download_path(client: httpx.AsyncClient) -> tuple[bool, str]:
    """
    Force qBittorrent's save path to /downloads, the volume shared with
    Sonarr/Radarr. This is done live over the API (not just in the config
    file qbit_init.py seeds before first boot) because qBittorrent re-writes
    its own in-memory settings back to disk on every shutdown - if it was
    ever started once with the wrong default, a plain container restart
    clobbers a file-only fix before qBittorrent reads it back. Setting it
    live, every time linking runs, is self-healing regardless of ordering.
    """
    try:
        prefs = {
            "save_path": "/downloads/",
            "temp_path_enabled": True,
            "temp_path": "/downloads/incomplete/",
        }
        resp = await client.post(
            f"http://{QBIT_HOST}:{QBIT_PORT}/api/v2/app/setPreferences",
            data={"json": json.dumps(prefs)},
        )
        if resp.status_code >= 400:
            return False, f"error ({resp.status_code}): {resp.text[:200]}"
        return True, "save path set to /downloads"
    except httpx.HTTPError as exc:
        return False, f"could not reach qBittorrent: {exc}"


async def ensure_root_folder(client: httpx.AsyncClient, base_url: str, api_key: str, path: str) -> tuple[bool, str]:
    headers = {"X-Api-Key": api_key}

    existing = await client.get(f"{base_url}/api/v3/rootfolder", headers=headers)
    existing.raise_for_status()
    for rf in existing.json():
        if rf.get("path") == path:
            return True, "already set"

    resp = await client.post(f"{base_url}/api/v3/rootfolder", headers=headers, json={"path": path})
    if resp.status_code >= 400:
        return False, f"error ({resp.status_code}): {resp.text[:200]}"
    return True, "set"


async def run_linking(qbit_user: str, qbit_pass: str) -> None:
    if STATE.running:
        return
    STATE.running = True
    STATE.note("Linking started...")

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            # 1) wait until the *arr apps respond and have generated an API key
            for svc, url in (("sonarr", SONARR_URL), ("radarr", RADARR_URL), ("prowlarr", PROWLARR_URL)):
                STATE.note(f"{svc}: waiting until reachable...")
                up = await wait_until_up(client, url)
                STATE.services[svc].reachable = up
                if not up:
                    STATE.services[svc].error = "not reachable (timeout)"
                    STATE.note(f"{svc}: never became reachable, skipping")

            api_keys: dict[str, str | None] = {}
            for svc in ("sonarr", "radarr", "prowlarr"):
                if STATE.services[svc].reachable:
                    api_keys[svc] = await wait_for_api_key(API_KEY_PATHS[svc])
                    if not api_keys[svc]:
                        STATE.services[svc].error = "API key not found"
                        STATE.note(f"{svc}: could not find an API key in config.xml")

            # qBittorrent reachability (no API key extraction needed, we preset the login).
            # The /api/v2/* endpoints require authentication for non-localhost requests (403),
            # so we only check the WebUI root for a reachability probe.
            try:
                r = await client.get(f"http://{QBIT_HOST}:{QBIT_PORT}/", timeout=5)
                STATE.services["qbittorrent"].reachable = r.status_code == 200
            except httpx.HTTPError:
                STATE.services["qbittorrent"].reachable = False

            if STATE.services["qbittorrent"].reachable:
                ok, msg = await ensure_qbit_download_path(client)
                STATE.services["qbittorrent"].linked["download_path"] = ok
                STATE.note(f"qBittorrent download path: {msg}")

            # Plex reachability only (no auto-configuration - see README)
            try:
                r = await client.get(PLEX_URL, timeout=5)
                STATE.services["plex"].reachable = r.status_code < 500
            except httpx.HTTPError:
                STATE.services["plex"].reachable = False

            # Seerr reachability only (no auto-configuration - see README).
            # Its admin account requires a real Plex sign-in, and its settings API
            # is locked to session-cookie auth even with a valid API key.
            try:
                r = await client.get(f"{SEERR_URL}/api/v1/settings/public", timeout=5)
                STATE.services["seerr"].reachable = r.status_code == 200
            except httpx.HTTPError:
                STATE.services["seerr"].reachable = False

            # 2) set the root folder in Sonarr / Radarr (the already-mounted /tv, /movies)
            if STATE.services["sonarr"].reachable and api_keys.get("sonarr"):
                ok, msg = await ensure_root_folder(client, SONARR_URL, api_keys["sonarr"], SONARR_TV_PATH)
                STATE.services["sonarr"].linked["root_folder"] = ok
                STATE.note(f"Sonarr root folder ({SONARR_TV_PATH}): {msg}")

            if STATE.services["radarr"].reachable and api_keys.get("radarr"):
                ok, msg = await ensure_root_folder(client, RADARR_URL, api_keys["radarr"], RADARR_MOVIES_PATH)
                STATE.services["radarr"].linked["root_folder"] = ok
                STATE.note(f"Radarr root folder ({RADARR_MOVIES_PATH}): {msg}")

            # 3) link qBittorrent into Sonarr / Radarr
            if STATE.services["sonarr"].reachable and api_keys.get("sonarr") and STATE.services["qbittorrent"].reachable:
                ok, msg = await link_qbittorrent_to_arr(
                    client, SONARR_URL, api_keys["sonarr"], "tv-sonarr", qbit_user, qbit_pass
                )
                STATE.services["sonarr"].linked["qbittorrent"] = ok
                STATE.note(f"Sonarr <-> qBittorrent: {msg}")

            if STATE.services["radarr"].reachable and api_keys.get("radarr") and STATE.services["qbittorrent"].reachable:
                ok, msg = await link_qbittorrent_to_arr(
                    client, RADARR_URL, api_keys["radarr"], "radarr", qbit_user, qbit_pass
                )
                STATE.services["radarr"].linked["qbittorrent"] = ok
                STATE.note(f"Radarr <-> qBittorrent: {msg}")

            # 4) link Sonarr / Radarr into Prowlarr (so it syncs its indexers to them)
            if STATE.services["prowlarr"].reachable and api_keys.get("prowlarr"):
                if STATE.services["sonarr"].reachable and api_keys.get("sonarr"):
                    ok, msg = await link_arr_to_prowlarr(
                        client, "Sonarr", SONARR_URL, api_keys["sonarr"], api_keys["prowlarr"]
                    )
                    STATE.services["prowlarr"].linked["sonarr"] = ok
                    STATE.note(f"Prowlarr <-> Sonarr: {msg}")

                if STATE.services["radarr"].reachable and api_keys.get("radarr"):
                    ok, msg = await link_arr_to_prowlarr(
                        client, "Radarr", RADARR_URL, api_keys["radarr"], api_keys["prowlarr"]
                    )
                    STATE.services["prowlarr"].linked["radarr"] = ok
                    STATE.note(f"Prowlarr <-> Radarr: {msg}")

        STATE.note("Linking finished.")
    except Exception as exc:  # noqa: BLE001 - surfaced to the end user in the status log
        STATE.note(f"Error while linking: {exc}")
    finally:
        STATE.running = False
        STATE.last_run = datetime.now(timezone.utc).isoformat()
