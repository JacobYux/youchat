"""youchat command-line interface."""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from typing import List, Optional

from youchat import VERSION
from youchat.common import HubClient, HubError, ensure_private_dir, home_dir, short_host, sock_path
from youchat.mcp_server import fmt_inbox, fmt_msg, fmt_time


def _client(as_name: Optional[str]) -> HubClient:
    c = HubClient()
    c.register(kind="human", name=as_name or "human")
    return c


def self_command() -> List[str]:
    """Absolute command that re-invokes this youchat (works for zipapp, script, or `python -m`)."""
    exe = sys.executable or "python3"
    main = os.path.abspath(sys.argv[0])
    if main.endswith("__main__.py"):  # python -m youchat
        pkg_root = os.path.dirname(os.path.dirname(main))
        return ["env", "PYTHONPATH=" + pkg_root, exe, "-m", "youchat"]
    return [exe, main]


# -- commands ----------------------------------------------------------------------------
def cmd_hub(a: argparse.Namespace) -> None:
    from youchat.hub import run_hub
    run_hub(daemon=a.daemon)


def cmd_mcp(a: argparse.Namespace) -> None:
    from youchat.mcp_server import run_mcp
    run_mcp()


def cmd_send(a: argparse.Namespace) -> None:
    body = sys.stdin.read() if a.body == ["-"] else " ".join(a.body)
    res = _client(a.as_name).call("send", {"to": a.to, "body": body, "thread_id": a.thread})
    print("sent %s -> %s (thread %s)%s" % (res["id"], ", ".join(res["recipients"]), res["thread_id"],
                                            "  offline: " + ", ".join(res["offline"]) if res["offline"] else ""))


def cmd_inbox(a: argparse.Namespace) -> None:
    c = _client(a.as_name)
    if a.wait:
        res = c.call("wait", {"timeout": a.wait}, timeout=a.wait + 15)
    else:
        res = c.call("inbox", {"peek": a.peek})
    print(fmt_inbox(res, "No unread messages for %s." % c.name))


def cmd_agents(a: argparse.Namespace) -> None:
    rows = HubClient().call("agents")
    if not rows:
        print("(no agents yet)")
    for r in rows:
        print("%-32s %-12s %-14s %-8s unread=%-3d %s" % (
            r["name"], r["kind"], r["host"], "online" if r["online"] else "offline", r["unread"],
            r["description"]))


def cmd_thread(a: argparse.Namespace) -> None:
    for m in HubClient().call("thread", {"thread_id": a.thread_id}):
        print(fmt_msg(m) + "\n")


def cmd_teams(a: argparse.Namespace) -> None:
    from youchat.mcp_server import fmt_team
    c = HubClient()
    teams = [c.call("team_get", {"name": a.name})] if a.name else c.call("teams")
    print("\n\n".join(fmt_team(t) for t in teams) if teams else "(no teams yet)")


def cmd_forget(a: argparse.Namespace) -> None:
    c = HubClient()
    for n in a.names:
        try:
            print("%s: %s" % (n, "removed" if c.call("forget", {"name": n})["removed"] else "not found"))
        except HubError as e:
            print("%s: %s" % (n, e))


def cmd_runner(a: argparse.Namespace) -> None:
    from youchat.runner import run_runner
    run_runner(host=a.host, max_jobs=a.max_jobs, pre=a.pre, daemon=a.daemon)


def cmd_sessions(a: argparse.Namespace) -> None:
    from youchat.sessions import find_sessions
    rows = find_sessions(a.query or "", a.cli or "")
    for s in rows:
        print("%-36s %-11s %s  %s%s%s" % (s["title"][:36], s["cli"], s["session_id"], fmt_time(s["updated"]),
                                         "  OPEN" if s["running"] else "", "" if s["latest"] else "  (older)"))
    if not rows:
        print("(no named sessions)")


def cmd_wakes(a: argparse.Namespace) -> None:
    for w in HubClient().call("wakes", {"limit": a.n}):
        dur = ("%.0fs" % (w["finished_at"] - w["started_at"])) if w["finished_at"] else "running"
        print("#%-4d %s  %-18s %-6s on %-22s exit=%-4s %-8s %s" % (
            w["id"], fmt_time(w["started_at"]), w["agent"], w["team"], w["host"],
            "" if w["exit_code"] is None else w["exit_code"], dur, w["note"]))


def cmd_wake_reset(a: argparse.Namespace) -> None:
    HubClient().call("wake_reset", {"agent": a.agent})
    print("auto-wake re-enabled for", a.agent)


def cmd_pause(a: argparse.Namespace) -> None:
    status = "paused" if a.cmd == "pause" else "active"
    HubClient().call("team_update", {"name": a.team, "status": status})
    print("team %s: %s" % (a.team, status))


def _hook_state(update: Optional[dict] = None) -> dict:
    path = os.path.join(home_dir(), "hook-state.json")
    try:
        with open(path) as f:
            state = json.load(f)
    except (OSError, ValueError):
        state = {}
    if update is not None:
        state.update(update)
        with open(path, "w") as f:
            json.dump(state, f)
        os.chmod(path, 0o600)
    return state


def hook_agy_stop() -> None:
    """Antigravity Stop hook. stdin: {conversationId, fullyIdle, ...}. If the conversation is a youchat
    member with messages it has not been told about yet, answer decision=continue so the open session
    handles them now. Each message triggers at most once (no loops)."""
    allow = {"decision": "stop"}
    try:
        payload = json.load(sys.stdin)
        cid = payload.get("conversationId") or ""
        c = HubClient()
        agent = c.call("whois_session", {"session_ids": [cid]}).get("agent") if cid else None
        if not agent:
            agent = os.environ.get("YOUCHAT_NAME")
        if not agent:
            print(json.dumps(allow))
            return
        seen = int(_hook_state().get(agent, 0))
        msgs = c.call("watch", {"agent": agent, "after_seq": seen,
                                "timeout": float(os.environ.get("YOUCHAT_STOP_WAIT", "0"))}, timeout=120)
        if not msgs:
            print(json.dumps(allow))
            return
        _hook_state({agent: max(int(m["seq"]) for m in msgs)})
        summary = "; ".join("[%s] from %s (thread %s)" % (m.get("kind") or "chat", m["sender"], m["thread_id"])
                            for m in msgs[:5])
        print(json.dumps({"decision": "continue", "reason":
            "[youchat] %s has %d new message(s): %s. Call check_inbox now, handle them according to your role "
            "and the team charter (reply with send_message, same thread_id), then finish normally."
            % (agent, len(msgs), summary)}, ensure_ascii=False))
    except Exception as e:  # noqa: BLE001 - a hook must never break the user's session
        print("youchat hook: %s" % e, file=sys.stderr)
        print(json.dumps(allow))


def cmd_hook(a: argparse.Namespace) -> None:
    if a.name == "agy-stop":
        hook_agy_stop()
    elif a.name == "install-agy":
        path = os.path.expanduser("~/.gemini/config/hooks.json")
        try:
            with open(path) as f:
                cfg = json.load(f)
        except (OSError, ValueError):
            cfg = {}
        cfg["youchat-inbox"] = {"Stop": [{"type": "command", "timeout": 60,
                                         "command": " ".join(shlex.quote(x) for x in self_command() +
                                                             ["hook", "agy-stop"])}]}
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(cfg, f, indent=2)
        print("installed Antigravity Stop hook 'youchat-inbox' in %s (applies to newly started sessions)" % path)


def interactive_command(m: dict) -> str:
    """Shell command that opens member m's own session interactively (channel push on for Claude)."""
    o = m.get("options") or {}
    bypass = str(o.get("permission_mode") or "").lower() in ("bypass", "bypasspermissions",
                                                            "dangerously-skip-permissions", "yolo")
    sid, cli = m["session_id"], m["cli"]
    if cli == "claude":
        argv = ["claude"] + (["--dangerously-skip-permissions"] if bypass else []) + \
               ["--dangerously-load-development-channels", "server:youchat"] + (["--resume", sid] if sid else
                                                                               ["-n", m["agent"]])
    elif cli == "antigravity":
        argv = ["agy"] + (["--conversation", sid] if sid else []) + (["--dangerously-skip-permissions"] if bypass else [])
    elif cli == "codex":
        argv = ["codex"] + (["resume", sid] if sid else [])
    else:
        raise SystemExit("cannot open a %r member" % cli)
    cwd = m.get("cwd") or "~"
    return "cd %s && YOUCHAT_NAME=%s %s" % (shlex.quote(cwd), shlex.quote(m["agent"]),
                                           " ".join(shlex.quote(x) for x in argv))


def cmd_open(a: argparse.Namespace) -> None:
    from youchat.sessions import running_session_ids
    c = HubClient()
    teams = [c.call("team_get", {"name": a.team})] if a.team else c.call("teams")
    m = next((x for t in teams for x in t["members"] if x["agent"] == a.member), None)
    if m is None:
        raise SystemExit("no member %r%s" % (a.member, " in team " + a.team if a.team else ""))
    if m["online"] or (m["session_id"] and m["session_id"] in running_session_ids()):
        raise SystemExit("%s is already open (online). Close it first if you want to reopen it." % a.member)
    cmd = interactive_command(m)
    if a.print or sys.platform != "darwin":
        print(cmd)
        return
    term = (a.terminal or os.environ.get("YOUCHAT_TERMINAL") or
            ("ghostty" if os.path.isdir("/Applications/Ghostty.app") else "terminal")).lower()
    if term == "ghostty":
        # login shell (PATH incl. ~/.local/bin); drop into a shell afterwards so the window stays usable
        inner = "%s; exec /bin/zsh -l" % cmd
        subprocess.run(["open", "-na", "Ghostty", "--args", "--title=%s" % a.member,
                        "--command=/bin/zsh -lc %s" % shlex.quote(inner)], check=True)
    else:
        script = 'tell application "Terminal"\n  activate\n  do script "%s"\nend tell' % \
                 cmd.replace("\\", "\\\\").replace('"', '\\"')
        subprocess.run(["osascript", "-e", script], check=True, stdout=subprocess.DEVNULL)
    print("opened %s in a new %s window:\n  %s" % (a.member, "Ghostty" if term == "ghostty" else "Terminal", cmd))


LAUNCHD = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key><array>{args}</array>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>{path}</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardErrorPath</key><string>{log}</string>
  <key>StandardOutPath</key><string>{log}</string>
</dict></plist>
"""


def cmd_service(a: argparse.Namespace) -> None:
    """Install (or remove) launchd agents on macOS: the ssh tunnel to the hub and the local runner."""
    if sys.platform != "darwin":
        raise SystemExit("`youchat service` manages macOS launchd agents. On Linux use cron, e.g.:\n"
                         "  */5 * * * * ~/.local/bin/youchat runner --daemon [--pre claude_on]")
    from xml.sax.saxutils import escape
    agents_dir = os.path.expanduser("~/Library/LaunchAgents")
    os.makedirs(agents_dir, exist_ok=True)
    uid = os.getuid()
    base = self_command()
    specs = []
    if a.hub:  # local-only mode: the hub runs on this Mac
        specs.append(("com.youchat.hub", base + ["hub"]))
    if a.tunnel:
        specs.append(("com.youchat.tunnel", base + ["tunnel", a.tunnel]))
    specs.append(("com.youchat.runner", base + ["runner"] + (["--pre", a.pre] if a.pre else [])))
    if a.action == "uninstall":
        specs = [("com.youchat.hub", []), ("com.youchat.tunnel", []), ("com.youchat.runner", [])]
    for label, argv in specs:
        plist = os.path.join(agents_dir, label + ".plist")
        subprocess.run(["launchctl", "bootout", "gui/%d/%s" % (uid, label)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if a.action == "uninstall":
            if os.path.exists(plist):
                os.unlink(plist)
            print("removed", label)
            continue
        from youchat.runner import search_path
        with open(plist, "w") as f:
            f.write(LAUNCHD.format(label=label, args="".join("<string>%s</string>" % escape(x) for x in argv),
                                   path=escape(search_path()),
                                   log=escape(os.path.join(home_dir(), label.split(".")[-1] + ".log"))))
        r = subprocess.run(["launchctl", "bootstrap", "gui/%d" % uid, plist], stderr=subprocess.PIPE)
        print("%s %s" % ("installed" if r.returncode == 0 else "FAILED", label),
              r.stderr.decode().strip() if r.returncode else "")


def cmd_tail(a: argparse.Namespace) -> None:
    c = HubClient()
    since = 0
    first = True
    while True:
        try:
            rows = c.call("log", {"since": since, "limit": a.n if first else 200})
        except HubError as e:
            print("[youchat] %s; retrying..." % e, file=sys.stderr)
            time.sleep(3)
            continue
        for m in rows:
            print(fmt_msg(m) + "\n", flush=True)
            since = m["seq"]
        first = False
        time.sleep(1)


def cmd_tunnel(a: argparse.Namespace) -> None:
    """Keep `ssh -L <local sock>:<remote sock>` alive with exponential backoff."""
    local = os.path.expanduser(a.local_sock or sock_path())
    remote = a.remote_sock
    if not remote:
        print("[youchat] resolving remote uid on %s ..." % a.host, file=sys.stderr)
        out = subprocess.run(["ssh"] + (a.ssh_opt or []) + [a.host, "id -u"], stdout=subprocess.PIPE, timeout=120)
        ruid = out.stdout.decode().strip().splitlines()[-1:] or [""]
        if out.returncode != 0 or not ruid[0].isdigit():
            raise SystemExit("could not determine remote uid; pass --remote-sock /abs/path/hub.sock")
        remote = "/tmp/youchat-%s/hub.sock" % ruid[0]
    cmd = ["ssh", "-N",
           "-o", "ExitOnForwardFailure=yes",
           "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
           "-o", "StreamLocalBindUnlink=yes",   # replace a stale local socket file
           "-o", "StreamLocalBindMask=0177",    # local socket is created 0600
           "-L", "%s:%s" % (local, remote)] + (a.ssh_opt or []) + [a.host]
    backoff = 1
    while True:
        ensure_private_dir(os.path.dirname(local) or ".")  # macOS periodically cleans /tmp
        print("[youchat] %s  %s" % (fmt_time(time.time()), " ".join(shlex.quote(x) for x in cmd)),
              file=sys.stderr, flush=True)
        started = time.time()
        try:
            rc = subprocess.call(cmd)
        except KeyboardInterrupt:
            break
        if time.time() - started > 60:
            backoff = 1
        print("[youchat] ssh exited (%s); reconnecting in %ds" % (rc, backoff), file=sys.stderr, flush=True)
        try:
            time.sleep(backoff)
        except KeyboardInterrupt:
            break
        backoff = min(backoff * 2, 60)


def cmd_setup(a: argparse.Namespace) -> None:
    base = self_command() + ["mcp"]
    q = " ".join(shlex.quote(x) for x in base)
    print("# Register youchat as an MCP server in each agent CLI on THIS machine:\n")
    print("claude mcp add -s user youchat -e YOUCHAT_KIND=claude -- %s" % q)
    print("codex mcp add youchat --env YOUCHAT_KIND=codex -- %s" % q)
    print("agy mcp add --env YOUCHAT_KIND=antigravity youchat -- %s" % q)
    print("\n# Codex: pre-approve youchat's messaging-only tools, otherwise `codex exec` (approval=never)")
    print("# rejects every youchat call. Append to ~/.codex/config.toml:")
    print("# (also add  env_vars = [\"YOUCHAT_NAME\", \"YOUCHAT_DESC\"]  under [mcp_servers.youchat]:")
    print("#  Codex strips the environment of MCP servers, so `YOUCHAT_NAME=X codex` would not bind otherwise)")
    from youchat.mcp_server import TOOLS
    for t in TOOLS:
        print('[mcp_servers.youchat.tools.%s]\napproval_mode = "approve"' % t["name"])
    print("\n# Optional per-session identity: set YOUCHAT_NAME / YOUCHAT_DESC in the environment,")
    print("# or just ask the agent to call the `register` tool with a name.")
    print("\n# Hub socket used by this machine: %s" % sock_path())


def cmd_status(a: argparse.Namespace) -> None:
    print("youchat %s  home=%s  sock=%s" % (VERSION, home_dir(), sock_path()))
    try:
        HubClient().call("ping")
        print("hub: reachable")
    except HubError as e:
        print("hub: NOT reachable (%s)" % e)
        sys.exit(1)


def main(argv: Optional[List[str]] = None) -> None:
    p = argparse.ArgumentParser(prog="youchat", description="Cross-machine messaging bus for AI coding agents.")
    p.add_argument("--version", action="version", version="youchat " + VERSION)
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("hub", help="run the hub on this machine")
    s.add_argument("--daemon", action="store_true",
                   help="detach and log to $YOUCHAT_HOME/hub.log; no-op if already running (cron-safe)")
    s.set_defaults(fn=cmd_hub)
    sub.add_parser("mcp", help="MCP stdio server (launched by agent CLIs)").set_defaults(fn=cmd_mcp)
    sub.add_parser("status", help="check hub reachability").set_defaults(fn=cmd_status)
    sub.add_parser("agents", help="list agents").set_defaults(fn=cmd_agents)
    sub.add_parser("setup", help="print MCP registration commands for claude/codex/agy").set_defaults(fn=cmd_setup)

    s = sub.add_parser("send", help="send a message as a human")
    s.add_argument("to")
    s.add_argument("body", nargs="+", help="message text, or '-' to read stdin")
    s.add_argument("--thread")
    s.add_argument("--as", dest="as_name")
    s.set_defaults(fn=cmd_send)

    s = sub.add_parser("inbox", help="read your (human) inbox")
    s.add_argument("--as", dest="as_name")
    s.add_argument("--peek", action="store_true", help="do not mark as read")
    s.add_argument("--wait", type=int, default=0, metavar="SECONDS", help="block until a message arrives")
    s.set_defaults(fn=cmd_inbox)

    s = sub.add_parser("thread", help="show a thread")
    s.add_argument("thread_id")
    s.set_defaults(fn=cmd_thread)

    s = sub.add_parser("teams", help="show all teams, or one team")
    s.add_argument("name", nargs="?")
    s.set_defaults(fn=cmd_teams)

    s = sub.add_parser("forget", help="remove stale (offline) agent identities")
    s.add_argument("names", nargs="+")
    s.set_defaults(fn=cmd_forget)

    s = sub.add_parser("runner", help="run the wake executor for this host (resumes/creates member sessions)")
    s.add_argument("--host", help="host name the hub knows this machine by (default: %s)" % short_host())
    s.add_argument("--max-jobs", type=int, default=2, help="parallel background turns (default 2)")
    s.add_argument("--pre", help="shell snippet run before each agent, e.g. 'claude_on' (proxy from ~/.bashrc)")
    s.add_argument("--daemon", action="store_true", help="detach; no-op if already running (cron-safe)")
    s.set_defaults(fn=cmd_runner)

    s = sub.add_parser("sessions", help="list named Claude/Codex/agy sessions on this machine")
    s.add_argument("query", nargs="?")
    s.add_argument("--cli", choices=["claude", "codex", "antigravity"])
    s.set_defaults(fn=cmd_sessions)

    s = sub.add_parser("wakes", help="recent background wakes")
    s.add_argument("-n", type=int, default=20)
    s.set_defaults(fn=cmd_wakes)

    s = sub.add_parser("wake-reset", help="re-enable auto-wake for an agent suspended for making no progress")
    s.add_argument("agent")
    s.set_defaults(fn=cmd_wake_reset)

    s = sub.add_parser("open", help="open a member's session interactively (macOS Terminal; Claude gets live push)")
    s.add_argument("member")
    s.add_argument("--team")
    s.add_argument("--print", action="store_true", help="only print the command")
    s.add_argument("--terminal", choices=["ghostty", "terminal"],
                   help="default: Ghostty if installed, else Terminal.app (or $YOUCHAT_TERMINAL)")
    s.set_defaults(fn=cmd_open)

    s = sub.add_parser("hook", help="agent hooks: 'agy-stop' (run by Antigravity), 'install-agy' (register it)")
    s.add_argument("name", choices=["agy-stop", "install-agy"])
    s.set_defaults(fn=cmd_hook)

    for name, hlp in (("pause", "stop all background wakes of a team"), ("resume", "resume a paused team")):
        s = sub.add_parser(name, help=hlp)
        s.add_argument("team")
        s.set_defaults(fn=cmd_pause)

    s = sub.add_parser("service", help="macOS: install/uninstall launchd agents (hub / tunnel / runner)")
    s.add_argument("action", choices=["install", "uninstall"])
    s.add_argument("--tunnel", metavar="SSH_HOST", help="also keep `youchat tunnel SSH_HOST` running")
    s.add_argument("--hub", action="store_true", help="also run the hub on this Mac (local-only setup)")
    s.add_argument("--pre", help="passed to the runner (see `youchat runner --pre`)")
    s.set_defaults(fn=cmd_service)

    s = sub.add_parser("tail", help="follow all messages on the bus")
    s.add_argument("-n", type=int, default=20)
    s.set_defaults(fn=cmd_tail)

    s = sub.add_parser("tunnel", help="forward a remote hub socket to this machine over ssh (auto-reconnect)")
    s.add_argument("host", help="ssh destination, e.g. myserver or user@10.0.0.5")
    s.add_argument("--remote-sock", help="hub.sock path on the server (default: /tmp/youchat-<remote uid>/hub.sock)")
    s.add_argument("--local-sock", help="local socket path (default: %s)" % sock_path())
    s.add_argument("--ssh-opt", action="append", help="extra ssh argument (repeatable), e.g. --ssh-opt=-p2222")
    s.set_defaults(fn=cmd_tunnel)

    a = p.parse_args(argv)
    if not getattr(a, "fn", None):
        p.print_help()
        sys.exit(2)
    try:
        a.fn(a)
    except HubError as e:
        print("youchat: %s" % e, file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        pass
