import asyncio
import os

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from app.compose_control import recreate_services
from app.docker_metrics import render_name_map
from app.linking import STATE, get_connection_info, run_linking
from app.storage import list_drives, set_plex_claim, use_drive
from app.updater import SETTINGS, auto_update_loop, trigger_update

QBIT_USER = os.environ.get("QBIT_USER", "admin")
QBIT_PASS = os.environ.get("QBIT_PASS", "admin")

app = FastAPI(title="Home Media Manager")
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

# Port + path only, not full URLs: this app doesn't know what hostname/IP
# the browser is actually reaching it on (could be localhost on a dev
# machine, or a LAN IP/hostname on a real server) - so "Open" links are
# built client-side in the browser against whatever host it's already on
# (window.location.hostname), not hardcoded here.
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
    asyncio.create_task(run_linking(QBIT_USER, QBIT_PASS))
    asyncio.create_task(auto_update_loop())


def build_status() -> dict:
    result = {}
    for key, meta in SERVICE_LINKS.items():
        svc = STATE.services.get(key)
        result[key] = {
            "label": meta["label"],
            "port": meta["port"],
            "path": meta["path"],
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
            "services": build_status(),
            "log": list(reversed(STATE.log)),
            "running": STATE.running,
            "last_run": STATE.last_run,
            "auto_update": SETTINGS.auto_update,
            "grafana_port": 3000,
            "prometheus_port": 9090,
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


@app.get("/api/keys")
async def api_keys():
    return JSONResponse(get_connection_info())


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
