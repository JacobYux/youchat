"""Wake scheduling end-to-end: real hub + real runner, with a fake `claude` CLI on PATH.

The fake CLI behaves like a headless agent turn: binds to YOUCHAT_NAME, reads its inbox, replies
"ack: <body>" to each sender and prints a JSON result containing a session id. Members whose name
ends with -LAZY do nothing (to exercise no-progress suspension).
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
sys.path.insert(0, SRC)

from youchat.common import HubClient  # noqa: E402

FAKE_CLAUDE = r'''#!%(py)s
import json, os, sys
sys.path.insert(0, %(src)r)
from youchat.common import HubClient
name = os.environ["YOUCHAT_NAME"]
args = sys.argv[1:]
with open(os.path.join(%(tmp)r, "calls.log"), "a") as f:
    f.write(json.dumps({"name": name, "args": args, "cwd": os.getcwd()}) + "\n")
sid = args[args.index("-r") + 1] if "-r" in args else "new-sess-" + name
prompt = args[args.index("-p") + 1] if "-p" in args else ""
stash = os.path.join(%(tmp)r, name + ".stash")
if name.endswith(("-FLAKY", "-DEAD")):
    c = HubClient()
    c.register(kind="claude", name=name)
    if "中途中断" not in prompt:            # first attempt: consume the mail, then the "network" drops
        msgs = c.call("inbox")["messages"]
        json.dump(msgs, open(stash, "w"))
        print(json.dumps({"type": "result", "session_id": sid, "is_error": True,
                          "subtype": "error_during_execution", "terminal_reason": "aborted_streaming"}))
        sys.exit(0)
    if name.endswith("-DEAD"):              # retries keep failing
        print(json.dumps({"type": "result", "session_id": sid, "is_error": True, "subtype": "error_during_execution"}))
        sys.exit(0)
    for m in json.load(open(stash)):        # retry: finish the interrupted work
        c.call("send", {"to": m["sender"], "body": "ack-after-retry: " + m["body"], "thread_id": m["thread_id"],
                        "kind": "result"})
elif not name.endswith("-LAZY"):
    c = HubClient()
    c.register(kind="claude", name=name)
    for m in c.call("inbox")["messages"]:
        c.call("send", {"to": m["sender"], "body": "ack: " + m["body"], "thread_id": m["thread_id"],
                        "kind": "result"})
print(json.dumps({"type": "result", "session_id": sid, "result": "done"}))
'''


class Wake(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="ycw", dir="/tmp")
        cls.home = os.path.join(cls.tmp, "home")
        cls.sock = os.path.join(cls.home, "hub.sock")
        cls.bin = os.path.join(cls.tmp, "bin")
        os.makedirs(cls.bin)
        fake = os.path.join(cls.bin, "claude")
        with open(fake, "w") as f:
            f.write(FAKE_CLAUDE % {"py": sys.executable, "src": SRC, "tmp": cls.tmp})
        os.chmod(fake, 0o755)
        cls.env = dict(os.environ, YOUCHAT_HOME=cls.home, YOUCHAT_SOCK=cls.sock, PYTHONPATH=SRC,
                       YOUCHAT_HOST="wakehost", YOUCHAT_WAKE_COOLDOWN="0.5", YOUCHAT_RETRY_DELAYS="0.2,0.2",
                       PATH=cls.bin + os.pathsep + os.environ.get("PATH", ""))
        os.environ.update(YOUCHAT_HOME=cls.home, YOUCHAT_SOCK=cls.sock)
        cls.hub = subprocess.Popen([sys.executable, "-m", "youchat", "hub"], env=cls.env, stderr=subprocess.DEVNULL)
        for _ in range(100):
            if os.path.exists(cls.sock):
                break
            time.sleep(0.05)
        cls.runner = subprocess.Popen([sys.executable, "-m", "youchat", "runner"], env=cls.env,
                                      stderr=open(os.path.join(cls.tmp, "runner.log"), "w"))
        cls.lead = HubClient(cls.sock)
        cls.lead.register(kind="antigravity", name="W-LEAD")
        cls.lead.call("team_create", {"name": "W", "charter": "test team"})
        cls.lead.call("team_add", {"team": "W", "agent": "W-LEAD", "role": "lead"})

    @classmethod
    def tearDownClass(cls):
        for p in (cls.runner, cls.hub):
            p.terminate()
            p.wait(5)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def add(self, name, **kw):
        fields = {"team": "W", "agent": name, "role": "worker", "cli": "claude", "host": "wakehost",
                  "cwd": self.tmp}
        fields.update(kw)
        self.lead.call("team_add", fields)

    def wait_reply(self, timeout=20):
        deadline = time.time() + timeout
        while time.time() < deadline:
            msgs = self.lead.call("wait", {"timeout": 5}, timeout=15)["messages"]
            if msgs:
                return msgs
        self.fail("no reply within %ss; runner log:\n%s" % (timeout, open(os.path.join(self.tmp, "runner.log")).read()))

    def calls(self):
        with open(os.path.join(self.tmp, "calls.log")) as f:
            return [json.loads(x) for x in f]

    def test_1_resume_existing_session(self):
        sid = "11111111-2222-3333-4444-555555555555"
        self.add("W-B", session_id=sid, options={"permission_mode": "bypass"}, effort="high")
        self.lead.call("send", {"to": "W-B", "body": "run case 7", "kind": "task"})
        msgs = self.wait_reply()
        self.assertEqual(msgs[0]["body"], "ack: run case 7")
        self.assertEqual(msgs[0]["sender"], "W-B")
        call = [c for c in self.calls() if c["name"] == "W-B"][-1]
        self.assertIn("-r", call["args"])
        self.assertEqual(call["args"][call["args"].index("-r") + 1], sid)
        self.assertIn("--dangerously-skip-permissions", call["args"])
        self.assertEqual(call["args"][call["args"].index("--effort") + 1], "high")
        self.assertEqual(os.path.realpath(call["cwd"]), os.path.realpath(self.tmp))
        time.sleep(0.5)
        w = [x for x in self.lead.call("wakes") if x["agent"] == "W-B"][0]
        self.assertEqual(w["exit_code"], 0)

    def test_2_create_new_session(self):
        self.add("W-NEW", options={"create": True})
        self.lead.call("send", {"to": "W-NEW", "body": "hello new", "kind": "task"})
        self.assertEqual(self.wait_reply()[0]["body"], "ack: hello new")
        call = [c for c in self.calls() if c["name"] == "W-NEW"][-1]
        self.assertIn("-n", call["args"])
        time.sleep(0.5)
        m = [x for x in self.lead.call("team_get", {"name": "W"})["members"] if x["agent"] == "W-NEW"][0]
        self.assertEqual(m["session_id"], "new-sess-W-NEW")
        self.assertNotIn("create", m["options"])

    def test_3_online_member_not_woken(self):
        self.add("W-ON", session_id="aaaaaaaa-2222-3333-4444-555555555555")
        on = HubClient(self.sock)
        on.register(kind="claude", name="W-ON")
        before = len(self.calls())
        self.lead.call("send", {"to": "W-ON", "body": "you are online"})
        time.sleep(2)
        self.assertEqual(len(self.calls()), before)
        self.assertEqual(on.call("inbox")["messages"][0]["body"], "you are online")

    def test_4_no_progress_suspends_and_tells_human(self):
        self.add("W-LAZY", session_id="bbbbbbbb-2222-3333-4444-555555555555")
        human = HubClient(self.sock)
        human.register(kind="human", name="human")
        self.lead.call("send", {"to": "W-LAZY", "body": "please do it"})
        deadline = time.time() + 25
        notice = None
        while time.time() < deadline and not notice:
            msgs = human.call("wait", {"timeout": 5}, timeout=15)["messages"]
            notice = next((m for m in msgs if m["sender"] == "youchat"), None)
        self.assertIsNotNone(notice, "no suspension notice")
        self.assertEqual(notice["kind"], "ask_human")
        self.assertIn("W-LAZY", notice["body"])
        n = len([c for c in self.calls() if c["name"] == "W-LAZY"])
        self.assertEqual(n, 3)
        time.sleep(2)
        self.assertEqual(len([c for c in self.calls() if c["name"] == "W-LAZY"]), 3)  # suspended

    def test_6_interrupted_turn_is_retried(self):
        self.add("W-FLAKY", session_id="dddddddd-2222-3333-4444-555555555555")
        self.lead.call("send", {"to": "W-FLAKY", "body": "long job", "kind": "task"})
        msgs = self.wait_reply()
        self.assertEqual(msgs[0]["body"], "ack-after-retry: long job")
        calls = [c for c in self.calls() if c["name"] == "W-FLAKY"]
        self.assertEqual(len(calls), 2)
        self.assertIn("中途中断", calls[1]["args"][calls[1]["args"].index("-p") + 1])
        time.sleep(0.5)
        w = [x for x in self.lead.call("wakes") if x["agent"] == "W-FLAKY"][0]
        self.assertIn("retried 1", w["note"])

    def test_7_unrecoverable_turn_notifies_human(self):
        human = HubClient(self.sock)
        human.register(kind="human", name="human")
        human.call("inbox")  # drain older notices
        self.add("W-DEAD", session_id="eeeeeeee-2222-3333-4444-555555555555")
        self.lead.call("send", {"to": "W-DEAD", "body": "doomed"})
        deadline, notice = time.time() + 20, None
        while time.time() < deadline and not notice:
            msgs = human.call("wait", {"timeout": 5}, timeout=15)["messages"]
            notice = next((m for m in msgs if m["sender"] == "youchat" and "W-DEAD" in m["body"]), None)
        self.assertIsNotNone(notice)
        self.assertIn("did not finish", notice["body"])
        self.assertEqual(len([c for c in self.calls() if c["name"] == "W-DEAD"]), 3)  # 1 + 2 retries

    def test_5_paused_team_not_woken(self):
        self.add("W-P", session_id="cccccccc-2222-3333-4444-555555555555")
        self.lead.call("team_update", {"name": "W", "status": "paused"})
        try:
            self.lead.call("send", {"to": "W-P", "body": "while paused"})
            time.sleep(2)
            self.assertFalse([c for c in self.calls() if c["name"] == "W-P"])
        finally:
            self.lead.call("team_update", {"name": "W", "status": "active"})
        self.assertEqual(self.wait_reply()[0]["body"], "ack: while paused")  # resumes after unpause

    def test_8_codex_and_agy_command_builders(self):
        from youchat.cli import interactive_command
        from youchat.runner import build_command
        cmd_u, _ = build_command({
            "cli": "codex", "agent": "DEV-CDX-1", "session_id": "sid-123", "prompt": "do dev",
            "model": "gpt-6.1-sol", "effort": "ultra", "options": {"ultra": True, "permission_mode": "bypass"},
        })
        self.assertIn("-m", cmd_u)
        self.assertEqual(cmd_u[cmd_u.index("-m") + 1], "gpt-6.1-sol")
        self.assertIn('model_reasoning_effort="ultra"', cmd_u)
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", cmd_u)
        self.assertEqual(cmd_u[-3:], ["resume", "sid-123", "do dev"])

        cmd_m, _ = build_command({
            "cli": "codex", "agent": "RUN-CDX-1", "session_id": "", "prompt": "submit job",
            "model": "gpt-6.1-sol", "effort": "medium", "options": {"permission_mode": "bypass"},
        })
        self.assertEqual(cmd_m[cmd_m.index("-m") + 1], "gpt-6.1-sol")
        self.assertIn('model_reasoning_effort="medium"', cmd_m)
        self.assertEqual(cmd_m[-1], "submit job")
        self.assertNotIn("resume", cmd_m)

        icmd = interactive_command({
            "cli": "codex", "agent": "DEV-CDX-1", "session_id": "sid-123", "cwd": "/tmp",
            "model": "gpt-6.1-sol", "effort": "ultra", "options": {"ultra": True, "permission_mode": "bypass"},
        })
        self.assertIn("-m gpt-6.1-sol", icmd)
        self.assertIn('model_reasoning_effort="ultra"', icmd)
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", icmd)

        cmd_agy, notes = build_command({
            "cli": "antigravity", "agent": "DEV-AGY-1", "session_id": "cid-456", "prompt": "review",
            "heavy": True, "options": {"boost": True, "permission_mode": "bypass"},
        })
        self.assertEqual(cmd_agy[cmd_agy.index("-p") + 1], "/boost review")
        self.assertIn("boost", notes)

        from youchat.sessions import SESSION_ARG_RE
        u1 = "3b26d6b9-a98c-46cc-9205-c84613227886"
        u2 = "01a143bc-10b3-7592-ad84-cb82e0a7eba1"
        self.assertEqual(SESSION_ARG_RE.findall("agy --conversation %s" % u1), [u1])
        self.assertEqual(SESSION_ARG_RE.findall("agy --conversation=%s" % u1), [u1])
        self.assertEqual(SESSION_ARG_RE.findall("codex resume %s -m gpt-6.1-sol" % u2), [u2])
        self.assertEqual(SESSION_ARG_RE.findall("sqlite3 ~/.codex/db 'SELECT * WHERE id=%s'" % u1), [])

    def test_9_pty_live_push(self):
        import pty
        import select
        import threading
        from youchat.pty_wrap import _watcher_loop
        sid = "ffffffff-2222-3333-4444-555555555555"
        self.add("W-PTY", session_id=sid, options={"boost": True, "permission_mode": "bypass"})
        master, slave = pty.openpty()
        # Dummy live child process representing an open interactive TUI window
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"],
                                stdin=slave, stdout=slave, stderr=slave, close_fds=True)
        os.close(slave)
        state = {"last_key": 0.0, "last_out": time.time() - 5.0, "line_dirty": False, "tui_ready": True, "stop": False}
        t = threading.Thread(target=_watcher_loop,
                             args=(master, proc, "antigravity", ["--conversation", sid], state), daemon=True)
        t.start()
        try:
            self.lead.call("send", {"to": "W-PTY", "body": "live task", "kind": "task"})
            buf = b""
            deadline = time.time() + 8.0
            while time.time() < deadline and b"\r" not in buf:
                r, _, _ = select.select([master], [], [], 0.5)
                if r:
                    buf += os.read(master, 4096)
            txt = buf.decode("utf-8", errors="replace")
            self.assertIn("/boost [youchat]", txt)
            self.assertIn("W-PTY", txt)
            self.assertIn("\r", txt)
        finally:
            state["stop"] = True
            proc.terminate()
            proc.wait()
            os.close(master)

    def test_10_codex_state_sqlite_and_rollout_sync(self):
        import sqlite3
        from youchat import sessions
        from youchat.runner import Runner
        fake_home = os.path.join(self.tmp, "codex_home")
        codex_dir = os.path.join(fake_home, ".codex")
        sess_dir = os.path.join(codex_dir, "sessions", "2026", "10", "16")
        os.makedirs(sess_dir, exist_ok=True)
        sid = "01a143bc-10b3-7592-ad84-cb82e0a7eba1"
        rpath = os.path.join(sess_dir, "rollout-2026-10-16T01-02-45-%s.jsonl" % sid)
        line1 = ('{"type":"session_meta","payload":{"id":"%s","cwd":"/tmp/x86_dev",'
                 '"originator":"codex_exec","source":"exec"}}\n') % sid
        line2 = '{"type":"turn.completed"}\n'
        with open(rpath, "w", encoding="utf-8") as f:
            f.write(line1 + line2)
        orig_size = os.path.getsize(rpath)
        db = os.path.join(codex_dir, "state_5.sqlite")
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, source TEXT, name TEXT, title TEXT, rollout_path TEXT)")
        con.execute("INSERT INTO threads VALUES (?, 'exec', NULL, 'raw prompt', ?)", (sid, rpath))
        con.commit()
        con.close()

        fake_codex = os.path.join(self.bin, "codex")
        with open(fake_codex, "w") as f:
            f.write("#!%s\nimport json\nprint(json.dumps({'type':'thread.started','thread_id':%r}))\n"
                    "print(json.dumps({'type':'turn.completed'}))\n" % (sys.executable, sid))
        os.chmod(fake_codex, 0o755)

        old_home = os.environ.get("HOME")
        old_path = os.environ.get("PATH")
        old_sess_home = sessions.HOME
        os.environ["HOME"] = fake_home
        os.environ["PATH"] = self.env["PATH"]
        sessions.HOME = fake_home
        try:
            r = Runner(host="wakehost")
            r._done = lambda *a, **kw: None  # type: ignore[method-assign]
            r._run_job({"wake_id": 99, "agent": "X86-DEV-CDX-1", "cli": "codex", "session_id": "",
                        "prompt": "init", "cwd": self.tmp, "options": {}})
            con = sqlite3.connect(db)
            row = con.execute("SELECT source, name, title FROM threads WHERE id=?", (sid,)).fetchone()
            con.close()
            self.assertEqual(row, ("cli", "X86-DEV-CDX-1", "X86-DEV-CDX-1"))
            self.assertEqual(os.path.getsize(rpath), orig_size)
            with open(rpath, "rb") as f:
                first = f.readline()
            self.assertIn(b'"source": "cli"', first)
            self.assertNotIn(b'"source":"exec"', first)
            found = sessions.codex_sessions()
            self.assertEqual(len(found), 1)
            self.assertEqual(found[0]["title"], "X86-DEV-CDX-1")
        finally:
            if old_home is not None:
                os.environ["HOME"] = old_home
            if old_path is not None:
                os.environ["PATH"] = old_path
            sessions.HOME = old_sess_home
            if os.path.exists(fake_codex):
                os.unlink(fake_codex)

    def test_11_codex_app_server_and_unwrapped_window_handling(self):
        from youchat.pty_wrap import is_session_idle
        from youchat.sessions import _classify_agent_proc
        self.assertEqual(
            _classify_agent_proc("/Users/u/.codex/packages/app-server-daemon/bin/codex app-server --managed-daemon"),
            "codex-daemon",
        )
        self.assertIsNone(
            _classify_agent_proc("/Applications/ChatGPT.app/Contents/Frameworks/Codex Framework.framework/Codex --type=renderer")
        )
        self.assertEqual(_classify_agent_proc("codex resume 01a143bc-10b3-7592-ad84-cb82e0a7eba1 --yolo"), "codex")
        self.assertEqual(_classify_agent_proc("agy --dangerously-skip-permissions"), "agy")
        self.assertEqual(_classify_agent_proc("claude --resume 6a5ed01e-b296-4055-8837-3f65b072bc60"), "claude")

        from youchat import sessions
        fake_home = os.path.join(self.tmp, "home_claude_idle")
        proj = os.path.join(fake_home, ".claude", "projects", "testproj")
        os.makedirs(proj, exist_ok=True)
        sid = "6a5ed01e-b296-4055-8837-3f65b072bc60"
        cpath = os.path.join(proj, sid + ".jsonl")
        with open(cpath, "w") as f:
            f.write(json.dumps({"type": "assistant", "message": {"stop_reason": "end_turn"}}) + "\n")
            f.write(json.dumps({"type": "system", "subtype": "turn_duration", "durationMs": 1200}) + "\n")
        old_mtime = time.time() - 30
        os.utime(cpath, (old_mtime, old_mtime))
        old_sess_home = sessions.HOME
        from youchat import pty_wrap
        pty_wrap.HOME = fake_home
        try:
            self.assertTrue(is_session_idle("claude", sid))
            with open(cpath, "a") as f:
                f.write(json.dumps({"type": "api-request"}) + "\n")
            os.utime(cpath, (old_mtime, old_mtime))
            self.assertFalse(is_session_idle("claude", sid))
        finally:
            pty_wrap.HOME = old_sess_home


if __name__ == "__main__":
    unittest.main()



