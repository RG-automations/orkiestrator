"""Untrusted data lives in inputs/*.csv (scraped comments, competitor posts) and ends up inside LLM
prompts, and LLM output ends up inside HTML pages, SVG files and SQL parameters. None of those hops may
turn attacker-controlled text into executed code, a broken-out-of SQL statement or unescaped markup.
These are not prompt-injection *prevention* tests (an LLM can still be talked into writing a bad post;
a human still has to read it before approving) — they check the mechanical boundaries: storage, rendering
and the one place content could change between approval and publish.
"""
import json
import sys
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from orkiestrator import carousel, gate, leads, web  # noqa: E402
from orkiestrator.db import connect  # noqa: E402

PAYLOADS = (
    "<script>alert(1)</script>",
    "'); DROP TABLE posts; --",
    "{{7*7}}${7*7}#{7*7}",
    "\" onmouseover=\"alert(1)",
    "$(rm -rf /)`id`",
    "../../../../etc/passwd",
)


class StorageIsNeverExecuted(unittest.TestCase):
    """Attacker-controlled text reaches add_draft/leads.capture as plain column values, via parameterized
    SQL (the only kind this codebase uses). It must round-trip byte-for-byte and never affect other rows."""

    def setUp(self):
        self.conn = connect(":memory:")

    def test_malicious_draft_content_is_stored_and_read_back_unexecuted(self):
        for payload in PAYLOADS:
            pid = gate.add_draft(self.conn, "agent:x", "linkedin", "text_post", payload, hook=payload, payload=json.dumps({"x": payload}))
            row = self.conn.execute("SELECT body, hook, payload FROM posts WHERE id=?", (pid,)).fetchone()
            self.assertEqual(row["body"], payload)
            self.assertEqual(row["hook"], payload)
            self.assertEqual(json.loads(row["payload"])["x"], payload)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0], len(PAYLOADS))

    def test_malicious_inbound_message_does_not_corrupt_other_rows(self):
        for payload in PAYLOADS:
            leads.capture(self.conn, "instagram", payload, payload)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM inbound").fetchone()[0], len(PAYLOADS))
        rows = self.conn.execute("SELECT handle, message FROM inbound").fetchall()
        self.assertEqual({r["handle"] for r in rows}, set(PAYLOADS))

    def test_classify_never_raises_on_adversarial_text(self):
        for payload in PAYLOADS + ("", "a" * 10000, "\x00\x01\x02"):
            leads.classify(payload)  # must not raise, whatever the input


class RenderingEscapesContent(unittest.TestCase):
    """Anything that lands in an SVG or an HTML page by way of a post's text must come out escaped."""

    def test_svg_slides_escape_markup(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = carousel.render(1, [{"title": PAYLOADS[0], "text": PAYLOADS[1]}], root=Path(tmp))
            svg = paths[0].read_text(encoding="utf-8")
            self.assertNotIn("<script>", svg)
            self.assertIn("&lt;script&gt;", svg)

    def test_queue_page_escapes_post_body_and_hook(self):
        tmp = Path(tempfile.mkdtemp())
        db = tmp / "q.db"
        conn = connect(db)
        pid = gate.add_draft(conn, "agent:x", "linkedin", "text_post", PAYLOADS[0], hook=PAYLOADS[3])
        gate.transition(conn, pid, "submit", "agent:x")
        srv = web.make_server(0, llm=None, outbox=tmp / "out", token="tok", db_path=db)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        c = HTTPConnection("127.0.0.1", port)
        c.request("GET", "/queue", headers={"Cookie": "orki=tok"})
        body = c.getresponse().read().decode()
        self.assertNotIn("<script>alert(1)</script>", body)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", body)
        self.assertNotIn('onmouseover="alert(1)"', body)


class ContentFrozenBetweenApprovalAndPublish(unittest.TestCase):
    """An agent (or injected instructions reaching agent-level code) can still only create drafts and
    submit them — it cannot forge a human: actor (see test_hitl.py) and, once a post is claimed for
    publishing, edit_body refuses to touch it (see test_hitl.py's test_claimed_post_cannot_be_edited...).
    This test is the end-to-end version: attacker-controlled text that an agent tries to sneak into an
    *already-approved* post never reaches the platform call."""

    def test_attempted_post_swap_after_approval_is_blocked_before_publish(self):
        conn = connect(":memory:")
        pid = gate.add_draft(conn, "agent:x", "linkedin", "text_post", "tekst zatwierdzony przez wlasciciela", hook="h")
        gate.transition(conn, pid, "submit", "agent:x")
        gate.transition(conn, pid, "approve", "human:owner")
        gate.transition(conn, pid, "schedule", "agent:scheduler", scheduled_at=gate.now())
        # A compromised agent tries to swap the body via the only non-gate path available: raw SQL.
        conn.execute("UPDATE posts SET body=? WHERE id=?", (PAYLOADS[0], pid))
        conn.commit()
        from orkiestrator import publisher
        with tempfile.TemporaryDirectory() as tmp:
            published = publisher.run_due(conn, outbox=Path(tmp))
        self.assertEqual(published, [])  # claim_for_publish's hash check refuses the tampered row
        self.assertEqual(conn.execute("SELECT status FROM posts WHERE id=?", (pid,)).fetchone()["status"], "scheduled")


if __name__ == "__main__":
    unittest.main()
