"""youchat command-line interface."""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
from typing import List, Optional, Set

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
    if getattr(a, "json", False):
        print(json.dumps(teams, ensure_ascii=False))
        return
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


def _quote_cwd(cwd: str) -> str:
    if not cwd or cwd == "~":
        return "~"
    if cwd.startswith("~/"):
        return "~/" + shlex.quote(cwd[2:])
    return shlex.quote(cwd)


def interactive_command(m: dict) -> str:
    """Shell command that opens member m's own session interactively (channel push on for Claude)."""
    from youchat.hub import CLI_DEFAULTS
    from youchat.runner import BYPASS
    o = m.get("options") or {}
    perm = str(o.get("permission_mode") or "").lower()
    bypass = perm in BYPASS
    sid, cli = m.get("session_id") or "", m.get("cli") or ""
    dfl = CLI_DEFAULTS.get(cli, {})
    model = m.get("model") or dfl.get("model") or ""
    effort = m.get("effort") or dfl.get("effort") or ""
    if cli == "claude":
        argv = ["claude"]
        if bypass:
            argv += ["--dangerously-skip-permissions"]
        elif perm:
            argv += ["--permission-mode", perm]
        if model:
            argv += ["--model", model]
        if effort:
            argv += ["--effort", effort]
        argv += ["--dangerously-load-development-channels", "server:youchat"]
        argv += ["--resume", sid] if sid else ["-n", m["agent"]]
    elif cli == "antigravity":
        argv = ["youchat", "pty", "agy"] + (["--conversation", sid] if sid else [])
        if model:
            argv += ["--model", model]
        if effort:
            argv += ["--effort", effort]
        if bypass:
            argv += ["--dangerously-skip-permissions"]
        elif perm in ("accept-edits", "acceptedits", "auto", "plan"):
            argv += ["--mode", "accept-edits" if perm == "acceptedits" else perm]
    elif cli == "codex":
        eff = "ultra" if o.get("ultra") else effort
        argv = ["youchat", "pty", "codex"] + (["resume", sid] if sid else []) + \
               (["-m", model] if model else []) + \
               (["-c", 'model_reasoning_effort="%s"' % eff] if eff else [])
        if bypass:
            argv += ["--dangerously-bypass-approvals-and-sandbox"]
        elif perm in ("full-auto", "auto"):
            argv += ["--approve-for-me"]
    else:
        raise SystemExit("cannot open a %r member" % cli)
    cwd = m.get("cwd") or "~"
    return "cd %s && YOUCHAT_NAME=%s %s" % (_quote_cwd(cwd), shlex.quote(m["agent"]),
                                           " ".join(shlex.quote(x) for x in argv))


STRIP_ENV_KEYS = ("YOUCHAT_IN_PTY", "YOUCHAT_NAME", "CLAUDECODE", "CLAUDE_CODE_SESSION_ID")
REMOTE_PATH_EXPORT = 'export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"'
UNSET_ENV_CMD = "unset " + " ".join(STRIP_ENV_KEYS)

_TMUX_PREAMBLE = """\
%s
%s
command -v tmux >/dev/null 2>&1 || { echo "youchat: tmux is required to open a group (brew install tmux)" >&2; exit 1; }
tmux start-server \\; set-environment -gu YOUCHAT_IN_PTY \\; set-environment -gu YOUCHAT_NAME \\; set-environment -gu CLAUDECODE \\; set-environment -gu CLAUDE_CODE_SESSION_ID 2>/dev/null || true
_yc_sid_busy() {
  [ -n "$1" ] || return 1
  ps -axo args= 2>/dev/null | grep -F -- "$1" | grep -Ev '_yc_ensure_win|grep -F' | grep -Eq '(^|[/ ])(claude|codex|agy)(-bin)?([ ]|$)|/claude/versions/'
}
_yc_win_busy() {
  _s="$1"; _w="$2"
  _info=$(tmux list-windows -t "=$_s" -F '#{window_name}|#{pane_dead}|#{pane_pid}|#{pane_current_command}|#{@yc_skip}' 2>/dev/null | awk -F'|' -v w="$_w" '$1 == w {print; exit}')
  [ -n "$_info" ] || return 1
  _dead=$(printf '%%s' "$_info" | cut -d'|' -f2)
  _pid=$(printf '%%s' "$_info" | cut -d'|' -f3)
  _cmd=$(printf '%%s' "$_info" | cut -d'|' -f4)
  _skip=$(printf '%%s' "$_info" | cut -d'|' -f5)
  [ "$_dead" = "1" ] || [ "$_skip" = "1" ] && return 1
  case "$_cmd" in
    zsh|-zsh|bash|-bash|sh|-sh|fish|-fish)
      ps -axo ppid=,comm= 2>/dev/null | awk -v p="$_pid" '$1 == p && $2 !~ /gitstatusd/ {found=1} END {exit !found}' && return 0
      return 1 ;;
    *) return 0 ;;
  esac
}
""" % (REMOTE_PATH_EXPORT, UNSET_ENV_CMD)


_TMUX_ENSURE_WIN = """\
_yc_ensure_win() {
  _s="$1"; _w="$2"; _sid="$3"; _ext="$4"; _run_cmd="$5"; _skip_cmd="$6"
  if _yc_win_busy "$_s" "$_w"; then
    return 0
  fi
  _pcmd="$_run_cmd"; _sk=0
  if [ "$_ext" = "1" ] || _yc_sid_busy "$_sid"; then
    _pcmd="$_skip_cmd"; _sk=1
  fi
  if ! tmux has-session -t "=$_s" 2>/dev/null; then
    tmux new-session -d -s "$_s" -n "$_w" /bin/sh -c "$_pcmd"
    tmux set-environment -t "=$_s" -u YOUCHAT_IN_PTY \\; set-environment -t "=$_s" -u YOUCHAT_NAME \\; set-environment -t "=$_s" -u CLAUDECODE \\; set-environment -t "=$_s" -u CLAUDE_CODE_SESSION_ID 2>/dev/null || true
  elif ! tmux list-windows -t "=$_s" -F '#{window_name}' 2>/dev/null | grep -Fx -- "$_w" >/dev/null 2>&1; then
    tmux new-window -d -t "=$_s:" -n "$_w" /bin/sh -c "$_pcmd"
  else
    tmux respawn-pane -k -t "=$_s:=$_w" /bin/sh -c "$_pcmd"
  fi
  if [ "$_sk" = "1" ]; then
    tmux set-option -w -t "=$_s:=$_w" @yc_skip 1 2>/dev/null || true
  else
    tmux set-option -w -t "=$_s:=$_w" -u @yc_skip 2>/dev/null || true
  fi
  tmux set-option -w -t "=$_s:=$_w" automatic-rename off \\; set-option -w -t "=$_s:=$_w" allow-rename off \\; select-pane -t "=$_s:=$_w" -T "$_w" 2>/dev/null || true
}
"""


def _clean_env() -> dict:
    env = dict(os.environ)
    for k in STRIP_ENV_KEYS:
        env.pop(k, None)
    return env


def tmux_session_name(team_name: str) -> str:
    return "yc-" + re.sub(r"[^A-Za-z0-9_-]", "-", team_name)


def tmux_setup_script(team: dict, *, tail: bool = False, ssh_host: Optional[str] = None,
                      already_open: Optional[Set[str]] = None) -> str:
    members = [m for m in (team.get("members") or []) if m.get("cli") in ("claude", "codex", "antigravity")]
    if not members and not tail:
        raise SystemExit("team %r has no agent members to open" % team["name"])
    sess = tmux_session_name(team["name"])
    tmux_host = (ssh_host or short_host()).lower()
    lines = [_TMUX_PREAMBLE.rstrip(), _TMUX_ENSURE_WIN.rstrip()]
    for m in members:
        wname = m["agent"]
        sid = m.get("session_id") or ""
        mcmd = interactive_command(m)
        mhost = (m.get("host") or "").strip()
        if not ssh_host and mhost and mhost.lower() != tmux_host:
            mcmd = "ssh -t %s %s" % (shlex.quote(mhost), shlex.quote("%s; %s; %s" % (REMOTE_PATH_EXPORT, UNSET_ENV_CMD, mcmd)))
        ext = "1" if (already_open and wname in already_open) else "0"
        run_cmd = "%s; unset YOUCHAT_IN_PTY CLAUDECODE CLAUDE_CODE_SESSION_ID; export YOUCHAT_NAME=%s; %s; exec ${SHELL:-/bin/zsh} -l" % (
            REMOTE_PATH_EXPORT, shlex.quote(wname), mcmd)
        skip_msg = "[youchat] %s is already open outside tmux; close that window first to run it here.\n" % wname
        skip_cmd = "%s; unset YOUCHAT_IN_PTY CLAUDECODE CLAUDE_CODE_SESSION_ID; export YOUCHAT_NAME=%s; printf %%s %s; exec ${SHELL:-/bin/zsh} -l" % (
            REMOTE_PATH_EXPORT, shlex.quote(wname), shlex.quote(skip_msg))
        lines.append("_yc_ensure_win %s %s %s %s %s %s" % (
            shlex.quote(sess), shlex.quote(wname), shlex.quote(sid), ext,
            shlex.quote(run_cmd), shlex.quote(skip_cmd)))
    if tail:
        tcmd = "%s; %s; youchat tail %s; exec ${SHELL:-/bin/zsh} -l" % (
            REMOTE_PATH_EXPORT, UNSET_ENV_CMD, shlex.quote(team["name"]))
        lines.append("_yc_ensure_win %s tail '' 0 %s %s" % (
            shlex.quote(sess), shlex.quote(tcmd), shlex.quote(tcmd)))
    return "\n".join(lines)


def group_open_command(team: dict, *, cc: bool = True, tail: bool = False,
                       ssh_host: Optional[str] = None, already_open: Optional[Set[str]] = None) -> str:
    setup = tmux_setup_script(team, tail=tail, ssh_host=ssh_host, already_open=already_open)
    sess = shlex.quote(tmux_session_name(team["name"]))
    attach = ("tmux -CC attach -d -t %s" if cc else "tmux attach -t %s") % sess
    script = setup + "\n" + attach
    return ("ssh -t %s %s" % (shlex.quote(ssh_host), shlex.quote(script))) if ssh_host else script


def _ensure_iterm_tmux_tabs() -> None:
    if sys.platform != "darwin":
        return
    try:
        r = subprocess.run(["defaults", "read", "com.googlecode.iterm2", "OpenTmuxWindowsIn"],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5)
        if r.returncode != 0 or r.stdout.decode("utf-8", "replace").strip() not in ("1", "2"):
            subprocess.run(["defaults", "write", "com.googlecode.iterm2", "OpenTmuxWindowsIn", "-int", "2"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        r2 = subprocess.run(["defaults", "read", "com.googlecode.iterm2", "AutoHideTmuxClientSession"],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5)
        if r2.returncode != 0:
            subprocess.run(["defaults", "write", "com.googlecode.iterm2", "AutoHideTmuxClientSession", "-bool", "true"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
    except (OSError, subprocess.SubprocessError):
        pass


def _open_terminal_window(term: str, cmd: str, title: str, keep_shell: bool = False) -> None:
    env = _clean_env()
    if term == "iterm":
        inner = ("%s; exec /bin/zsh -l" % cmd) if keep_shell else cmd
        zcmd = ("/bin/zsh -lc %s" % shlex.quote(inner)).replace("\\", "\\\\").replace('"', '\\"')
        script = 'tell application "iTerm"\n  activate\n  create window with default profile command "%s"\nend tell' % zcmd
        subprocess.run(["osascript", "-e", script], check=True, env=env, stdout=subprocess.DEVNULL)
    elif term == "ghostty":
        inner = ("%s; exec /bin/zsh -l" % cmd) if keep_shell else cmd
        subprocess.run(["open", "-na", "Ghostty", "--args", "--title=%s" % title,
                        "--command=/bin/zsh -lc %s" % shlex.quote(inner)], check=True, env=env)
    else:
        script = 'tell application "Terminal"\n  activate\n  do script "%s"\nend tell' % \
                 cmd.replace("\\", "\\\\").replace('"', '\\"')
        subprocess.run(["osascript", "-e", script], check=True, env=env, stdout=subprocess.DEVNULL)


def _fetch_remote_teams(ssh_host: str, team_name: Optional[str] = None) -> List[dict]:
    tcmd = "youchat teams --json" + (" " + shlex.quote(team_name) if team_name else "")
    r = subprocess.run(["ssh", ssh_host, "%s; %s" % (REMOTE_PATH_EXPORT, tcmd)],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, env=_clean_env())
    if r.returncode != 0:
        raise HubError(r.stderr.decode("utf-8", "replace").strip() or ("ssh %s failed (%d)" % (ssh_host, r.returncode)))
    return json.loads(r.stdout.decode("utf-8", "replace"))


def _resolve_terminal(raw: Optional[str], group: bool = False) -> str:
    t = (raw or os.environ.get("YOUCHAT_TERMINAL") or "").lower()
    if t in ("iterm", "iterm2"):
        return "iterm"
    if t in ("ghostty", "terminal"):
        return t
    has_iterm = os.path.isdir("/Applications/iTerm.app") or os.path.isdir(os.path.expanduser("~/Applications/iTerm.app"))
    has_ghostty = os.path.isdir("/Applications/Ghostty.app") or os.path.isdir(os.path.expanduser("~/Applications/Ghostty.app"))
    if group:
        return "iterm" if (has_iterm or sys.platform != "darwin") else ("ghostty" if has_ghostty else "terminal")
    if os.environ.get("TERM_PROGRAM") == "iTerm.app" and has_iterm:
        return "iterm"
    return "ghostty" if has_ghostty else ("iterm" if has_iterm else "terminal")


def _open_group(team: dict, a: argparse.Namespace) -> None:
    from youchat.sessions import running_session_ids
    members = [m for m in (team.get("members") or []) if m.get("cli") in ("claude", "codex", "antigravity")]
    ssh_host = getattr(a, "ssh", None)
    if not ssh_host and members:
        loc = short_host().lower()
        hosts = {((m.get("host") or "").strip() or loc).lower(): (m.get("host") or "").strip() for m in members}
        if len(hosts) == 1 and next(iter(hosts)) != loc:
            ssh_host = next(iter(hosts.values()))
    running = set() if ssh_host else running_session_ids()
    already_open = {m["agent"] for m in members
                    if m.get("online") or (not ssh_host and m.get("session_id") and m["session_id"] in running)}
    term = _resolve_terminal(getattr(a, "terminal", None), group=True)
    cc_arg = getattr(a, "cc", None)
    use_cc = cc_arg if cc_arg is not None else (term == "iterm")
    tail = bool(getattr(a, "tail", False))
    full_cmd = group_open_command(team, cc=use_cc, tail=tail, ssh_host=ssh_host, already_open=already_open)
    if a.print or sys.platform != "darwin":
        print(full_cmd)
        return
    if term == "iterm" and use_cc:
        _ensure_iterm_tmux_tabs()
    sess = tmux_session_name(team["name"])
    if ssh_host:
        _open_terminal_window(term, full_cmd, sess, keep_shell=False)
    else:
        setup = tmux_setup_script(team, tail=tail, ssh_host=None, already_open=already_open)
        r = subprocess.run(["/bin/sh", "-c", setup], env=_clean_env())
        if r.returncode != 0:
            raise SystemExit(r.returncode)
        attach = "%s; %s %s" % (REMOTE_PATH_EXPORT, "tmux -CC attach -d -t" if use_cc else "tmux attach -t",
                                shlex.quote(sess))
        _open_terminal_window(term, attach, sess, keep_shell=False)
    label = {"iterm": "iTerm2", "ghostty": "Ghostty"}.get(term, "Terminal")
    print("opened team %s in %s (tmux session %s%s)" % (
        team["name"], label, sess, " on " + ssh_host if ssh_host else ""))


def _match_team(teams: List[dict], name: str) -> Optional[dict]:
    return next((t for t in teams if t["name"] == name), None) or \
           next((t for t in teams if t["name"].lower() == name.lower()), None)


def _team_sources(ssh_host: Optional[str], team_name: Optional[str] = None, prefer_local: bool = False):
    local = lambda: HubClient().call("teams")
    if not ssh_host:
        return [local]
    remote = lambda: _fetch_remote_teams(ssh_host, team_name)
    return [local, remote] if prefer_local else [remote, local]


def cmd_open(a: argparse.Namespace) -> None:
    from youchat.sessions import running_session_ids
    raw = (getattr(a, "member", None) or "").strip()
    team_opt = (getattr(a, "team", None) or "").strip()
    force_group = bool(getattr(a, "group", False) or getattr(a, "tail", False) or getattr(a, "cc", None) is not None)
    if raw.startswith("team:"):
        raw, force_group = raw[5:], True
    if not raw and not team_opt:
        raise SystemExit("specify a team or member name to open")
    ssh_host = getattr(a, "ssh", None)
    prefer_local = bool(getattr(a, "print", False))
    if (not raw and team_opt) or (force_group and not team_opt):
        tname = team_opt or raw
        last_err: Optional[Exception] = None
        for src in _team_sources(ssh_host, tname, prefer_local):
            try:
                t_match = _match_team(src(), tname)
                if t_match is not None:
                    _open_group(t_match, a)
                    return
            except Exception as e:
                last_err = e
        if last_err:
            raise last_err if isinstance(last_err, HubError) else HubError(str(last_err))
        raise SystemExit("no such team %r" % tname)
    m = None
    last_err = None
    for src in _team_sources(ssh_host, team_opt or None, prefer_local):
        try:
            teams = src()
        except Exception as e:
            last_err = e
            continue
        if not team_opt:
            t_match = _match_team(teams, raw)
            if t_match is not None:
                _open_group(t_match, a)
                return
        else:
            t_match = _match_team(teams, team_opt)
            teams = [t_match] if t_match else []
        m = next((x for t in teams for x in t["members"] if x["agent"] == raw), None)
        if m is not None:
            break
    if m is None:
        if last_err:
            raise last_err if isinstance(last_err, HubError) else HubError(str(last_err))
        raise SystemExit("no member %r%s" % (raw, " in team " + team_opt if team_opt else ""))
    mhost = ssh_host or ((m.get("host") or "").strip() if (m.get("host") or "").strip().lower() != short_host().lower() else "")
    if m["online"] or (not mhost and m["session_id"] and m["session_id"] in running_session_ids()):
        raise SystemExit("%s is already open (online). Close it first if you want to reopen it." % raw)
    cmd = interactive_command(m)
    if mhost:
        cmd = "ssh -t %s %s" % (shlex.quote(mhost), shlex.quote("%s; %s; %s" % (REMOTE_PATH_EXPORT, UNSET_ENV_CMD, cmd)))
    if a.print or sys.platform != "darwin":
        print(cmd)
        return
    term = _resolve_terminal(getattr(a, "terminal", None), group=False)
    _open_terminal_window(term, cmd, raw, keep_shell=True)
    label = {"iterm": "iTerm2", "ghostty": "Ghostty"}.get(term, "Terminal")
    print("opened %s in a new %s window:\n  %s" % (raw, label, cmd))


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
    if a.hub and a.tunnel:
        raise SystemExit("cannot install both --hub (local hub) and --tunnel (remote hub) at the same time")
    specs = []
    cleanup_labels = []
    if a.hub:  # local-only mode: the hub runs on this Mac
        specs.append(("com.youchat.hub", base + ["hub"]))
        cleanup_labels.append("com.youchat.tunnel")
    if a.tunnel:
        specs.append(("com.youchat.tunnel", base + ["tunnel", a.tunnel]))
        cleanup_labels.append("com.youchat.hub")
    specs.append(("com.youchat.runner", base + ["runner"] + (["--pre", a.pre] if a.pre else [])))
    for clabel in cleanup_labels:
        cplist = os.path.join(agents_dir, clabel + ".plist")
        subprocess.run(["launchctl", "bootout", "gui/%d/%s" % (uid, clabel)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if os.path.exists(cplist):
            os.unlink(cplist)
            print("removed conflicting", clabel)
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
    flt = getattr(a, "team", None) or getattr(a, "filter", None) or ""
    since = 0
    first = True
    while True:
        try:
            members: set = set()
            if flt and not flt.startswith("t-"):
                t = c.call("team_get", {"name": flt})
                members = {m["agent"] for m in t["members"]} | {"team:" + flt}
            rows = c.call("log", {"since": since, "limit": (a.n * 5 if flt else a.n) if first else 200})
        except HubError as e:
            print("[youchat] %s; retrying..." % e, file=sys.stderr)
            time.sleep(3)
            continue
        if rows:
            since = rows[-1]["seq"]
        if flt.startswith("t-"):
            rows = [m for m in rows if m.get("thread_id") == flt]
        elif members:
            rows = [m for m in rows if m["sender"] in members or (set(m["recipient"].split(",")) & members)]
        if first and flt:
            rows = rows[-a.n:]
        for m in rows:
            print(fmt_msg(m) + "\n", flush=True)
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
    raw = sys.argv[1:] if argv is None else list(argv)
    if raw and raw[0] == "pty":
        from youchat.pty_wrap import run_pty
        sys.exit(run_pty(raw[1:]))
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
    s.add_argument("--json", action="store_true", help="output raw JSON")
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

    for cmd_name, is_grp, hlp in (
        ("open", False, "open a whole team group (iTerm2 + tmux -CC tabs) or a single member session"),
        ("group", True, "open a team's group chat in iTerm2 + tmux -CC (one tab per agent, local or over --ssh)"),
    ):
        s = sub.add_parser(cmd_name, help=hlp)
        s.add_argument("member", nargs="?", help="team name or member name")
        s.add_argument("--team", help="team name (when opening a specific member, or instead of positional arg)")
        s.add_argument("--group", "-g", action="store_true", default=is_grp, help="treat target as a team name")
        s.add_argument("--ssh", metavar="HOST", help="open group/member on remote host via SSH (e.g. macmini)")
        s.add_argument("--tail", action="store_true", help="add a 'tail' tab following team messages")
        cc = s.add_mutually_exclusive_group()
        cc.add_argument("--cc", dest="cc", action="store_true", default=None, help="use tmux -CC (iTerm2 native tabs)")
        cc.add_argument("--no-cc", dest="cc", action="store_false", help="use standard tmux attach instead of -CC")
        s.add_argument("--print", action="store_true", help="only print the shell command")
        s.add_argument("--terminal", choices=["iterm", "iterm2", "ghostty", "terminal"],
                       help="terminal app (default: iTerm2 for groups, or $YOUCHAT_TERMINAL)")
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

    s = sub.add_parser("tail", help="follow messages on the bus (all teams, or filter by team / thread)")
    s.add_argument("filter", nargs="?", help="optional team name (e.g. X86, SPN) or thread id (t-xxxx)")
    s.add_argument("--team", help="only show messages involving this team")
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
