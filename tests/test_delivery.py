"""Delivery into open codex/agy sessions.

1. The hub tells the runner whether a member's messages are being watched right now (a live `youchat pty`
   wrapper keeps a `watch` connection open). The runner trusts that instead of guessing from the process list.
2. An unwrapped codex/agy TUI that is open in a tmux pane gets the wake line typed into that pane.
"""
import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from youchat import hub as hubmod  # noqa: E402
from youchat import runner as runnermod  # noqa: E402

TMUX = shutil.which("tmux") or "/opt/homebrew/bin/tmux"


class HubWatchedFlag(unittest.TestCase):
    def test_job_says_whether_a_watcher_is_connected(self):
        tmp = tempfile.mkdtemp(prefix="ycd")
        self.addCleanup(shutil.rmtree, tmp, True)
        h = hubmod.Hub(os.path.join(tmp, "hub.db"))
        self.addCleanup(h.db.close)
        lead, runner, watcher = hubmod.Conn(None), hubmod.Conn(None), hubmod.Conn(None)
        h.op_hello(lead, kind="human", host="h", name="lead")
        h.op_hello(runner, kind="runner", host="h", name="runner@h")
        h.op_team_create(lead, name="T")
        for a in ("W1", "W2"):
            h.op_team_add(lead, team="T", agent=a, cli="codex", host="h", session_id="sid-" + a)
        h.op_send(lead, to=["W1", "W2"], body="please review")
        h.db.execute("UPDATE messages SET created_at = created_at - 600")  # past the online grace period

        async def run():
            await h.op_watch(watcher, agent="W1", timeout=0)  # like the pty wrapper; connection stays open
            res = await h.op_runner_wait(runner, host="h", timeout=0, max_jobs=5)
            return {j["agent"]: j for j in res["jobs"]}

        jobs = asyncio.run(run())
        self.assertIs(jobs["W1"]["watched"], True)
        self.assertIs(jobs["W2"]["watched"], False)
        h._set_offline(watcher)  # wrapper exited: its connection is gone
        self.assertFalse(h.watch_conns.get("W1"))


class RunnerTrustsHub(unittest.TestCase):
    """The ANDAIR case: an unrelated wrapped codex made live_push_session_ids() list AA-CDX's session,
    so the runner logged 'open with live push' and neither delivered nor told anyone."""

    def setUp(self):
        tmp = tempfile.mkdtemp(prefix="ycr")
        self.addCleanup(shutil.rmtree, tmp, True)
        patches = [mock.patch.dict(os.environ, {"YOUCHAT_HOME": tmp}),
                   mock.patch.object(runnermod, "running_session_ids", return_value={"S"}),
                   mock.patch("youchat.sessions.live_push_session_ids", return_value={"S"}),  # the false positive
                   mock.patch("youchat.pty_wrap.is_session_idle", return_value=True)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.notify = mock.patch.object(runnermod, "notify_desktop").start()
        self.addCleanup(mock.patch.stopall)
        self.r = runnermod.Runner(host="h")

    def job(self, watched):
        return {"cli": "codex", "agent": "AA-CDX", "wake_id": 1, "unread": 2, "watched": watched}

    def test_unwatched_session_is_not_treated_as_live_push(self):
        self.assertTrue(self.r._should_yield_open(self.job(False), "S"))  # still locked: never drive in parallel
        self.notify.assert_called_once()                                    # ...but the user is told

    def test_watched_session_is_left_to_its_wrapper(self):
        self.assertTrue(self.r._should_yield_open(self.job(True), "S"))
        self.notify.assert_not_called()


STUB_TUI = """#!%(py)s
import sys
with open(%(out)r, "a") as f:
    f.write("started " + " ".join(sys.argv[1:]) + "\\n")
for line in sys.stdin:
    with open(%(out)r, "a") as f:
        f.write("got " + line)
"""
STUB_PTY = """#!%(py)s
import subprocess, sys
sys.exit(subprocess.call(sys.argv[2:]))  # stands in for `youchat pty <cli> ...`
"""


@unittest.skipUnless(os.path.exists(TMUX), "needs tmux")
class TmuxDelivery(unittest.TestCase):
    """Private tmux server, stub TUIs, test-only member names: never the user's real panes."""

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="yct", dir="/tmp"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.out = os.path.join(self.tmp, "codex.out")
        env = mock.patch.dict(os.environ, {"TMUX_TMPDIR": self.tmp, "YOUCHAT_HOME": self.tmp,
                                           "YOUCHAT_SOCK": os.path.join(self.tmp, "no-hub.sock")})
        env.start()
        self.addCleanup(env.stop)
        for k in ("TMUX", "TMUX_PANE"):  # a set $TMUX beats TMUX_TMPDIR and would reach the real tmux server
            os.environ.pop(k, None)
        self.tmux_env = dict(os.environ)
        for p in (mock.patch("youchat.pty_wrap.is_session_idle", return_value=True),
                  mock.patch.object(runnermod, "TMUX_READ_TIMEOUT", 0.5),
                  mock.patch.object(runnermod, "TMUX_QUIET", 1.0)):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmux, "kill-server")
        # AA-CDX's case: codex restarted by hand in picker mode (no session id on its command line), not wrapped;
        # codex titles its pane "<session name> | <dir>"
        self.tmux("new-session", "-d", "-s", "yc-T", "-n", "T-CDX", "%s --yolo resume --yolo" % self.stub("codex", self.out))
        self.tmux("select-pane", "-t", "=yc-T:=T-CDX", "-T", "T-CDX | andair")
        self.wait_for(lambda: os.path.exists(self.out))
        self.r = runnermod.Runner(host="h")
        self.done = mock.patch.object(self.r, "_done").start()
        self.addCleanup(mock.patch.stopall)

    def tmux(self, *args):
        return subprocess.run([TMUX] + list(args), env=self.tmux_env, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, universal_newlines=True)

    def stub(self, name, out, template=STUB_TUI):
        path = os.path.join(self.tmp, name)
        with open(path, "w") as f:
            f.write(template % {"py": sys.executable, "out": out})
        os.chmod(path, 0o755)
        return path

    def wait_for(self, cond, timeout=10.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if cond():
                return True
            time.sleep(0.1)
        return False

    @staticmethod
    def read(path):
        with open(path) as f:
            return f.read()

    def got(self, path=None):
        return [line for line in self.read(path or self.out).splitlines(True) if line.startswith("got ")]

    def job(self, agent="T-CDX", cli="codex", sid="019f0000-0000-7000-8000-000000000001"):
        return {"cli": cli, "agent": agent, "wake_id": 7, "unread": 2, "watched": False, "options": {},
                "heavy": False, "session_id": sid, "lease_seconds": 600}

    def drive(self, job):
        """Like the runner retrying a wake: the first look records the screen, the next (screen unchanged) acts."""
        first = self.r._drive_tmux_pane(job, job["session_id"])
        time.sleep(1.2)
        return first, self.r._drive_tmux_pane(job, job["session_id"])

    def test_wake_line_is_typed_into_the_members_pane(self):
        job = self.job()
        with mock.patch.object(self.r, "_should_yield_open", return_value=True):  # never a real headless turn
            self.r._run_job(job)
            self.assertEqual(self.done.call_args[0][3], "busy")  # first look only records the screen
            self.assertEqual(self.got(), [])
            time.sleep(1.2)
            self.r._run_job(job)
        self.assertEqual(self.done.call_args[0][3], "tmux")
        self.assertTrue(self.wait_for(lambda: self.got()), "nothing typed into the pane")
        self.assertIn("T-CDX", self.got()[0])
        self.assertIn("check_inbox", self.got()[0])

    def test_agy_pane_found_by_its_conversation_id(self):
        # AA-AGY's case: agy restarted by hand with --conversation=<id>; its pane title is not the member name
        out, sid = os.path.join(self.tmp, "agy.out"), "1cb41721-aea3-4cf2-adc0-00b49e8be75f"
        self.tmux("new-window", "-d", "-t", "=yc-T:", "-n", "zsh",
                  "%s --dangerously-skip-permissions --conversation=%s" % (self.stub("agy", out), sid))
        self.assertTrue(self.wait_for(lambda: os.path.exists(out)))
        self.assertEqual(self.drive(self.job("T-AGY", "antigravity", sid)), (True, True))
        self.assertTrue(self.wait_for(lambda: self.got(out)))
        self.assertIn("T-AGY", self.got(out)[0])
        self.assertEqual(self.got(), [])  # nothing went to the codex pane

    def test_another_members_pane_is_left_alone(self):
        job = self.job("T-LW", sid="019f0000-0000-7000-8000-0000000000ff")  # same cli, other member and session
        self.assertEqual(self.drive(job), (False, False))
        self.assertEqual(self.got(), [])

    def test_wrapped_pane_is_left_to_youchat_pty(self):
        out = os.path.join(self.tmp, "wrapped.out")
        os.makedirs(os.path.join(self.tmp, "w"))
        wrapper, codex = self.stub("youchat", out, STUB_PTY), self.stub("w/codex", out)
        self.tmux("new-window", "-d", "-t", "=yc-T:", "-n", "T-W", "%s pty %s --yolo resume" % (wrapper, codex))
        self.tmux("select-pane", "-t", "=yc-T:=T-W", "-T", "T-W | andair")
        self.assertTrue(self.wait_for(lambda: os.path.exists(out)))
        self.assertEqual(self.drive(self.job("T-W")), (False, False))
        self.assertEqual(self.got(out), [])

    def test_typing_in_the_pane_defers_delivery(self):
        # iTerm2 `tmux -CC` sends keys as commands: tmux's client_activity never moves, but the screen does
        client = subprocess.Popen([TMUX, "-C", "attach", "-t", "=yc-T"], env=self.tmux_env,
                                  stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(client.wait)
        self.addCleanup(client.kill)
        self.addCleanup(client.stdin.close)
        pane = self.tmux("display-message", "-p", "-t", "=yc-T:=T-CDX", "#{pane_id}").stdout.strip()
        job = self.job()
        self.assertTrue(self.r._drive_tmux_pane(job, job["session_id"]))
        time.sleep(1.2)
        client.stdin.write(("send-keys -t %s -l halfway\n" % pane).encode())
        client.stdin.flush()
        self.assertTrue(self.wait_for(lambda: "halfway" in self.tmux("capture-pane", "-p", "-t", pane).stdout))
        self.assertTrue(self.r._drive_tmux_pane(job, job["session_id"]))
        self.assertEqual(self.done.call_args[0][3], "busy")
        time.sleep(0.5)
        self.assertEqual(self.got(), [])


if __name__ == "__main__":
    unittest.main()
