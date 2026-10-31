"""End-to-end tests: real hub process, real MCP stdio subprocess. Stdlib only.

Run:  python3 -m unittest discover -s tests -v
"""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
sys.path.insert(0, SRC)

from youchat.common import HubClient, HubError  # noqa: E402


class E2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="yc", dir="/tmp")  # short path: AF_UNIX limit is ~104 bytes
        cls.home = os.path.join(cls.tmp, "home")
        cls.sock = os.path.join(cls.home, "hub.sock")
        cls.env = dict(os.environ, YOUCHAT_HOME=cls.home, YOUCHAT_SOCK=cls.sock, PYTHONPATH=SRC,
                       YOUCHAT_HOST="testhost")
        os.environ.update(YOUCHAT_HOME=cls.home, YOUCHAT_SOCK=cls.sock)
        cls.hub = subprocess.Popen([sys.executable, "-m", "youchat", "hub"], env=cls.env,
                                   stderr=subprocess.PIPE)
        for _ in range(100):
            if os.path.exists(cls.sock):
                break
            time.sleep(0.05)
        else:
            raise RuntimeError("hub did not start")

    @classmethod
    def tearDownClass(cls):
        cls.hub.terminate()
        cls.hub.wait(5)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def agent(self, name, kind="claude"):
        c = HubClient(self.sock)
        c.register(kind=kind, name=name)
        return c

    def test_permissions(self):
        self.assertEqual(stat.S_IMODE(os.stat(self.home).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(self.sock).st_mode), 0o600)

    def test_second_hub_refuses(self):
        r = subprocess.run([sys.executable, "-m", "youchat", "hub"], env=self.env,
                           stderr=subprocess.PIPE, timeout=10)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn(b"already running", r.stderr)

    def test_send_and_inbox(self):
        a, b = self.agent("a1"), self.agent("b1", "codex")
        res = a.call("send", {"to": "b1", "body": "hello b"})
        self.assertEqual(res["recipients"], ["b1"])
        self.assertTrue(res["thread_id"].startswith("t-"))
        inbox = b.call("inbox")
        self.assertEqual([m["body"] for m in inbox["messages"]], ["hello b"])
        self.assertEqual(b.call("inbox")["messages"], [])  # marked read
        # reply in thread increments hop
        r2 = b.call("send", {"to": "a1", "body": "hi a", "thread_id": res["thread_id"]})
        self.assertEqual(r2["thread_id"], res["thread_id"])
        thread = a.call("thread", {"thread_id": res["thread_id"]})
        self.assertEqual([m["hop"] for m in thread], [0, 1])

    def test_unknown_recipient(self):
        a = self.agent("a2")
        with self.assertRaises(HubError) as cm:
            a.call("send", {"to": "nobody", "body": "x"})
        self.assertIn("unknown recipient", str(cm.exception))

    def test_wait_wakes_up(self):
        a, b = self.agent("a3"), self.agent("b3")
        got = {}

        def waiter():
            t0 = time.time()
            got["res"] = b.call("wait", {"timeout": 10}, timeout=20)
            got["dt"] = time.time() - t0

        th = threading.Thread(target=waiter)
        th.start()
        time.sleep(0.5)
        a.call("send", {"to": "b3", "body": "wake up"})
        th.join(15)
        self.assertEqual(got["res"]["messages"][0]["body"], "wake up")
        self.assertLess(got["dt"], 5)

    def test_broadcast_and_unread_counter(self):
        a, b, c = self.agent("a4"), self.agent("b4"), self.agent("c4")
        res = a.call("send", {"to": "*", "body": "all hands"})
        self.assertIn("b4", res["recipients"])
        self.assertNotIn("a4", res["recipients"])
        b.call("ping")
        self.assertGreaterEqual(b.last_unread, 1)

    def test_reconnect_reregisters(self):
        a, b = self.agent("a5"), self.agent("b5")
        b.close()  # simulate tunnel drop
        a.call("send", {"to": "b5", "body": "after drop"})
        self.assertEqual(b.call("inbox")["messages"][0]["body"], "after drop")

    def test_mcp_stdio(self):
        target = self.agent("target6", "codex")
        p = subprocess.Popen([sys.executable, "-m", "youchat", "mcp"], env=self.env,
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        def rpc(mid, method, params=None):
            p.stdin.write((json.dumps({"jsonrpc": "2.0", "id": mid, "method": method,
                                       "params": params or {}}) + "\n").encode())
            p.stdin.flush()
            return json.loads(p.stdout.readline())

        try:
            init = rpc(1, "initialize", {"protocolVersion": "2025-06-18",
                                         "clientInfo": {"name": "claude-code", "version": "x"},
                                         "capabilities": {}})
            self.assertEqual(init["result"]["serverInfo"]["name"], "youchat")
            p.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
            tools = rpc(2, "tools/list")["result"]["tools"]
            self.assertIn("send_message", [t["name"] for t in tools])
            r = rpc(3, "tools/call", {"name": "register", "arguments": {"name": "mcp6"}})
            self.assertIn("mcp6", r["result"]["content"][0]["text"])
            r = rpc(4, "tools/call", {"name": "send_message",
                                      "arguments": {"to": "target6", "body": "from mcp"}})
            self.assertFalse(r["result"].get("isError"), r)
            self.assertEqual(target.call("inbox")["messages"][0]["sender"], "mcp6")
            target.call("send", {"to": "mcp6", "body": "pong"})
            r = rpc(5, "tools/call", {"name": "wait_for_message", "arguments": {"timeout_seconds": 5}})
            self.assertIn("pong", r["result"]["content"][0]["text"])
            agents = rpc(6, "tools/call", {"name": "list_agents", "arguments": {}})
            self.assertIn("mcp6 (you)", agents["result"]["content"][0]["text"])
            self.assertIn("-32601", json.dumps(rpc(7, "bogus/method")))
        finally:
            p.stdin.close()
            p.wait(5)

    def test_exclusive_names_and_takeover(self):
        a = self.agent("ex1")
        b = HubClient(self.sock)
        with self.assertRaises(HubError) as cm:
            b.register(kind="claude", name="ex1")
        self.assertIn("already in use", str(cm.exception))
        b.register(kind="claude", name="ex1", force=True)  # takeover
        b.call("ping")
        # humans' CLI invocations may share a name
        h1, h2 = HubClient(self.sock), HubClient(self.sock)
        h1.register(kind="human", name="human@x")
        h2.register(kind="human", name="human@x")

    def test_kinds_and_teams(self):
        lead, dev, run = self.agent("T-AGY"), self.agent("T-DEV"), self.agent("T-RUN")
        with self.assertRaises(HubError):
            lead.call("send", {"to": "T-DEV", "body": "x", "kind": "bogus"})
        t = lead.call("team_create", {"name": "TT", "charter": "Build X. DEV codes, RUN runs, AGY reviews."})
        self.assertEqual(t["members"], [])
        lead.call("team_add", {"team": "TT", "agent": "T-AGY", "role": "review", "cli": "antigravity"})
        lead.call("team_add", {"team": "TT", "agent": "T-DEV", "role": "dev", "cli": "claude",
                               "model": "opus", "effort": "high", "options": {"ultra": True}})
        # placeholder member that has never connected can already receive (queued) messages
        lead.call("team_add", {"team": "TT", "agent": "T-RUN2", "role": "runs cases", "cli": "claude"})
        team = lead.call("team_get", {"name": "TT"})
        dev_m = [m for m in team["members"] if m["agent"] == "T-DEV"][0]
        self.assertEqual(dev_m["options"], {"ultra": True})
        self.assertTrue(dev_m["online"])
        res = lead.call("send", {"to": "team:TT", "body": "kickoff", "kind": "task"})
        self.assertEqual(sorted(res["recipients"]), ["T-DEV", "T-RUN2"])
        self.assertIn("T-RUN2", res["offline"])
        msg = dev.call("inbox")["messages"][0]
        self.assertEqual(msg["kind"], "task")
        self.assertEqual(dev.call("my_teams")[0]["name"], "TT")
        # hello returns team briefing data
        again = HubClient(self.sock)
        self.assertEqual(again.register(kind="claude", name="T-RUN2")["teams"][0]["name"], "TT")
        lead.call("team_update", {"name": "TT", "status": "paused"})
        self.assertEqual(lead.call("team_get", {"name": "TT"})["status"], "paused")
        with self.assertRaises(HubError):
            lead.call("team_create", {"name": "TT"})

    def test_forget(self):
        tmp = self.agent("gone1")
        tmp.close()
        time.sleep(0.2)
        a = self.agent("keeper")
        self.assertTrue(a.call("forget", {"name": "gone1"})["removed"])
        with self.assertRaises(HubError):
            a.call("forget", {"name": "keeper"})  # online

    def test_mcp_join_team_briefing(self):
        lead = self.agent("J-LEAD")
        lead.call("team_create", {"name": "JT", "charter": "Charter text here."})
        lead.call("team_add", {"team": "JT", "agent": "J-LEAD", "role": "acceptance and review"})
        p = subprocess.Popen([sys.executable, "-m", "youchat", "mcp"], env=self.env,
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        def rpc(mid, method, params=None):
            p.stdin.write((json.dumps({"jsonrpc": "2.0", "id": mid, "method": method,
                                       "params": params or {}}) + "\n").encode())
            p.stdin.flush()
            return json.loads(p.stdout.readline())

        try:
            rpc(1, "initialize", {"clientInfo": {"name": "claude-code"}})
            r = rpc(2, "tools/call", {"name": "join_team", "arguments": {"team": "JT", "name": "J-DEV"}})
            self.assertIn("Ask the user", r["result"]["content"][0]["text"])  # no role yet
            r = rpc(3, "tools/call", {"name": "join_team",
                                      "arguments": {"team": "JT", "role": "development and tests"}})
            text = r["result"]["content"][0]["text"]
            self.assertIn("You are J-DEV. Your role: development and tests", text)
            self.assertIn("J-LEAD", text)
            self.assertIn("Charter text here.", text)
            lead.call("send", {"to": "J-DEV", "body": "please implement", "kind": "task"})
            r = rpc(4, "tools/call", {"name": "check_inbox", "arguments": {}})
            text = r["result"]["content"][0]["text"]
            self.assertIn("[task]", text)
            self.assertIn("[team JT] you are J-DEV", text)
        finally:
            p.stdin.close()
            p.wait(5)

    def test_mcp_channel_push(self):
        sender = self.agent("CH-SENDER")
        env = dict(self.env, YOUCHAT_NAME="CH-CLD")
        p = subprocess.Popen([sys.executable, "-m", "youchat", "mcp"], env=env,
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            p.stdin.write((json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                       "params": {"clientInfo": {"name": "claude-code"}}}) + "\n").encode())
            p.stdin.flush()
            init = json.loads(p.stdout.readline())
            self.assertEqual(init["id"], 1)
            self.assertIn("claude/channel", init["result"]["capabilities"]["experimental"])
            time.sleep(0.5)
            sender.call("send", {"to": "CH-CLD", "body": "live hello", "kind": "handoff"})
            note = json.loads(p.stdout.readline())
            self.assertEqual(note["method"], "notifications/claude/channel")
            self.assertIn("live hello", note["params"]["content"])
            self.assertEqual(note["params"]["meta"]["sender"], "CH-SENDER")
            self.assertEqual(note["params"]["meta"]["kind"], "handoff")
            unread = [a for a in sender.call("agents") if a["name"] == "CH-CLD"][0]["unread"]
            self.assertEqual(unread, 1)  # push does not consume the message
        finally:
            p.stdin.close()
            p.wait(5)

    def test_agy_stop_hook_once_per_message(self):
        lead = self.agent("H-LEAD")
        lead.call("team_create", {"name": "HT", "charter": "x"})
        lead.call("team_add", {"team": "HT", "agent": "H-AGY", "role": "review", "cli": "antigravity",
                               "session_id": "conv-123"})

        def hook():
            r = subprocess.run([sys.executable, "-m", "youchat", "hook", "agy-stop"], env=self.env, timeout=30,
                               input=json.dumps({"conversationId": "conv-123", "fullyIdle": True}).encode(),
                               stdout=subprocess.PIPE)
            return json.loads(r.stdout)

        self.assertEqual(hook()["decision"], "stop")  # nothing new
        lead.call("send", {"to": "H-AGY", "body": "please review", "kind": "review_request"})
        out = hook()
        self.assertEqual(out["decision"], "continue")
        self.assertIn("review_request", out["reason"])
        self.assertEqual(hook()["decision"], "stop")  # same message never re-triggers

    def test_mcp_without_hub(self):
        env = dict(self.env, YOUCHAT_SOCK=os.path.join(self.tmp, "missing.sock"))
        p = subprocess.run([sys.executable, "-m", "youchat", "mcp"], env=env, timeout=10,
                           input=(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                              "params": {"clientInfo": {"name": "codex"}}}) + "\n" +
                                  json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                              "params": {"name": "check_inbox", "arguments": {}}}) + "\n"
                                  ).encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        lines = [json.loads(x) for x in p.stdout.decode().splitlines()]
        self.assertEqual(lines[0]["result"]["serverInfo"]["name"], "youchat")
        self.assertTrue(lines[1]["result"]["isError"])
        self.assertIn("youchat tunnel", lines[1]["result"]["content"][0]["text"])

    def test_watch_resets_after_seq_on_hub_switch(self):
        a, b = self.agent("W-A"), self.agent("W-B")
        a.call("send", {"to": "W-B", "body": "after db reset"})
        msgs = b.call("watch", {"agent": "W-B", "after_seq": 999999, "timeout": 0})
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["body"], "after db reset")

    def _cli(self, *args):
        return subprocess.run([sys.executable, "-m", "youchat"] + list(args), env=self.env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)

    def test_open_group_cli_and_ssh(self):
        lead = self.agent("G-LEAD")
        lead.close()
        c = self.agent("G-SETUP")
        c.call("team_create", {"name": "GRP", "charter": "group test"})
        c.call("team_add", {"team": "GRP", "agent": "G-CLD", "cli": "claude", "host": "testhost",
                            "session_id": "11111111-1111-1111-1111-111111111111", "cwd": "~/proj dir",
                            "model": "opus", "effort": "high", "options": {"permission_mode": "bypass"}})
        c.call("team_add", {"team": "GRP", "agent": "G-CDX", "cli": "codex", "host": "macmini",
                            "session_id": "22222222-2222-2222-2222-222222222222", "cwd": "/tmp",
                            "model": "gpt-5.4", "options": {"ultra": True, "permission_mode": "auto"}})
        c.call("team_add", {"team": "GRP", "agent": "G-AGY", "cli": "antigravity", "host": "testhost",
                            "session_id": "33333333-3333-3333-3333-333333333333",
                            "options": {"permission_mode": "accept-edits"}})
        j = json.loads(self._cli("teams", "GRP", "--json").stdout.decode())
        self.assertEqual(j[0]["name"], "GRP")
        self.assertEqual(len(j[0]["members"]), 3)
        out = self._cli("open", "GRP", "--print", "--terminal", "iterm").stdout.decode()
        self.assertIn("_yc_ensure_win yc-GRP G-CLD", out)
        self.assertIn("_yc_ensure_win yc-GRP G-CDX", out)
        self.assertIn("_yc_ensure_win yc-GRP G-AGY", out)
        self.assertIn("ssh -t macmini", out)  # hybrid: G-CDX is on macmini while GRP opens locally
        self.assertNotIn("_yc_ensure_win yc-GRP tail", out)
        self.assertTrue(out.strip().endswith("tmux -CC attach -d -t yc-GRP"))
        out_tail = self._cli("group", "GRP", "--tail", "--no-cc", "--print").stdout.decode()
        self.assertIn("_yc_ensure_win yc-GRP tail", out_tail)
        self.assertTrue(out_tail.strip().endswith("tmux attach -t yc-GRP"))
        out_ssh = self._cli("open", "GRP", "--ssh", "macmini", "--print", "--terminal", "iterm").stdout.decode()
        self.assertTrue(out_ssh.startswith("ssh -t macmini "))
        self.assertIn("tmux -CC attach -d -t yc-GRP", out_ssh)
        # single-member open (local and remote-host member)
        m_local = self._cli("open", "G-CLD", "--print").stdout.decode().strip()
        self.assertIn("cd ~/'proj dir' && YOUCHAT_NAME=G-CLD claude --dangerously-skip-permissions", m_local)
        m_rem = self._cli("open", "G-CDX", "--print").stdout.decode().strip()
        self.assertTrue(m_rem.startswith("ssh -t macmini "))
        # team where all members live on macmini auto-opens on macmini via ssh
        c.call("team_create", {"name": "RGRP", "charter": "remote only"})
        c.call("team_add", {"team": "RGRP", "agent": "R-1", "cli": "claude", "host": "macmini"})
        self.assertTrue(self._cli("open", "RGRP", "--print").stdout.decode().startswith("ssh -t macmini "))
        # error cases: empty team, missing target
        c.call("team_create", {"name": "EMPTY", "charter": "none"})
        self.assertNotEqual(self._cli("open", "EMPTY", "--print").returncode, 0)
        self.assertNotEqual(self._cli("open").returncode, 0)

    def test_tmux_setup_execution(self):
        if not shutil.which("tmux"):
            self.skipTest("tmux not installed")
        from youchat.cli import tmux_setup_script
        fake_bin = os.path.join(self.tmp, "fake_bin")
        os.makedirs(fake_bin, exist_ok=True)
        for name in ("claude", "youchat"):
            p = os.path.join(fake_bin, name)
            with open(p, "w") as f:
                f.write("#!/bin/sh\nsleep 30\n")
            os.chmod(p, 0o755)
        tsock = "yct_" + str(os.getpid())
        twrap = os.path.join(fake_bin, "tmux")
        real_tmux = shutil.which("tmux")
        with open(twrap, "w") as f:
            f.write('#!/bin/sh\nexec "%s" -L "%s" "$@"\n' % (real_tmux, tsock))
        os.chmod(twrap, 0o755)
        team = {"name": "T.1", "members": [
            {"agent": "A1", "cli": "claude", "session_id": "11111111-2222-3333-4444-555555555555", "cwd": self.tmp},
            {"agent": "A2", "cli": "codex", "session_id": "22222222-2222-3333-4444-555555555555", "cwd": self.tmp},
        ]}
        env = dict(self.env, PATH=fake_bin + ":" + os.environ.get("PATH", ""))
        script1 = tmux_setup_script(team, tail=True, already_open={"A2"}).replace(
            'export PATH="', 'export PATH="' + fake_bin + ":")
        try:
            subprocess.run(["/bin/sh", "-c", script1], check=True, env=env, timeout=15)
            wins = subprocess.run([real_tmux, "-L", tsock, "list-windows", "-t", "=yc-T-1",
                                   "-F", "#{window_name}|#{pane_pid}"],
                                  stdout=subprocess.PIPE, check=True).stdout.decode().splitlines()
            wmap = dict(w.split("|", 1) for w in wins)
            self.assertEqual(list(wmap.keys()), ["A1", "A2", "tail"])
            time.sleep(0.4)
            # re-run with A2 now free: A1 is busy so its pane_pid stays identical; A2 was idle after skip_cmd so it respawns
            script2 = tmux_setup_script(team, tail=True, already_open=set()).replace(
                'export PATH="', 'export PATH="' + fake_bin + ":")
            subprocess.run(["/bin/sh", "-c", script2], check=True, env=env, timeout=15)
            wins2 = dict(w.split("|", 1) for w in subprocess.run(
                [real_tmux, "-L", tsock, "list-windows", "-t", "=yc-T-1", "-F", "#{window_name}|#{pane_pid}"],
                stdout=subprocess.PIPE, check=True).stdout.decode().splitlines())
            self.assertEqual(wins2["A1"], wmap["A1"])
        finally:
            subprocess.run([real_tmux, "-L", tsock, "kill-server"], stderr=subprocess.DEVNULL)


if __name__ == "__main__":
    unittest.main()
