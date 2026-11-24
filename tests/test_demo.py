"""End-to-end demo run on a copy of the repo: offline provider, no keys, no network, dry-run publishing only."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

SCRIPT = r"""
import json, sys
from orkiestrator import ai, brain, publisher
from orkiestrator.db import connect
conn = connect()
res = brain.morning_loop(conn, ai.default_llm)
rows = conn.execute("SELECT id, platform, kind, status FROM posts ORDER BY id").fetchall()
before = publisher.run_due(conn)
print(json.dumps({"failures": res["failures"], "posts": [dict(r) for r in rows], "published_without_approval": before}))
"""


class DemoRunTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        for name in ("src", "knowledge", "inputs"):
            shutil.copytree(REPO / name, self.root / name, ignore=shutil.ignore_patterns("__pycache__"))
        self.env = {k: v for k, v in os.environ.items()
                    if k not in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "ANTHROPIC_API_KEY", "FB_PAGE_ID", "FB_PAGE_TOKEN",
                                 "DISCORD_BOT_TOKEN", "TELEGRAM_BOT_TOKEN", "SMTP_HOST", "ORKI_DB")}
        self.env.update(ORKI_PROVIDER="demo", PYTHONPATH=str(self.root / "src"))

    def run_py(self, code):
        out = subprocess.run([sys.executable, "-c", code], cwd=self.root, env=self.env, capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr)
        return out.stdout

    def test_morning_loop_fills_the_queue_and_nothing_is_published(self):
        result = json.loads(self.run_py(SCRIPT).strip().splitlines()[-1])
        self.assertEqual(result["failures"], [])
        statuses = {p["status"] for p in result["posts"]}
        self.assertEqual(statuses, {"pending_approval"})
        kinds = sorted(p["kind"] for p in result["posts"])
        self.assertEqual([k for k in kinds if k != "reply"], ["carousel", "text_post", "video"])
        self.assertIn("reply", kinds)
        self.assertEqual(result["published_without_approval"], [])
        self.assertFalse((self.root / "data" / "outbox").exists())
        self.assertTrue(list((self.root / "data" / "briefs").glob("*.md")))

    def test_owner_approval_then_dry_run_publish(self):
        self.run_py(SCRIPT)
        out = self.run_py(
            "from orkiestrator import gate, publisher\n"
            "from orkiestrator.db import connect\n"
            "c = connect()\n"
            "pid = c.execute(\"SELECT id FROM posts WHERE status='pending_approval' ORDER BY id LIMIT 1\").fetchone()[0]\n"
            "gate.transition(c, pid, 'approve', 'human:owner')\n"
            "gate.transition(c, pid, 'schedule', 'agent:scheduler', scheduled_at=gate.now())\n"
            "print(publisher.run_due(c))\n")
        self.assertIn("outbox", out)
        files = list((self.root / "data" / "outbox").glob("*.json"))
        self.assertEqual(len(files), 1)
        self.assertEqual(json.loads(files[0].read_text(encoding="utf-8"))["approved_by"], "human:owner")


if __name__ == "__main__":
    unittest.main()
