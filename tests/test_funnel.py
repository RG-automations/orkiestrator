import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from orkiestrator import attribution, gate, hooks, interrupts, leads  # noqa: E402
from orkiestrator.db import connect  # noqa: E402


def published(conn, link, hook="h", platform="linkedin"):
    pid = gate.add_draft(conn, "agent:x", platform, "post", "text", hook=hook, link_id=link)
    gate.transition(conn, pid, "submit", "agent:x")
    gate.transition(conn, pid, "approve", "human:o")
    gate.transition(conn, pid, "publish", "agent:p")
    return pid


class FunnelTests(unittest.TestCase):
    def setUp(self):
        self.conn = connect(":memory:")

    def test_click_counts_only_known_links(self):
        pid = published(self.conn, "pr-1")
        self.assertTrue(attribution.record_click(self.conn, "pr-1"))
        self.assertFalse(attribution.record_click(self.conn, "nope"))
        self.assertFalse(attribution.record_click(self.conn, ""))
        row = attribution.funnel(self.conn)[0]
        self.assertEqual((row["id"], row["clicks"]), (pid, 1))

    def test_lead_to_client_flows_into_funnel_without_double_counting(self):
        pid = published(self.conn, "pr-1")
        for _ in range(4):
            attribution.record_click(self.conn, "pr-1")
        leads.capture(self.conn, "linkedin", "@a", "how much?", post_id=pid)
        lead_id = self.conn.execute("SELECT id FROM leads").fetchone()[0]
        leads.set_status(self.conn, lead_id, "replied")
        leads.mark_client(self.conn, lead_id, 490)
        row = attribution.funnel(self.conn)[0]
        self.assertEqual((row["clicks"], row["customers"], row["revenue"]), (4, 1, 490.0))
        self.assertEqual(row["click_to_customer"], 0.25)
        self.assertEqual(self.conn.execute("SELECT status FROM leads").fetchone()[0], "client")

    def test_bad_status_rejected(self):
        with self.assertRaises(ValueError):
            leads.set_status(self.conn, 1, "whatever")

    def test_collab_and_negative_are_not_leads(self):
        leads.capture(self.conn, "instagram", "@brand", "we would love a collab with you")
        leads.capture(self.conn, "tiktok", "@t", "this is a scam")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0], 0)
        self.assertEqual([r[0] for r in self.conn.execute("SELECT intent FROM inbound ORDER BY id")], ["collab", "negative"])

    def test_hooks_repeat_and_retire_and_file_is_idempotent(self):
        winner = published(self.conn, "pr-1", hook="Win")
        loser = published(self.conn, "pr-2", hook="Lose")
        for _ in range(25):
            attribution.record_click(self.conn, "pr-2")
        for _ in range(3):
            attribution.record_click(self.conn, "pr-1")
        leads.capture(self.conn, "linkedin", "@a", "how much?", post_id=winner)
        leads.mark_client(self.conn, 1, 490)
        repeat, retire = hooks.score(self.conn)
        self.assertEqual([r["id"] for r in repeat], [winner])
        self.assertEqual([r["id"] for r in retire], [loser])
        p = Path(tempfile.mkdtemp()) / "w.md"
        hooks.write_winning_patterns(self.conn, p)
        hooks.write_winning_patterns(self.conn, p)
        self.assertEqual(p.read_text(encoding="utf-8").count("generated:start"), 1)

    def test_ab_needs_data_then_picks_winner(self):
        a, b = published(self.conn, "pr-a"), published(self.conn, "pr-b")
        exp = hooks.create_experiment(self.conn, "hook test", a, b)
        self.assertEqual(hooks.compare(self.conn, exp)[0], "not enough data")
        for _ in range(25):
            attribution.record_click(self.conn, "pr-a")
            attribution.record_click(self.conn, "pr-b")
        leads.capture(self.conn, "linkedin", "@x", "how much?", post_id=b)
        leads.mark_client(self.conn, 1, 100)
        self.assertEqual(hooks.compare(self.conn, exp)[0], "b")


class InterruptTests(unittest.TestCase):
    def setUp(self):
        self.conn = connect(":memory:")
        self.dir = Path(tempfile.mkdtemp())
        (self.dir / "trends.csv").write_text("source,topic,week_growth_pct,note\ntiktok,ghosted,180,x\nyt,calm,20,x\n", encoding="utf-8")
        (self.dir / "competitors.csv").write_text(
            "account,platform,post_id,views,account_avg_views,first_3_seconds,format\n@a,tiktok,c1,412000,18000,hookA,reel\n@b,tiktok,c2,15000,18000,hookB,reel\n",
            encoding="utf-8")

    def kinds(self, alerts):
        return sorted(a.kind for a in alerts)

    def test_trend_and_competitor_rules(self):
        alerts = interrupts.evaluate(self.conn, self.dir, interrupts.thresholds())
        self.assertEqual(self.kinds(alerts), ["competitor_win", "trend_spike"])

    def test_inbound_rules_and_priority_order(self):
        leads.capture(self.conn, "tiktok", "@t", "this is a scam")
        leads.capture(self.conn, "instagram", "@m", "how much is it?")
        leads.capture(self.conn, "instagram", "@b", "partnership offer")
        alerts = interrupts.evaluate(self.conn, self.dir)
        self.assertEqual(alerts[0].kind, "buy_intent")
        self.assertIn("negative_comment", self.kinds(alerts))
        self.assertIn("collab", self.kinds(alerts))

    def test_night_buy_intent_is_flagged(self):
        leads.capture(self.conn, "instagram", "@n", "how much is it?")
        self.conn.execute("UPDATE inbound SET created_at='2026-10-22T22:30:00+00:00'")
        night = [a for a in interrupts.evaluate(self.conn, self.dir) if a.kind == "buy_intent"][0]
        self.assertIn("nocne", night.text)
        self.conn.execute("UPDATE inbound SET created_at='2026-10-22T10:30:00+00:00'")
        day = [a for a in interrupts.evaluate(self.conn, self.dir) if a.kind == "buy_intent"][0]
        self.assertNotIn("nocne", day.text)

    def test_first_hour_flop_and_click_spike(self):
        pid = published(self.conn, "pr-1")
        self.conn.execute("INSERT INTO hourly (post_id, views, ts) VALUES (?,?,?)", (pid, 12, gate.now()))
        for _ in range(12):
            attribution.record_click(self.conn, "pr-1")
        alerts = interrupts.evaluate(self.conn, self.dir)
        self.assertIn("first_hour_flop", self.kinds(alerts))
        self.assertIn("click_spike", self.kinds(alerts))

    def test_store_deduplicates(self):
        alerts = interrupts.evaluate(self.conn, self.dir)
        self.assertEqual(len(interrupts.store(self.conn, alerts)), 2)
        self.assertEqual(interrupts.store(self.conn, alerts), [])
        interrupts.dismiss(self.conn, interrupts.open_alerts(self.conn)[0]["id"])
        self.assertEqual(len(interrupts.open_alerts(self.conn)), 1)

    def test_messy_csv_cells_do_not_crash_and_unknown_is_skipped(self):
        (self.dir / "trends.csv").write_text("source,topic,week_growth_pct,note\nx,a,brak danych,n\nx,b,~180%,n\n", encoding="utf-8")
        (self.dir / "competitors.csv").write_text(
            "account,platform,post_id,views,account_avg_views,first_3_seconds,format\n"
            "@a,yt,l1,99000 (480 likes; 46 comments),~15000,h,v\n"
            "@b,yt,l2,brak danych,~45000,h,v\n"
            "@c,yt,l3,50000,0,h,v\n", encoding="utf-8")
        alerts = interrupts.evaluate(self.conn, self.dir, interrupts.thresholds())
        self.assertEqual(self.kinds(alerts), ["competitor_win", "trend_spike"])
        self.assertEqual(interrupts._num("1,953,235 views"), 1953235.0)
        self.assertIsNone(interrupts._num("brak danych"))

    def test_threshold_parsing(self):
        p = self.dir / "k.md"
        p.write_text("thresholds: trend_growth_pct=50, competitor_multiple=2\n", encoding="utf-8")
        th = interrupts.thresholds(p)
        self.assertEqual((th["trend_growth_pct"], th["competitor_multiple"], th["click_spike_multiple"]), (50.0, 2.0, 3.0))


if __name__ == "__main__":
    unittest.main()
