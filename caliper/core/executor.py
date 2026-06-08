"""Code executor with a confined working directory.

Safety model — **read anywhere, write only in the workspace**:
the agent may READ any path it has OS permission to, but every WRITE, temp file, and
output is confined to one workspace directory the user chooses. Two layers:

  1. Workspace confinement (portable): each step runs in a fresh subdir under the
     workspace, with TMPDIR redirected there, plus a static guard that BLOCKS
     catastrophic or read-only-input-violating operations before they run.
  2. OS sandbox (Linux, auto when available): if `bwrap` (bubblewrap) is present, the
     step runs with the whole filesystem mounted READ-ONLY except the workspace and
     /tmp — so writing outside the workspace is impossible at the OS level.

Configure the workspace via `caliper init`, the CALIPER_WORKSPACE env var, or
Executor(workspace=...). If unset, it defaults to ./caliper_workspace.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from typing import Iterable, List, Optional

from ..config import DEFAULT_TIMEOUT_SEC, get_workspace


class WorkspacePolicyError(RuntimeError):
    pass


@dataclass
class ExecResult:
    ok: bool
    stdout: str
    stderr: str
    returncode: int
    blocked: bool = False


# Operations refused outright, anywhere in a step's code.
_CATASTROPHIC = [
    (re.compile(r"\brm\s+-\w*[rf]\w*\b"), "recursive/forced rm"),
    (re.compile(r"\bshutil\.rmtree\b"), "shutil.rmtree"),
    (re.compile(r"\b(mkfs|fdisk|parted)\b"), "disk-formatting command"),
    (re.compile(r"\bdd\b[^\n]*\bof=/"), "dd writing to an absolute path/device"),
    (re.compile(r"\bsudo\b"), "sudo / privilege escalation"),
    (re.compile(r":\s*\(\)\s*\{\s*:\s*\|\s*:"), "fork bomb"),
    (re.compile(r"\bchmod\s+-R\b|\bchown\s+-R\b"), "recursive chmod/chown"),
    (re.compile(r">\s*/dev/(sd|nvme|disk)"), "write to a raw disk device"),
]

# Idioms that write to / delete / move a path.
_WRITE_VERBS = ("rm ", "rmdir", "mv ", "shutil.move", "os.remove", "os.unlink",
                "os.rename", "truncate ", "shutil.rmtree", "rsync")


def check_code(code: str, workspace: str, readonly_paths: Iterable[str]) -> List[str]:
    """Return policy violations for a step's code; empty list means allowed.

    Conservative by design: a false block is recoverable (the agent retries), a missed
    destructive op is not. This is the portable policy layer; `bwrap` is the hard layer.
    """
    violations: List[str] = []
    for pat, msg in _CATASTROPHIC:
        if pat.search(code):
            violations.append(msg)
    for ro in readonly_paths:
        if ro and ro in code:
            if any(w in code for w in _WRITE_VERBS) or re.search(r">>?\s*" + re.escape(ro), code):
                violations.append(f"write/delete touching read-only input: {ro}")
    return sorted(set(violations))


class Executor:
    def __init__(self, workspace: Optional[str] = None,
                 readonly_inputs: Optional[List[str]] = None,
                 confine: str = "auto", timeout: int = DEFAULT_TIMEOUT_SEC,
                 python: Optional[str] = None):
        ws = workspace or get_workspace()
        self.workspace = (os.path.abspath(os.path.expanduser(ws)) if ws
                          else os.path.join(os.getcwd(), "caliper_workspace"))
        self.workspace_explicit = bool(ws)
        self.readonly_inputs = [os.path.abspath(os.path.expanduser(p)) for p in (readonly_inputs or [])]
        self.confine = confine
        self.timeout = timeout
        self.python = python or sys.executable

    def _use_bwrap(self) -> bool:
        if self.confine in ("none", "guard"):
            return False
        if self.confine == "bwrap":
            return True
        return (sys.platform.startswith("linux") and shutil.which("bwrap") is not None)

    def run(self, code: str, inputs: Optional[List[dict]] = None) -> ExecResult:
        os.makedirs(self.workspace, exist_ok=True)

        readonly = set(self.readonly_inputs)
        for f in (inputs or []):
            if f.get("path"):
                readonly.add(os.path.abspath(os.path.expanduser(f["path"])))

        violations = check_code(code, self.workspace, readonly)
        if violations:
            return ExecResult(False, "", "BLOCKED by workspace policy: " + "; ".join(violations),
                              -3, blocked=True)

        runs = os.path.join(self.workspace, ".caliper_runs")
        os.makedirs(runs, exist_ok=True)
        rundir = tempfile.mkdtemp(prefix="run_", dir=runs)
        script = os.path.join(rundir, "step.py")
        with open(script, "w") as fh:
            fh.write(code)

        env = dict(os.environ)
        env["CALIPER_INPUTS"] = json.dumps(inputs or [])
        env["TMPDIR"] = rundir
        env["CALIPER_WORKSPACE"] = self.workspace

        if self._use_bwrap():
            cmd = ["bwrap", "--die-with-parent", "--unshare-pid",
                   "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
                   "--tmpfs", "/tmp", "--bind", self.workspace, self.workspace,
                   self.python, script]
        else:
            cmd = [self.python, script]

        try:
            proc = subprocess.run(cmd, cwd=rundir, env=env, capture_output=True,
                                  text=True, timeout=self.timeout)
        except subprocess.TimeoutExpired as e:
            return ExecResult(False, e.stdout or "", f"timeout after {self.timeout}s", -1)
        return ExecResult(proc.returncode == 0, proc.stdout, proc.stderr, proc.returncode)
