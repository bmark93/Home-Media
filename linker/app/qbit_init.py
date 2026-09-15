"""
Init script (run by the 'qbit-init' service on every startup, before the
qBittorrent container starts/restarts). The QBIT_USER / QBIT_PASS from .env
are always enforced here - if someone changes the password through the
WebUI, the next `docker compose up` (e.g. on a restart) resets it back to
whatever is in .env. If you want a different password permanently, edit
.env, not the qBittorrent UI.

It also enforces the default save path to point at /downloads, the volume
shared with Sonarr/Radarr (otherwise qBittorrent falls back to its own
internal default under /config, which those apps can never see or import
from). Everything else (port, speed limits, etc.) that may have been
configured through the UI is left untouched.
"""
import base64
import hashlib
import os
from pathlib import Path

CONFIG_DIR = Path("/config/qBittorrent")
CONFIG_FILE = CONFIG_DIR / "qBittorrent.conf"


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    derived = hashlib.pbkdf2_hmac("sha512", password.encode("utf-8"), salt, 100_000)
    salt_b64 = base64.b64encode(salt).decode("ascii")
    hash_b64 = base64.b64encode(derived).decode("ascii")
    return f"@ByteArray({salt_b64}:{hash_b64})"


def main() -> None:
    username = os.environ.get("QBIT_USER", "admin")
    password = os.environ.get("QBIT_PASS", "admin")
    subnet = os.environ.get("QBIT_TRUSTED_SUBNET", "172.30.0.0/24")

    # These keys are overwritten on every startup, driven by .env -
    # AuthSubnetWhitelist* lets containers on our own docker network
    # (Sonarr, Radarr, exporter, linker) reach the WebUI API without a
    # password (otherwise the brute-force protection could end up
    # banning our own containers).
    managed = {
        "WebUI\\Username": username,
        "WebUI\\Password_PBKDF2": hash_password(password),
        "WebUI\\CSRFProtection": "false",
        "WebUI\\ClickjackingProtection": "false",
        "WebUI\\LocalHostAuth": "false",
        "WebUI\\AuthSubnetWhitelistEnabled": "true",
        "WebUI\\AuthSubnetWhitelist": subnet,
        # Without this, qBittorrent falls back to its own internal default
        # (something under /config), which Sonarr/Radarr never see - they
        # only have /downloads mounted. That breaks importing finished
        # downloads into the library, even though the torrent itself
        # completes fine. /downloads is the volume shared with Sonarr/Radarr.
        "Downloads\\SavePath": "/downloads/",
        "Downloads\\TempPathEnabled": "true",
        "Downloads\\TempPath": "/downloads/incomplete/",
    }

    lines = CONFIG_FILE.read_text().splitlines() if CONFIG_FILE.exists() else []

    kept = [ln for ln in lines if not any(ln.startswith(f"{k}=") for k in managed)]
    if "[Preferences]" not in kept:
        kept.append("[Preferences]")
    insert_at = kept.index("[Preferences]") + 1
    managed_lines = [f"{k}={v}" for k, v in managed.items()]
    new_lines = kept[:insert_at] + managed_lines + kept[insert_at:]

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text("\n".join(new_lines) + "\n")
    print(f"[qbit-init] Enforced WebUI login for user '{username}' (from .env) in {CONFIG_FILE}.")


if __name__ == "__main__":
    main()
