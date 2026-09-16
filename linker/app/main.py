import asyncio
import os

from app.cleanup import SETTINGS as CLEANUP_SETTINGS
from app.cleanup import cleanup_loop, run_cleanup_now
from app.cleanup import update_settings as update_cleanup_settings
from app.compose_control import recreate_services
from app.docker_metrics import render_name_map
from app.linking import STATE, get_connection_info, run_linking
from app.security import (
    SECURITY_HEADERS,
    SecurityMiddleware,
    password_matches,
    validate_credentials,
)
from app.storage import list_drives, set_plex_claim, use_drive
from app.updater import SETTINGS, auto_update_loop, trigger_update
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

QBIT_USER = os.environ.get("QBIT_USER", "admin")
QBIT_PASS = os.environ.get("QBIT_PASS", "admin")

app = FastAPI(title="Home Media Manager")
app.add_middleware(SecurityMiddleware)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")


@app.exception_handler(Exception)
async def internal_error(request: Request, exc: Exception):
    return JSONResponse({"detail": "Internal server error"}, status_code=500,
                        headers=SECURITY_HEADERS)

# SERVER_IP in .env is the single source of truth for "Open" links - set it
# to this machine's real LAN IP/hostname. Always plain http://, never https,
# no per-browser cleverness.
SERVER_IP = os.environ.get("SERVER_IP", "localhost")

SERVICE_LINKS = {
    "plex": {"label": "Plex", "port": 32400, "path": "/web"},
    "seerr": {"label": "Seerr", "port": 5055, "path": ""},
    "sonarr": {"label": "Sonarr", "port": 8989, "path": ""},
    "radarr": {"label": "Radarr", "port": 7878, "path": ""},
    "prowlarr": {"label": "Prowlarr", "port": 9696, "path": ""},
    "qbittorrent": {"label": "qBittorrent", "port": 8080, "path": ""},
}


@app.on_event("startup")
async def on_startup() -> None:
    app.state.credentials = validate_credentials(
        os.environ.get("LINKER_USER"), os.environ.get("LINKER_PASS")
    )
    asyncio.create_task(run_linking(QBIT_USER, QBIT_PASS))
    asyncio.create_task(auto_update_loop())
    asyncio.create_task(cleanup_loop())


def build_status() -> dict:
    result = {}
    for key, meta in SERVICE_LINKS.items():
        svc = STATE.services.get(key)
        result[key] = {
            "label": meta["label"],
            "url": f"http://{SERVER_IP}:{meta['port']}{meta['path']}",
            "reachable": svc.reachable if svc else False,
            "linked": svc.linked if svc else {},
            "error": svc.error if svc else None,
        }
    return result


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "csrf_token": request.state.csrf_token,
            "services": build_status(),
            "log": list(reversed(STATE.log)),
            "running": STATE.running,
            "last_run": STATE.last_run,
            "auto_update": SETTINGS.auto_update,
            "cleanup": CLEANUP_SETTINGS.as_dict(),
            "grafana_url": f"http://{SERVER_IP}:3000",
            "prometheus_url": f"http://{SERVER_IP}:9090",
        },
    )


@app.get("/api/status")
async def api_status():
    return JSONResponse(
        {
            "services": build_status(),
            "running": STATE.running,
            "last_run": STATE.last_run,
            "log": list(reversed(STATE.log)),
            "auto_update": SETTINGS.auto_update,
        }
    )


@app.post("/api/relink")
async def api_relink():
    if not STATE.running:
        asyncio.create_task(run_linking(QBIT_USER, QBIT_PASS))
    return JSONResponse({"started": True})


@app.get("/api/settings")
async def get_settings():
    return JSONResponse({"auto_update": SETTINGS.auto_update})


@app.post("/api/settings/auto-update")
async def set_auto_update(request: Request):
    body = await request.json()
    enabled = bool(body.get("enabled", False))
    SETTINGS.auto_update = enabled
    SETTINGS.save()
    STATE.note(f"Auto-update {'enabled' if enabled else 'disabled'} via UI.")
    return JSONResponse({"auto_update": SETTINGS.auto_update})


@app.get("/api/cleanup/settings")
async def get_cleanup_settings():
    return JSONResponse(CLEANUP_SETTINGS.as_dict())


@app.post("/api/cleanup/settings")
async def set_cleanup_settings(request: Request):
    body = await request.json()
    result = update_cleanup_settings(body)
    STATE.note(
        f"Cleanup schedule updated: {'enabled' if result['enabled'] else 'disabled'}, "
        f"{result['frequency']} at {result['time']}."
    )
    return JSONResponse(result)


@app.post("/api/cleanup/run-now")
async def api_cleanup_run_now():
    ok, msg = run_cleanup_now()
    return JSONResponse({"ok": ok, "message": msg})


@app.get("/api/keys")
async def api_keys():
    return JSONResponse({
        service: {"host": info["host"], "port": info["port"],
                  "key_available": bool(info["api_key"])}
        for service, info in get_connection_info().items()
    })


@app.post("/api/keys/reveal")
async def reveal_keys(request: Request):
    try:
        body = await request.json()
    except ValueError:
        body = None
    password = body.get("password") if isinstance(body, dict) else None
    if not password_matches(password, request.app.state.credentials[1]):
        return JSONResponse({"detail": "Re-authentication failed"}, status_code=403,
                            headers={"Cache-Control": "no-store"})
    return JSONResponse(get_connection_info(), headers={"Cache-Control": "no-store"})


@app.get("/api/drives")
async def api_drives():
    return JSONResponse({"drives": list_drives()})


@app.post("/api/drives/use")
async def api_use_drive(request: Request):
    body = await request.json()
    host_path = body.get("path", "")
    result = use_drive(host_path)
    if not result["ok"]:
        STATE.note(f"Storage: failed to switch to {host_path}: {result['error']}")
        return JSONResponse(result)

    STATE.note(f"Storage: .env updated to use {host_path}, recreating sonarr/radarr/plex/qbittorrent...")
    applied, output = await recreate_services("sonarr", "radarr", "plex", "qbittorrent")
    result["applied"] = applied
    result["apply_output"] = output
    STATE.note("Storage: containers recreated with new paths." if applied else f"Storage: apply failed: {output}")
    if applied:
        asyncio.create_task(run_linking(QBIT_USER, QBIT_PASS))
    return JSONResponse(result)


@app.post("/api/plex-claim")
async def api_plex_claim(request: Request):
    body = await request.json()
    token = body.get("token", "")
    result = set_plex_claim(token)
    if not result["ok"]:
        STATE.note(f"Plex claim: failed to save token: {result['error']}")
        return JSONResponse(result)

    STATE.note("Plex claim token saved to .env, recreating plex...")
    applied, output = await recreate_services("plex")
    result["applied"] = applied
    result["apply_output"] = output
    STATE.note("Plex claim: container recreated, Plex should be claimed now." if applied else f"Plex claim: apply failed: {output}")
    return JSONResponse(result)


@app.get("/metrics", response_class=PlainTextResponse)
async def metrics():
    return await render_name_map()


@app.post("/api/update-now")
async def update_now():
    ok, msg = await trigger_update()
    STATE.note(f"Manual update trigger: {msg}" if ok else f"Manual update failed: {msg}")
    return JSONResponse({"ok": ok, "message": msg})
