"""Discover local agent sessions (Claude Code / Codex / Antigravity CLI) and detect which are running.

Pure stdlib, read-only. Used by the runner (to avoid double-driving a session the user has open)
and by the MCP bridge (find_sessions tool, automatic identity binding).
"""
from __future__ import annotations

import glob
import json
import mmap
import os
import re
import sqlite3
import subprocess
from typing import Any, Dict, List, Optional, Set

UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
HOME = os.path.expanduser("~")


def _last_json_line_with(mm: mmap.mmap, needle: bytes) -> Optional[Dict[str, Any]]:
    pos = mm.rfind(needle)
    if pos < 0:
        return None
    start = mm.rfind(b"\n", 0, pos) + 1
    end = mm.find(b"\n", pos)
    try:
        return json.loads(mm[start:end if end >= 0 else len(mm)])
    except ValueError:
        return None


def _first_value(mm: mmap.mmap, key: bytes) -> str:
    pos = mm.find(b'"' + key + b'":"')
    if pos < 0:
        return ""
    pos += len(key) + 4
    end = mm.find(b'"', pos)
    return mm[pos:end].decode("utf-8", "replace") if end > 0 else ""


def claude_sessions() -> List[Dict[str, Any]]:
    out = []
    for path in glob.glob(os.path.join(HOME, ".claude", "projects", "*", "*.jsonl")):
        try:
            size = os.path.getsize(path)
            if size == 0:
                continue
            with open(path, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
                title_obj = _last_json_line_with(mm, b'{"type":"custom-title"')
                out.append({
                    "cli": "claude",
                    "session_id": os.path.basename(path)[:-6],
                    "title": (title_obj or {}).get("customTitle", ""),
                    "cwd": _first_value(mm, b"cwd"),
                    "updated": os.path.getmtime(path),
                })
        except (OSError, ValueError):
            continue
    return out


def codex_sessions() -> List[Dict[str, Any]]:
    names: Dict[str, Dict[str, Any]] = {}
    idx = os.path.join(HOME, ".codex", "session_index.jsonl")
    try:
        with open(idx, encoding="utf-8") as f:
            for line in f:
                try:
                    d = json.loads(line)
                    names[d["id"]] = d  # later lines win (renames)
                except (ValueError, KeyError):
                    pass
    except OSError:
        pass
    out = []
    for path in glob.glob(os.path.join(HOME, ".codex", "sessions", "*", "*", "*", "rollout-*.jsonl")):
        m = UUID_RE.search(os.path.basename(path))
        if not m:
            continue
        sid = m.group(0)
        cwd = ""
        try:
            with open(path, encoding="utf-8") as f:
                first = json.loads(f.readline() or "{}")
            cwd = (first.get("payload") or {}).get("cwd", "")
        except (OSError, ValueError):
            pass
        out.append({"cli": "codex", "session_id": sid, "title": (names.get(sid) or {}).get("thread_name", ""),
                    "cwd": cwd, "updated": os.path.getmtime(path)})
    return out


def agy_sessions() -> List[Dict[str, Any]]:
    out = []
    for db in (os.path.join(HOME, ".gemini", "antigravity-cli", "conversation_summaries.db"),):
        if not os.path.exists(db):
            continue
        try:
            con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=2)
            rows = con.execute("SELECT conversation_id, title, workspace_uris, "
                               "strftime('%s', last_modified_time) FROM conversation_summaries").fetchall()
            con.close()
        except sqlite3.Error:
            continue
        for cid, title, ws, ts in rows:
            cwd = ""
            try:
                uris = json.loads(ws or "[]")
                cwd = uris[0].replace("file://", "", 1) if uris else ""
            except (ValueError, AttributeError):
                pass
            out.append({"cli": "antigravity", "session_id": cid, "title": title or "", "cwd": cwd,
                        "updated": float(ts or 0)})
    return out


def find_sessions(query: str = "", cli: str = "") -> List[Dict[str, Any]]:
    """Named sessions matching query (case-insensitive substring of title), newest first.
    Sessions sharing a title are all returned; the newest one is marked latest=True."""
    found: List[Dict[str, Any]] = []
    for fn, name in ((claude_sessions, "claude"), (codex_sessions, "codex"), (agy_sessions, "antigravity")):
        if cli and cli != name:
            continue
        found += fn()
    q = query.lower()
    found = [s for s in found if s["title"] and (not q or q in s["title"].lower() or q == s["session_id"])]
    found.sort(key=lambda s: -s["updated"])
    running = running_session_ids()
    seen: Set[str] = set()
    for s in found:
        key = s["cli"] + "\0" + s["title"]
        s["latest"] = key not in seen
        seen.add(key)
        s["running"] = s["session_id"] in running
    return found


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def claude_running() -> Dict[int, str]:
    """pid -> sessionId of live Claude Code processes (from ~/.claude/sessions/<pid>.json)."""
    out = {}
    for path in glob.glob(os.path.join(HOME, ".claude", "sessions", "*.json")):
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            pid = int(d.get("pid") or 0)
            if pid and d.get("sessionId") and _pid_alive(pid):
                out[pid] = d["sessionId"]
        except (OSError, ValueError, TypeError):
            continue
    return out


def _ps() -> List[List[str]]:
    try:
        txt = subprocess.run(["ps", "-axo", "pid=,ppid=,args="], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, timeout=10).stdout.decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError):
        return []
    rows = []
    for line in txt.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) >= 2:
            rows.append(parts + [""] * (3 - len(parts)))
    return rows


def running_session_ids() -> Set[str]:
    """Session ids that currently have a live process on this host (Claude registry + any
    process whose command line mentions a session UUID, e.g. `agy --conversation <id>`)."""
    ids = set(claude_running().values())
    for _pid, _ppid, args in _ps():
        if any(k in args for k in ("claude", "codex", "agy")):
            ids.update(UUID_RE.findall(args))
    return ids


def identify_self() -> List[str]:
    """Session ids this process belongs to, found by walking up the parent-process chain:
    a Claude ancestor's registry entry, or a UUID in an ancestor's command line."""
    rows = {int(p): (int(pp), a) for p, pp, a in _ps() if p.isdigit() and pp.isdigit()}
    claude = claude_running()
    ids: List[str] = []
    pid = os.getppid()
    for _ in range(8):
        if pid in claude:
            ids.append(claude[pid])
        if pid in rows:
            ids += [u for u in UUID_RE.findall(rows[pid][1]) if u not in ids]
            pid = rows[pid][0]
        else:
            break
        if pid <= 1:
            break
    return ids
