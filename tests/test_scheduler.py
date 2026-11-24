import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from orkiestrator import gate, publisher, scheduler  # noqa: E402
from orkiestrator.db import connect  # noqa: E402


class SchedulerTests(unittest.TestCase):
    def test_parse_slots(self):
        self.assertEqual(scheduler.parse_slots("slots: mon-fri 11:00, 19:30"), ([0, 1, 2, 3, 4], [(11, 0), (19, 30)]))
        self.assertEqual(scheduler.parse_slots("slots: tue-thu 08:00"), ([1, 2, 3], [(8, 0)]))
        self.assertEqual(scheduler.parse_slots("slots: sat 10:00"), ([5], [(10, 0)]))
        self.assertEqual(scheduler.parse_slots("nothing here"), scheduler.DEFAULT_SLOTS)

    def test_next_slot_is_future_on_allowed_day_and_skips_taken(self):
        after = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)  # a Sunday
        first = scheduler.next_slot("linkedin", after)
        self.assertGreater(first, after.isoformat())
        local = datetime.fromisoformat(first).astimezone()
        self.assertIn(local.weekday(), (1, 2, 3))
        self.assertEqual((local.hour, local.minute), (8, 0))
        second = scheduler.next_slot("linkedin", after, taken=[first])
        self.assertGreater(second, first)

    def test_future_post_is_not_published_before_its_time(self):
        conn = connect(":memory:")
        pid = gate.add_draft(conn, "agent:x", "linkedin", "post", "text")
        gate.transition(conn, pid, "submit", "agent:x")
        gate.transition(conn, pid, "approve", "human:o")
        gate.transition(conn, pid, "schedule", "agent:s", scheduled_at="2999-01-01T08:00:00+00:00")
        out = tempfile.mkdtemp()
        self.assertEqual(publisher.run_due(conn, outbox=out), [])
        self.assertEqual(len(publisher.run_due(conn, as_of="2999-01-02T00:00:00+00:00", outbox=out)), 1)


if __name__ == "__main__":
    unittest.main()
