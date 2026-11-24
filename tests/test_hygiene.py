import http.client
import io
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path
from unittest import mock
from urllib.parse import unquote, urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from orkiestrator import ai, cleanup, gate, interrupts, leads, pipeline, web  # noqa: E402
from orkiestrator.db import connect  # noqa: E402

AGENT, OWNER = "agent:content-studio", "human:owner"
PL = "Właściciel małej agencji traci godziny na raporty, których klient nie czyta."


def post(conn, hook="Hak", body="Treść", status="pending_approval", kind="text_post", note="zły ton", **kw):
    pid = gate.add_draft(conn, AGENT, "linkedin", kind, body, hook=hook, **kw)
    gate.transition(conn, pid, "submit", AGENT)
    if status == "rejected":
        gate.transition(conn, pid, "reject", OWNER, note=note)
    elif status == "published":
        gate.transition(conn, pid, "approve", OWNER)
        gate.transition(conn, pid, "publish", "agent:p")
    return pid


def item(hook, **extra):
    d = {"platform": "linkedin", "kind": "text_post", "hooks": [{"text": hook, "score": 8}], "body_text": PL}
    d.update(extra)
    return d


class Llm:
    """Test double: answers generation, rewrite and brand-check prompts; records every prompt."""

    def __init__(self, items=None, rewrite=None):
        self.items, self.rewrite, self.prompts = items or [], rewrite, []

    def __call__(self, system, prompt):
        self.prompts.append(prompt)
        if "draft up to" in prompt:
            return json.dumps(self.items)
        if "Rewrite this JSON object" in prompt:
            return json.dumps(self.rewrite) if self.rewrite is not None else "nie json"
        return "[]"


class Env(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "briefs").mkdir()
        (self.tmp / "briefs" / "b.md").write_text("# Brief", encoding="utf-8")
        for target, attr, value in [(pipeline, "BRIEFS", self.tmp / "briefs")]:
            p = mock.patch.object(target, attr, value)
            p.start()
            self.addCleanup(p.stop)
        for name, value in (("language", lambda: "Polish"), ("source_corpus", lambda: "Brief 40% i https://znane.pl oraz 2 500 zł netto"),
                            ("source_files", dict)):
            p = mock.patch.object(pipeline, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.conn = connect(":memory:")

    def statuses(self):
        return [tuple(r) for r in self.conn.execute("SELECT status, hook FROM posts ORDER BY id")]


class MigrationTests(unittest.TestCase):
    def old_db(self, with_row):
        path = Path(tempfile.mkdtemp()) / "data" / "queue.db"
        path.parent.mkdir(parents=True)
        c = sqlite3.connect(str(path))
        c.execute("CREATE TABLE posts (id INTEGER PRIMARY KEY AUTOINCREMENT, platform TEXT, kind TEXT, title TEXT, hook TEXT, body TEXT,"
                  " brief_ref TEXT, link_id TEXT, status TEXT, scheduled_at TEXT, created_by TEXT, approved_by TEXT, approved_at TEXT,"
                  " published_at TEXT, note TEXT, created_at TEXT, payload TEXT NOT NULL DEFAULT '', warnings TEXT NOT NULL DEFAULT '')")
        if with_row:
            c.execute("INSERT INTO posts (platform, kind, hook, body, status) VALUES ('linkedin','post','stary hak','tekst','pending_approval')")
        c.commit()
        c.close()
        return path

    def test_existing_database_is_backed_up_before_columns_are_added(self):
        path = self.old_db(with_row=True)
        conn = connect(path)
        backups = list((path.parent / "backups").glob("queue-before-migration-*.db"))
        self.assertEqual(len(backups), 1)
        old_cols = {r[1] for r in sqlite3.connect(str(backups[0])).execute("PRAGMA table_info(posts)")}
        self.assertNotIn("archived_at", old_cols)  # the backup is the pre-migration state
        self.assertEqual(sqlite3.connect(str(backups[0])).execute("SELECT hook FROM posts").fetchone()[0], "stary hak")
        for table in ("posts", "inbound", "leads"):
            cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            self.assertTrue({"archived_at", "is_test"} <= cols, table)
        self.assertEqual(conn.execute("SELECT hook, is_test, archived_at FROM posts").fetchone()[:], ("stary hak", 0, None))
        connect(path)  # second connect: nothing pending, no new backup
        self.assertEqual(len(list((path.parent / "backups").glob("*.db"))), 1)

    def test_new_empty_database_makes_no_backup(self):
        path = Path(tempfile.mkdtemp()) / "data" / "queue.db"
        connect(path)
        self.assertFalse((path.parent / "backups").exists())

    def test_old_database_without_rows_is_not_backed_up(self):
        path = self.old_db(with_row=False)
        connect(path)
        self.assertFalse((path.parent / "backups").exists())


class ArchiveTests(Env):
    def test_only_finished_posts_can_be_archived_and_it_is_logged_and_reversible(self):
        pending, rejected = post(self.conn), post(self.conn, hook="B", status="rejected")
        with self.assertRaises(gate.GateError):
            gate.archive(self.conn, pending, OWNER)
        gate.archive(self.conn, rejected, OWNER)
        row = self.conn.execute("SELECT status, archived_at FROM posts WHERE id=?", (rejected,)).fetchone()
        self.assertEqual((row["status"], bool(row["archived_at"])), ("rejected", True))
        actions = [r[0] for r in self.conn.execute("SELECT action FROM events WHERE post_id=? ORDER BY id", (rejected,))]
        self.assertEqual(actions[-1], "archive")
        gate.unarchive(self.conn, rejected, OWNER)
        self.assertIsNone(self.conn.execute("SELECT archived_at FROM posts WHERE id=?", (rejected,)).fetchone()[0])

    def test_resubmitting_an_archived_rejected_post_brings_it_back(self):
        pid = post(self.conn, status="rejected")
        gate.archive(self.conn, pid, OWNER)
        gate.transition(self.conn, pid, "submit", OWNER)
        row = self.conn.execute("SELECT status, archived_at FROM posts WHERE id=?", (pid,)).fetchone()
        self.assertEqual((row["status"], row["archived_at"]), ("pending_approval", None))

    def test_archived_inbound_stops_alerting_and_repeats_are_not_stored_twice(self):
        leads.capture(self.conn, "tiktok", "@t", "this is a scam")
        leads.capture(self.conn, "tiktok", "@t", "this is a scam")  # same CSV pasted again
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM inbound").fetchone()[0], 1)
        d = Path(tempfile.mkdtemp())
        self.assertEqual([a.kind for a in interrupts.evaluate(self.conn, d)], ["negative_comment"])
        leads.archive_inbound(self.conn, 1)
        self.assertEqual(interrupts.evaluate(self.conn, d), [])


class GenerationTests(Env):
    def test_blocked_without_calling_the_api_when_three_already_wait(self):
        for i in range(3):
            post(self.conn, hook=f"H{i}")
        llm = Llm([item("Nowy")])
        with self.assertRaises(pipeline.QueueFull) as cm:
            pipeline.run_content(self.conn, llm)
        self.assertIn("3", str(cm.exception))
        self.assertEqual(llm.prompts, [])

    def test_replies_do_not_count_as_waiting_posts(self):
        for i in range(5):
            post(self.conn, hook=f"r{i}", kind="reply")
        created, _ = pipeline.run_content(self.conn, Llm([item("Nowy hak")]))
        self.assertEqual(len(created), 1)

    def test_replies_skip_source_types_that_are_not_a_platform(self):
        d = Path(tempfile.mkdtemp())
        (d / "comments.csv").write_text(
            "platform,post_ref,handle,comment\nblog (AgencyAnalytics),u,@a,Nice read\ninstagram,u,@b,Great post\n", encoding="utf-8")
        reply = json.dumps([{"handle": "@a", "platform": "blog", "kind": "reply", "body": "x"},
                            {"handle": "@b", "platform": "instagram", "kind": "reply", "body": "Dzięki!"}])
        with mock.patch.object(pipeline, "INPUTS", d):
            created, _, failed = pipeline.run_replies(self.conn, lambda s, p: reply)
        self.assertEqual(len(created), 1)
        self.assertEqual(failed, [("@a", "zła platforma")])
        self.assertEqual([r[0] for r in self.conn.execute("SELECT platform FROM posts")], ["instagram"])

    def test_never_more_than_three_and_only_the_free_room_is_filled(self):
        post(self.conn, hook="Czeka 1")
        post(self.conn, hook="Czeka 2")
        llm = Llm([item("Jeden"), item("Dwa"), item("Trzy"), item("Cztery")])
        created, _ = pipeline.run_content(self.conn, llm)
        self.assertEqual(len(created), 1)
        self.assertIn("draft up to 1 pieces", llm.prompts[0])
        fresh = connect(":memory:")
        created, _ = pipeline.run_content(fresh, Llm([item(f"H{i}") for i in range(7)]))
        self.assertEqual(len(created), 3)

    def test_repeating_the_run_does_not_duplicate_an_idea(self):
        llm = Llm([item("Ten sam hak")])
        first, _ = pipeline.run_content(self.conn, llm)
        gate.transition(self.conn, first[0], "reject", OWNER, note="zbyt ogólne")
        again, failed = pipeline.run_content(self.conn, llm)
        self.assertEqual(again, [])
        self.assertIn("duplikat", failed[0][1])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0], 1)

    def test_second_click_while_a_run_is_active_is_refused(self):
        self.assertTrue(pipeline._GENERATION_LOCK.acquire(blocking=False))
        try:
            with self.assertRaises(ValueError) as cm:
                pipeline.run_content(self.conn, Llm([item("X")]))
            self.assertIn("trwa", str(cm.exception))
        finally:
            pipeline._GENERATION_LOCK.release()
        self.assertEqual(len(pipeline.run_content(self.conn, Llm([item("X")]))[0]), 1)

    def test_api_failure_leaves_no_partial_batch_and_releases_the_lock(self):
        def boom(system, prompt):
            raise RuntimeError("429")

        with self.assertRaises(RuntimeError):
            pipeline.run_content(self.conn, boom)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0], 0)
        self.assertEqual(len(pipeline.run_content(self.conn, Llm([item("Po błędzie")]))[0]), 1)


class RetryTests(unittest.TestCase):
    def http_error(self, code):
        return urllib.error.HTTPError("http://x", code, "err", {}, io.BytesIO(b"limit"))

    def test_429_is_retried_with_growing_waits_and_then_succeeds(self):
        calls, sleeps = [], []

        class Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=0):
            calls.append(1)
            if len(calls) < 3:
                raise self.http_error(429)
            return Resp(b'{"ok": true}')

        with mock.patch.object(ai.urllib.request, "urlopen", fake_urlopen):
            self.assertEqual(ai._post("http://x", {}, {}, sleep=sleeps.append), {"ok": True})
        self.assertEqual((len(calls), sleeps), (3, [8, 20]))

    def test_gives_up_after_the_last_wait_with_a_helpful_message_and_does_not_retry_other_errors(self):
        sleeps = []
        with mock.patch.object(ai.urllib.request, "urlopen", lambda req, timeout=0: (_ for _ in ()).throw(self.http_error(429))):
            with self.assertRaises(RuntimeError) as cm:
                ai._post("http://x", {}, {}, sleep=sleeps.append)
        self.assertEqual(sleeps, [8, 20, 45])
        self.assertIn("Limit zapytań", str(cm.exception))
        sleeps.clear()
        with mock.patch.object(ai.urllib.request, "urlopen", lambda req, timeout=0: (_ for _ in ()).throw(self.http_error(400))):
            with self.assertRaises(RuntimeError):
                ai._post("http://x", {}, {}, sleep=sleeps.append)
        self.assertEqual(sleeps, [])


class RejectionFeedbackTests(Env):
    def test_last_five_reasons_reach_the_next_prompt_newest_first(self):
        for i in range(7):
            post(self.conn, hook=f"Odrzucony {i}", status="rejected", note=f"powód {i}")
        post(self.conn, hook="Bez powodu", status="rejected", note="")
        llm = Llm([item("Nowy")])
        pipeline.run_content(self.conn, llm)
        prompt = llm.prompts[0]
        self.assertIn("Do not repeat these mistakes", prompt)
        for i in (6, 5, 4, 3, 2):
            self.assertIn(f"powód {i}", prompt)
        for i in (0, 1):
            self.assertNotIn(f"powód {i}", prompt)
        self.assertLess(prompt.index("powód 6"), prompt.index("powód 2"))
        self.assertNotIn("Bez powodu", prompt)

    def test_no_rejections_means_no_feedback_section(self):
        llm = Llm([item("Nowy")])
        pipeline.run_content(self.conn, llm)
        self.assertNotIn("Do not repeat these mistakes", llm.prompts[0])


class LanguageTests(Env):
    EN = "Your clients never read the long reports that you send them every month and this is the real problem."

    def test_detection_flags_only_clearly_english_text(self):
        self.assertTrue(pipeline.clearly_english(self.EN))
        self.assertFalse(pipeline.clearly_english(PL))
        self.assertFalse(pipeline.clearly_english("Ziarnko"))  # too short to judge
        self.assertFalse(pipeline.clearly_english("AI dla agencji: n8n, Make i Zapier w jednym procesie"))

    def test_english_draft_is_rewritten_once_and_saved_in_polish(self):
        llm = Llm([item("Why your reports are ignored", body_text=self.EN)], rewrite=item("Dlaczego raporty są ignorowane"))
        created, failed = pipeline.run_content(self.conn, llm)
        self.assertEqual((len(created), failed), (1, []))
        self.assertEqual(self.statuses(), [("pending_approval", "Dlaczego raporty są ignorowane")])
        self.assertEqual(sum("Rewrite this JSON object" in p for p in llm.prompts), 1)

    def test_still_english_after_the_single_retry_is_never_saved(self):
        llm = Llm([item("Why your reports are ignored", body_text=self.EN)], rewrite=item("Still the same", body_text=self.EN))
        created, failed = pipeline.run_content(self.conn, llm)
        self.assertEqual(created, [])
        self.assertIn("nie jest po polsku", failed[0][1])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0], 0)
        self.assertEqual(sum("Rewrite this JSON object" in p for p in llm.prompts), 1)  # exactly one retry

    def test_garbage_from_the_retry_drops_the_draft(self):
        llm = Llm([item("Why", body_text=self.EN)], rewrite=None)
        self.assertEqual(pipeline.run_content(self.conn, llm)[0], [])

    def test_check_is_skipped_for_other_target_languages(self):
        with mock.patch.object(pipeline, "language", lambda: "English"):
            created, _ = pipeline.run_content(self.conn, Llm([item("Why", body_text=self.EN)]))
        self.assertEqual(len(created), 1)


class SourceTests(Env):
    def run_one(self, **extra):
        return pipeline.run_content(self.conn, Llm([item("Hak", **extra)]))

    def test_unknown_link_is_blocked(self):
        created, failed = self.run_one(body_text=PL + " Zobacz https://wymyslony.pl/raport")
        self.assertEqual(created, [])
        self.assertIn("link spoza danych", failed[0][1])

    def test_known_link_passes(self):
        self.assertEqual(len(self.run_one(body_text=PL + " Zobacz https://znane.pl")[0]), 1)

    def test_invented_percentage_or_amount_is_blocked_but_known_ones_pass(self):
        created, failed = self.run_one(body_text=PL + " Oszczędzasz 73% czasu.")
        self.assertEqual(created, [])
        self.assertIn("73%", failed[0][1])
        created, failed = self.run_one(body_text=PL + " Wdrożenie kosztuje 9 999 zł.")
        self.assertIn("9999zł", failed[0][1].replace(" ", ""))
        ok, _ = pipeline.run_content(self.conn, Llm([item("Inny hak", body_text=PL + " To 40% i 2 500 zł netto.")]))
        self.assertEqual(len(ok), 1)

    def test_fact_with_unverifiable_source_saves_the_draft_with_a_warning(self):
        created, _ = self.run_one(facts=[{"claim": "Klienci nie czytają raportów", "source": "ankieta-xyz.pdf"}])
        self.assertEqual(len(created), 1)
        warning = self.conn.execute("SELECT warnings FROM posts").fetchone()[0]
        self.assertIn("źródło niezweryfikowane", warning)
        self.assertIn("Klienci nie czytają", warning)

    def test_fact_with_a_source_found_in_the_data_has_no_warning_and_is_shown_to_the_owner(self):
        created, _ = self.run_one(facts=[{"claim": "Raport mówi o 40%", "source": "https://znane.pl"}])
        self.assertEqual(len(created), 1)
        row = self.conn.execute("SELECT warnings, body FROM posts").fetchone()
        self.assertEqual(row["warnings"], "")
        self.assertIn("SOURCES", row["body"])
        self.assertIn("[https://znane.pl]", row["body"])


class FileSourceTests(Env):
    FILES = {"faq.md": "Pakiet hurtowy: 2 500 zł netto wdrożenie + 400 zł netto miesięcznie. Wdrożenie trwa jeden dzień.",
             "knowledge/offer/faq.md": "Pakiet hurtowy: 2 500 zł netto wdrożenie + 400 zł netto miesięcznie. Wdrożenie trwa jeden dzień."}

    def run_one(self, facts, **extra):
        with mock.patch.object(pipeline, "source_files", lambda: self.FILES):
            return pipeline.run_content(self.conn, Llm([item("Hak", facts=facts, **extra)]))

    def warnings(self):
        return self.conn.execute("SELECT warnings FROM posts").fetchone()[0]

    def test_a_file_name_source_that_really_contains_the_claim_is_verified(self):
        for src in ("faq.md", "knowledge/offer/faq.md", "knowledge" + chr(92) + "offer" + chr(92) + "faq.md", "FAQ.md (Twoje dane)"):
            conn = connect(":memory:")
            with mock.patch.object(pipeline, "source_files", lambda: self.FILES):
                created, _ = pipeline.run_content(conn, Llm([item("Hak " + src, facts=[{"claim": "Pakiet hurtowy: 2 500 zł netto wdrożenie", "source": src}])]))
            self.assertEqual(len(created), 1, src)
            self.assertNotIn("źródło", conn.execute("SELECT warnings FROM posts").fetchone()[0], src)

    def test_a_file_that_does_not_back_the_claim_is_flagged(self):
        self.run_one([{"claim": "Wdrożenie trwa dwa tygodnie i obejmuje szkolenie zespołu", "source": "faq.md"}])
        self.assertIn("źródło nie potwierdza twierdzenia", self.warnings())

    def test_unknown_file_and_missing_source_are_flagged_differently(self):
        self.run_one([{"claim": "Raport z ankiety", "source": "ankieta.pdf"}, {"claim": "Bez niczego", "source": ""}])
        w = self.warnings()
        self.assertIn("źródło niezweryfikowane: Raport z ankiety", w)
        self.assertIn("fakt bez źródła: Bez niczego", w)

    def test_a_bare_invented_number_is_flagged_but_numbers_from_the_data_are_not(self):
        self.run_one([], body_text=PL + " Raport z 73 tabelami i 40% kosztów, wdrożenie za 2 500 zł.")
        w = self.warnings()
        self.assertIn("liczba bez pokrycia w danych: 73", w)
        self.assertNotIn("liczba bez pokrycia w danych: 40", w)
        self.assertNotIn("2500", w)


class WebBase(Env):
    def setUp(self):
        super().setUp()
        self.db = self.tmp / "q.db"
        self.conn = connect(self.db)
        self.srv = web.make_server(0, llm=Llm([item("Z panelu")]), outbox=self.tmp / "out", token="tok", db_path=self.db)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.shutdown)
        p = mock.patch.object(cleanup, "MANIFEST", self.tmp / "none.json")
        p.start()
        self.addCleanup(p.stop)

    def req(self, method, path, data=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port)
        h = {"Cookie": "orki=tok"}
        body = urlencode(data) if data is not None else None
        if body is not None:
            h["Content-Type"] = "application/x-www-form-urlencoded"
        c.request(method, path, body if method == "POST" else None, h)
        r = c.getresponse()
        out = r.status, r.read().decode(), r.getheader("Location") or ""
        c.close()
        return out


class QueueTabTests(WebBase):
    def test_default_view_shows_only_what_needs_a_decision_and_tabs_hold_the_rest(self):
        post(self.conn, hook="DO DECYZJI")
        post(self.conn, hook="ODRZUCONY RAZ", status="rejected")
        post(self.conn, hook="JUZ WYSZEDL", status="published")
        _, page, _ = self.req("GET", "/queue")
        self.assertIn("DO DECYZJI", page)
        self.assertNotIn("ODRZUCONY RAZ", page)
        self.assertNotIn("JUZ WYSZEDL", page)
        for label in ("Do zatwierdzenia (1)", "Zaplanowane (0)", "Opublikowane (1)", "Odrzucone (1)", "Archiwum (0)"):
            self.assertIn(label, page)
        self.assertIn("ODRZUCONY RAZ", self.req("GET", "/queue?tab=rejected")[1])
        self.assertNotIn("DO DECYZJI", self.req("GET", "/queue?tab=rejected")[1])
        self.assertIn("JUZ WYSZEDL", self.req("GET", "/queue?tab=published")[1])
        self.assertIn("DO DECYZJI", self.req("GET", "/queue?tab=nieznana")[1])  # unknown tab falls back to the default

    def test_archive_moves_a_post_out_of_its_tab_and_back(self):
        pid = post(self.conn, hook="DO SCHOWANIA", status="rejected")
        code, _, loc = self.req("POST", f"/archive/{pid}", {})
        self.assertEqual(code, 303)
        self.assertTrue(loc.startswith("/queue?tab=rejected&msg="), loc)  # a valid URL: one query string
        self.assertNotIn("DO SCHOWANIA", self.req("GET", "/queue?tab=rejected")[1])
        archive = self.req("GET", "/queue?tab=archive")[1]
        self.assertIn("DO SCHOWANIA", archive)
        self.assertIn("Przywróć z archiwum", archive)
        self.req("POST", f"/unarchive/{pid}", {})
        self.assertIn("DO SCHOWANIA", self.req("GET", "/queue?tab=rejected")[1])

    def test_pending_post_cannot_be_archived_from_the_panel(self):
        pid = post(self.conn)
        _, _, loc = self.req("POST", f"/archive/{pid}", {})
        self.assertIn("err=1", loc)
        self.assertEqual(self.conn.execute("SELECT archived_at FROM posts").fetchone()[0], None)

    def test_requeue_sends_a_rejected_post_back_for_approval(self):
        pid = post(self.conn, status="rejected")
        self.req("POST", f"/requeue/{pid}", {})
        self.assertEqual(self.conn.execute("SELECT status FROM posts").fetchone()[0], "pending_approval")

    def test_reject_with_quick_reason_stores_it_and_shows_it(self):
        pid = post(self.conn, hook="DO ODRZUCENIA")
        _, page, _ = self.req("GET", "/queue")
        for reason in pipeline_reasons():
            self.assertIn(f"<option>{reason}</option>", page)
        self.req("POST", f"/reject/{pid}", {"reason": "zły ton", "note": "za sztywno"})
        self.assertEqual(self.conn.execute("SELECT status, note FROM posts").fetchone()[:], ("rejected", "zły ton: za sztywno"))
        self.assertIn("Powód odrzucenia: zły ton: za sztywno", self.req("GET", "/queue?tab=rejected")[1])
        pid2 = post(self.conn, hook="B")
        self.req("POST", f"/reject/{pid2}", {"reason": "po angielsku", "note": ""})
        self.assertEqual(self.conn.execute("SELECT note FROM posts WHERE id=?", (pid2,)).fetchone()[0], "po angielsku")
        pid3 = post(self.conn, hook="C")
        self.req("POST", f"/reject/{pid3}", {"reason": "", "note": ""})
        self.assertEqual(self.conn.execute("SELECT status, note FROM posts WHERE id=?", (pid3,)).fetchone()[:], ("rejected", ""))

    def test_scheduled_post_can_be_cancelled_by_rejecting_it(self):
        pid = post(self.conn, hook="ZAPLANOWANY")
        self.req("POST", f"/approve/{pid}", {})
        self.assertIn("ZAPLANOWANY", self.req("GET", "/queue?tab=scheduled")[1])
        self.req("POST", f"/reject/{pid}", {"reason": "nie moja teza", "note": ""})
        self.assertEqual(self.conn.execute("SELECT status FROM posts").fetchone()[0], "rejected")

    def test_generate_button_enforces_the_limit_with_a_clear_message(self):
        for i in range(3):
            post(self.conn, hook=f"Czeka {i}")
        code, _, loc = self.req("POST", "/generate/content", {})
        self.assertIn("err=1", loc)
        self.assertIn("czeka już 3", unquote(loc))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0], 3)

    def test_skipped_drafts_are_explained_in_the_panel_message(self):
        post(self.conn, hook="Z panelu", status="rejected")
        _, _, loc = self.req("POST", "/generate/content", {})
        self.assertIn("Pominięte", unquote(loc))
        self.assertIn("duplikat", unquote(loc))
        self.assertIn("Z panelu", unquote(loc))

    def test_archiving_a_lead_hides_it_from_the_leads_page(self):
        leads.capture(self.conn, "instagram", "@kupujacy", "how much is it?")
        self.assertIn("@kupujacy", self.req("GET", "/leads")[1])
        self.req("POST", "/lead/1/archive", {})
        self.assertNotIn("@kupujacy", self.req("GET", "/leads")[1])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0], 1)  # hidden, not deleted


def pipeline_reasons():
    return web.REJECT_REASONS


class TestFlagTests(WebBase):
    def test_flagged_rows_are_tagged_and_removed_by_the_cleanup_button_only(self):
        keep = post(self.conn, hook="PRAWDZIWY")
        test = gate.add_draft(self.conn, AGENT, "linkedin", "text_post", "x", hook="PROBNY", is_test=True)
        gate.transition(self.conn, test, "submit", AGENT)
        page = self.req("GET", "/queue")[1]
        self.assertIn("TEST", page)
        self.assertIn("Usuń dane testowe", page)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0], 2)  # viewing deletes nothing
        code, _, loc = self.req("POST", "/cleanup", {})
        self.assertIn("Usunięto", unquote(loc))
        self.assertEqual([r[0] for r in self.conn.execute("SELECT id FROM posts")], [keep])
        self.assertNotIn("Usuń dane testowe", self.req("GET", "/queue")[1])

    def test_cleanup_with_nothing_flagged_says_so(self):
        _, _, loc = self.req("POST", "/cleanup", {})
        self.assertIn("err=1", loc)


if __name__ == "__main__":
    unittest.main()
