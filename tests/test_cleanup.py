import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from orkiestrator import cleanup, gate  # noqa: E402
from orkiestrator.db import connect  # noqa: E402


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        for d in ("data/assets/1", "data/assets/2", "data/briefs", "demo"):
            (self.root / d).mkdir(parents=True)
        (self.root / "data/assets/1/s.svg").write_text("x")
        (self.root / "data/assets/2/s.svg").write_text("keep")
        (self.root / "data/briefs/old.md").write_text("old")
        (self.root / "data/briefs/real.md").write_text("real")
        (self.root / "demo/e.py").write_text("x")
        self.db = self.root / "data" / "queue.db"
        self.conn = connect(self.db)
        self.ids = []
        for i in range(3):
            pid = gate.add_draft(self.conn, "agent:x", "linkedin", "post", f"body {i}")
            gate.transition(self.conn, pid, "submit", "agent:x")
            self.ids.append(pid)

    def manifest(self, **kw):
        m = {"post_ids": self.ids[:2], "lead_ids": [], "inbound_ids": [], "paths": ["data/assets/1", "data/briefs/old.md", "demo"]}
        m.update(kw)
        return m

    def test_removes_only_listed_things_and_backs_up_first(self):
        res = cleanup.run(self.conn, self.manifest(), root=self.root, manifest_path=self.root / "none.json")
        self.assertEqual((res["posts"], res["paths"]), (2, 3))
        self.assertEqual([r[0] for r in self.conn.execute("SELECT id FROM posts")], [self.ids[2]])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM events WHERE post_id IN (?,?)", self.ids[:2]).fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM events WHERE post_id=?", (self.ids[2],)).fetchone()[0], 2)
        self.assertFalse((self.root / "data/assets/1").exists())
        self.assertFalse((self.root / "demo").exists())
        self.assertTrue((self.root / "data/assets/2/s.svg").exists())
        self.assertTrue((self.root / "data/briefs/real.md").exists())
        import sqlite3
        backup = sqlite3.connect(str(self.root / res["backup"]))
        self.assertEqual(backup.execute("SELECT COUNT(*) FROM posts").fetchone()[0], 3)  # backup has everything

    def test_refuses_paths_outside_data_and_demo_and_the_database(self):
        for bad in ("../secret.txt", "inputs/goals.md", "knowledge", "data", "data/queue.db", "src"):
            with self.assertRaises(ValueError, msg=bad):
                cleanup.preview(self.conn, self.manifest(paths=[bad]), root=self.root)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0], 3)

    def test_panel_button_deletes_only_after_owner_click_and_needs_session(self):
        import http.client
        import threading
        from unittest import mock
        from urllib.parse import unquote
        from orkiestrator import web
        mp = self.root / "data" / "cleanup_manifest.json"
        mp.write_text(json.dumps(self.manifest()), encoding="utf-8")
        with mock.patch.object(cleanup, "ROOT", self.root), mock.patch.object(cleanup, "MANIFEST", mp):
            srv = web.make_server(0, token="tok", db_path=self.db)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            self.addCleanup(srv.shutdown)

            def req(method, path, cookie=True):
                c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1])
                c.request(method, path, "" if method == "POST" else None, {"Cookie": "orki=tok"} if cookie else {})
                r = c.getresponse()
                return r.status, r.read().decode(), r.getheader("Location") or ""

            self.assertEqual(req("POST", "/cleanup", cookie=False)[0], 401)
            self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0], 3)
            code, page, _ = req("GET", "/queue")  # viewing the page deletes nothing
            self.assertIn("Usuń dane testowe", page)
            self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0], 3)
            code, _, loc = req("POST", "/cleanup")
            self.assertEqual(code, 303)
            self.assertIn("Usunięto", unquote(loc))
            self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0], 1)
            self.assertNotIn("Usuń dane testowe", req("GET", "/queue")[1])

    def test_invalid_manifest_deletes_nothing(self):
        with self.assertRaises(ValueError):
            cleanup.run(self.conn, self.manifest(paths=["data/briefs/old.md", "inputs/goals.md"]), root=self.root)
        self.assertTrue((self.root / "data/briefs/old.md").exists())
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0], 3)

    def test_manifest_is_retired_after_run_and_second_run_is_harmless(self):
        mp = self.root / "data" / "cleanup_manifest.json"
        mp.write_text(json.dumps(self.manifest()), encoding="utf-8")
        cleanup.run(self.conn, cleanup.load(mp), root=self.root, manifest_path=mp)
        self.assertFalse(mp.exists())
        self.assertTrue(mp.with_suffix(".done.json").exists())
        res = cleanup.run(self.conn, self.manifest(), root=self.root, manifest_path=mp)
        self.assertEqual((res["posts"], res["paths"]), (0, 0))


if __name__ == "__main__":
    unittest.main()
