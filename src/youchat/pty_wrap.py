"""Transparent PTY wrapper for interactive `agy` and `codex` TUI sessions.

While the user has an `agy` or `codex` window open in Ghostty/Terminal, neither CLI natively
supports Claude Code's MCP channel push when sitting idle at the `>` prompt. This wrapper passes
all terminal I/O and window-resize signals through untouched, watches the youchat hub in a
background thread, and — when unread messages arrive and both the user and the agent session are
idle — injects a single-line wake prompt + Enter directly into the PTY master.
"""
from __future__ import annotations

import errno
import fcntl
import glob
import json
import os
import pty
import select
import shutil
import signal
import struct
import subprocess
import sys
import termios
import threading
import time
import tty
from typing import Any, Dict, List, Optional, Tuple

from youchat.common import HubClient, HubError
from youchat.runner import EXTRA_PATH, _agy_transcript_incomplete
from youchat.sessions import HOME, SESSION_ARG_RE, codex_recent_thread_ids, lsof_session_ids

HEAVY_KINDS = {"task", "handoff", "review_request"}
NON_INTERACTIVE_AGY = {"-p", "--print", "mcp", "--help", "-h", "--version", "-v"}
NON_INTERACTIVE_CODEX_SUB = {"exec", "e", "mcp", "login", "logout", "completion", "help", "review", "cloud"}
NON_INTERACTIVE_FLAGS = {"--help", "-h", "--version", "-V"}


def _is_youchat_wrapper(path: str) -> bool:
    try:
        if os.path.isfile(path) and os.path.getsize(path) < 512:
            with open(path, "rb") as f:
                return b"youchat-pty-wrapper" in f.read(512)
    except OSError:
        pass
    return False


def _real_which(exe: str) -> str:
    if os.path.sep in exe and not _is_youchat_wrapper(exe):
        return exe
    base = os.path.basename(exe)
    parts = os.environ.get("PATH", "").split(os.pathsep)
    for d in parts + [p for p in EXTRA_PATH if p not in parts]:
        for name in (base, base + "-bin"):
            cand = os.path.join(d, name)
            if os.path.isfile(cand) and os.access(cand, os.X_OK) and not _is_youchat_wrapper(cand):
                return cand
    if base == "codex":
        cands = sorted(glob.glob("/opt/homebrew/Caskroom/codex/*/bin/codex"))
        if cands:
            return cands[-1]
    if base == "claude":
        cands = sorted(glob.glob(os.path.expanduser("~/.local/share/claude/versions/*")))
        if cands:
            return cands[-1]
    return shutil.which(exe) or exe


def _cli_kind(exe: str) -> str:
    base = os.path.basename(exe).lower()
    if "codex" in base:
        return "codex"
    if "agy" in base or "antigravity" in base:
        return "antigravity"
    if "claude" in base:
        return "claude"
    return "agent"


def should_wrap(cli_kind: str, args: List[str]) -> bool:
    if os.environ.get("YOUCHAT_IN_PTY") == "1" or os.environ.get("YOUCHAT_PTY") == "0":
        return False
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return False
    if cli_kind in ("antigravity", "claude"):
        return not any(a in NON_INTERACTIVE_AGY or a.startswith("--output-format") for a in args)
    if cli_kind == "codex":
        if any(a in NON_INTERACTIVE_FLAGS for a in args):
            return False
        first_pos = next((a for a in args if not a.startswith("-")), "")
        return first_pos not in NON_INTERACTIVE_CODEX_SUB
    return False


def _codex_rollout_path(sid: str) -> Optional[str]:
    if not sid:
        return None
    matches = glob.glob(os.path.join(HOME, ".codex", "sessions", "*", "*", "*", "rollout-*%s.jsonl" % sid))
    return max(matches, key=os.path.getmtime) if matches else None


def _claude_session_path(sid: str) -> Optional[str]:
    if not sid:
        return None
    matches = glob.glob(os.path.join(HOME, ".claude", "projects", "*", "%s.jsonl" % sid))
    return max(matches, key=os.path.getmtime) if matches else None


def is_session_idle(cli_kind: str, sid: str) -> bool:
    """True when the agent's on-disk session log shows no turn is currently in flight."""
    if not sid:
        return True
    now = time.time()
    if cli_kind == "antigravity":
        path = os.path.join(HOME, ".gemini", "antigravity-cli", "brain", sid,
                            ".system_generated", "logs", "transcript.jsonl")
        if not os.path.isfile(path):
            return True
        try:
            if now - os.path.getmtime(path) < 2.0:
                return False
        except OSError:
            return True
        return _agy_transcript_incomplete(sid) == "OK"
    if cli_kind == "codex":
        path = _codex_rollout_path(sid)
        if not path:
            return True
        try:
            if now - os.path.getmtime(path) < 2.0:
                return False
            with open(path, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()[-20:]
        except OSError:
            return True
        for line in reversed(lines):
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("type") == "event_msg":
                ev = (d.get("payload") or {}).get("type") or ""
                if ev in ("task_complete", "turn_aborted", "error"):
                    return True
                if ev in ("task_started", "agent_reasoning", "exec_command_begin", "mcp_tool_call_begin"):
                    return False
        return True
    if cli_kind == "claude":
        path = _claude_session_path(sid)
        if not path:
            return True
        try:
            mtime = os.path.getmtime(path)
            if now - mtime < 10.0:
                return False
            with open(path, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()[-20:]
        except OSError:
            return True
        for line in reversed(lines):
            try:
                d = json.loads(line)
            except ValueError:
                continue
            tp = d.get("type")
            if tp == "system" and d.get("subtype") == "turn_duration":
                return True
            if tp == "assistant":
                sr = (d.get("message") or {}).get("stop_reason")
                if sr == "end_turn":
                    return True
                if sr == "tool_use" and now - mtime < 180.0:
                    return False
            if tp in ("api-request", "api-request-blob") and now - mtime < 180.0:
                return False
            if tp == "user":
                c = str((d.get("message") or {}).get("content") or "")
                if "<local-command-" in c or "<command-name>" in c:
                    continue
                if now - mtime < 180.0:
                    return False
        return True
    return True


def _session_mtime(cli_kind: str, sid: str) -> float:
    if not sid:
        return 0.0
    if cli_kind == "antigravity":
        p = os.path.join(HOME, ".gemini", "antigravity-cli", "brain", sid,
                         ".system_generated", "logs", "transcript.jsonl")
    else:
        p = _codex_rollout_path(sid) or ""
    try:
        return os.path.getmtime(p) if p and os.path.isfile(p) else 0.0
    except OSError:
        return 0.0


def _is_mouse_or_focus(data: bytes) -> bool:
    return data.startswith((b"\x1b[<", b"\x1b[M", b"\x1b[I", b"\x1b[O"))


def _copy_winsize(src_fd: int, dst_fd: int) -> None:
    try:
        ws = fcntl.ioctl(src_fd, termios.TIOCGWINSZ, b"\x00" * 8)
        fcntl.ioctl(dst_fd, termios.TIOCSWINSZ, ws)
    except OSError:
        pass


def _resolve_identity(hub: HubClient, pid: int, cli_kind: str, args: List[str],
                      cur_agent: str, cur_sid: str, started_at: float = 0.0) -> Tuple[str, str, bool]:
    sids: List[str] = []
    for u in lsof_session_ids([pid]):
        if u not in sids:
            sids.append(u)
    if cli_kind == "codex":
        for u in codex_recent_thread_ids(since_ts=max(0.0, started_at - 2.0), limit=1):
            if u not in sids:
                sids.append(u)
    for u in SESSION_ARG_RE.findall(" ".join(args)):
        if u not in sids:
            sids.append(u)
    if cur_sid and cur_sid not in sids:
        sids.append(cur_sid)
    agent = cur_agent or os.environ.get("YOUCHAT_NAME") or ""
    sid = sids[0] if sids else cur_sid
    boost = False
    try:
        if sids and (not agent or sids[0] != cur_sid):
            who = hub.call("whois_session", {"session_ids": sids}, timeout=5)
            if who.get("agent"):
                agent = who["agent"]
                sid = who.get("session_id") or sid
        if agent:
            for t in hub.call("teams", timeout=5):
                for m in t.get("members") or []:
                    if m.get("agent") == agent:
                        if not sid and m.get("session_id"):
                            sid = m["session_id"]
                        opts = m.get("options") or {}
                        if opts.get("boost"):
                            boost = True
    except HubError:
        pass
    return agent, sid, boost


def _watcher_loop(master_fd: int, proc: subprocess.Popen, cli_kind: str,
                  args: List[str], state: Dict[str, Any]) -> None:
    hub = HubClient()
    watcher = HubClient()
    agent = os.environ.get("YOUCHAT_NAME") or ""
    sid = ""
    boost = False
    last_seq = 0
    last_inject = 0.0
    started_at = state.get("started_at") or time.time()

    while proc.poll() is None and not state["stop"]:
        agent, sid, boost = _resolve_identity(hub, proc.pid, cli_kind, args, agent, sid, started_at)
        if not agent:
            time.sleep(2.0)
            continue
        if last_inject > 0.0 and (time.time() - last_inject >= 25.0) and is_session_idle(cli_kind, sid):
            last_seq = 0
        try:
            msgs = watcher.call("watch", {"agent": agent, "after_seq": last_seq, "timeout": 10}, timeout=20)
        except HubError:
            time.sleep(3.0)
            continue
        if not msgs:
            continue

        # Wait until TUI is ready and both the user and the TUI session are idle before injecting
        while proc.poll() is None and not state["stop"]:
            now = time.time()
            if state["line_dirty"] and now - state["last_key"] > 45.0:
                state["line_dirty"] = False
            min_boot = 9.0 if cli_kind == "antigravity" else 3.5
            tui_ready = bool(state.get("tui_ready")) or (now - started_at >= min_boot)
            user_idle = (now - state["last_key"] >= 3.0) and not state["line_dirty"]
            out_idle = (now - state["last_out"] >= 1.2)
            cooldown_ok = (now - last_inject >= 25.0)
            _, sid, boost = _resolve_identity(hub, proc.pid, cli_kind, args, agent, sid, started_at)
            if tui_ready and user_idle and out_idle and cooldown_ok and is_session_idle(cli_kind, sid):
                break
            time.sleep(0.8)

        if proc.poll() is not None or state["stop"]:
            break

        # Re-check via watch(after_seq=0, timeout=0) if messages are still unread
        try:
            still_unread = watcher.call("watch", {"agent": agent, "after_seq": 0, "timeout": 0}, timeout=5)
        except HubError:
            still_unread = msgs
        if not still_unread:
            last_seq = max(last_seq, max(int(m["seq"]) for m in msgs))
            continue

        heavy = any(m.get("kind") in HEAVY_KINDS for m in still_unread)
        prefix = "/boost " if (cli_kind == "antigravity" and boost and heavy) else ""
        senders = ", ".join(sorted({m.get("sender") or "?" for m in still_unread}))
        prompt = (
            "%s[youchat] 你（%s）有 %d 条新消息（来自 %s）。"
            "请先调用 check_inbox 读取全部未读消息，然后按你的职责和小组章程处理，回复时沿用原 thread_id。"
            % (prefix, agent, len(still_unread), senders)
        )
        before_mtime = _session_mtime(cli_kind, sid)
        try:
            os.write(master_fd, prompt.encode("utf-8"))
            time.sleep(0.15)
            os.write(master_fd, b"\r")
            last_inject = time.time()
            last_seq = max(last_seq, max(int(m["seq"]) for m in still_unread))
            # If a slash-command popup (e.g. /boost) absorbed the first Enter without submitting, press Enter once more
            time.sleep(1.2)
            if proc.poll() is None and _session_mtime(cli_kind, sid) <= before_mtime and is_session_idle(cli_kind, sid):
                os.write(master_fd, b"\r")
        except OSError:
            break


def run_pty(argv: List[str]) -> int:
    if not argv:
        raise SystemExit("usage: youchat pty <agy|codex|claude> [args...]")
    exe = _real_which(argv[0])
    args = list(argv[1:])
    cli_kind = _cli_kind(exe)
    if cli_kind == "claude":
        if should_wrap(cli_kind, args) and "--dangerously-load-development-channels" not in args:
            args = ["--dangerously-load-development-channels", "server:youchat"] + args
        os.execvp(exe, [exe] + args)
    if not should_wrap(cli_kind, args):
        os.execvp(exe, [exe] + args)

    stdin_fd = sys.stdin.fileno()
    stdout_fd = sys.stdout.fileno()
    orig_termios = termios.tcgetattr(stdin_fd)
    master_fd, slave_fd = pty.openpty()
    _copy_winsize(stdin_fd, master_fd)

    env = dict(os.environ, YOUCHAT_IN_PTY="1")
    proc = subprocess.Popen([exe] + args, stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
                            env=env, close_fds=True)
    os.close(slave_fd)

    def _on_sigwinch(_sig: int, _frame: Any) -> None:
        _copy_winsize(stdin_fd, master_fd)
        try:
            proc.send_signal(signal.SIGWINCH)
        except OSError:
            pass

    old_winch = signal.signal(signal.SIGWINCH, _on_sigwinch)
    state: Dict[str, Any] = {
        "started_at": time.time(),
        "last_key": 0.0,
        "last_out": time.time(),
        "line_dirty": False,
        "tui_ready": False,
        "stop": False,
    }
    t = threading.Thread(target=_watcher_loop, args=(master_fd, proc, cli_kind, args, state), daemon=True)
    t.start()

    try:
        tty.setraw(stdin_fd)
        while True:
            try:
                rlist, _, _ = select.select([stdin_fd, master_fd], [], [], 1.0)
            except select.error as e:
                if e.args and e.args[0] == errno.EINTR:
                    continue
                break
            if stdin_fd in rlist:
                try:
                    data = os.read(stdin_fd, 4096)
                except OSError:
                    break
                if not data:
                    break
                if not _is_mouse_or_focus(data):
                    state["last_key"] = time.time()
                    if any(b in data for b in (b"\r", b"\n", b"\x03", b"\x15")):
                        state["line_dirty"] = False
                    elif not data.startswith(b"\x1b"):
                        state["line_dirty"] = True
                try:
                    os.write(master_fd, data)
                except OSError:
                    break
            if master_fd in rlist:
                try:
                    out = os.read(master_fd, 16384)
                except OSError:
                    break
                if not out:
                    break
                if b"Quota:" in out and b"sync /usage" not in out and b"Idle" in out:
                    state["tui_ready"] = True
                is_tip_only = (b"\xf0\x9f\x92\xa1" in out and len(out) < 220
                               and b"Working" not in out and b"Generating" not in out)
                if not is_tip_only:
                    state["last_out"] = time.time()
                try:
                    os.write(stdout_fd, out)
                except OSError:
                    break
            if proc.poll() is not None and not rlist:
                break
    finally:
        state["stop"] = True
        try:
            termios.tcsetattr(stdin_fd, termios.TCSAFLUSH, orig_termios)
        except termios.error:
            pass
        signal.signal(signal.SIGWINCH, old_winch)
        try:
            os.close(master_fd)
        except OSError:
            pass

    return proc.wait()
