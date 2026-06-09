"""File-based persistence — no database.

Chat history is one JSON file per session under <workspace>/.sessions/, and a running
"experience" log (tool installs, errors, fixes) as JSONL under <workspace>/.experience/.
On the EC2 these live in the EC2 workspace; the web layer also mirrors them to the lab
server's workspace. Large data is never copied — only these small logs.
"""
from __future__ import annotations

import glob
import json
import os
import time
from typing import Dict


def _sessions_dir(ws: str) -> str:
    return os.path.join(ws, ".sessions")


def save_session(ws: str, sid: str, session: dict) -> str:
    d = _sessions_dir(ws)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, sid + ".json")
    with open(path, "w") as f:
        json.dump(session, f)
    return path


def delete_session(ws: str, sid: str) -> None:
    try:
        os.remove(os.path.join(_sessions_dir(ws), sid + ".json"))
    except OSError:
        pass


def load_sessions(ws: str) -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    for p in glob.glob(os.path.join(_sessions_dir(ws), "*.json")):
        try:
            sid = os.path.splitext(os.path.basename(p))[0]
            out[sid] = json.load(open(p))
        except (OSError, json.JSONDecodeError):
            continue
    return out


def append_experience(ws: str, record: dict) -> None:
    d = os.path.join(ws, ".experience")
    os.makedirs(d, exist_ok=True)
    rec = {"ts": int(time.time()), **record}
    with open(os.path.join(d, "log.jsonl"), "a") as f:
        f.write(json.dumps(rec) + "\n")


def session_as_jsonl(session: dict) -> str:
    """A session rendered as JSONL text (for mirroring to the lab as a log file)."""
    lines = [json.dumps({"title": session.get("title"), "ts": session.get("ts")})]
    lines += [json.dumps(m) for m in session.get("messages", [])]
    return "\n".join(lines) + "\n"
