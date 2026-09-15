"""
Exposes a tiny Prometheus "info" metric mapping each container's short ID to
its real name, by querying the Docker API directly over the socket this
container already has access to (for the storage/Plex-claim automation).

cAdvisor's own metrics only carry the raw cgroup ID (see containers.json's
comment for why - a Docker Desktop / containerd-socket limitation, not
something fixable by cAdvisor config), so the Grafana dashboard joins that
against this metric with `* on(short_id) group_left(name) ...` to show real
container names in its legends instead of hex IDs.
"""
from __future__ import annotations

import re

import httpx

NAME_PATTERN = re.compile(r"^[^-]+-(.+)-\d+$")


def _clean_name(raw: str) -> str:
    name = raw.lstrip("/")
    m = NAME_PATTERN.match(name)
    return m.group(1) if m else name


async def render_name_map() -> str:
    transport = httpx.AsyncHTTPTransport(uds="/var/run/docker.sock")
    lines = [
        "# HELP docker_container_name_map Maps a container's short ID to its name (value is always 1)",
        "# TYPE docker_container_name_map gauge",
    ]
    try:
        async with httpx.AsyncClient(transport=transport, timeout=5) as client:
            resp = await client.get("http://docker/containers/json")
            resp.raise_for_status()
            for container in resp.json():
                short_id = container["Id"][:12]
                names = container.get("Names") or []
                name = _clean_name(names[0]) if names else short_id
                lines.append(f'docker_container_name_map{{short_id="{short_id}",name="{name}"}} 1')
    except httpx.HTTPError:
        pass
    return "\n".join(lines) + "\n"
