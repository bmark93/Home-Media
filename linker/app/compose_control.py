"""
Lets the linker apply its own "Use this drive" / Plex-claim changes by
recreating the affected sibling containers itself over the Docker socket,
instead of making you copy-paste a command into a terminal.

This only works because docker-compose.yml mounts the project directory into
this container at the exact same absolute path it has on the real host (see
that file for why) - `docker compose up -d <service>` run from in here talks
to the real daemon through the shared socket, and every relative volume path
in the compose file resolves identically whether Compose is invoked from a
host terminal or from in here.
"""
from __future__ import annotations

import asyncio
import os

PROJECT_DIR = os.environ.get("PROJECT_DIR", "")


async def recreate_services(*services: str) -> tuple[bool, str]:
    if not PROJECT_DIR:
        return False, "PROJECT_DIR is not set - can't locate docker-compose.yml"

    # Compose interpolates ${PWD} in docker-compose.yml (used for the linker's own
    # project-dir/docker-socket mounts) from the *shell* env var PWD, which this
    # subprocess doesn't inherit automatically - set it explicitly so it matches
    # PROJECT_DIR instead of resolving blank.
    env = {**os.environ, "PWD": PROJECT_DIR}

    proc = await asyncio.create_subprocess_exec(
        "docker",
        "compose",
        "up",
        "-d",
        *services,
        cwd=PROJECT_DIR,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    raw = await proc.stdout.read()
    await proc.wait()
    output = raw.decode(errors="replace").strip()
    if proc.returncode == 0:
        return True, output[-2000:]
    return False, output[-2000:]
