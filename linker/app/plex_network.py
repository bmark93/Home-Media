"""
Auto-fixes Plex's local/remote misclassification under Docker Desktop (Mac/
Windows) - see README.md's Troubleshooting section for the full story.

Short version: Docker Desktop's port publishing runs through an internal VM,
so every client that reaches Plex through its published port - even one on
the user's own LAN - shows up to Plex with a source IP from Docker Desktop's
internal NAT range, not the client's real address. Plex doesn't recognize
that range as local and tags the connection "WAN". On top of that, Plex's
automatic UPnP remote-access mapping can never succeed from inside that VM
(no real interface to the router), so Plex falls back to routing through
Plex Relay - which is what actually drives the "Remote Play"/quality-capped
behavior clients see, not just the WAN tag.

Nothing here is hardcoded to one user's network: SERVER_IP comes from .env
(same one the rest of the linker uses to build its own dashboard links), and
the Docker Desktop NAT range is discovered at runtime by resolving
host.docker.internal - a name only Docker Desktop defines. On native Linux
Docker that name doesn't resolve (unless the user opted in via extra_hosts),
so this step degrades to only setting customConnections and otherwise quietly
no-ops - native Linux doesn't have this problem in the first place, since
published-port source IPs and UPnP both work normally there.
"""
from __future__ import annotations

import ipaddress
import os
import re
import socket
import xml.etree.ElementTree as ET
from pathlib import Path

import httpx

PLEX_URL = "http://plex:32400"
PLEX_PREFS_PATH = (
    Path(os.environ.get("PROJECT_DIR", ""))
    / "config/plex/Library/Application Support/Plex Media Server/Preferences.xml"
)


def _read_plex_token() -> str | None:
    if not PLEX_PREFS_PATH.exists():
        return None
    try:
        token = ET.parse(PLEX_PREFS_PATH).getroot().get("PlexOnlineToken")
        return token or None
    except ET.ParseError:
        return None


def _docker_desktop_nat_subnet() -> str | None:
    """
    The private /24 Docker Desktop's VM NATs published-port traffic through -
    the source IP Plex actually sees for every client, LAN or not. Only
    resolvable under Docker Desktop; None (and this whole fix mostly a no-op)
    on native Linux Docker.
    """
    try:
        gateway_ip = socket.gethostbyname("host.docker.internal")
        return str(ipaddress.ip_network(f"{gateway_ip}/24", strict=False))
    except (OSError, ValueError):
        return None


def _lan_subnet_from_server_ip(server_ip: str) -> str | None:
    """Best-effort /24 guess around SERVER_IP - covers the common home-router case."""
    try:
        return str(ipaddress.ip_network(f"{server_ip}/24", strict=False))
    except ValueError:
        return None  # SERVER_IP is a hostname, or still the unconfigured "localhost" default


def _extract_pref_value(prefs_xml: str, pref_id: str) -> str:
    m = re.search(rf'<Setting id="{pref_id}"[^>]*\bvalue="([^"]*)"', prefs_xml)
    return m.group(1) if m else ""


async def apply(client: httpx.AsyncClient) -> tuple[bool, str]:
    token = _read_plex_token()
    if not token:
        return False, "skipped (Plex not signed in yet - no token in Preferences.xml)"

    server_ip = os.environ.get("SERVER_IP", "localhost")
    docker_nat_subnet = _docker_desktop_nat_subnet()
    lan_subnet = _lan_subnet_from_server_ip(server_ip)
    wanted_networks = {s for s in (docker_nat_subnet, lan_subnet) if s}

    if not wanted_networks and server_ip == "localhost":
        return False, "skipped (set SERVER_IP in .env to your server's LAN IP first)"

    headers = {"X-Plex-Token": token}
    try:
        prefs_resp = await client.get(f"{PLEX_URL}/:/prefs", headers=headers, timeout=10)
        prefs_resp.raise_for_status()
    except httpx.HTTPError as exc:
        return False, f"could not reach Plex: {exc}"
    current = prefs_resp.text

    changes: dict[str, str] = {}

    existing_networks = {
        n.strip() for n in _extract_pref_value(current, "allowedNetworks").split(",") if n.strip()
    }
    merged_networks = existing_networks | wanted_networks
    if merged_networks != existing_networks:
        changes["allowedNetworks"] = ",".join(sorted(merged_networks))

    if server_ip != "localhost":
        wanted_connection = f"http://{server_ip}:32400"
        existing_connections = {
            c.strip() for c in _extract_pref_value(current, "customConnections").split(",") if c.strip()
        }
        merged_connections = existing_connections | {wanted_connection}
        if merged_connections != existing_connections:
            changes["customConnections"] = ",".join(sorted(merged_connections))

    if not changes:
        return True, "already up to date"

    resp = await client.put(f"{PLEX_URL}/:/prefs", headers=headers, params=changes, timeout=10)
    if resp.status_code >= 400:
        return False, f"error ({resp.status_code}): {resp.text[:200]}"
    return True, f"updated {', '.join(changes)}"
