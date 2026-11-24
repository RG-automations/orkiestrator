"""Negative tests for the human-in-the-loop guarantees: every way out without a valid, current human approval must fail."""
import http.client
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import urlencode

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from orkiestrator import adapters, gate, publisher, web  # noqa: E402
from orkiestrator.db import connect  # noqa: E402

AGENT, OWNER = "agent:content-studio", "human:owner"


def pending(conn, body="tekst posta"):
    pid = gate.add_draft(conn, AGENT, "linkedin", "text_post", body, hook="hak", payload='{"k": 1}')
    gate.transition(conn, pid, "submit", AGENT)
    return pid


def approved_and_due(conn, body="tekst posta"):
    pid = pending(conn, body)
    gate.transition(conn, pid, "approve", OWNER)
    gate.transition(conn, pid, "schedule", "agent:scheduler", scheduled_at="2000-01-01T00:00:00+00:00")
    return pid


class GateTests(unittest.TestCase):
    def setUp(self):
        self.conn = connect(":memory:")
        self.outbox = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.outbox, ignore_errors=True)

    def status(self, pid):
        return self.conn.execute("SELECT status FROM posts WHERE id=?", (pid,)).fetchone()[0]

    def publish_due(self):
        return publisher.run_due(self.conn, outbox=self.outbox)

    def test_non_human_actors_cannot_approve_or_reject(self):
        pid = pending(self.conn)
        for actor in ("agent:x", "human:", "Human:owner", "owner", "", " human:owner"):
            for action in ("approve", "reject"):
                with self.assertRaises(gate.GateError, msg=(actor, action)):
                    gate.transition(self.conn, pid, action, actor)
        self.assertEqual(self.status(pid), "pending_approval")

    def test_forged_scheduled_status_without_approval_is_never_published(self):
        pid = pending(self.conn)
        self.conn.execute("UPDATE posts SET status='scheduled', scheduled_at='2000-01-01T00:00:00+00:00' WHERE id=?", (pid,))
        self.assertEqual(self.publish_due(), [])
        with self.assertRaises(gate.GateError):
            gate.transition(self.conn, pid, "publish", "agent:publisher")
        self.assertFalse(list(self.outbox.iterdir()))

    def test_forged_human_approval_columns_without_matching_hash_are_refused(self):
        pid = pending(self.conn)
        self.conn.execute("UPDATE posts SET status='scheduled', approved_by='human:owner', approved_at='2026-01-01', "
                          "scheduled_at='2000-01-01T00:00:00+00:00' WHERE id=?", (pid,))
        with self.assertRaises(gate.GateError):
            self.publish_due()
        self.assertEqual(self.status(pid), "scheduled")
        self.assertFalse(list(self.outbox.iterdir()))

    def test_content_changed_outside_the_gate_after_approval_is_not_published(self):
        for column, value in (("body", "podmieniony tekst"), ("hook", "inny hak"), ("payload", '{"k": 2}'), ("platform", "facebook")):
            pid = approved_and_due(self.conn)
            self.conn.execute(f"UPDATE posts SET {column}=? WHERE id=?", (value, pid))
            with self.assertRaises(gate.GateError, msg=column):
                gate.transition(self.conn, pid, "publish", "agent:publisher")
            self.assertEqual(self.status(pid), "scheduled", column)
            self.conn.execute("DELETE FROM posts WHERE id=?", (pid,))
        self.assertFalse(list(self.outbox.iterdir()))

    def test_edit_after_approval_or_scheduling_voids_it(self):
        for schedule in (False, True):
            pid = pending(self.conn)
            gate.transition(self.conn, pid, "approve", OWNER)
            if schedule:
                gate.transition(self.conn, pid, "schedule", "agent:scheduler", scheduled_at="2000-01-01T00:00:00+00:00")
            gate.edit_body(self.conn, pid, AGENT, "nowa wersja")
            row = self.conn.execute("SELECT status, approved_by, approved_at, approved_hash, scheduled_at FROM posts WHERE id=?", (pid,)).fetchone()
            self.assertEqual(tuple(row), ("draft", None, None, None, None))
            self.assertEqual(self.publish_due(), [])
            with self.assertRaises(gate.GateError):
                gate.transition(self.conn, pid, "schedule", "agent:scheduler", scheduled_at="2000-01-01T00:00:00+00:00")

    def test_approval_from_before_content_hashing_must_be_given_again(self):
        pid = approved_and_due(self.conn)
        self.conn.execute("UPDATE posts SET approved_hash=NULL WHERE id=?", (pid,))
        with self.assertRaises(gate.GateError):
            self.publish_due()

    def test_published_post_cannot_be_published_again_or_edited(self):
        pid = approved_and_due(self.conn)
        self.assertEqual(len(self.publish_due()), 1)
        self.assertEqual(self.publish_due(), [])
        with self.assertRaises(gate.GateError):
            gate.transition(self.conn, pid, "publish", "agent:publisher")
        with self.assertRaises(gate.GateError):
            gate.edit_body(self.conn, pid, OWNER, "po publikacji")

    def test_failed_platform_call_leaves_post_unpublished(self):
        pid = approved_and_due(self.conn)

        class Boom:
            def publish(self, post):
                raise RuntimeError("API down")

        with mock.patch.object(publisher, "get_publisher", lambda platform, outbox=None: Boom()):
            with self.assertRaises(RuntimeError):
                self.publish_due()
        self.assertEqual(self.status(pid), "scheduled")

    def test_every_transition_is_logged_with_its_actor(self):
        pid = approved_and_due(self.conn)
        self.publish_due()
        log = [tuple(r) for r in self.conn.execute("SELECT actor, action FROM events WHERE post_id=? ORDER BY id", (pid,))]
        self.assertEqual(log, [(AGENT, "create"), (AGENT, "submit"), (OWNER, "approve"),
                               ("agent:scheduler", "schedule"), ("agent:publisher", "publish")])


class ProcessTests(unittest.TestCase):
    """Real processes on a copy of the repo, so nothing touches the working tree."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        for name in ("src", "knowledge", "inputs"):
            shutil.copytree(REPO / name, self.root / name, ignore=shutil.ignore_patterns("__pycache__"))
        self.env = {k: v for k, v in os.environ.items() if k not in ("ORKI_DB", "ORKI_TOKEN")}
        self.env.update(PYTHONPATH=str(self.root / "src"), FB_PAGE_ID="123", FB_PAGE_TOKEN="not-a-real-token")
        self.conn = connect(self.root / "data" / "queue.db")

    def cli(self, *args):
        return subprocess.run([sys.executable, "-m", "orkiestrator", *args], cwd=self.root, env=self.env,
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)

    def test_cli_approve_and_reject_refuse_without_interactive_terminal(self):
        pid = pending(self.conn)
        for cmd in ("approve", "reject"):
            out = self.cli(cmd, str(pid))
            self.assertEqual(out.returncode, 1, out.stderr)
            self.assertIn("interactive terminal", out.stderr)
        self.assertEqual(self.conn.execute("SELECT status FROM posts WHERE id=?", (pid,)).fetchone()[0], "pending_approval")

    def test_cli_publish_due_is_dry_run_even_with_facebook_credentials(self):
        """Live mode exists only in the memory of a panel process after the owner typed the confirmation."""
        pid = pending(self.conn)
        self.conn.execute("UPDATE posts SET platform='facebook' WHERE id=?", (pid,))
        self.conn.commit()
        gate.transition(self.conn, pid, "approve", OWNER)
        gate.transition(self.conn, pid, "schedule", "agent:scheduler", scheduled_at="2000-01-01T00:00:00+00:00")
        out = self.cli("publish-due")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertTrue((self.root / "data" / "outbox" / f"facebook-{pid}.json").exists())
        self.assertNotIn("facebook:", out.stdout)


class PanelTests(unittest.TestCase):
    TOKEN = "panel-test-token"

    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        self.db = tmp / "q.db"
        self.srv = web.make_server(0, llm=None, outbox=tmp / "out", token=self.TOKEN, db_path=self.db)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.shutdown)
        adapters.LIVE["confirmed"] = False
        self.addCleanup(lambda: adapters.LIVE.update(confirmed=False))
        self.pid = pending(connect(self.db))

    def req(self, method, path, cookie=None, headers=None, data=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port)
        h = dict(headers or {})
        if cookie is not None:
            h["Cookie"] = f"orki={cookie}"
        body = urlencode(data) if data else None
        if body:
            h["Content-Type"] = "application/x-www-form-urlencoded"
        c.request(method, path, body, h)
        r = c.getresponse()
        r.read()
        return r.status

    def status(self):
        return connect(self.db).execute("SELECT status FROM posts WHERE id=?", (self.pid,)).fetchone()[0]

    def test_approve_requires_the_owner_session(self):
        for cookie in (None, "", "wrong", self.TOKEN[:-1]):
            self.assertEqual(self.req("POST", f"/approve/{self.pid}", cookie=cookie), 401, cookie)
            self.assertEqual(self.req("POST", f"/publish-now/{self.pid}", cookie=cookie), 401, cookie)
        self.assertEqual(self.status(), "pending_approval")

    def test_token_in_query_string_does_not_approve(self):
        self.assertEqual(self.req("POST", f"/approve/{self.pid}?token={self.TOKEN}"), 401)
        self.assertEqual(self.req("GET", "/?token=wrong"), 401)
        self.assertEqual(self.status(), "pending_approval")

    def test_cross_origin_post_is_refused_even_with_the_cookie(self):
        status = self.req("POST", f"/approve/{self.pid}", cookie=self.TOKEN, headers={"Origin": "http://evil.example"})
        self.assertEqual(status, 403)
        self.assertEqual(self.status(), "pending_approval")

    def test_get_cannot_change_state(self):
        self.assertEqual(self.req("GET", f"/approve/{self.pid}", cookie=self.TOKEN), 404)
        self.assertEqual(self.status(), "pending_approval")

    def test_live_mode_needs_exact_confirmation_and_credentials(self):
        with mock.patch.dict("os.environ", {"FB_PAGE_ID": "", "FB_PAGE_TOKEN": ""}):
            self.req("POST", "/live", cookie=self.TOKEN, data={"confirm": "OPUBLIKUJ NAPRAWDE"})
        self.assertFalse(adapters.LIVE["confirmed"])
        with mock.patch.dict("os.environ", {"FB_PAGE_ID": "1", "FB_PAGE_TOKEN": "t"}):
            self.req("POST", "/live", cookie=self.TOKEN, data={"confirm": "tak"})
            self.assertFalse(adapters.LIVE["confirmed"])
            self.req("POST", "/live", cookie=None, data={"confirm": "OPUBLIKUJ NAPRAWDE"})
            self.assertFalse(adapters.LIVE["confirmed"])

    def test_owner_session_approves_as_human_and_binds_the_content(self):
        self.assertEqual(self.req("POST", f"/approve/{self.pid}", cookie=self.TOKEN), 303)
        row = connect(self.db).execute("SELECT * FROM posts WHERE id=?", (self.pid,)).fetchone()
        self.assertEqual(row["status"], "scheduled")
        self.assertTrue(row["approved_by"].startswith("human:"))
        self.assertEqual(row["approved_hash"], gate.content_hash(row))


if __name__ == "__main__":
    unittest.main()
