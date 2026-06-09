"""RemoteExecutor — run a code step on a remote compute host over SSH.

Mirrors core.executor.Executor, but the step runs on a remote machine (the lab server)
inside a confined workspace there: reads inputs in place, writes only under the
workspace, and STREAMS stdout back as it runs. This is how the EC2 "brain" dispatches
CPU work to the lab server.

Two execution modes:
  • run()        — synchronous: stream stdout until the step finishes (seconds–minutes).
  • launch_job() — detached: start the step with setsid+nohup so it survives the SSH
                   disconnect and the web request, writing live progress to status.json;
                   the brain polls job_status() (best for hours/days-long jobs).

One SSH connection is reused across calls (cheap per-step overhead); paramiko opens an
independent channel per command, so concurrent requests still run in parallel.
"""
from __future__ import annotations

import json
import os
import posixpath
import shlex
import threading
import time
from typing import Callable, List, Optional

from .executor import ExecResult, check_code

# Runs ON the lab inside the job dir: execs step.py detached, tees output to `log`,
# parses CALIPER_PROGRESS / CALIPER_RESULT lines, and keeps status.json current.
_RUNNER = r'''import json, os, re, subprocess, sys, time
JOB = os.path.dirname(os.path.abspath(__file__))
STATUS = os.path.join(JOB, "status.json")
LOG = os.path.join(JOB, "log")
def load():
    try: return json.load(open(STATUS))
    except Exception: return {}
def save(**kw):
    s = load(); s.update(kw); s["updated"] = int(time.time())
    json.dump(s, open(STATUS, "w"))
prog = re.compile(r"CALIPER_PROGRESS:(\{.*\})")
resre = re.compile(r"CALIPER_RESULT:(\{.*\})")
save(state="running")
result = None
with open(LOG, "w") as lg:
    p = subprocess.Popen([sys.executable, "step.py"], cwd=JOB, env=dict(os.environ),
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    for line in p.stdout:
        lg.write(line); lg.flush()
        m = prog.search(line)
        if m:
            try:
                d = json.loads(m.group(1))
                save(progress=float(d.get("frac", 0)), eta_seconds=d.get("eta"))
            except Exception: pass
        m = resre.search(line)
        if m:
            try: result = json.loads(m.group(1))
            except Exception: pass
    rc = p.wait()
save(state="done" if rc == 0 else "failed",
     progress=1.0 if rc == 0 else load().get("progress", 0.0),
     result=result, returncode=rc)
'''


class RemoteExecutor:
    def __init__(self, host: str, user: str, key_filename: Optional[str] = None,
                 password: Optional[str] = None, port: int = 22,
                 workspace: str = ".", readonly_inputs: Optional[List[str]] = None,
                 python: str = "python3", path_prepend: str = "", timeout: int = 1800,
                 bwrap: str = ""):
        self.host = host
        self.user = user
        self.key_filename = key_filename
        self.password = password
        self.port = port
        self.workspace = workspace
        self.readonly_inputs = list(readonly_inputs or [])
        self.python = python
        self.path_prepend = path_prepend
        self.timeout = timeout
        self.bwrap = bwrap  # path to bubblewrap on the remote; OS-confines writes to workspace
        self._client = None
        self._lock = threading.Lock()

    def _new_client(self):
        import paramiko
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        c.connect(self.host, port=self.port, username=self.user,
                  key_filename=self.key_filename, password=self.password, timeout=30)
        tr = c.get_transport()
        if tr:
            tr.set_keepalive(30)  # keep the reused connection alive through idle periods
        return c

    def _conn(self):
        """A live, reused SSH connection (reconnects if it dropped)."""
        with self._lock:
            t = self._client.get_transport() if self._client else None
            if self._client is None or t is None or not t.is_active():
                self._client = self._new_client()
            return self._client

    def close(self):
        with self._lock:
            if self._client:
                try:
                    self._client.close()
                finally:
                    self._client = None

    def _guard(self, code: str, inputs: Optional[List[dict]]):
        readonly = set(self.readonly_inputs)
        for f in (inputs or []):
            if f.get("path"):
                readonly.add(f["path"])
        return check_code(code, self.workspace, readonly)

    def _env(self, inputs, rundir: str) -> str:
        return (f"CALIPER_INPUTS={shlex.quote(json.dumps(inputs or []))} "
                f"CALIPER_WORKSPACE={shlex.quote(self.workspace)} "
                f"TMPDIR={shlex.quote(rundir)} ")

    def _path(self) -> str:
        return f"PATH={self.path_prepend}:$PATH " if self.path_prepend else ""

    def run(self, code: str, inputs: Optional[List[dict]] = None,
            on_output: Optional[Callable[[str], None]] = None) -> ExecResult:
        violations = self._guard(code, inputs)
        if violations:
            return ExecResult(False, "", "BLOCKED by workspace policy: " + "; ".join(violations),
                              -3, blocked=True)

        c = self._conn()
        run_id = "run_" + os.urandom(4).hex()
        rundir = posixpath.join(self.workspace, ".caliper_runs", run_id)
        _, so, _ = c.exec_command("mkdir -p " + shlex.quote(rundir))
        so.channel.recv_exit_status()

        sftp = c.open_sftp()
        with sftp.open(posixpath.join(rundir, "step.py"), "w") as f:
            f.write(code)
        sftp.close()

        inner = f"{self._path()}{self._env(inputs, rundir)}{shlex.quote(self.python)} step.py"
        if self.bwrap:
            ws = shlex.quote(self.workspace)
            cmd = (f"{shlex.quote(self.bwrap)} --ro-bind / / --dev /dev --proc /proc "
                   f"--tmpfs /tmp --bind {ws} {ws} --chdir {shlex.quote(rundir)} "
                   f"-- /bin/sh -c {shlex.quote(inner)}")
        else:
            cmd = f"cd {shlex.quote(rundir)} && {inner}"

        chan = c.get_transport().open_session()
        chan.settimeout(self.timeout)
        chan.exec_command(cmd)
        chan.setblocking(False)

        out, err = [], []
        deadline = time.time() + self.timeout
        while True:
            progressed = False
            while chan.recv_ready():
                d = chan.recv(8192).decode(errors="replace")
                out.append(d)
                progressed = True
                if on_output and d:
                    on_output(d)
            while chan.recv_stderr_ready():
                err.append(chan.recv_stderr(8192).decode(errors="replace"))
                progressed = True
            if chan.exit_status_ready() and not chan.recv_ready() and not chan.recv_stderr_ready():
                break
            if time.time() > deadline:
                return ExecResult(False, "".join(out), f"timeout after {self.timeout}s", -1)
            if not progressed:
                time.sleep(0.05)
        rc = chan.recv_exit_status()
        return ExecResult(rc == 0, "".join(out), "".join(err), rc)

    # --- detached jobs (hours/days) ------------------------------------------------
    def launch_job(self, code: str, inputs: Optional[List[dict]] = None,
                   eta_seconds: Optional[float] = None) -> dict:
        """Start a step DETACHED on the lab; returns {job_id, state} immediately.

        The step keeps running after the SSH connection and the web request are gone.
        Progress/state live in <workspace>/.caliper_jobs/<job_id>/status.json; poll with
        job_status(). Long loops should print `CALIPER_PROGRESS:{"frac":0-1,"eta":sec}`.
        """
        violations = self._guard(code, inputs)
        if violations:
            return {"state": "failed", "error": "BLOCKED by workspace policy: " + "; ".join(violations),
                    "blocked": True}
        c = self._conn()
        job_id = "job_" + os.urandom(5).hex()
        jobdir = posixpath.join(self.workspace, ".caliper_jobs", job_id)
        _, so, _ = c.exec_command("mkdir -p " + shlex.quote(jobdir))
        so.channel.recv_exit_status()

        sftp = c.open_sftp()
        with sftp.open(posixpath.join(jobdir, "step.py"), "w") as f:
            f.write(code)
        with sftp.open(posixpath.join(jobdir, "runner.py"), "w") as f:
            f.write(_RUNNER)
        with sftp.open(posixpath.join(jobdir, "status.json"), "w") as f:
            f.write(json.dumps({"id": job_id, "state": "running", "progress": 0.0,
                                "eta_seconds": eta_seconds, "started": int(time.time()),
                                "updated": int(time.time())}))
        sftp.close()

        # `</dev/null` detaches stdin so the SSH channel closes immediately — otherwise
        # exec_command would block until the (hours-long) job ends, defeating the point.
        launch = (f"cd {shlex.quote(jobdir)} && {self._path()}{self._env(inputs, jobdir)}"
                  f"setsid nohup {shlex.quote(self.python)} runner.py >/dev/null 2>&1 </dev/null & echo $!")
        _, so, _ = c.exec_command(launch)
        pid = so.readline().strip()
        return {"job_id": job_id, "state": "running", "pid": pid}

    def job_status(self, job_id: str, log_tail: int = 4000) -> dict:
        """Read a detached job's status.json (+ a tail of its log) over SSH."""
        c = self._conn()
        jobdir = posixpath.join(self.workspace, ".caliper_jobs", job_id)
        sftp = c.open_sftp()
        try:
            with sftp.open(posixpath.join(jobdir, "status.json")) as f:
                st = json.loads(f.read().decode())
        except Exception:
            return {"id": job_id, "state": "unknown"}
        try:
            with sftp.open(posixpath.join(jobdir, "log")) as f:
                st["log_tail"] = f.read().decode(errors="replace")[-log_tail:]
        except Exception:
            st["log_tail"] = ""
        finally:
            sftp.close()
        return st

    def write_workspace_file(self, relpath: str, content: str, append: bool = False) -> None:
        """Write a small log file under the remote workspace (mirrors EC2 logs to the lab)."""
        c = self._conn()
        full = posixpath.join(self.workspace, relpath)
        _, so, _ = c.exec_command("mkdir -p " + shlex.quote(posixpath.dirname(full)))
        so.channel.recv_exit_status()
        sftp = c.open_sftp()
        with sftp.open(full, "a" if append else "w") as f:
            f.write(content)
        sftp.close()

    def provision(self, install_cmd: str, on_output: Optional[Callable[[str], None]] = None) -> ExecResult:
        """Run a vetted install command (from the pack allow-list) on the remote host."""
        c = self._conn()
        chan = c.get_transport().open_session()
        chan.settimeout(self.timeout)
        chan.exec_command(f"cd {shlex.quote(self.workspace)} && {self._path()}{install_cmd}")
        out = []
        chan.setblocking(True)
        f = chan.makefile()
        for line in f:
            out.append(line)
            if on_output:
                on_output(line)
        rc = chan.recv_exit_status()
        return ExecResult(rc == 0, "".join(out), "", rc)
