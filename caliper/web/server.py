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
from typing import List

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse

from ..core.agent import CaliperAgent
from ..core.executor import Executor
from ..core.registry import load_pack
from ..config import get_workspace
from ..llm import make_llm
from ..trust.judge import Judge

HERE = os.path.dirname(__file__)
DATA_ROOT = os.path.realpath(os.environ.get("CALIPER_DATA_ROOT", os.getcwd()))
WORKSPACE = os.path.realpath(get_workspace() or os.path.join(os.getcwd(), "caliper_workspace"))
PACK = os.environ.get("CALIPER_PACK", "bio")
_SESSIONS = set()           # auth tokens
_HISTORY: dict = {}         # session_id -> {"title":..., "ts":..., "messages":[...]}

app = FastAPI(title="Caliper")


# --- auth ------------------------------------------------------------------------
def require_auth(request: Request):
    pw = os.environ.get("CALIPER_WEB_PASSWORD")
    if not pw:
        return  # dev-open (use Cloudflare Access / a password in production)
    if request.cookies.get("caliper_session") not in _SESSIONS:
        raise HTTPException(status_code=401, detail="login required")


@app.post("/api/login")
async def login(request: Request):
    body = await request.json()
    pw = os.environ.get("CALIPER_WEB_PASSWORD")
    if pw and body.get("password") == pw:
        tok = secrets.token_urlsafe(24)
        _SESSIONS.add(tok)
        r = JSONResponse({"ok": True})
        r.set_cookie("caliper_session", tok, httponly=True, samesite="lax")
        return r
    return JSONResponse({"ok": False}, status_code=401)


@app.get("/api/whoami")
def whoami(_=Depends(require_auth)):
    return {"data_root": DATA_ROOT, "pack": PACK,
            "auth_required": bool(os.environ.get("CALIPER_WEB_PASSWORD"))}


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
    return CaliperAgent(pack=pack, llm=llm, judge=Judge(llm), executor=Executor())


_AGENT = None


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
