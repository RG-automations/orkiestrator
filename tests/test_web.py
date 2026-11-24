import http.client
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from orkiestrator import gate, pipeline, web  # noqa: E402
from orkiestrator.db import connect  # noqa: E402

TOKEN = "t0ken"


def fake_llm(system, prompt):
    if "weekly research brief" in prompt:
        return "# Brief\n- idea"
    if "competitor posts below" in prompt:
        return "[]"
    if "draft up to" in prompt:
        return json.dumps([
            {"platform": "linkedin", "kind": "text_post", "hooks": [{"text": "Good hook", "score": 9}, {"text": "Meh", "score": 3}],
             "body_text": "Clean body text.", "caption": "cap"},
            {"platform": "tiktok", "kind": "video", "hooks": [{"text": "Bad", "score": 5}],
             "script": [{"t": "0-3s", "say": "Let's delve in — unlock it", "on_screen": "x"}]},
        ])
    if "Check the post against" in prompt:
        return json.dumps(["tone is too salesy"]) if "Clean body" in prompt else "[]"
    return "[]"


class WebTests(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.db = tmp / "q.db"
        self.outbox = tmp / "outbox"
        p = mock.patch.object(pipeline, "BRIEFS", tmp / "briefs")
        p.start()
        self.addCleanup(p.stop)
        self.srv = web.make_server(0, llm=fake_llm, outbox=self.outbox, token=TOKEN, db_path=self.db)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.shutdown)

    def req(self, method, path, data=None, cookie=True):
        c = http.client.HTTPConnection("127.0.0.1", self.port)
        h = {"Cookie": f"orki={TOKEN}"} if cookie else {}
        body = urlencode(data) if data is not None else None
        if body:
            h["Content-Type"] = "application/x-www-form-urlencoded"
        c.request(method, path, body, h)
        r = c.getresponse()
        return r.status, r.read().decode(), r.getheader("Location")

    def test_healthz_is_public_and_empty(self):
        status, body, _ = self.req("GET", "/healthz", cookie=False)
        self.assertEqual((status, body), (200, "ok"))
        status, _, _ = self.req("GET", "/", cookie=False)
        self.assertEqual(status, 401)

    def draft(self):
        conn = connect(self.db)
        pid = gate.add_draft(conn, "agent:x", "linkedin", "post", "text", hook="h")
        gate.transition(conn, pid, "submit", "agent:x")
        return pid

    def status(self, pid):
        return connect(self.db).execute("SELECT status FROM posts WHERE id=?", (pid,)).fetchone()[0]

    def test_no_cookie_no_access_and_no_approval(self):
        pid = self.draft()
        self.assertEqual(self.req("GET", "/queue", cookie=False)[0], 401)
        self.assertEqual(self.req("POST", f"/approve/{pid}", {}, cookie=False)[0], 401)
        self.assertEqual(self.status(pid), "pending_approval")

    def test_token_link_sets_session(self):
        code, _, loc = self.req("GET", f"/?token={TOKEN}", cookie=False)
        self.assertEqual((code, loc), (303, "/"))
        self.assertEqual(self.req("GET", "/?token=wrong", cookie=False)[0], 401)

    def test_pages_render(self):
        for path in ("/", "/queue", "/setup", "/inputs", "/leads", "/report"):
            code, body, _ = self.req("GET", path)
            self.assertEqual(code, 200, path)

    def test_approve_schedules_future_slot_and_does_not_publish_yet(self):
        pid = self.draft()
        self.assertEqual(self.req("POST", f"/approve/{pid}", {})[0], 303)
        row = connect(self.db).execute("SELECT status, scheduled_at, approved_by FROM posts WHERE id=?", (pid,)).fetchone()
        self.assertEqual(row["status"], "scheduled")
        self.assertGreater(row["scheduled_at"], gate.now())
        self.assertTrue(row["approved_by"].startswith("human:"))
        self.assertFalse(self.outbox.exists())

    def test_publish_now_publishes_dry_run(self):
        pid = self.draft()
        self.assertEqual(self.req("POST", f"/publish-now/{pid}", {})[0], 303)
        self.assertEqual(self.status(pid), "published")
        self.assertTrue((self.outbox / f"linkedin-{pid}.json").exists())

    def test_replies_publish_immediately_on_approve(self):
        conn = connect(self.db)
        pid = gate.add_draft(conn, "agent:x", "instagram", "reply", "thanks!", hook="reply")
        gate.transition(conn, pid, "submit", "agent:x")
        self.req("POST", f"/approve/{pid}", {})
        self.assertEqual(self.status(pid), "published")

    def test_reject_and_edit(self):
        a, b = self.draft(), self.draft()
        self.req("POST", f"/reject/{a}", {"note": "nie"})
        self.assertEqual(self.status(a), "rejected")
        self.req("POST", f"/edit/{b}", {"body": "new text"})
        self.assertEqual(self.status(b), "pending_approval")

    def test_edit_with_bad_style_is_not_resubmitted(self):
        pid = self.draft()
        self.req("POST", f"/edit/{pid}", {"body": "unlock — growth"})
        self.assertEqual(self.status(pid), "draft")

    def test_generate_content_queues_only_lint_clean_drafts(self):
        self.req("POST", "/generate/research", {})
        self.assertIsNotNone(pipeline.latest_brief())
        code, _, loc = self.req("POST", "/generate/content", {})
        self.assertEqual(code, 303)
        rows = connect(self.db).execute("SELECT platform, status FROM posts ORDER BY id").fetchall()
        self.assertEqual([tuple(r) for r in rows], [("linkedin", "pending_approval"), ("tiktok", "draft")])
        first = connect(self.db).execute("SELECT hook, body, link_id, warnings, payload FROM posts WHERE id=1").fetchone()
        self.assertEqual(first["hook"], "Good hook")
        self.assertTrue(first["body"].startswith("Good hook"))
        self.assertEqual(first["link_id"], "pr-1")
        self.assertEqual(first["warnings"], "tone is too salesy")
        self.assertIn("hooks", first["payload"])

    def test_generate_without_key_shows_message(self):
        srv = web.make_server(0, llm=None, token=TOKEN, db_path=self.db)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        with mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "", "GEMINI_API_KEY": "", "GOOGLE_API_KEY": "", "ORKI_PROVIDER": ""}):
            c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1])
            c.request("POST", "/generate/research", "", {"Cookie": f"orki={TOKEN}"})
            r = c.getresponse()
            self.assertEqual(r.status, 303)
            self.assertIn("GEMINI_API_KEY", __import__("urllib.parse").parse.unquote(r.getheader("Location")))


class EnvConfigTests(unittest.TestCase):
    def test_token_host_and_public_url_from_env(self):
        env = {"ORKI_TOKEN": "z-env", "ORKI_HOST": "127.0.0.1", "ORKI_PUBLIC_URL": "http://panel.example.lan/"}
        with mock.patch.dict("os.environ", env):
            srv = web.make_server(0, db_path=Path(tempfile.mkdtemp()) / "q.db")
            self.addCleanup(srv.server_close)
            self.assertEqual(srv.token, "z-env")
            self.assertEqual(srv.server_address[0], "127.0.0.1")
            self.assertEqual(web.public_url(8765), "http://panel.example.lan/")
        with mock.patch.dict("os.environ", {"ORKI_TOKEN": "", "ORKI_PUBLIC_URL": ""}):
            self.assertEqual(web.public_url(8765), "http://127.0.0.1:8765/")


if __name__ == "__main__":
    unittest.main()
