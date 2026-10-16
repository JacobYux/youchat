"""Shared paths and the synchronous hub client used by the MCP bridge and the CLI.

Wire protocol (hub <-> client): one JSON object per line over a Unix socket.
  request:  {"id": 1, "op": "send", "args": {...}}
  response: {"id": 1, "ok": true, "result": {...}, "unread": 0}
            {"id": 1, "ok": false, "error": "message"}
"""
from __future__ import annotations

import json
import os
import socket
import stat
import threading
from typing import Any, Dict, Optional


def home_dir() -> str:
    """Persistent data (SQLite DB, logs). May live on a network FS."""
    return os.path.expanduser(os.environ.get("YOUCHAT_HOME", "~/.youchat"))


def sock_path() -> str:
    """Hub socket. Defaults to node-local /tmp/youchat-<uid>/ rather than $HOME, because
    $HOME is often a cluster-shared filesystem (GPFS/NFS) where a socket file would be
    visible on every node but connectable on only one."""
    p = os.environ.get("YOUCHAT_SOCK")
    if p:
        return os.path.expanduser(p)
    return "/tmp/youchat-%d/hub.sock" % os.getuid()


def ensure_private_dir(d: str) -> str:
    """Create d as 0700, or verify an existing d is a real directory we own (not a symlink
    planted by another user in a world-writable parent such as /tmp)."""
    try:
        os.mkdir(d, 0o700)
    except FileExistsError:
        pass
    except FileNotFoundError:
        os.makedirs(d, mode=0o700, exist_ok=True)
    st = os.lstat(d)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        raise SystemExit("refusing to use %s: not a directory owned by uid %d" % (d, os.getuid()))
    os.chmod(d, 0o700)  # mkdir mode is masked by umask; also neutralises inherited ACLs (mask -> ---)
    return d


def ensure_home() -> str:
    return ensure_private_dir(home_dir())


def ensure_sock_dir() -> str:
    return ensure_private_dir(os.path.dirname(sock_path()) or ".")


def short_host() -> str:
    return os.environ.get("YOUCHAT_HOST") or socket.gethostname().split(".")[0].lower()


class HubError(Exception):
    """The hub understood the request but rejected it."""


class HubUnavailable(HubError):
    """The hub socket cannot be reached (hub down, tunnel down, VPN down...)."""


class HubClient:
    """Blocking, single-in-flight client. Reconnects transparently and re-registers
    the same identity after a reconnect (e.g. when the SSH tunnel bounces)."""

    def __init__(self, path: Optional[str] = None):
        self.path = path or sock_path()
        self.sock: Optional[socket.socket] = None
        self.rfile = None
        self._id = 0
        self._lock = threading.Lock()
        self.hello_args: Optional[Dict[str, Any]] = None
        self.last_unread = 0

    @property
    def name(self) -> Optional[str]:
        return self.hello_args.get("name") if self.hello_args else None

    # -- connection management -------------------------------------------------
    def _connect(self) -> None:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.connect(self.path)
        except OSError as e:
            s.close()
            raise HubUnavailable("cannot reach youchat hub at %s (%s)" % (self.path, e))
        self.sock = s
        self.rfile = s.makefile("rb")
        if self.hello_args is not None:  # same identity reconnecting: take over any half-dead old connection
            self._roundtrip("hello", dict(self.hello_args, force=True), 15)

    def close(self) -> None:
        for f in (self.rfile, self.sock):
            try:
                if f is not None:
                    f.close()
            except OSError:
                pass
        self.sock = None
        self.rfile = None

    def _roundtrip(self, op: str, args: Dict[str, Any], timeout: float) -> Any:
        assert self.sock is not None and self.rfile is not None
        self._id += 1
        rid = self._id
        self.sock.settimeout(timeout)
        self.sock.sendall((json.dumps({"id": rid, "op": op, "args": args}) + "\n").encode())
        while True:
            line = self.rfile.readline()
            if not line:
                raise ConnectionError("hub closed the connection")
            resp = json.loads(line)
            if resp.get("id") == rid:
                break
        if "unread" in resp:
            self.last_unread = int(resp["unread"])
        if not resp.get("ok"):
            raise HubError(resp.get("error") or "unknown hub error")
        return resp.get("result")

    # -- public API --------------------------------------------------------------
    def call(self, op: str, args: Optional[Dict[str, Any]] = None, timeout: float = 15) -> Any:
        args = {k: v for k, v in (args or {}).items() if v is not None}
        with self._lock:
            for attempt in (0, 1):
                try:
                    if self.sock is None:
                        self._connect()  # may replay hello, which can fail mid-way too
                    return self._roundtrip(op, args, timeout)
                except HubError:
                    raise
                except (OSError, ConnectionError, ValueError) as e:
                    self.close()
                    if attempt:
                        raise HubUnavailable("lost connection to youchat hub at %s (%s)" % (self.path, e))
        raise AssertionError("unreachable")

    def register(self, kind: str, name: Optional[str] = None, description: Optional[str] = None,
                 host: Optional[str] = None, force: bool = False) -> Dict[str, Any]:
        args = {"kind": kind, "host": host or short_host(), "name": name, "description": description}
        res = self.call("hello", dict(args, force=force or None))
        self.hello_args = {k: v for k, v in args.items() if v is not None}
        self.hello_args["name"] = res["name"]
        return res
