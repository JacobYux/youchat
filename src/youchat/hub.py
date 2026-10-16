"""youchat hub: asyncio Unix-socket server + SQLite message store.

Security model: listens ONLY on a Unix socket inside a 0700 directory, socket mode 0600,
and additionally verifies the peer UID (same user or root). No TCP listener at all.
Remote machines reach it through `ssh -L local.sock:remote.sock`, in which case the peer
is the user's own sshd process, so the UID check still passes.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import socket
import sqlite3
import struct
import sys
import time
import uuid
from typing import Any, Dict, List, Optional, Set

from youchat.common import ensure_home, ensure_sock_dir, home_dir, sock_path

NAME_RE = re.compile(r"^[A-Za-z0-9_.@:+-]{1,64}$")
AUTO_NAME_RE = re.compile(r"^[a-z]+@[A-Za-z0-9_.-]+-[0-9a-f]{4}$")
MAX_BODY = 256 * 1024
MAX_WAIT = 300

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    name        TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    host        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL,
    last_seen   REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    id         TEXT UNIQUE NOT NULL,
    thread_id  TEXT NOT NULL,
    sender     TEXT NOT NULL,
    recipient  TEXT NOT NULL,      -- as addressed: an agent name, a comma list, or '*'
    body       TEXT NOT NULL,
    hop        INTEGER NOT NULL,   -- position in thread; used later for loop protection
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_thread ON messages(thread_id, seq);
CREATE TABLE IF NOT EXISTS deliveries (
    message_id TEXT NOT NULL,
    recipient  TEXT NOT NULL,
    read_at    REAL,
    PRIMARY KEY (message_id, recipient)
);
CREATE INDEX IF NOT EXISTS deliveries_unread ON deliveries(recipient, read_at);
CREATE TABLE IF NOT EXISTS teams (
    name       TEXT PRIMARY KEY,
    charter    TEXT NOT NULL DEFAULT '',
    status     TEXT NOT NULL DEFAULT 'active',   -- active | paused
    created_by TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS members (
    team       TEXT NOT NULL,
    agent      TEXT NOT NULL,
    role       TEXT NOT NULL DEFAULT '',
    host       TEXT NOT NULL DEFAULT '',
    cli        TEXT NOT NULL DEFAULT '',        -- claude | codex | antigravity | human
    session_id TEXT NOT NULL DEFAULT '',        -- existing session to resume ('' = unknown / new)
    cwd        TEXT NOT NULL DEFAULT '',
    model      TEXT NOT NULL DEFAULT '',
    effort     TEXT NOT NULL DEFAULT '',
    options    TEXT NOT NULL DEFAULT '{}',      -- JSON: ultra / boost / permission_mode ...
    joined_at  REAL NOT NULL,
    PRIMARY KEY (team, agent)
);
CREATE TABLE IF NOT EXISTS wakes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    agent       TEXT NOT NULL,
    team        TEXT NOT NULL,
    host        TEXT NOT NULL,
    started_at  REAL NOT NULL,
    finished_at REAL,
    exit_code   INTEGER,
    note        TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS wakes_team ON wakes(team, started_at);
"""

MESSAGE_KINDS = ("chat", "task", "handoff", "question", "result", "review_request", "review", "ask_human")
MEMBER_FIELDS = ("role", "host", "cli", "session_id", "cwd", "model", "effort")
TEAM_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,48}$")
WAKEABLE_CLIS = ("claude", "codex", "antigravity")
WAKE_COOLDOWN = float(os.environ.get("YOUCHAT_WAKE_COOLDOWN", "20"))       # s between wakes of one agent
WAKE_BUDGET = int(os.environ.get("YOUCHAT_WAKE_BUDGET", "30"))             # wakes per team per hour
WAKE_LEASE = float(os.environ.get("YOUCHAT_WAKE_LEASE", str(3 * 3600)))   # max runtime of one wake
NOPROGRESS_LIMIT = 3                                                       # wakes that read nothing
HUMAN = "human"
HEAVY_KINDS = {"task", "handoff", "review_request"}  # wakes that deserve boost / ultra modes


def _migrate(db: sqlite3.Connection) -> None:
    cols = {r[1] for r in db.execute("PRAGMA table_info(messages)")}
    if "kind" not in cols:
        db.execute("ALTER TABLE messages ADD COLUMN kind TEXT NOT NULL DEFAULT 'chat'")


def log(*a: Any) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, file=sys.stderr, flush=True)


def peer_uid(sock: Any) -> Optional[int]:
    """Best-effort peer UID of a connected Unix socket (Linux SO_PEERCRED, macOS LOCAL_PEERCRED)."""
    try:
        if hasattr(socket, "SO_PEERCRED"):
            raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            return struct.unpack("3i", raw)[1]
        if sys.platform == "darwin":
            raw = sock.getsockopt(0, 0x001, 256)  # SOL_LOCAL, LOCAL_PEERCRED -> struct xucred
            return struct.unpack_from("=II", raw)[1]
    except (OSError, struct.error):
        pass
    return None


class Conn:
    def __init__(self, writer: asyncio.StreamWriter):
        self.writer = writer
        self.name: Optional[str] = None
        self.kind: str = ""


class Hub:
    def __init__(self, db_path: str):
        self.db = sqlite3.connect(db_path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        # No WAL: WAL needs shared memory and is unsafe on network filesystems (GPFS/NFS $HOME).
        self.db.executescript(SCHEMA)
        _migrate(self.db)
        # wakes left open by a previous hub process can no longer be tracked: close them out
        self.db.execute("UPDATE wakes SET finished_at=?, note='hub restarted' WHERE finished_at IS NULL",
                        (time.time(),))
        os.chmod(db_path, 0o600)
        self.online: Dict[str, Set[Conn]] = {}
        self.waiters: Dict[str, Set["asyncio.Future[None]"]] = {}
        # wake scheduling (phase 2b)
        self.runner_waiters: Set["asyncio.Future[None]"] = set()
        self.leases: Dict[str, Dict[str, Any]] = {}   # agent -> {expires, wake_id, unread, host}
        self.last_wake: Dict[str, float] = {}        # agent -> when its last wake finished
        self.noprogress: Dict[str, int] = {}         # agent -> consecutive wakes that consumed nothing
        self.human_cursor: Dict[str, int] = {}       # runner name -> last message seq notified
        self.next_due = float("inf")                  # earliest time a cooling-down agent becomes wakeable

    # -- connection loop -----------------------------------------------------------
    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        uid = peer_uid(writer.get_extra_info("socket"))
        if uid is not None and uid not in (os.getuid(), 0):
            log("rejected connection from uid", uid)
            writer.close()
            return
        conn = Conn(writer)
        tasks: Set["asyncio.Task[None]"] = set()
        try:
            while True:
                try:
                    line = await reader.readline()
                except (ConnectionError, ValueError):
                    break
                if not line:
                    break
                t = asyncio.ensure_future(self._dispatch(conn, line))
                tasks.add(t)
                t.add_done_callback(tasks.discard)
        finally:
            for t in tasks:
                t.cancel()
            self._set_offline(conn)
            try:
                writer.close()
            except Exception:
                pass

    async def _dispatch(self, conn: Conn, line: bytes) -> None:
        rid = None
        try:
            req = json.loads(line)
            rid = req.get("id")
            op = req.get("op", "")
            fn = getattr(self, "op_" + op, None)
            if fn is None:
                raise ValueError("unknown op %r" % op)
            args = req.get("args") or {}
            result = fn(conn, **args)
            if asyncio.iscoroutine(result):
                result = await result
            resp: Dict[str, Any] = {"id": rid, "ok": True, "result": result}
        except asyncio.CancelledError:
            return
        except TypeError as e:
            resp = {"id": rid, "ok": False, "error": "bad arguments: %s" % e}
        except Exception as e:  # noqa: BLE001 - report every failure to the client
            resp = {"id": rid, "ok": False, "error": str(e)}
        if conn.name:
            resp["unread"] = self._unread_count(conn.name)
        try:
            conn.writer.write((json.dumps(resp) + "\n").encode())
            await conn.writer.drain()
        except (ConnectionError, RuntimeError):
            pass

    def _set_offline(self, conn: Conn) -> None:
        if conn.name and conn.name in self.online:
            self.online[conn.name].discard(conn)
            if not self.online[conn.name]:
                del self.online[conn.name]
                log("offline:", conn.name)
                self._poke_runners()  # it may have unread messages that now need a wake

    # -- helpers -----------------------------------------------------------------------
    def _require(self, conn: Conn) -> str:
        if not conn.name:
            raise ValueError("not registered: call hello first")
        return conn.name

    def _unread_count(self, name: str) -> int:
        row = self.db.execute(
            "SELECT COUNT(*) FROM deliveries WHERE recipient=? AND read_at IS NULL", (name,)).fetchone()
        return int(row[0])

    def _agent_names(self) -> List[str]:
        return [r[0] for r in self.db.execute("SELECT name FROM agents ORDER BY name")]

    def _fetch_unread(self, name: str, limit: int, mark: bool) -> Dict[str, Any]:
        rows = self.db.execute(
            """SELECT m.* FROM deliveries d JOIN messages m ON m.id = d.message_id
               WHERE d.recipient=? AND d.read_at IS NULL ORDER BY m.seq LIMIT ?""",
            (name, max(1, min(int(limit), 200)))).fetchall()
        msgs = [dict(r) for r in rows]
        if mark and msgs:
            now = time.time()
            self.db.executemany("UPDATE deliveries SET read_at=? WHERE message_id=? AND recipient=?",
                                [(now, m["id"], name) for m in msgs])
        return {"messages": msgs, "remaining": self._unread_count(name)}

    def _notify(self, name: str) -> None:
        for fut in list(self.waiters.get(name, ())):
            if not fut.done():
                fut.set_result(None)
        self._poke_runners()

    def _poke_runners(self) -> None:
        for fut in list(self.runner_waiters):
            if not fut.done():
                fut.set_result(None)

    # -- ops -------------------------------------------------------------------------
    def op_ping(self, conn: Conn) -> Dict[str, Any]:
        return {"pong": True, "time": time.time()}

    def op_hello(self, conn: Conn, kind: str, host: str, name: Optional[str] = None,
                 description: Optional[str] = None, force: bool = False) -> Dict[str, Any]:
        kind = (kind or "agent").lower()
        if name:
            if not NAME_RE.match(name):
                raise ValueError("invalid name %r (allowed: letters, digits, _.@:+-; max 64)" % name)
        else:
            base = "%s@%s" % (kind, host)
            while True:
                name = "%s-%s" % (base, uuid.uuid4().hex[:4])
                if not self.db.execute("SELECT 1 FROM agents WHERE name=?", (name,)).fetchone():
                    break
        # Names are exclusive: one live session per name (humans' CLI invocations may share).
        others = [c for c in self.online.get(name, ()) if c is not conn]
        if others and not (kind == "human" and all(c.kind == "human" for c in others)):
            if not force:
                raise ValueError("name %r is already in use by another live session. Pick another name, or "
                                 "close that session first (or register with force=true to take it over)." % name)
            for c in others:  # takeover: e.g. a half-dead connection left behind by a dropped tunnel
                self._set_offline(c)
                c.name = None
                try:
                    c.writer.close()
                except Exception:
                    pass
        now = time.time()
        # Portable upsert (no ON CONFLICT: old distro SQLite builds lack it).
        if self.db.execute("SELECT 1 FROM agents WHERE name=?", (name,)).fetchone():
            self.db.execute("UPDATE agents SET kind=?, host=?, last_seen=? WHERE name=?", (kind, host, now, name))
            if description:
                self.db.execute("UPDATE agents SET description=? WHERE name=?", (description, name))
        else:
            self.db.execute(
                "INSERT INTO agents(name, kind, host, description, created_at, last_seen) VALUES(?,?,?,?,?,?)",
                (name, kind, host, description or "", now, now))
        # Members added by a coordinator may lack host/cli: learn them from the session itself.
        self.db.execute("UPDATE members SET host=? WHERE agent=? AND host=''", (host, name))
        if kind in WAKEABLE_CLIS:
            self.db.execute("UPDATE members SET cli=? WHERE agent=? AND cli=''", (kind, name))
        conn.kind = kind
        if kind == "runner":  # a (re)started runner: jobs leased by its previous incarnation are gone
            for a, lease in list(self.leases.items()):
                if lease.get("runner") == name:
                    del self.leases[a]
                    self.db.execute("UPDATE wakes SET finished_at=?, note='runner restarted' "
                                    "WHERE id=? AND finished_at IS NULL", (now, lease["wake_id"]))
                    log("released stale wake lease of %s (runner %s restarted)" % (a, name))
        if conn.name != name:
            old = conn.name
            self._set_offline(conn)
            conn.name = name
            self.online.setdefault(name, set()).add(conn)
            log("online:", name)
            if old and AUTO_NAME_RE.match(old) and old not in self.online and not self.db.execute(
                    "SELECT 1 FROM messages WHERE sender=? UNION SELECT 1 FROM deliveries WHERE recipient=? "
                    "UNION SELECT 1 FROM members WHERE agent=?", (old, old, old)).fetchone():
                self.db.execute("DELETE FROM agents WHERE name=?", (old,))  # unused throwaway identity
        return {"name": name, "unread": self._unread_count(name), "teams": self._teams_of(name)}

    def op_agents(self, conn: Conn) -> List[Dict[str, Any]]:
        out = []
        for r in self.db.execute("SELECT * FROM agents ORDER BY last_seen DESC"):
            d = dict(r)
            d["online"] = d["name"] in self.online
            d["unread"] = self._unread_count(d["name"])
            d["teams"] = [m["team"] for m in self.db.execute("SELECT team FROM members WHERE agent=?", (d["name"],))]
            out.append(d)
        return out

    def _ensure_human(self) -> None:
        if not self.db.execute("SELECT 1 FROM agents WHERE name=?", (HUMAN,)).fetchone():
            now = time.time()
            self.db.execute("INSERT INTO agents(name, kind, host, description, created_at, last_seen) "
                            "VALUES(?,?,?,?,?,?)", (HUMAN, "human", "", "the user", now, now))

    def _resolve_recipients(self, sender: str, targets: List[str]) -> List[str]:
        if HUMAN in targets:
            self._ensure_human()
        known = self._agent_names()
        recipients: List[str] = []
        for t in targets:
            if t == "*":
                recipients += [n for n in known if n != sender]
            elif t.startswith("team:"):
                team = t[5:]
                self._team_row(team)
                recipients += [r[0] for r in self.db.execute(
                    "SELECT agent FROM members WHERE team=? AND agent!=? ORDER BY joined_at", (team, sender))]
            elif t in known:
                recipients.append(t)
            else:
                raise ValueError("unknown recipient %r; known agents: %s; teams: %s" % (
                    t, ", ".join(known) or "(none)",
                    ", ".join("team:" + r[0] for r in self.db.execute("SELECT name FROM teams")) or "(none)"))
        recipients = list(dict.fromkeys(recipients))
        if not recipients:
            raise ValueError("no recipients (nobody else in that audience yet)")
        return recipients

    def op_send(self, conn: Conn, to: Any, body: str, thread_id: Optional[str] = None,
                kind: str = "chat") -> Dict[str, Any]:
        sender = self._require(conn)
        if not isinstance(body, str) or not body.strip():
            raise ValueError("body must be a non-empty string")
        if len(body.encode()) > MAX_BODY:
            raise ValueError("body too large (max %d bytes)" % MAX_BODY)
        kind = (kind or "chat").lower()
        if kind not in MESSAGE_KINDS:
            raise ValueError("invalid kind %r; use one of: %s" % (kind, ", ".join(MESSAGE_KINDS)))
        targets = to if isinstance(to, list) else [t.strip() for t in str(to).split(",")]
        targets = [t for t in targets if t]
        return self._post(sender, targets, body, thread_id, kind)

    def _post(self, sender: str, targets: List[str], body: str, thread_id: Optional[str],
              kind: str) -> Dict[str, Any]:
        recipients = self._resolve_recipients(sender, targets)
        hop = 0
        if thread_id:
            row = self.db.execute("SELECT MAX(hop) FROM messages WHERE thread_id=?", (thread_id,)).fetchone()
            hop = 0 if row[0] is None else row[0] + 1
        else:
            thread_id = "t-" + uuid.uuid4().hex[:8]
        msg_id = "m-" + uuid.uuid4().hex[:10]
        now = time.time()
        self.db.execute("BEGIN")
        self.db.execute(
            "INSERT INTO messages(id, thread_id, sender, recipient, body, hop, created_at, kind) VALUES(?,?,?,?,?,?,?,?)",
            (msg_id, thread_id, sender, ",".join(targets), body, hop, now, kind))
        self.db.executemany("INSERT INTO deliveries(message_id, recipient) VALUES(?,?)",
                            [(msg_id, r) for r in recipients])
        self.db.execute("COMMIT")
        for r in recipients:
            self._notify(r)
        log("msg %s [%s] %s -> %s (thread %s)" % (msg_id, kind, sender, ",".join(recipients), thread_id))
        return {"id": msg_id, "thread_id": thread_id, "recipients": recipients, "kind": kind,
                "offline": [r for r in recipients if r not in self.online]}

    def _system_notice(self, body: str) -> None:
        """Tell the user something (budget exhausted, agent stuck...) via the 'human' inbox."""
        try:
            self._post("youchat", [HUMAN], body, None, "ask_human")
        except ValueError:
            pass

    # -- teams -------------------------------------------------------------------------
    def _team_row(self, team: str) -> sqlite3.Row:
        row = self.db.execute("SELECT * FROM teams WHERE name=?", (team,)).fetchone()
        if row is None:
            names = [r[0] for r in self.db.execute("SELECT name FROM teams ORDER BY name")]
            raise ValueError("no such team %r; existing teams: %s" % (team, ", ".join(names) or "(none)"))
        return row

    def _team_dict(self, team: str) -> Dict[str, Any]:
        d = dict(self._team_row(team))
        members = []
        for r in self.db.execute("SELECT * FROM members WHERE team=? ORDER BY joined_at", (team,)):
            m = dict(r)
            m["options"] = json.loads(m["options"] or "{}")
            m["online"] = m["agent"] in self.online
            members.append(m)
        d["members"] = members
        return d

    def _teams_of(self, agent: str) -> List[Dict[str, Any]]:
        return [self._team_dict(r[0]) for r in
                self.db.execute("SELECT team FROM members WHERE agent=? ORDER BY joined_at", (agent,))]

    def op_team_create(self, conn: Conn, name: str, charter: str = "") -> Dict[str, Any]:
        if not TEAM_NAME_RE.match(name or ""):
            raise ValueError("invalid team name %r (letters, digits, _.-; max 48)" % name)
        if self.db.execute("SELECT 1 FROM teams WHERE name=?", (name,)).fetchone():
            raise ValueError("team %r already exists (use update_charter / add_member to change it)" % name)
        self.db.execute("INSERT INTO teams(name, charter, created_by, created_at) VALUES(?,?,?,?)",
                        (name, charter or "", conn.name or "", time.time()))
        log("team created:", name, "by", conn.name)
        return self._team_dict(name)

    def op_team_update(self, conn: Conn, name: str, charter: Optional[str] = None,
                       status: Optional[str] = None) -> Dict[str, Any]:
        self._team_row(name)
        if charter is not None:
            self.db.execute("UPDATE teams SET charter=? WHERE name=?", (charter, name))
        if status is not None:
            if status not in ("active", "paused"):
                raise ValueError("status must be 'active' or 'paused'")
            self.db.execute("UPDATE teams SET status=? WHERE name=?", (status, name))
            if status == "active":
                for r in self.db.execute("SELECT agent FROM members WHERE team=?", (name,)).fetchall():
                    self.noprogress.pop(r[0], None)
                self._poke_runners()
        return self._team_dict(name)

    def op_team_add(self, conn: Conn, team: str, agent: str, options: Optional[Dict[str, Any]] = None,
                    **fields: Any) -> Dict[str, Any]:
        self._team_row(team)
        if not NAME_RE.match(agent or ""):
            raise ValueError("invalid agent name %r" % agent)
        bad = set(fields) - set(MEMBER_FIELDS)
        if bad:
            raise TypeError("unexpected member field(s): %s" % ", ".join(sorted(bad)))
        now = time.time()
        if not self.db.execute("SELECT 1 FROM agents WHERE name=?", (agent,)).fetchone():
            # Placeholder so others can already address (and queue messages for) a not-yet-started member.
            self.db.execute(
                "INSERT INTO agents(name, kind, host, description, created_at, last_seen) VALUES(?,?,?,?,?,?)",
                (agent, fields.get("cli") or "agent", fields.get("host") or "", fields.get("role") or "", now, now))
        existing = self.db.execute("SELECT * FROM members WHERE team=? AND agent=?", (team, agent)).fetchone()
        if existing:
            sets = {k: v for k, v in fields.items() if v is not None}
            if options is not None:
                merged = json.loads(existing["options"] or "{}")
                merged.update(options)
                sets["options"] = json.dumps(merged, ensure_ascii=False)
            for k, v in sets.items():
                self.db.execute("UPDATE members SET %s=? WHERE team=? AND agent=?" % k, (str(v), team, agent))
        else:
            vals = {k: str(fields.get(k) or "") for k in MEMBER_FIELDS}
            self.db.execute(
                "INSERT INTO members(team, agent, %s, options, joined_at) VALUES(?,?,%s,?,?)"
                % (", ".join(MEMBER_FIELDS), ",".join("?" * len(MEMBER_FIELDS))),
                [team, agent] + [vals[k] for k in MEMBER_FIELDS] + [json.dumps(options or {}, ensure_ascii=False), now])
            log("member added: %s -> %s" % (agent, team))
        return self._team_dict(team)

    def op_team_remove(self, conn: Conn, team: str, agent: str) -> Dict[str, Any]:
        self._team_row(team)
        self.db.execute("DELETE FROM members WHERE team=? AND agent=?", (team, agent))
        return self._team_dict(team)

    def op_team_get(self, conn: Conn, name: str) -> Dict[str, Any]:
        return self._team_dict(name)

    def op_teams(self, conn: Conn) -> List[Dict[str, Any]]:
        return [self._team_dict(r[0]) for r in self.db.execute("SELECT name FROM teams ORDER BY created_at")]

    def op_my_teams(self, conn: Conn) -> List[Dict[str, Any]]:
        return self._teams_of(self._require(conn))

    def op_forget(self, conn: Conn, name: str) -> Dict[str, Any]:
        """Remove a stale agent identity (must be offline). Its messages stay in history."""
        if name in self.online:
            raise ValueError("%r is online; close that session first" % name)
        n = self.db.execute("DELETE FROM agents WHERE name=?", (name,)).rowcount
        self.db.execute("DELETE FROM deliveries WHERE recipient=? AND read_at IS NULL", (name,))
        self.db.execute("DELETE FROM members WHERE agent=?", (name,))
        return {"removed": bool(n)}

    # -- runners & wakes (phase 2b) ----------------------------------------------------------
    def op_whois_session(self, conn: Conn, session_ids: List[str]) -> Dict[str, Any]:
        """Which member name owns one of these session ids? (zero-config identity binding)"""
        for sid in session_ids or []:
            row = self.db.execute("SELECT agent, team FROM members WHERE session_id=? ORDER BY joined_at LIMIT 1",
                                  (sid,)).fetchone()
            if row:
                return {"agent": row[0], "team": row[1], "session_id": sid}
        return {"agent": None}

    def _team_wakes_last_hour(self, team: str) -> int:
        return int(self.db.execute("SELECT COUNT(*) FROM wakes WHERE team=? AND started_at>? AND note!='busy'",
                                   (team, time.time() - 3600)).fetchone()[0])

    def _candidates(self, host: str) -> List[Dict[str, Any]]:
        now = time.time()
        for a, lease in list(self.leases.items()):
            if lease["expires"] < now:
                log("wake lease expired:", a)
                del self.leases[a]
        rows = self.db.execute(
            "SELECT m.*, t.status AS team_status, t.charter AS charter FROM members m "
            "JOIN teams t ON t.name = m.team WHERE m.host=? ORDER BY m.joined_at", (host,)).fetchall()
        out: List[Dict[str, Any]] = []
        seen: Set[str] = set()
        for r in rows:
            m = dict(r)
            a = m["agent"]
            if a in seen or m["cli"] not in WAKEABLE_CLIS or m["team_status"] != "active":
                continue
            opts = json.loads(m["options"] or "{}")
            if (not m["session_id"] and not opts.get("create")) or opts.get("wake") is False:
                continue
            if a in self.online or a in self.leases:
                continue
            due = self.last_wake.get(a, 0) + WAKE_COOLDOWN
            if due > now:
                if self._unread_count(a):
                    self.next_due = min(self.next_due, due)
                continue
            if self.noprogress.get(a, 0) >= NOPROGRESS_LIMIT:
                continue
            unread = self._unread_count(a)
            if unread == 0:
                continue
            if self._team_wakes_last_hour(m["team"]) >= WAKE_BUDGET:
                self.db.execute("UPDATE teams SET status='paused' WHERE name=?", (m["team"],))
                log("team %s paused: wake budget exhausted" % m["team"])
                self._system_notice(
                    "Team %s was paused automatically: it used its wake budget (%d background wakes in the last "
                    "hour). Check the threads (`youchat tail`), then resume it with update_team(status='active') "
                    "or `youchat resume %s`." % (m["team"], WAKE_BUDGET, m["team"]))
                continue
            seen.add(a)
            m["options"] = opts
            m["unread"] = unread
            kinds = {r[0] for r in self.db.execute(
                "SELECT m.kind FROM deliveries d JOIN messages m ON m.id = d.message_id "
                "WHERE d.recipient=? AND d.read_at IS NULL", (a,))}
            m["heavy"] = bool(kinds & HEAVY_KINDS)  # real work arrived (vs. acks / chit-chat)
            out.append(m)
        return out

    @staticmethod
    def _wake_prompt(m: Dict[str, Any]) -> str:
        role = m["role"] or "（见章程）"
        lines = []
        if not m["session_id"]:
            lines.append("你是新建的对话 %s，属于 %s 小组，角色：%s。先调用 get_team 了解章程和分工。"
                         % (m["agent"], m["team"], role))
        lines += [
            "[youchat] 你（%s，%s 小组，角色：%s）有 %d 条新消息。" % (m["agent"], m["team"], role, m["unread"]),
            "请先调用 check_inbox 读取全部新消息，然后按你的职责和小组章程处理：",
            "- 需要别人接手或汇报：send_message 发给对应组员（kind 选 task / handoff / result / review_request / "
            "review / question），回复时沿用原 thread_id；",
            "- 需要用户拍板的事：send_message(to=\"human\", kind=\"ask_human\")，然后结束本轮；",
            "- 处理完当前消息就结束本轮，不要空等——之后有新消息时你会被自动唤醒。",
        ]
        return "\n".join(lines)

    def _human_notices(self, runner: str) -> List[Dict[str, Any]]:
        top = int(self.db.execute("SELECT COALESCE(MAX(seq), 0) FROM messages").fetchone()[0])
        cur = self.human_cursor.get(runner)
        self.human_cursor[runner] = top
        if cur is None:  # first poll of this runner: don't replay history
            return []
        rows = self.db.execute(
            "SELECT m.sender, m.kind, m.body, m.thread_id FROM messages m JOIN deliveries d ON d.message_id = m.id "
            "WHERE d.recipient=? AND m.seq>? AND m.seq<=? ORDER BY m.seq", (HUMAN, cur, top)).fetchall()
        return [dict(r) for r in rows]

    async def op_runner_wait(self, conn: Conn, host: str, timeout: float = 15, max_jobs: int = 2) -> Dict[str, Any]:
        runner = self._require(conn)
        self.next_due = float("inf")
        cands = self._candidates(host)
        if not cands:
            wait = max(0.0, min(float(timeout), 60, self.next_due - time.time() + 0.05))
            fut: "asyncio.Future[None]" = asyncio.get_event_loop().create_future()
            self.runner_waiters.add(fut)
            try:
                await asyncio.wait_for(fut, wait)
            except asyncio.TimeoutError:
                pass
            finally:
                self.runner_waiters.discard(fut)
            cands = self._candidates(host)
        jobs = []
        now = time.time()
        for m in cands[:max(1, int(max_jobs))]:
            wid = self.db.execute("INSERT INTO wakes(agent, team, host, started_at) VALUES(?,?,?,?)",
                                  (m["agent"], m["team"], host, now)).lastrowid
            self.leases[m["agent"]] = {"expires": now + WAKE_LEASE, "wake_id": wid, "unread": m["unread"],
                                       "runner": runner}
            log("wake %d: %s (%s, %d unread) via %s" % (wid, m["agent"], m["cli"], m["unread"], runner))
            job = {k: m[k] for k in ("agent", "team", "cli", "session_id", "cwd", "model", "effort",
                                     "options", "unread", "heavy")}
            job.update(wake_id=wid, prompt=self._wake_prompt(m), lease_seconds=WAKE_LEASE)
            jobs.append(job)
        return {"jobs": jobs, "human": self._human_notices(runner)}

    def op_runner_done(self, conn: Conn, agent: str, wake_id: int, exit_code: Optional[int] = None,
                       session_id: Optional[str] = None, note: str = "") -> Dict[str, Any]:
        lease = self.leases.pop(agent, None)
        now = time.time()
        # busy = the session is open on that host: back off longer, it will read its messages itself
        self.last_wake[agent] = now + (60 if note == "busy" else 0)
        self.db.execute("UPDATE wakes SET finished_at=?, exit_code=?, note=? WHERE id=?",
                        (now, exit_code, (note or "")[:500], wake_id))
        if session_id:  # a newly created session reports its id: remember it for future resumes
            for r in self.db.execute("SELECT team, options FROM members WHERE agent=? AND session_id=''",
                                     (agent,)).fetchall():
                opts = json.loads(r["options"] or "{}")
                opts.pop("create", None)
                self.db.execute("UPDATE members SET session_id=?, options=? WHERE team=? AND agent=?",
                                (session_id, json.dumps(opts, ensure_ascii=False), r["team"], agent))
            log("wake %s: %s now bound to session %s" % (wake_id, agent, session_id))
        if note != "busy":  # 'busy' = the session is open elsewhere; not the agent's fault
            before = lease["unread"] if lease else 0
            after = self._unread_count(agent)
            if after >= before > 0:
                self.noprogress[agent] = self.noprogress.get(agent, 0) + 1
                if self.noprogress[agent] == NOPROGRESS_LIMIT:
                    log("wake: %s made no progress %d times; auto-wake suspended" % (agent, NOPROGRESS_LIMIT))
                    self._system_notice(
                        "%s was woken %d times in a row but did not read its messages (last exit code %s; %s). "
                        "Automatic wakes for it are suspended. Check it, then run `youchat wake-reset %s`."
                        % (agent, NOPROGRESS_LIMIT, exit_code, note or "no note", agent))
            else:
                self.noprogress[agent] = 0
        log("wake %s done: %s exit=%s %s" % (wake_id, agent, exit_code, note))
        self._poke_runners()
        return {"ok": True}

    def op_wake_reset(self, conn: Conn, agent: str) -> Dict[str, Any]:
        self.noprogress.pop(agent, None)
        self.last_wake.pop(agent, None)
        self.leases.pop(agent, None)
        self._poke_runners()
        return {"ok": True}

    def op_wakes(self, conn: Conn, limit: int = 20) -> List[Dict[str, Any]]:
        rows = self.db.execute("SELECT * FROM wakes ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        out = [dict(r) for r in rows]
        for d in out:
            d["running"] = d["agent"] in self.leases and self.leases[d["agent"]]["wake_id"] == d["id"]
        return out

    async def op_watch(self, conn: Conn, agent: str, after_seq: int = 0, timeout: float = 50) -> List[Dict[str, Any]]:
        """Unread messages of `agent` newer than after_seq, waiting up to timeout. Does NOT mark them
        read: used to push notifications into a live session, which then reads them via inbox."""
        def fetch() -> List[Dict[str, Any]]:
            rows = self.db.execute(
                "SELECT m.* FROM deliveries d JOIN messages m ON m.id = d.message_id "
                "WHERE d.recipient=? AND d.read_at IS NULL AND m.seq>? ORDER BY m.seq LIMIT 50",
                (agent, int(after_seq))).fetchall()
            return [dict(r) for r in rows]
        msgs = fetch()
        if not msgs:
            fut: "asyncio.Future[None]" = asyncio.get_event_loop().create_future()
            self.waiters.setdefault(agent, set()).add(fut)
            try:
                await asyncio.wait_for(fut, max(0.0, min(float(timeout), MAX_WAIT)))
            except asyncio.TimeoutError:
                pass
            finally:
                self.waiters.get(agent, set()).discard(fut)
            msgs = fetch()
        return msgs

    def op_inbox(self, conn: Conn, limit: int = 20, peek: bool = False) -> Dict[str, Any]:
        return self._fetch_unread(self._require(conn), limit, mark=not peek)

    async def op_wait(self, conn: Conn, timeout: float = 30, limit: int = 20) -> Dict[str, Any]:
        name = self._require(conn)
        if self._unread_count(name) == 0:
            fut: "asyncio.Future[None]" = asyncio.get_event_loop().create_future()
            self.waiters.setdefault(name, set()).add(fut)
            try:
                await asyncio.wait_for(fut, max(0.0, min(float(timeout), MAX_WAIT)))
            except asyncio.TimeoutError:
                pass
            finally:
                self.waiters.get(name, set()).discard(fut)
        return self._fetch_unread(name, limit, mark=True)

    def op_thread(self, conn: Conn, thread_id: str, limit: int = 200) -> List[Dict[str, Any]]:
        rows = self.db.execute(
            "SELECT * FROM (SELECT * FROM messages WHERE thread_id=? ORDER BY seq DESC LIMIT ?) ORDER BY seq",
            (thread_id, max(1, min(int(limit), 1000)))).fetchall()
        if not rows:
            raise ValueError("no such thread %r" % thread_id)
        return [dict(r) for r in rows]

    def op_log(self, conn: Conn, since: int = 0, limit: int = 100) -> List[Dict[str, Any]]:
        if since <= 0:  # tail: start from the most recent `limit` messages
            rows = self.db.execute(
                "SELECT * FROM (SELECT * FROM messages ORDER BY seq DESC LIMIT ?) ORDER BY seq", (limit,))
        else:
            rows = self.db.execute("SELECT * FROM messages WHERE seq>? ORDER BY seq LIMIT ?", (since, limit))
        return [dict(r) for r in rows]


def _socket_is_live(path: str) -> bool:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.connect(path)
        return True
    except OSError:
        return False
    finally:
        s.close()


async def _keepalive(path: str) -> None:
    """systemd-tmpfiles / tmpwatch delete /tmp entries untouched for N days. Refresh our
    socket dir and socket hourly so a long-idle hub doesn't lose its socket."""
    while True:
        await asyncio.sleep(3600)
        for p in (os.path.dirname(path), path):
            try:
                os.utime(p, None, follow_symlinks=False)
            except (OSError, NotImplementedError):
                pass


async def _serve(path: str, db_path: str) -> None:
    hub = Hub(db_path)
    old_umask = os.umask(0o177)  # socket is created 0600 atomically: no permission race window
    try:
        server = await asyncio.start_unix_server(hub.handle, path=path, limit=4 * MAX_BODY)
    finally:
        os.umask(old_umask)
    os.chmod(path, 0o600)
    my_inode = os.stat(path).st_ino
    log("youchat hub listening on", path, "db", db_path)
    ka = asyncio.ensure_future(_keepalive(path))

    stop = asyncio.get_event_loop().create_future()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            asyncio.get_event_loop().add_signal_handler(sig, lambda: stop.done() or stop.set_result(None))
        except (NotImplementedError, RuntimeError):
            pass
    try:
        await stop
    finally:
        ka.cancel()
        server.close()
        try:  # Python >= 3.12 wait_closed() also waits for open client connections
            await asyncio.wait_for(server.wait_closed(), 2)
        except asyncio.TimeoutError:
            pass
        try:  # only remove the socket if it is still ours (a successor hub may already own the path)
            if os.stat(path).st_ino == my_inode:
                os.unlink(path)
        except OSError:
            pass
        log("youchat hub stopped")


def _daemonize(log_path: str) -> None:
    if os.fork() > 0:
        os._exit(0)
    os.setsid()
    if os.fork() > 0:
        os._exit(0)
    os.chdir("/")
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    null = os.open(os.devnull, os.O_RDONLY)
    os.dup2(null, 0)
    os.dup2(fd, 1)
    os.dup2(fd, 2)


def run_hub(daemon: bool = False) -> None:
    path = sock_path()
    ensure_home()
    ensure_sock_dir()
    if os.path.exists(path):
        if _socket_is_live(path):
            if daemon:  # idempotent: safe to call from cron every few minutes
                return
            raise SystemExit("youchat hub already running at %s" % path)
        os.unlink(path)
    if daemon:
        _daemonize(os.path.join(home_dir(), "hub.log"))
    asyncio.run(_serve(path, os.path.join(home_dir(), "hub.db")))
