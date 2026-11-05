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
import hashlib
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
TMUX_QUIET = 45.0          # s a tmux pane's screen must stay unchanged before we type into it
TMUX_READ_TIMEOUT = 180.0  # s to wait for the agent to read its messages after the wake line was typed


def log(*a: Any) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, file=sys.stderr, flush=True)


def search_path() -> str:
    parts = os.environ.get("PATH", "").split(os.pathsep)
    return os.pathsep.join(parts + [p for p in EXTRA_PATH if p not in parts])


def which(exe: str) -> str:
    for d in search_path().split(os.pathsep):
        for name in (exe, exe + "-bin"):
            cand = os.path.join(d, name)
            if os.path.isfile(cand) and os.access(cand, os.X_OK):
                try:
                    if os.path.getsize(cand) < 512 and b"youchat-pty-wrapper" in open(cand, "rb").read(512):
                        continue
                except OSError:
                    pass
                return cand
    if exe == "codex":
        import glob as _glob
        cands = sorted(_glob.glob("/opt/homebrew/Caskroom/codex/*/bin/codex"))
        if cands:
            return cands[-1]
    if exe == "claude":
        import glob as _glob
        cands = sorted(_glob.glob(os.path.expanduser("~/.local/share/claude/versions/*")))
        if cands:
            return cands[-1]
    return shutil.which(exe, path=search_path()) or exe


def build_command(job: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    """Return (argv, notes) for one headless turn of the member's session."""
    from youchat.hub import CLI_DEFAULTS
    cli, sid, prompt = job["cli"], job.get("session_id") or "", job["prompt"]
    dfl = CLI_DEFAULTS.get(cli, {})
    o = job.get("options") or {}
    model = job.get("model") or dfl.get("model") or ""
    effort = job.get("effort") or dfl.get("effort") or ""
    perm = str(o.get("permission_mode") or "bypass").lower()
    if perm in ("default", "normal"):
        perm = ""
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
            cmd += ["--approve-for-me"]
        cmd += ["resume", sid, prompt] if sid else [prompt]
    elif cli == "antigravity":
        if o.get("boost", True) and job.get("heavy"):
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


CONT_PROMPT = (
    "[youchat] 你（%(agent)s）上一轮后台处理中途中断（%(err)s）。\n"
    "请先调用 check_inbox 查看是否还有未读消息，并检查上一轮进行到了哪一步（是否有半写的文件、已提交的作业或尚未发出的汇报），"
    "从断点继续完成任务并调用 send_message 回复；若上一轮其实已经完成了回复，直接结束本轮即可。"
)


def retry_delays() -> List[float]:
    raw = os.environ.get("YOUCHAT_RETRY_DELAYS", "15,45")
    return [float(x) for x in raw.split(",") if x.strip()]


def _parse_json_objects(text: str) -> List[Dict[str, Any]]:
    objs: List[Dict[str, Any]] = []
    try:
        o = json.loads(text)
        if isinstance(o, dict):
            objs.append(o)
    except ValueError:
        pass
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                o = json.loads(line)
                if isinstance(o, dict):
                    objs.append(o)
            except ValueError:
                pass
    return objs


def _agy_transcript_incomplete(cid: str) -> Optional[str]:
    """Check ~/.gemini/antigravity-cli/brain/<cid>/.system_generated/logs/transcript.jsonl.
    Note: `agy -p --output-format json` sets status=ERROR forever if *any* past turn in the
    conversation ever had an ERROR_MESSAGE step. The transcript's final step tells us whether
    *this* turn actually finished (PLANNER_RESPONSE + DONE + no pending tool_calls) or aborted."""
    if not cid:
        return None
    path = os.path.expanduser("~/.gemini/antigravity-cli/brain/%s/.system_generated/logs/transcript.jsonl" % cid)
    if not os.path.isfile(path):
        return None
    try:
        last = None
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.strip():
                    last = line
        if not last:
            return None
        d = json.loads(last)
    except (OSError, ValueError):
        return None
    st, tp, tools = d.get("status"), d.get("type"), d.get("tool_calls") or []
    if tp == "ERROR_MESSAGE" or st not in ("DONE", None) or tools:
        return "transcript ended at %s (%s)" % (tp, st)
    return "OK"


def turn_error(cli: str, code: Optional[int], text: str, sid: Optional[str] = None) -> Optional[str]:
    """Return a short reason if a headless CLI turn aborted mid-work (even when exit code is 0)."""
    if code is None:
        return "timed out"
    if code != 0:
        return "exit %d" % code
    objs = _parse_json_objects(text)
    if cli == "claude":
        for d in reversed(objs):
            if d.get("is_error") or d.get("subtype") == "error_during_execution" or d.get("terminal_reason") in (
                "aborted_streaming", "api_error"
            ):
                return str(d.get("terminal_reason") or d.get("subtype") or "is_error")[:120]
    elif cli == "antigravity":
        last = objs[-1] if objs else {}
        cid = sid or str(last.get("conversation_id") or "")
        tr = _agy_transcript_incomplete(cid)
        if tr == "OK":
            return None
        if tr:
            return str(last.get("error") or tr)[:120]
        if last.get("status") == "ERROR" and not str(last.get("response") or "").strip():
            return str(last.get("error") or "status=ERROR")[:120]
    elif cli == "codex":
        completed = any(d.get("type") == "turn.completed" for d in objs)
        for d in reversed(objs):
            if d.get("type") in ("turn.failed", "error") and not completed:
                err = d.get("error")
                msg = err.get("message") if isinstance(err, dict) else (err or d.get("message") or d.get("type"))
                return str(msg)[:120]
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
        self.last_unwrapped_notice: Dict[str, float] = {}
        self.pane_seen: Dict[str, Tuple[str, float]] = {}  # tmux pane -> (hash of its screen, unchanged since)
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

    def _should_yield_open(self, job: Dict[str, Any], sid: str) -> bool:
        cli, agent = job["cli"], job["agent"]
        if cli == "antigravity" and sid:
            desktop_dir = os.path.expanduser("~/.gemini/antigravity/brain/%s" % sid)
            cli_db = os.path.expanduser("~/.gemini/antigravity-cli/conversations/%s.db" % sid)
            if os.path.isdir(desktop_dir) and not os.path.exists(cli_db):
                now = time.time()
                if now - self.last_unwrapped_notice.get(agent, 0.0) >= 300.0:
                    self.last_unwrapped_notice[agent] = now
                    notify_desktop(
                        "youchat · %s 有 %d 条新消息" % (agent, job.get("unread") or 1),
                        "该会话位于 Antigravity 桌面版（GUI），请在窗口内触发一次 check_inbox。",
                    )
                log("wake %s: %s session %s is an Antigravity Desktop GUI session; notified user"
                    % (job["wake_id"], agent, sid))
                return True
        if not sid or sid not in running_session_ids():
            return False
        from youchat.pty_wrap import is_session_idle
        from youchat.sessions import claude_live_records, live_push_session_ids
        if cli != "claude" and "watched" in job:
            live = job["watched"]  # the hub knows whether a `youchat pty` wrapper is listening right now
        else:
            live = sid in live_push_session_ids()  # Claude: channels flag on its command line; older hubs
        if live:
            log("wake %s: %s session %s is open with live push; not driving in parallel" % (job["wake_id"], agent, sid))
            return True
        if cli == "claude":
            rec = claude_live_records().get(sid)
            sock_path = (rec or {}).get("messagingSocketPath") or ""
            if sock_path and os.path.exists(sock_path):
                return False
            if rec and rec.get("status") == "busy":
                log("wake %s: %s session %s is actively running a turn (status=busy); yielding" % (job["wake_id"], agent, sid))
                return True
        if not is_session_idle(cli, sid):
            log("wake %s: %s session %s is actively running a turn; yielding" % (job["wake_id"], agent, sid))
            return True
        if cli in ("antigravity", "codex"):
            now = time.time()
            if now - self.last_unwrapped_notice.get(agent, 0.0) >= 300.0:
                self.last_unwrapped_notice[agent] = now
                cmd_name = "agy" if cli == "antigravity" else "codex"
                notify_desktop(
                    "youchat · %s 有 %d 条新消息" % (agent, job.get("unread") or 1),
                    "该 %s 窗口在旧终端标签页启动（未启用自动推送）；请在窗口内输入 check_inbox 或重开一次 %s。"
                    % (cmd_name, cmd_name),
                )
            log("wake %s: %s session %s is open in unwrapped %s (locked); notified user"
                % (job["wake_id"], agent, sid, cli))
            return True
        log("wake %s: %s session %s is open in unwrapped %s window and idle; driving turn"
            % (job["wake_id"], agent, sid, cli))
        return False

    def _drive_claude_socket(self, job: Dict[str, Any], sid: str) -> bool:
        import glob
        import socket
        from youchat.pty_wrap import is_session_idle
        from youchat.sessions import claude_live_records, live_push_session_ids
        if not sid or sid in live_push_session_ids():
            return False
        rec = claude_live_records().get(sid)
        sock_path = (rec or {}).get("messagingSocketPath") or ""
        pid = int((rec or {}).get("pid") or 0)
        if not sock_path or not pid or not os.path.exists(sock_path):
            return False
        agent = job["agent"]
        started = time.time()
        deadline = started + max(60.0, float(job.get("lease_seconds", 10800)) - 60.0)
        if rec.get("status") == "busy" or not is_session_idle("claude", sid):
            # Don't hold a runner slot while the window works (a long turn would starve every other
            # agent's wakes). Report busy; the hub retries after its busy back-off.
            log("wake %s: %s session %s is busy in live Claude window; retrying later"
                % (job["wake_id"], agent, sid[:8]))
            self._done(job, None, None, "busy")
            return True
        try:
            sp = os.path.expanduser("~/.claude/settings.json")
            if os.path.isfile(sp):
                with open(sp, encoding="utf-8") as f:
                    cfg = json.load(f)
                if isinstance(cfg, dict) and cfg.get("crossSessionInbound") != "accept":
                    cfg["crossSessionInbound"] = "accept"
                    with open(sp, "w", encoding="utf-8") as f:
                        json.dump(cfg, f, ensure_ascii=False, indent=2)
                    time.sleep(0.3)
        except Exception:
            pass
        token = ""
        for kp in glob.glob(os.path.expanduser("~/.claude/sessions/%d.*.key" % pid)):
            try:
                token = json.load(open(kp, encoding="utf-8")).get("peerToken") or ""
                if token:
                    break
            except Exception:
                continue
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(5.0)
        try:
            s.connect(sock_path)
            if token:
                s.sendall((json.dumps({"type": "auth", "token": token}) + "\n").encode("utf-8"))
            payload = {"type": "user", "session_id": sid, "priority": "next", "from": "youchat",
                       "message": {"role": "user", "content": job["prompt"]}}
            s.sendall((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        except OSError:
            return False
        finally:
            s.close()
        logpath = os.path.join(self.logdir, "%s-%s.log" % (agent, job["wake_id"]))
        inject_ts = time.time()
        log("wake %s: %s (claude %s) injected into live session via %s"
            % (job["wake_id"], agent, sid[:8], sock_path))
        try:
            with open(logpath, "wb") as out:
                os.chmod(logpath, 0o600)
                out.write(("injected into live claude socket %s (pid=%d, sid=%s)\n\n%s\n"
                           % (sock_path, pid, sid, job["prompt"])).encode("utf-8"))
        except OSError:
            pass
        saw_busy = False
        while time.time() < deadline:
            time.sleep(1.5)
            cur = claude_live_records().get(sid)
            if not cur:
                break
            if cur.get("status") == "busy" or not is_session_idle("claude", sid):
                saw_busy = True
                continue
            if saw_busy or (time.time() - inject_ts >= 8.0 and is_session_idle("claude", sid)):
                break
        log("wake %s: %s finished exit=0 in %.0fs (live-socket %s)"
            % (job["wake_id"], agent, time.time() - started, sock_path))
        self._done(job, 0, None, "live-socket")
        return True

    @staticmethod
    def _tmux_pane_for(job: Dict[str, Any], sid: str) -> Optional[Tuple[str, str, bool]]:
        """(pane id, tmux session, pane in copy mode) of the tmux pane where the member's session is open in a
        codex/agy TUI that is NOT under `youchat pty` (e.g. restarted by hand after a self-update). The pane must
        show the session id on the TUI's command line, or carry the member's name as its title (codex titles
        panes "<session name> | <dir>"; `youchat open` titles new windows after the member)."""
        from youchat.sessions import _classify_agent_proc, _ps
        exe = which("tmux")
        if not os.path.isabs(exe):
            return None
        try:
            out = subprocess.run([exe, "list-panes", "-a", "-F",
                                  "#{pane_id}|#{pane_pid}|#{pane_in_mode}|#{session_name}|#{pane_title}"],
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return None
        if out.returncode:
            return None  # no tmux server
        children: Dict[int, List[int]] = {}
        args_of: Dict[int, str] = {}
        for p, pp, a in _ps():
            if p.isdigit() and pp.isdigit():
                children.setdefault(int(pp), []).append(int(p))
                args_of[int(p)] = a
        want = "agy" if job["cli"] == "antigravity" else "codex"
        for line in out.stdout.decode("utf-8", "replace").splitlines():
            parts = line.split("|", 4)
            if len(parts) < 5 or not parts[1].isdigit():
                continue
            pane, pid, in_mode, sess, title = parts
            tuis, stack = [], [int(pid)]
            while stack:
                p = stack.pop()
                a = args_of.get(p, "")
                if "youchat" in a and " pty " in a:
                    continue  # wrapped: that window has its own live push
                if _classify_agent_proc(a) == want:
                    tuis.append(a)
                    continue
                stack.extend(children.get(p, []))
            if len(tuis) == 1 and ((sid and sid in tuis[0]) or title.split(" | ")[0].strip() == job["agent"]):
                return pane, sess, in_mode == "1"
        return None

    def _drive_tmux_pane(self, job: Dict[str, Any], sid: str) -> bool:
        """Live push for an unwrapped codex/agy TUI open in a tmux pane: type the wake line into the pane, the way
        `youchat pty` would. True = handled (wake reported); False = no such pane, use the usual handling."""
        from youchat.pty_wrap import _session_mtime, is_session_idle, wake_line
        cli, agent = job["cli"], job["agent"]
        if cli not in ("codex", "antigravity") or job.get("watched"):
            return False
        found = self._tmux_pane_for(job, sid)
        if not found:
            return False
        pane, sess, in_mode = found
        exe = which("tmux")
        # Typing shows on screen (also under iTerm2 `tmux -CC`, where tmux sees keys as send-keys commands and
        # client_activity never moves). Only type once the screen has not changed for TMUX_QUIET seconds, so a
        # half-written prompt is never submitted. (agy re-sends invisible mode escapes while idle: compare the
        # visible screen, not output activity.)
        screen = subprocess.run([exe, "capture-pane", "-p", "-t", pane], stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL).stdout
        digest, now = hashlib.sha1(screen).hexdigest(), time.time()
        if self.pane_seen.get(pane, ("",))[0] != digest:
            self.pane_seen[pane] = (digest, now)
        changed = now - self.pane_seen[pane][1] < TMUX_QUIET
        if in_mode or changed or not is_session_idle(cli, sid):
            log("wake %s: %s is open in tmux pane %s but busy (%s); retrying later" % (
                job["wake_id"], agent, pane, "scrolling" if in_mode else "screen changed recently" if changed
                else "working"))
            self._done(job, None, None, "busy")
            return True
        boost = cli == "antigravity" and bool((job.get("options") or {}).get("boost", True)) and bool(job.get("heavy"))
        before = _session_mtime(cli, sid)
        subprocess.run([exe, "send-keys", "-t", pane, "-l", "--", wake_line(agent, job.get("unread") or 1, boost=boost)])
        time.sleep(0.15)
        subprocess.run([exe, "send-keys", "-t", pane, "Enter"])
        if boost:  # the /boost popup can swallow the first Enter
            time.sleep(1.2)
            if _session_mtime(cli, sid) <= before and is_session_idle(cli, sid):
                subprocess.run([exe, "send-keys", "-t", pane, "Enter"])
        log("wake %s: %s (%s) wake line typed into tmux pane %s (%s)" % (job["wake_id"], agent, cli, pane, sess))
        # Report once it has read its mail (the hub counts a wake that consumed nothing), not after the whole turn.
        deadline, hub = time.time() + TMUX_READ_TIMEOUT, HubClient()
        try:
            while time.time() < deadline:
                time.sleep(2.0)
                left = next((a["unread"] for a in hub.call("agents") if a["name"] == agent), 0)
                if left < (job.get("unread") or 1):
                    break
        except HubError:
            pass
        finally:
            hub.close()
        self._done(job, 0, None, "tmux")
        return True

    def _run_job(self, job: Dict[str, Any]) -> None:
        agent, sid = job["agent"], job.get("session_id") or ""
        if sid and self._drive_tmux_pane(job, sid):
            return
        if self._should_yield_open(job, sid):
            self._done(job, None, None, "busy")
            return
        if job["cli"] == "claude" and sid and self._drive_claude_socket(job, sid):
            return
        try:
            cmd, notes = build_command(job)
        except ValueError as e:
            self._done(job, None, None, str(e))
            return
        env = dict(os.environ, YOUCHAT_NAME=agent, YOUCHAT_KIND=job["cli"], PATH=search_path())
        cwd = job.get("cwd") or os.path.expanduser("~")
        if not os.path.isdir(cwd):
            cwd = os.path.expanduser("~")
        logpath = os.path.join(self.logdir, "%s-%s.log" % (agent, job["wake_id"]))
        log("wake %s: %s (%s%s) in %s%s" % (job["wake_id"], agent, job["cli"], " " + sid[:8] if sid else " NEW",
                                             cwd, ("  [" + "; ".join(notes) + "]") if notes else ""))
        started = time.time()
        delays = retry_delays()
        new_sid: Optional[str] = None
        code: Optional[int] = None
        err: Optional[str] = None
        retries = 0
        first_err: Optional[str] = None

        for attempt in range(len(delays) + 1):
            if attempt > 0:
                delay = delays[attempt - 1]
                log("wake %s: %s interrupted (%s); retry %d/%d in %.0fs"
                    % (job["wake_id"], agent, err, attempt, len(delays), delay))
                time.sleep(delay)
                cur_sid = sid or new_sid or ""
                if self._should_yield_open(job, cur_sid):
                    self._done(job, None, new_sid, "busy")
                    return
                rjob = dict(job, session_id=cur_sid, prompt=CONT_PROMPT % {"agent": agent, "err": err})
                cmd, _ = build_command(rjob)
                retries = attempt

            run_cmd = (["bash", "-ic", '%s >/dev/null 2>&1; exec "$@"' % self.pre, "youchat-wake"] + cmd
                       if self.pre else cmd)
            code = None
            chunk_start = 0
            try:
                mode = "wb" if attempt == 0 else "ab"
                with open(logpath, mode) as out:
                    if attempt == 0:
                        os.chmod(logpath, 0o600)
                    else:
                        out.write(("\n--- retry %d (%s) ---\n" % (attempt, err)).encode())
                    out.write(("$ %s\n\n" % " ".join(cmd[:3] + ["..."])).encode())
                    out.flush()
                    chunk_start = out.tell()
                    code = subprocess.run(
                        run_cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                        timeout=max(60, float(job.get("lease_seconds", 10800)) - 60)).returncode
            except subprocess.TimeoutExpired:
                err = "timed out"
            except OSError as e:
                err = "failed to start: %s" % e
                break

            chunk = ""
            try:
                with open(logpath, "rb") as f:
                    f.seek(chunk_start)
                    chunk = f.read().decode("utf-8", errors="replace")
            except OSError:
                pass
            if not sid and not new_sid:
                new_sid = extract_session_id(chunk)
            if job["cli"] == "codex" and (sid or new_sid):
                cid = sid or new_sid or ""
                try:
                    if new_sid:
                        idx = os.path.expanduser("~/.codex/session_index.jsonl")
                        if os.path.isdir(os.path.dirname(idx)):
                            with open(idx, "a", encoding="utf-8") as f:
                                f.write(json.dumps({"id": new_sid, "thread_name": agent,
                                                    "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}) + "\n")
                    import glob
                    import sqlite3
                    rpaths: List[str] = []
                    for db in glob.glob(os.path.expanduser("~/.codex/state_*.sqlite")):
                        con = sqlite3.connect(db, timeout=2)
                        row = con.execute("SELECT name, rollout_path FROM threads WHERE id=?", (cid,)).fetchone()
                        if row:
                            if row[1]:
                                rpaths.append(row[1])
                            if new_sid or not row[0] or row[0] == agent:
                                con.execute("UPDATE threads SET source='cli', name=?, title=? WHERE id=?", (agent, agent, cid))
                            else:
                                con.execute("UPDATE threads SET source='cli' WHERE id=? AND source='exec'", (cid,))
                            con.commit()
                        con.close()
                    for rp in rpaths or glob.glob(os.path.expanduser("~/.codex/sessions/*/*/*/rollout-*%s.jsonl" % cid)):
                        if os.path.isfile(rp):
                            with open(rp, "r+b") as f:
                                pos = f.readline().find(b'"source":"exec"')
                                if pos >= 0:
                                    f.seek(pos)
                                    f.write(b'"source": "cli"')
                except Exception:
                    pass
            if job["cli"] == "antigravity" and (sid or new_sid):
                cid = sid or new_sid or ""
                try:
                    ann_dir = os.path.expanduser("~/.gemini/antigravity-cli/annotations")
                    ann_path = os.path.join(ann_dir, cid + ".pbtxt")
                    if new_sid:
                        os.makedirs(ann_dir, exist_ok=True)
                        with open(ann_path, "w", encoding="utf-8") as f:
                            f.write('title:"%s"' % agent)
                    import sqlite3
                    db = os.path.expanduser("~/.gemini/antigravity-cli/conversation_summaries.db")
                    if os.path.isfile(db) and (new_sid or (os.path.isfile(ann_path) and
                                                           ('title:"%s"' % agent) in open(ann_path, encoding="utf-8", errors="replace").read())):
                        con = sqlite3.connect(db, timeout=2)
                        con.execute("UPDATE conversation_summaries SET title=? WHERE conversation_id=?", (agent, cid))
                        con.commit()
                        con.close()
                except Exception:
                    pass
            if code is not None:
                err = turn_error(job["cli"], code, chunk, sid or new_sid)
            if not err:
                break
            if first_err is None:
                first_err = err

        if err:
            notes.append("incomplete: %s" % err)
        elif retries:
            notes.append("retried %dx (%s)" % (retries, first_err))
        note = "; ".join(notes)
        log("wake %s: %s finished exit=%s in %.0fs (log %s)%s"
            % (job["wake_id"], agent, code, time.time() - started, logpath, ("  [" + note + "]") if note else ""))
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
