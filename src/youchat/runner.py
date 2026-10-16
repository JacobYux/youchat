"""`youchat runner`: per-host executor. Long-polls the hub for wake jobs (offline team members on
THIS host that have unread messages) and runs one headless turn of the member's own session:

  claude  -p PROMPT -r <session>         (new: -n <name>)
  codex   exec ... resume <session> PROMPT (new: exec PROMPT)
  agy     -p PROMPT --conversation <id>  (new: without --conversation)

The session runs with YOUCHAT_NAME=<member> so its youchat identity binds automatically. A session
that is currently open somewhere on this host is never driven in parallel (reported as 'busy').
"""
from __future__ import annotations

import fcntl
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from youchat.common import HubClient, HubError, ensure_private_dir, home_dir, short_host
from youchat.sessions import running_session_ids

BYPASS = ("bypass", "bypasspermissions", "dangerously-skip-permissions", "yolo")
EXTRA_PATH = [os.path.expanduser("~/.local/bin"), "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"]
ID_KEYS = ("session_id", "sessionId", "thread_id", "threadId", "conversation_id", "conversationId")


def log(*a: Any) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, file=sys.stderr, flush=True)


def search_path() -> str:
    parts = os.environ.get("PATH", "").split(os.pathsep)
    return os.pathsep.join(parts + [p for p in EXTRA_PATH if p not in parts])


def which(exe: str) -> str:
    return shutil.which(exe, path=search_path()) or exe


def build_command(job: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    """Return (argv, notes) for one headless turn of the member's session."""
    cli, sid, prompt = job["cli"], job.get("session_id") or "", job["prompt"]
    o = job.get("options") or {}
    model, effort = job.get("model") or "", job.get("effort") or ""
    perm = str(o.get("permission_mode") or "").lower()
    notes: List[str] = []
    if cli == "claude":
        cmd = [which("claude"), "-p", prompt, "--output-format", "json"]
        cmd += ["-r", sid] if sid else ["-n", job["agent"]]
        if model:
            cmd += ["--model", model]
        if effort:
            cmd += ["--effort", effort]
        if o.get("ultra"):
            notes.append("ultracode requested but Claude Code has no CLI flag for it; using saved defaults")
        if perm in BYPASS:
            cmd += ["--dangerously-skip-permissions"]
        elif perm:
            cmd += ["--permission-mode", perm]
        else:  # must stay last: --allowedTools is variadic
            cmd += ["--allowedTools", "mcp__youchat"]
    elif cli == "codex":
        cmd = [which("codex"), "exec", "--skip-git-repo-check", "--json"]
        if model:
            cmd += ["-m", model]
        eff = "ultra" if o.get("ultra") else effort
        if eff:
            cmd += ["-c", 'model_reasoning_effort="%s"' % eff]
        if perm in BYPASS:
            cmd += ["--dangerously-bypass-approvals-and-sandbox"]
        elif perm in ("full-auto", "auto"):
            cmd += ["--full-auto"]
        cmd += ["resume", sid, prompt] if sid else [prompt]
    elif cli == "antigravity":
        if o.get("boost") and job.get("heavy"):
            prompt = "/boost " + prompt  # /boost is a per-task command; print mode expands slash commands
            notes.append("boost")
        cmd = [which("agy"), "-p", prompt, "--output-format", "json"]
        if sid:
            cmd += ["--conversation", sid]
        if model:
            cmd += ["--model", model]
        if effort:
            cmd += ["--effort", effort]
        if perm in BYPASS:
            cmd += ["--dangerously-skip-permissions"]
        elif perm in ("accept-edits", "auto", "plan"):
            cmd += ["--mode", perm]
    else:
        raise ValueError("cannot wake cli %r" % cli)
    return cmd, notes


def extract_session_id(text: str) -> Optional[str]:
    """Find the session/thread/conversation id in a CLI's JSON (or JSON-lines) output."""
    candidates: List[Any] = []
    try:
        candidates.append(json.loads(text))
    except ValueError:
        pass
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                candidates.append(json.loads(line))
            except ValueError:
                pass

    def walk(o: Any) -> Optional[str]:
        if isinstance(o, dict):
            for k in ID_KEYS:
                if isinstance(o.get(k), str) and len(o[k]) >= 8:
                    return o[k]
            for v in o.values():
                r = walk(v)
                if r:
                    return r
        elif isinstance(o, list):
            for v in o:
                r = walk(v)
                if r:
                    return r
        return None

    for c in candidates:
        r = walk(c)
        if r:
            return r
    return None


def notify_desktop(title: str, body: str) -> None:
    if sys.platform != "darwin":
        return
    esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')[:240]  # noqa: E731
    try:
        subprocess.run(["osascript", "-e", 'display notification "%s" with title "%s"' % (esc(body), esc(title))],
                       timeout=10, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        pass


class Runner:
    def __init__(self, host: Optional[str] = None, max_jobs: int = 2, pre: Optional[str] = None):
        self.host = host or short_host()
        self.max_jobs = max(1, max_jobs)
        self.pre = pre
        self.active: Dict[str, threading.Thread] = {}
        self.logdir = ensure_private_dir(os.path.join(home_dir(), "wakes"))

    def run(self) -> None:
        hub = HubClient()
        backoff = 1.0
        registered = False
        log("youchat runner for host %r (max %d parallel wakes)" % (self.host, self.max_jobs))
        while True:
            try:
                if not registered:
                    hub.register(kind="runner", name="runner@%s" % self.host, force=True,
                                 description="executes wakes on %s" % self.host)
                    registered = True
                    backoff = 1.0
                self.active = {a: t for a, t in self.active.items() if t.is_alive()}
                free = self.max_jobs - len(self.active)
                if free <= 0:
                    time.sleep(2)
                    continue
                res = hub.call("runner_wait", {"host": self.host, "timeout": 15, "max_jobs": free}, timeout=45)
            except Exception as e:  # noqa: BLE001 - the runner must survive anything and retry
                log("hub unavailable (%s: %s); retrying in %ds" % (type(e).__name__, e, backoff))
                registered = False
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)
                continue
            for n in res.get("human") or []:
                notify_desktop("youchat · %s from %s" % (n["kind"], n["sender"]), n["body"])
            for job in res.get("jobs") or []:
                t = threading.Thread(target=self._run_job, args=(job,), daemon=True)
                self.active[job["agent"]] = t
                t.start()

    def _done(self, job: Dict[str, Any], exit_code: Optional[int], session_id: Optional[str], note: str) -> None:
        for attempt in range(5):
            try:
                HubClient().call("runner_done", {"agent": job["agent"], "wake_id": job["wake_id"],
                                                 "exit_code": exit_code, "session_id": session_id, "note": note})
                return
            except HubError as e:
                log("could not report wake %s (%s); retrying" % (job["wake_id"], e))
                time.sleep(2 ** attempt)

    def _run_job(self, job: Dict[str, Any]) -> None:
        agent, sid = job["agent"], job.get("session_id") or ""
        if sid and sid in running_session_ids():
            log("wake %s: %s session %s is open on this host; not driving it in parallel" % (job["wake_id"], agent, sid))
            self._done(job, None, None, "busy")
            return
        try:
            cmd, notes = build_command(job)
        except ValueError as e:
            self._done(job, None, None, str(e))
            return
        if self.pre:  # e.g. 'claude_on' to enable a proxy defined in ~/.bashrc
            cmd = ["bash", "-ic", '%s >/dev/null 2>&1; exec "$@"' % self.pre, "youchat-wake"] + cmd
        env = dict(os.environ, YOUCHAT_NAME=agent, YOUCHAT_KIND=job["cli"], PATH=search_path())
        cwd = job.get("cwd") or os.path.expanduser("~")
        if not os.path.isdir(cwd):
            cwd = os.path.expanduser("~")
        logpath = os.path.join(self.logdir, "%s-%s.log" % (agent, job["wake_id"]))
        log("wake %s: %s (%s%s) in %s%s" % (job["wake_id"], agent, job["cli"], " " + sid[:8] if sid else " NEW",
                                             cwd, ("  [" + "; ".join(notes) + "]") if notes else ""))
        started = time.time()
        code: Optional[int] = None
        note = "; ".join(notes)
        try:
            with open(logpath, "wb") as out:
                os.chmod(logpath, 0o600)
                out.write(("$ %s\n\n" % " ".join(cmd[:3] + ["..."])).encode())
                out.flush()
                code = subprocess.run(cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=out,
                                      stderr=subprocess.STDOUT,
                                      timeout=max(60, float(job.get("lease_seconds", 10800)) - 60)).returncode
        except subprocess.TimeoutExpired:
            note = (note + "; " if note else "") + "timed out"
        except OSError as e:
            note = (note + "; " if note else "") + "failed to start: %s" % e
        new_sid = None
        if not sid:
            try:
                with open(logpath, encoding="utf-8", errors="replace") as f:
                    new_sid = extract_session_id(f.read())
            except OSError:
                pass
        log("wake %s: %s finished exit=%s in %.0fs (log %s)" % (job["wake_id"], agent, code, time.time() - started,
                                                              logpath))
        self._done(job, code, new_sid, note)


def run_runner(host: Optional[str] = None, max_jobs: int = 2, pre: Optional[str] = None,
               daemon: bool = False) -> None:
    ensure_private_dir(home_dir())
    lock_path = os.path.join(home_dir(), "runner-%s.lock" % (host or short_host()))
    lock = open(lock_path, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        if daemon:  # cron/launchd idempotency: already running
            return
        raise SystemExit("a youchat runner for this host is already running (%s)" % lock_path)
    if daemon:
        from youchat.hub import _daemonize
        lock.close()  # re-acquire in the daemon child (flock is not inherited across our double fork safely)
        _daemonize(os.path.join(home_dir(), "runner.log"))
        lock = open(lock_path, "w")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os._exit(0)
    Runner(host, max_jobs, pre).run()
