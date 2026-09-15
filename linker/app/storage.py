"""
Setup helpers for the linker's "Host storage" section:
- list attached drives (as seen under /Volumes, /mnt or /media on the host)
  so the user can pick one instead of typing a path by hand
- point Plex/Sonarr/Radarr/qBittorrent's media+download folders at a chosen
  drive by rewriting .env (creating the movies/tv/downloads subfolders too)
- save a Plex claim token into .env

Actually applying it (recreating Sonarr/Radarr/Plex/qBittorrent with the new
bind mount) is handled separately by app/compose_control.py, called from the
API routes in app/main.py once this module reports success.
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path

DRIVE_ROOTS = {
    "/Volumes": Path("/hostfs/Volumes"),
    "/mnt": Path("/hostfs/mnt"),
    "/media": Path("/hostfs/media"),
}
ENV_FILE = Path(os.environ.get("PROJECT_DIR", "")) / ".env"
HOST_HOME = os.environ.get("HOST_HOME", "")
INTERNAL_DRIVE_SUBDIR = "HomeMediaManager-storage"

# macOS always lists these under /Volumes - the boot/recovery system
# partitions. This isn't a "using the internal drive is risky" situation
# (that's exactly what this project's own default ./media paths already do,
# safely) - it's specifically that macOS's *named system volume* mount,
# unlike a plain external drive or a normal folder path, isn't a real bind
# mount through Docker Desktop's virtualization: confirmed directly, a
# mkdir here reports success from inside a container without the folder
# ever landing on the real disk. Since that failure is invisible from here,
# a "use at your own risk" warning wouldn't actually let anyone make an
# informed choice, so these are excluded rather than merely flagged -
# storing on the internal drive is offered as a real, working option below
# instead, under the user's home directory.
BLOCKED_VOLUME_NAMES = {"Macintosh HD", "Recovery"}


def _human_bytes(n: int | None) -> str:
    if n is None:
        return "?"
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if size < 1024 or unit == "PB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} {unit}"
        size /= 1024
    return f"{size:.1f} PB"


def _disk_usage(path: Path) -> tuple[int | None, int | None]:
    try:
        usage = os.statvfs(path)
        return usage.f_bavail * usage.f_frsize, usage.f_blocks * usage.f_frsize
    except OSError:
        return None, None


def list_drives() -> list[dict]:
    drives = []

    for host_root, container_root in DRIVE_ROOTS.items():
        if not container_root.is_dir():
            continue
        for entry in sorted(container_root.iterdir()):
            if not entry.is_dir() or entry.name in BLOCKED_VOLUME_NAMES:
                continue
            free_bytes, total_bytes = _disk_usage(entry)
            drives.append(
                {
                    "host_path": f"{host_root}/{entry.name}",
                    "label": entry.name,
                    "free_bytes": free_bytes,
                    "total_bytes": total_bytes,
                    "free_human": _human_bytes(free_bytes),
                    "total_human": _human_bytes(total_bytes),
                }
            )

    if HOST_HOME:
        home_container_path = Path(HOST_HOME)
        if home_container_path.is_dir():
            free_bytes, total_bytes = _disk_usage(home_container_path)
            drives.append(
                {
                    "host_path": f"{HOST_HOME.rstrip('/')}/{INTERNAL_DRIVE_SUBDIR}",
                    "label": "Internal drive (this machine)",
                    "free_bytes": free_bytes,
                    "total_bytes": total_bytes,
                    "free_human": _human_bytes(free_bytes),
                    "total_human": _human_bytes(total_bytes),
                }
            )

    return drives


def _container_path_for(host_path: str) -> Path | None:
    roots = dict(DRIVE_ROOTS)
    if HOST_HOME:
        roots[HOST_HOME.rstrip("/")] = Path(HOST_HOME)  # mounted at the identical path
    for host_root, container_root in roots.items():
        if host_path == host_root or host_path.startswith(host_root + "/"):
            rest = host_path[len(host_root) :].lstrip("/")
            return container_root / rest if rest else container_root
    return None


def _update_env(values: dict[str, str]) -> tuple[bool, str]:
    if not os.environ.get("PROJECT_DIR"):
        return False, "PROJECT_DIR is not set on the linker container"
    if not ENV_FILE.exists():
        return False, ".env not found - has it been created yet? (cp .env.example .env)"

    lines = ENV_FILE.read_text().splitlines()
    keys_left = set(values)
    for i, line in enumerate(lines):
        for key in list(keys_left):
            if line.startswith(f"{key}="):
                lines[i] = f"{key}={values[key]}"
                keys_left.discard(key)
    for key in keys_left:
        lines.append(f"{key}={values[key]}")

    ENV_FILE.write_text("\n".join(lines) + "\n")
    return True, "updated"


def use_drive(host_path: str) -> dict:
    if host_path.rstrip("/").rsplit("/", 1)[-1] in BLOCKED_VOLUME_NAMES:
        return {"ok": False, "error": f"{host_path} is a system volume and isn't a valid storage target"}

    container_path = _container_path_for(host_path)
    if container_path is None:
        return {"ok": False, "error": f"unrecognized drive path: {host_path}"}
    # container_path itself may not exist yet (e.g. the internal-drive option
    # points at a subfolder this call is about to create) - what matters is
    # that its parent (the actual drive/home mount) is real.
    if not container_path.is_dir() and not container_path.parent.is_dir():
        return {"ok": False, "error": f"drive not found: {host_path}"}

    try:
        for sub in ("movies", "tv", "downloads"):
            folder = container_path / sub
            folder.mkdir(parents=True, exist_ok=True)
            # Belt-and-suspenders check: write a marker and read it back, to
            # catch permission issues or a mount that only looks writable.
            marker = folder / f".hmm-write-test-{uuid.uuid4().hex}"
            marker.write_text("ok")
            if marker.read_text() != "ok":
                raise OSError(f"wrote {marker} but could not read it back")
            marker.unlink()
    except OSError as exc:
        return {"ok": False, "error": f"could not create/verify folders on {host_path}: {exc}"}

    paths = {
        "MEDIA_MOVIES_PATH": f"{host_path}/movies",
        "MEDIA_TV_PATH": f"{host_path}/tv",
        "DOWNLOADS_PATH": f"{host_path}/downloads",
    }
    ok, msg = _update_env(paths)
    if not ok:
        return {"ok": False, "error": msg}
    return {"ok": True, "paths": paths}


def set_plex_claim(token: str) -> dict:
    token = token.strip()
    if not token:
        return {"ok": False, "error": "empty token"}
    ok, msg = _update_env({"PLEX_CLAIM": token})
    if not ok:
        return {"ok": False, "error": msg}
    return {"ok": True}
