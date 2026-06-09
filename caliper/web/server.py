"""Caliper web app — a chat UI over the agent. Config-driven; no site specifics.

Everything is configured by environment variables, so the SAME code runs locally
(for testing) and on a lab server (deployment):

  CALIPER_WORKSPACE   confined write directory (all outputs/temp live here)
  CALIPER_DATA_ROOT   read-only root the data browser/search is limited to
  CALIPER_PACK        domain pack to load (default: bio)
  CALIPER_PROVIDER    llm provider (anthropic | openai | mock)
  ANTHROPIC_API_KEY   (server-side only; never sent to the browser)
  CALIPER_WEB_PASSWORD  if set, the UI requires this password (else dev-open)

Run:  uvicorn caliper.web.server:app   (or `python -m caliper.web.server`)
"""
from __future__ import annotations

import json
import os
import queue
import secrets
import threading
import time
from collections import deque
from typing import List

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse

from ..core.agent import CaliperAgent
from ..core.executor import Executor
from ..core.remote_executor import RemoteExecutor
from ..core.registry import load_pack
from ..core import logstore
from ..config import get_workspace
from ..llm import make_llm
from ..trust.judge import Judge

HERE = os.path.dirname(__file__)
DATA_ROOT = os.path.realpath(os.environ.get("CALIPER_DATA_ROOT", os.getcwd()))
WORKSPACE = os.path.realpath(get_workspace() or os.path.join(os.getcwd(), "caliper_workspace"))
PACK = os.environ.get("CALIPER_PACK", "bio")
_SESSIONS: dict = {}        # token -> email (who is logged in)
_HISTORY: dict = {}         # session_id -> {"title":..., "ts":..., "messages":[...]}
ACCESS_LOG = deque(maxlen=1000)   # recent login events {ts, ip, email, event, ok}
_FAILS: dict = {}           # ip -> (count, last_ts)   (brute-force lockout)
_LOCK_AFTER, _LOCK_WINDOW = 5, 300   # >=5 fails within 300s -> locked for 300s

LAB_NAME = os.environ.get("CALIPER_LAB_NAME", "Chong's Lab")
try:
    # CALIPER_USERS: JSON {email: password}. Use a DEDICATED app password per user —
    # never anyone's institutional/UMN password.
    _USERS = json.loads(os.environ.get("CALIPER_USERS", "") or "{}")
except json.JSONDecodeError:
    _USERS = {}
_SINGLE_PW = os.environ.get("CALIPER_WEB_PASSWORD")  # legacy single-password fallback

try:  # restore chat history from disk (no DB; survives restarts)
    _HISTORY.update(logstore.load_sessions(WORKSPACE))
except Exception:  # noqa: BLE001
    pass

app = FastAPI(title="Caliper")


def _auth_configured() -> bool:
    return bool(_USERS or _SINGLE_PW)


def _check_creds(email: str, password: str) -> bool:
    if _USERS:
        exp = _USERS.get((email or "").strip().lower())
        return exp is not None and secrets.compare_digest(str(password), str(exp))
    if _SINGLE_PW:
        return secrets.compare_digest(str(password), _SINGLE_PW)
    return False


def client_ip(request: Request) -> str:
    return (request.headers.get("cf-connecting-ip")
            or request.headers.get("x-forwarded-for", "").split(",")[0].strip()
            or (request.client.host if request.client else "?"))


def log_access(ip: str, event: str, ok: bool, email: str = ""):
    entry = {"ts": int(time.time()), "ip": ip, "email": email, "event": event, "ok": ok}
    ACCESS_LOG.append(entry)
    try:
        with open(os.path.join(WORKSPACE, "access.log"), "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def _locked(ip: str) -> bool:
    cnt, last = _FAILS.get(ip, (0, 0))
    return cnt >= _LOCK_AFTER and (time.time() - last) < _LOCK_WINDOW


# --- auth ------------------------------------------------------------------------
def require_auth(request: Request):
    if not _auth_configured():
        return  # dev-open
    if request.cookies.get("caliper_session") not in _SESSIONS:
        raise HTTPException(status_code=401, detail="login required")


@app.post("/api/login")
async def login(request: Request):
    ip = client_ip(request)
    if _locked(ip):
        log_access(ip, "login-locked", False)
        return JSONResponse({"ok": False, "error": "too many attempts; try later"}, status_code=429)
    body = await request.json()
    email = (body.get("email") or "").strip().lower()
    if _check_creds(email, body.get("password", "")):
        tok = secrets.token_urlsafe(24)
        _SESSIONS[tok] = email or "user"
        _FAILS.pop(ip, None)
        log_access(ip, "login", True, email)
        secure = request.headers.get("x-forwarded-proto", "http") == "https"
        r = JSONResponse({"ok": True})
        r.set_cookie("caliper_session", tok, httponly=True, samesite="lax", secure=secure)
        return r
    cnt, _ = _FAILS.get(ip, (0, 0))
    _FAILS[ip] = (cnt + 1, time.time())
    log_access(ip, "login", False, email)
    return JSONResponse({"ok": False}, status_code=401)


@app.get("/api/branding")
def branding():
    return {"auth": _auth_configured()}  # agnostic: no lab identity revealed pre-login


@app.get("/api/access-log")
def access_log(_=Depends(require_auth)):
    return list(ACCESS_LOG)[-300:][::-1]


@app.get("/api/whoami")
def whoami(request: Request, _=Depends(require_auth)):
    email = _SESSIONS.get(request.cookies.get("caliper_session"), "")
    return {"data_root": DATA_ROOT, "pack": PACK, "lab_name": LAB_NAME,
            "email": email, "auth_required": _auth_configured(),
            "remote": os.environ.get("CALIPER_REMOTE_HOST") or "this server"}


# --- read-only data browser/search (confined to DATA_ROOT) -----------------------
def _safe(path: str) -> str:
    p = os.path.realpath(os.path.join(DATA_ROOT, path or ""))
    if p != DATA_ROOT and not p.startswith(DATA_ROOT + os.sep):
        raise HTTPException(status_code=400, detail="outside data root")
    return p


@app.get("/api/browse")
def browse(path: str = "", _=Depends(require_auth)):
    p = _safe(path)
    if not os.path.isdir(p):
        raise HTTPException(status_code=404, detail="not a directory")
    entries = []
    for n in sorted(os.listdir(p)):
        fp = os.path.join(p, n)
        try:
            entries.append({"name": n, "dir": os.path.isdir(fp),
                            "size": os.path.getsize(fp) if os.path.isfile(fp) else None})
        except OSError:
            continue
    return {"path": os.path.relpath(p, DATA_ROOT), "root": DATA_ROOT, "entries": entries}


@app.get("/api/search")
def search(q: str, _=Depends(require_auth), limit: int = 100):
    q = q.lower()
    hits = []
    for root, dirs, files in os.walk(DATA_ROOT):
        for n in files:
            if q in n.lower():
                hits.append(os.path.relpath(os.path.join(root, n), DATA_ROOT))
                if len(hits) >= limit:
                    return {"hits": hits, "truncated": True}
    return {"hits": hits, "truncated": False}


# --- agent (built once) ----------------------------------------------------------
def _build_agent() -> CaliperAgent:
    pack = load_pack(PACK)
    llm = make_llm()
    host = os.environ.get("CALIPER_REMOTE_HOST")
    if host:  # dispatch compute to the lab server
        ex = RemoteExecutor(
            host=host,
            user=os.environ.get("CALIPER_REMOTE_USER", "guest"),
            key_filename=os.environ.get("CALIPER_REMOTE_KEY") or None,
            password=os.environ.get("CALIPER_REMOTE_PASSWORD") or None,
            workspace=os.environ.get("CALIPER_REMOTE_WORKSPACE")
                      or os.environ.get("CALIPER_WORKSPACE", "."),
            python=os.environ.get("CALIPER_REMOTE_PYTHON", "python3"),
            path_prepend=os.environ.get("CALIPER_REMOTE_PATH", ""),
            bwrap=os.environ.get("CALIPER_REMOTE_BWRAP", ""),
        )
    else:
        ex = Executor()
    return CaliperAgent(pack=pack, llm=llm, judge=Judge(llm), executor=ex)


_AGENT = None


def _persist(sid: str, sess: dict, events: list):
    """Save chat history + experience to files (EC2), and mirror to the lab server."""
    try:
        logstore.save_session(WORKSPACE, sid, sess)
        exps = [e for e in events if e.get("type") == "experience"]
        for e in exps:
            logstore.append_experience(WORKSPACE, {"session": sid, **e})
        ex = agent().executor
        if isinstance(ex, RemoteExecutor):
            ex.write_workspace_file(f"sessions/{sid}.log", logstore.session_as_jsonl(sess))
            if exps:
                ex.write_workspace_file("experience/log.jsonl",
                                        "".join(json.dumps({"session": sid, **e}) + "\n" for e in exps),
                                        append=True)
    except Exception:  # noqa: BLE001  -- logging must never break a chat
        pass


def agent():
    global _AGENT
    if _AGENT is None:
        _AGENT = _build_agent()
    return _AGENT


# --- chat (streamed) -------------------------------------------------------------
@app.post("/api/chat")
async def chat(request: Request, _=Depends(require_auth)):
    body = await request.json()
    message = body.get("message", "")
    data_paths = body.get("data_paths", []) or []
    sid = body.get("session_id") or secrets.token_urlsafe(8)
    data_files = [{"path": _safe(p) if not os.path.isabs(p) else p, "label": os.path.basename(p)}
                  for p in data_paths]

    sess = _HISTORY.setdefault(sid, {"title": message[:60] or "New chat", "ts": int(time.time()),
                                     "messages": []})
    sess["messages"].append({"role": "user", "content": message})

    q: "queue.Queue" = queue.Queue()

    def worker():
        events = []
        try:
            result = agent().run(message, data_files, on_event=lambda e: (events.append(e), q.put(e)))
            sess["messages"].append({"role": "assistant", "events": events,
                                     "answer": result.answer, "trust": result.trust,
                                     "decision": result.decision})
            _persist(sid, sess, events)
        except Exception as e:  # noqa: BLE001
            q.put({"type": "error", "message": f"{type(e).__name__}: {e}"})
        finally:
            q.put({"type": "done", "session_id": sid})
            q.put(None)

    threading.Thread(target=worker, daemon=True).start()

    def gen():
        yield f"data: {json.dumps({'type': 'session', 'session_id': sid})}\n\n"
        while True:
            e = q.get()
            if e is None:
                break
            yield f"data: {json.dumps(e)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/api/artifact")
def artifact(path: str, _=Depends(require_auth)):
    """Serve a figure/file produced by a run — confined to the workspace."""
    p = os.path.realpath(path)
    if p != WORKSPACE and not p.startswith(WORKSPACE + os.sep):
        raise HTTPException(status_code=400, detail="outside workspace")
    if not os.path.isfile(p):
        raise HTTPException(status_code=404)
    return FileResponse(p)


@app.get("/api/sessions")
def sessions(_=Depends(require_auth)):
    return [{"id": k, "title": v["title"], "ts": v["ts"]}
            for k, v in sorted(_HISTORY.items(), key=lambda kv: -kv[1]["ts"])]


@app.get("/api/session/{sid}")
def session(sid: str, _=Depends(require_auth)):
    if sid not in _HISTORY:
        raise HTTPException(status_code=404)
    return _HISTORY[sid]


@app.delete("/api/session/{sid}")
def delete_session(sid: str, _=Depends(require_auth)):
    _HISTORY.pop(sid, None)
    logstore.delete_session(WORKSPACE, sid)
    return {"ok": True}


# --- frontend --------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def index():
    with open(os.path.join(HERE, "static", "index.html")) as f:
        return f.read()


def run():  # console entry / `python -m caliper.web.server`
    import uvicorn
    uvicorn.run(app, host=os.environ.get("CALIPER_WEB_HOST", "127.0.0.1"),
                port=int(os.environ.get("CALIPER_WEB_PORT", "8000")))


if __name__ == "__main__":
    run()
