import http.client
import json
import sys
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
from urllib.parse import unquote, urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from orkiestrator import adapters, attribution, brain, carousel, gate, hooks, interrupts, leads, notify, pipeline, web  # noqa: E402
from orkiestrator.db import connect  # noqa: E402

TOKEN = "t0ken"


def fake_llm(system, prompt):
    if "weekly research brief" in prompt:
        return "# Brief"
    if "draft up to" in prompt:
        return json.dumps([{"platform": "linkedin", "kind": "text_post", "hooks": [{"text": "H", "score": 8}], "body_text": "Clean."}])
    if "Draft replies" in prompt:
        return "[]"
    return "[]"


class Base(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.tmp = tmp
        for target, attr, value in [
            (pipeline, "BRIEFS", tmp / "briefs"), (carousel, "ROOT", tmp), (hooks, "ROOT", tmp), (brain, "ROOT", tmp),
        ]:
            p = mock.patch.object(target, attr, value)
            p.start()
            self.addCleanup(p.stop)
        (tmp / "knowledge" / "hooks").mkdir(parents=True)
        (tmp / "inputs").mkdir()
        self.db = tmp / "q.db"
        self.srv = web.make_server(0, llm=fake_llm, outbox=tmp / "out", token=TOKEN, db_path=self.db)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.shutdown)
        adapters.LIVE["confirmed"] = False
        self.addCleanup(lambda: adapters.LIVE.update(confirmed=False))

    def req(self, method, path, data=None, cookie=True):
        c = http.client.HTTPConnection("127.0.0.1", self.port)
        h = {"Cookie": f"orki={TOKEN}"} if cookie else {}
        body = urlencode(data) if data is not None else None
        if body:
            h["Content-Type"] = "application/x-www-form-urlencoded"
        c.request(method, path, body, h)
        r = c.getresponse()
        return r.status, r.read(), r.getheader("Location") or ""

    def published(self, link="pr-1"):
        conn = connect(self.db)
        pid = gate.add_draft(conn, "agent:x", "linkedin", "post", "text", hook="hk", link_id=link)
        gate.transition(conn, pid, "submit", "agent:x")
        gate.transition(conn, pid, "approve", "human:o")
        gate.transition(conn, pid, "publish", "agent:p")
        return pid


class TrackerAndAssets(Base):
    def test_click_route_is_public_counts_known_links_and_redirects(self):
        self.published("pr-1")
        goals = self.tmp / "goals.md"
        goals.write_text("offer_url: https://example.com/buy\n", encoding="utf-8")
        with mock.patch.dict(web.SETUP_FILES, {"goals": ("g", goals)}):
            code, _, loc = self.req("GET", "/r/pr-1", cookie=False)
            self.assertEqual((code, loc), (303, "https://example.com/buy"))
            self.assertEqual(self.req("GET", "/r/unknown", cookie=False)[0], 404)
        self.assertEqual(connect(self.db).execute("SELECT COUNT(*) FROM clicks").fetchone()[0], 1)

    def test_assets_need_auth_and_block_traversal(self):
        carousel.render(5, [{"title": "t", "text": "x"}], root=self.tmp / "data" / "assets")
        with mock.patch.object(carousel, "ROOT", self.tmp):
            self.assertEqual(self.req("GET", "/assets/5/slide-01.svg", cookie=False)[0], 401)
            code, body, _ = self.req("GET", "/assets/5/slide-01.svg")
            self.assertEqual(code, 200)
            self.assertIn(b"<svg", body)
            self.assertEqual(self.req("GET", "/assets/5/..%2F..%2Fq.db")[0], 404)
            self.assertEqual(self.req("GET", "/assets/5/secret.txt")[0], 404)


class AlertsLeadsLive(Base):
    def test_alerts_refresh_show_and_dismiss(self):
        leads.capture(connect(self.db), "instagram", "@m", "how much is it?")
        self.req("POST", "/generate/alerts", {})
        code, body, _ = self.req("GET", "/alerts")
        self.assertIn("Pytanie o zakup", body.decode())
        aid = connect(self.db).execute("SELECT id FROM alerts WHERE kind='buy_intent'").fetchone()[0]
        self.req("POST", f"/alert-done/{aid}", {})
        self.assertNotIn("Pytanie o zakup", self.req("GET", "/alerts")[1].decode())

    def test_lead_status_and_client_feed_the_funnel(self):
        pid = self.published("pr-1")
        leads.capture(connect(self.db), "linkedin", "@a", "how much?", post_id=pid)
        self.req("POST", "/lead/1/status", {"status": "replied"})
        self.assertEqual(connect(self.db).execute("SELECT status FROM leads").fetchone()[0], "replied")
        self.assertIn("err=1", self.req("POST", "/lead/1/client", {"amount": "abc"})[2])
        self.req("POST", "/lead/1/client", {"amount": "490,50"})
        row = attribution.funnel(connect(self.db))[0]
        self.assertEqual((row["customers"], row["revenue"]), (1, 490.5))
        self.assertIn("Klient", self.req("GET", "/leads")[1].decode())

    def test_live_mode_needs_exact_phrase_and_credentials(self):
        self.req("POST", "/live", {"confirm": "yes"})
        self.assertFalse(adapters.LIVE["confirmed"])
        with mock.patch.dict("os.environ", {"FB_PAGE_ID": "", "FB_PAGE_TOKEN": ""}):
            self.req("POST", "/live", {"confirm": "OPUBLIKUJ NAPRAWDE"})
            self.assertFalse(adapters.LIVE["confirmed"])
        with mock.patch.dict("os.environ", {"FB_PAGE_ID": "1", "FB_PAGE_TOKEN": "t"}):
            self.req("POST", "/live", {"confirm": "OPUBLIKUJ NAPRAWDE"})
            self.assertTrue(adapters.LIVE["confirmed"])
            self.assertIn("TRYB LIVE", self.req("GET", "/")[1].decode())
        self.req("POST", "/live-off", {})
        self.assertFalse(adapters.LIVE["confirmed"])

    def test_live_facebook_publish_goes_through_gate_only(self):
        sent = []
        conn = connect(self.db)
        pid = gate.add_draft(conn, "agent:x", "facebook", "text_post", "hello", hook="h")
        gate.transition(conn, pid, "submit", "agent:x")
        adapters.LIVE["confirmed"] = True
        with mock.patch.dict("os.environ", {"FB_PAGE_ID": "1", "FB_PAGE_TOKEN": "t"}), \
                mock.patch.object(adapters.FacebookPublisher, "publish", lambda self, post: sent.append(post["id"]) or "facebook:1"):
            from orkiestrator import publisher
            conn.execute("UPDATE posts SET status='approved', scheduled_at=? WHERE id=?", ("2000-01-01T00:00:00+00:00", pid))
            conn.commit()
            self.assertEqual(publisher.run_due(conn), [])  # forged approval: nothing sent
            self.assertEqual(sent, [])
            conn.execute("UPDATE posts SET status='pending_approval' WHERE id=?", (pid,))
            conn.commit()
            gate.transition(conn, pid, "approve", "human:o")
            gate.transition(conn, pid, "schedule", "agent:s", scheduled_at="2000-01-01T00:00:00+00:00")
            self.assertEqual(len(publisher.run_due(conn)), 1)
            self.assertEqual(sent, [pid])


class BrainAndNotify(Base):
    def test_morning_loop_runs_stages_writes_report_and_publishes_nothing(self):
        conn = connect(self.db)
        (self.tmp / "inputs" / "x.csv").write_text("a,b\n1,2\n", encoding="utf-8")
        with mock.patch.object(pipeline, "INPUTS", self.tmp / "inputs"), mock.patch.object(pipeline, "KNOW", self.tmp / "knowledge"):
            res = brain.morning_loop(conn, fake_llm, report_dir=self.tmp / "reports")
        self.assertTrue(res["report"].exists())
        self.assertIn("Czeka na Twoje zatwierdzenie: 1", res["summary"])
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM posts WHERE status IN ('approved','scheduled','published')").fetchone()[0], 0)
        self.assertEqual([r[0] for r in conn.execute("SELECT status FROM posts")], ["pending_approval"])

    def test_one_failing_stage_does_not_stop_the_rest(self):
        conn = connect(self.db)

        def llm(system, prompt):
            raise RuntimeError("api down")

        with mock.patch.object(pipeline, "INPUTS", self.tmp / "inputs"):
            res = brain.morning_loop(conn, llm, report_dir=self.tmp / "reports")
        self.assertEqual(len(res["failures"]), 3)
        self.assertTrue(res["report"].exists())

    def test_alerts_with_buy_intent_run_replies_first(self):
        conn = connect(self.db)
        leads.capture(conn, "instagram", "@m", "how much?")
        order = []
        with mock.patch.object(pipeline, "run_research", lambda llm: order.append("research") or Path("b.md")), \
                mock.patch.object(pipeline, "run_content", lambda c, llm: order.append("content") or ([], [])), \
                mock.patch.object(pipeline, "run_replies", lambda c, llm: order.append("replies") or ([], [], [])):
            brain.morning_loop(conn, fake_llm, report_dir=self.tmp / "reports")
        self.assertEqual(order, ["replies", "research", "content"])

    def test_due_now_once_per_day_after_configured_time(self):
        goals = self.tmp / "g.md"
        goals.write_text("morning: 08:30\n", encoding="utf-8")
        self.assertEqual(brain.morning_time(goals), (8, 30))
        with mock.patch.object(brain, "morning_time", lambda: (8, 30)):
            self.assertFalse(brain.due_now(datetime(2026, 10, 19, 8, 29)))
            self.assertTrue(brain.due_now(datetime(2026, 10, 19, 8, 30)))
            self.assertFalse(brain.due_now(datetime(2026, 10, 19, 9, 0), last_run_day=datetime(2026, 10, 19).date()))

    def test_daily_loop_tick_runs_once(self):
        calls = []
        with mock.patch.object(brain, "morning_loop", lambda conn, llm, panel_url="": calls.append(1) or {"summary": "ok"}), \
                mock.patch.object(brain, "morning_time", lambda: (8, 0)):
            stop = web.start_daily_loop("http://x/", interval=3600, llm=fake_llm, db_path=self.db, clock=lambda: datetime(2026, 10, 19, 9, 0))
            stop.tick()
            stop.tick()
            stop.set()
        self.assertEqual(len(calls), 1)

    def test_notify_noop_without_config_and_never_leaks_actions(self):
        with mock.patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "", "SMTP_HOST": "", "CHANNEL_ORKIESTRATOR": ""}):
            self.assertIsNone(notify.send("x"))
        sent = []
        env = {"TELEGRAM_BOT_TOKEN": "bot", "TELEGRAM_CHAT_ID": "42", "CHANNEL_ORKIESTRATOR": ""}
        with mock.patch.dict("os.environ", env):
            self.assertEqual(notify.send("3 posty czekają", "http://127.0.0.1:8765/", http=lambda u, d: sent.append((u, d))), "telegram")
        url, data = sent[0]
        self.assertIn("/botbot/sendMessage", url)
        text = unquote(data.decode().replace("+", " "))
        self.assertIn("Panel: http://127.0.0.1:8765/", text)
        self.assertNotIn("token=", text)
        self.assertNotIn("approve", text.lower())

    def test_notify_discord_wins_and_never_leaks_token(self):
        sent = []
        env = {"DISCORD_BOT_TOKEN": "dbot", "CHANNEL_ORKIESTRATOR": "123", "TELEGRAM_BOT_TOKEN": "bot", "TELEGRAM_CHAT_ID": "42"}
        with mock.patch.dict("os.environ", env):
            self.assertEqual(notify.send("2 posty czekają", "http://panel.example.lan/", http=lambda u, d: sent.append((u, d))), "discord")
        url, data = sent[0]
        self.assertIn("/channels/123/messages", url)
        text = json.loads(data.decode())["content"]
        self.assertIn("Panel: http://panel.example.lan/", text)
        self.assertNotIn("token=", text)

    def test_notify_failure_is_swallowed(self):
        def boom(u, d):
            raise OSError("network")

        with mock.patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "b", "TELEGRAM_CHAT_ID": "1"}):
            self.assertIsNone(notify.send("x", http=boom))


class SmtpTests(unittest.TestCase):
    def test_smtp_path(self):
        class FakeSMTP:
            sent = []

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def send_message(self, msg):
                FakeSMTP.sent.append(msg)

        with mock.patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": "", "SMTP_HOST": "h", "SMTP_TO": "me@example.com"}):
            self.assertEqual(notify.send("hello", "http://p/", smtp_factory=FakeSMTP), "smtp")
        self.assertEqual(FakeSMTP.sent[0]["To"], "me@example.com")
        self.assertIn("hello", FakeSMTP.sent[0].get_content())


if __name__ == "__main__":
    unittest.main()
