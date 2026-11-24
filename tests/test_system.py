import csv
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from orkiestrator import attribution, brief, gate, leads, publisher  # noqa: E402
from orkiestrator.db import connect  # noqa: E402

AGENT, OWNER = "agent:content", "human:owner"
PAST = "2000-01-01T00:00:00+00:00"


def new_draft(conn):
    return gate.add_draft(conn, AGENT, "instagram", "reel", "body", hook="hook", link_id="L1")


class GateTests(unittest.TestCase):
    def setUp(self):
        self.conn = connect(":memory:")
        self.outbox = tempfile.mkdtemp()

    def status(self, pid):
        return self.conn.execute("SELECT status FROM posts WHERE id=?", (pid,)).fetchone()[0]

    def test_agent_cannot_approve(self):
        pid = new_draft(self.conn)
        gate.transition(self.conn, pid, "submit", AGENT)
        with self.assertRaises(gate.GateError):
            gate.transition(self.conn, pid, "approve", AGENT)
        self.assertEqual(self.status(pid), "pending_approval")

    def test_cannot_skip_to_publish_or_schedule(self):
        pid = new_draft(self.conn)
        for action in ("publish", "schedule", "approve"):
            with self.assertRaises(gate.GateError):
                gate.transition(self.conn, pid, action, OWNER, scheduled_at=PAST)
        gate.transition(self.conn, pid, "submit", AGENT)
        with self.assertRaises(gate.GateError):
            gate.transition(self.conn, pid, "publish", AGENT)

    def test_forged_status_without_human_approval_is_refused(self):
        pid = new_draft(self.conn)
        self.conn.execute("UPDATE posts SET status='approved', scheduled_at=? WHERE id=?", (PAST, pid))
        self.conn.commit()
        with self.assertRaises(gate.GateError):
            gate.transition(self.conn, pid, "publish", AGENT)
        self.assertEqual(publisher.run_due(self.conn, outbox=self.outbox), [])

    def test_approved_post_publishes_and_is_logged(self):
        pid = new_draft(self.conn)
        gate.transition(self.conn, pid, "submit", AGENT)
        gate.transition(self.conn, pid, "approve", OWNER)
        gate.transition(self.conn, pid, "schedule", AGENT, scheduled_at=PAST)
        done = publisher.run_due(self.conn, outbox=self.outbox)
        self.assertEqual([d[0] for d in done], [pid])
        self.assertEqual(self.status(pid), "published")
        self.assertTrue((Path(self.outbox) / f"instagram-{pid}.json").exists())
        actions = [r[0] for r in self.conn.execute("SELECT action FROM events WHERE post_id=? ORDER BY id", (pid,))]
        self.assertEqual(actions, ["create", "submit", "approve", "schedule", "publish"])

    def test_unapproved_posts_never_reach_outbox(self):
        pid = new_draft(self.conn)
        gate.transition(self.conn, pid, "submit", AGENT)
        self.conn.execute("UPDATE posts SET scheduled_at=? WHERE id=?", (PAST, pid))
        self.conn.commit()
        self.assertEqual(publisher.run_due(self.conn, outbox=self.outbox), [])
        self.assertEqual(list(Path(self.outbox).glob("*")), [])

    def test_submit_blocked_by_voice_lint(self):
        pid = gate.add_draft(self.conn, AGENT, "linkedin", "post", "Let's delve in — unlock growth")
        with self.assertRaises(gate.GateError):
            gate.transition(self.conn, pid, "submit", AGENT)
        self.assertEqual(self.status(pid), "draft")

    def test_edit_voids_approval(self):
        pid = new_draft(self.conn)
        gate.transition(self.conn, pid, "submit", AGENT)
        gate.transition(self.conn, pid, "approve", OWNER)
        gate.edit_body(self.conn, pid, AGENT, "sneaky change")
        self.assertEqual(self.status(pid), "draft")
        with self.assertRaises(gate.GateError):
            gate.transition(self.conn, pid, "schedule", AGENT, scheduled_at=PAST)


class CliApproveTests(unittest.TestCase):
    """Exercises cli.main approve with the owner's terminal confirmation simulated (this is a test harness, not an approval)."""

    def setUp(self):
        import os
        from unittest import mock
        self.db = Path(tempfile.mkdtemp()) / "q.db"
        self.env = mock.patch.dict(os.environ, {"ORKI_DB": str(self.db)})
        self.env.start()
        self.addCleanup(self.env.stop)
        conn = connect(self.db)
        self.pid = new_draft(conn)
        gate.transition(conn, self.pid, "submit", AGENT)

    def status(self):
        return connect(self.db).execute("SELECT status FROM posts WHERE id=?", (self.pid,)).fetchone()[0]

    def test_non_interactive_approve_is_refused(self):
        from unittest import mock
        from orkiestrator import cli
        with mock.patch("sys.stdin.isatty", return_value=False):
            self.assertEqual(cli.main(["approve", str(self.pid)]), 1)
        self.assertEqual(self.status(), "pending_approval")

    def test_interactive_yes_approves_as_human(self):
        from unittest import mock
        from orkiestrator import cli
        with mock.patch("sys.stdin.isatty", return_value=True), mock.patch("builtins.input", return_value="y"):
            self.assertEqual(cli.main(["approve", str(self.pid)]), 0)
        self.assertEqual(self.status(), "approved")
        by = connect(self.db).execute("SELECT approved_by FROM posts WHERE id=?", (self.pid,)).fetchone()[0]
        self.assertTrue(by.startswith("human:"))

    def test_interactive_no_does_not_approve(self):
        from unittest import mock
        from orkiestrator import cli
        with mock.patch("sys.stdin.isatty", return_value=True), mock.patch("builtins.input", return_value="n"):
            self.assertEqual(cli.main(["approve", str(self.pid)]), 1)
        self.assertEqual(self.status(), "pending_approval")


class LeadTests(unittest.TestCase):
    def test_classify(self):
        self.assertEqual(leads.classify("how much is this?"), "lead")
        self.assertEqual(leads.classify("ile kosztuje kurs"), "lead")
        self.assertEqual(leads.classify("ile to kosztuje?"), "lead")
        self.assertEqual(leads.classify("jaka jest cena?"), "lead")
        self.assertEqual(leads.classify("ile osób to czyta"), "other")
        self.assertEqual(leads.classify("how much is the course and is there a link?"), "lead")
        self.assertEqual(leads.classify("what is the price?"), "lead")
        self.assertEqual(leads.classify("project pricing vs hourly, when to switch?"), "other")
        self.assertEqual(leads.classify("this is a scam"), "negative")
        self.assertEqual(leads.classify("thanks, great video"), "other")

    def test_capture_only_stores_leads(self):
        conn = connect(":memory:")
        leads.capture(conn, "instagram", "@a", "how much?")
        leads.capture(conn, "instagram", "@b", "nice")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0], 1)


class FunnelTests(unittest.TestCase):
    def test_attribution_and_brief(self):
        conn = connect(":memory:")
        pid = new_draft(conn)
        gate.transition(conn, pid, "submit", AGENT)
        gate.transition(conn, pid, "approve", OWNER)
        gate.transition(conn, pid, "publish", AGENT)
        path = Path(tempfile.mkdtemp()) / "m.csv"
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["post_id", "day", "followers_gained", "clicks", "customers", "revenue"])
            w.writerow([pid, "2026-10-19", 10, 40, 2, 200])
            w.writerow([pid, "2026-10-20", 5, 10, 0, 0])
        attribution.import_csv(conn, path)
        row = attribution.funnel(conn)[0]
        self.assertEqual((row["followers"], row["clicks"], row["customers"], row["revenue"]), (15, 50, 2, 200))
        self.assertEqual(row["click_to_customer"], 0.04)
        self.assertIn("200.00", brief.render(conn))


if __name__ == "__main__":
    unittest.main()
