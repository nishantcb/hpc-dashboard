"""
Cluster Watch — a small local dashboard for your own HPC login nodes.

Run (this device only):
    pip install -r requirements.txt
    uvicorn main:app --host 127.0.0.1 --port 8000

Run (reachable from other devices on your LAN too):
    DASHBOARD_PASSWORD=some-secret uvicorn main:app --host 0.0.0.0 --port 8000
    # or just: ./start.sh  (also keeps it running in the background — see README)

Then open http://127.0.0.1:8000 (or http://<this-machine's-LAN-IP>:8000 from
another device). Everything (including your SSH password/OTP) stays on your
own machine; nothing is written to disk except the list of server
names/hosts/usernames you add (never a password). If DASHBOARD_PASSWORD is
set, the dashboard itself is gated behind that password before any of this
is visible — set it whenever you bind to anything other than 127.0.0.1, so
the rest of your network can't just open the tab and see it.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
from pathlib import Path
from typing import Dict

from fastapi import FastAPI, Form, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware

from ssh_manager import ServerConnection

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "servers.json"
POLL_INTERVAL = 4  # seconds between probes per server — be a good citizen on shared login nodes
PUSH_INTERVAL = 1  # seconds between UI pushes (reads cached data, doesn't touch SSH)

# Optional dashboard-level password — separate from any HPC server's own
# password. Unset (the default) means no login screen, same as before; set
# it before binding to 0.0.0.0 so the dashboard isn't wide open on your LAN.
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD")
SESSION_COOKIE = "cw_session"
valid_sessions: set[str] = set()

app = FastAPI(title="Cluster Watch")
connections: Dict[str, ServerConnection] = {}


class ServerIn(BaseModel):
    name: str
    host: str
    port: int = 22
    username: str


class ConnectIn(BaseModel):
    password: str
    otp: str = ""


# -- dashboard login (only active when DASHBOARD_PASSWORD is set) ----------
LOGIN_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Cluster Watch — sign in</title>
<style>
  :root{{--bg:#0A0D13;--panel:rgba(17,21,31,.85);--border:rgba(255,255,255,.08);--text:#E9EEF7;--cyan:#22D3EE;--violet:#8B5CF6;}}
  *{{box-sizing:border-box;}}
  html,body{{margin:0;height:100%;}}
  body{{
    min-height:100vh;display:flex;align-items:center;justify-content:center;
    background:radial-gradient(1100px circle at 15% -10%, rgba(139,92,246,.18), transparent 42%),
               radial-gradient(1000px circle at 100% 0%, rgba(34,211,238,.12), transparent 46%), var(--bg);
    font-family:-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;color:var(--text);
  }}
  form{{background:var(--panel);border:1px solid var(--border);border-radius:14px;padding:2rem 2.2rem;width:min(90vw,320px);backdrop-filter:blur(16px);}}
  h1{{font-size:1.3rem;margin:0 0 1.1rem;background:linear-gradient(90deg,var(--cyan),var(--violet));-webkit-background-clip:text;background-clip:text;color:transparent;}}
  input{{width:100%;background:rgba(6,8,13,.7);border:1px solid var(--border);color:var(--text);border-radius:7px;padding:.6rem .7rem;font-size:.95rem;margin-bottom:.9rem;}}
  button{{width:100%;background:linear-gradient(90deg,var(--cyan),var(--violet));color:#08090D;border:none;border-radius:7px;padding:.6rem;font-weight:700;cursor:pointer;font-size:.95rem;}}
  .err{{color:#F5A524;font-size:.85rem;margin:0 0 .8rem;}}
</style>
</head>
<body>
  <form method="post" action="/login">
    <h1>Cluster Watch</h1>
    {error}
    <input type="password" name="password" placeholder="Dashboard password" autofocus required>
    <button type="submit">Unlock</button>
  </form>
</body>
</html>"""


def _is_authed(request_like) -> bool:
    if not DASHBOARD_PASSWORD:
        return True
    token = request_like.cookies.get(SESSION_COOKIE)
    return bool(token) and token in valid_sessions


class DashboardAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if not DASHBOARD_PASSWORD or request.url.path == "/login" or _is_authed(request):
            return await call_next(request)
        if request.url.path.startswith("/api/"):
            return PlainTextResponse("Unauthorized — dashboard is password-protected", status_code=401)
        return RedirectResponse("/login")


app.add_middleware(DashboardAuthMiddleware)


@app.get("/login")
async def login_form():
    return HTMLResponse(LOGIN_PAGE.format(error=""))


@app.post("/login")
async def login_submit(password: str = Form(...)):
    if DASHBOARD_PASSWORD and secrets.compare_digest(password, DASHBOARD_PASSWORD):
        token = secrets.token_urlsafe(32)
        valid_sessions.add(token)
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax", max_age=60 * 60 * 24 * 30)
        return resp
    error = '<p class="err">Wrong password.</p>'
    return HTMLResponse(LOGIN_PAGE.format(error=error), status_code=401)


def load_servers() -> None:
    if not CONFIG_PATH.exists():
        return
    for entry in json.loads(CONFIG_PATH.read_text()):
        connections.setdefault(entry["name"], ServerConnection(**entry))


def save_servers() -> None:
    data = [
        {"name": c.name, "host": c.host, "port": c.port, "username": c.username}
        for c in connections.values()
    ]
    CONFIG_PATH.write_text(json.dumps(data, indent=2))


load_servers()


@app.get("/api/servers")
def list_servers():
    return [
        {
            "name": c.name,
            "host": c.host,
            "port": c.port,
            "username": c.username,
            "connected": c.connected,
            "error": c.last_error,
        }
        for c in connections.values()
    ]


@app.post("/api/servers")
def add_server(s: ServerIn):
    if s.name in connections:
        return {"ok": False, "error": "a server with that label already exists"}
    connections[s.name] = ServerConnection(name=s.name, host=s.host, port=s.port, username=s.username)
    save_servers()
    return {"ok": True}


@app.delete("/api/servers/{name}")
def remove_server(name: str):
    c = connections.pop(name, None)
    if c:
        c.disconnect_sync()
        save_servers()
    return {"ok": True}


@app.post("/api/servers/{name}/connect")
async def connect_server(name: str, body: ConnectIn):
    c = connections.get(name)
    if not c:
        return {"ok": False, "error": "unknown server"}
    try:
        await asyncio.to_thread(c.connect_sync, body.password, body.otp)
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001 — surfacing the reason to the UI is the point
        c.connected = False
        c.last_error = str(exc) or exc.__class__.__name__
        return {"ok": False, "error": c.last_error}


@app.post("/api/servers/{name}/disconnect")
def disconnect_server(name: str):
    c = connections.get(name)
    if c:
        c.disconnect_sync()
    return {"ok": True}


@app.post("/api/servers/{name}/disk")
async def disk_usage(name: str):
    c = connections.get(name)
    if not c:
        return {"ok": False, "error": "unknown server"}
    if not c.connected:
        return {"ok": False, "error": "not connected"}
    try:
        disks = await asyncio.to_thread(c.run_disk_probe_sync)
        return {"ok": True, "disks": disks}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc) or exc.__class__.__name__}


async def poll_once() -> None:
    names, tasks = [], []
    for name, c in connections.items():
        if c.connected:
            names.append(name)
            tasks.append(asyncio.to_thread(c.run_probe_sync))
    if not tasks:
        return
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for name, result in zip(names, results):
        c = connections[name]
        if isinstance(result, Exception):
            c.connected = False
            c.last_error = str(result) or result.__class__.__name__
            c.last_stats = None
        else:
            c.last_stats = result
            c.last_error = None


async def background_poller() -> None:
    while True:
        try:
            await poll_once()
        except Exception:
            pass  # a single bad cycle should never kill the poller
        await asyncio.sleep(POLL_INTERVAL)


@app.on_event("startup")
async def on_startup() -> None:
    asyncio.create_task(background_poller())


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    if DASHBOARD_PASSWORD and not _is_authed(ws):
        await ws.close(code=4401)
        return
    await ws.accept()
    try:
        while True:
            payload = {
                name: {
                    "connected": c.connected,
                    "error": c.last_error,
                    "stats": c.last_stats,
                }
                for name, c in connections.items()
            }
            await ws.send_text(json.dumps(payload))
            await asyncio.sleep(PUSH_INTERVAL)
    except WebSocketDisconnect:
        pass


# Static frontend last, so it never shadows the API/WS routes above.
app.mount("/", StaticFiles(directory=BASE_DIR / "static", html=True), name="static")
