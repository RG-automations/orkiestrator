import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from orkiestrator import adapters  # noqa: E402
from orkiestrator.adapters import DryRunPublisher, FacebookPublisher  # noqa: E402

ENV = {"FB_PAGE_ID": "123", "FB_PAGE_TOKEN": "tok"}


class FacebookTests(unittest.TestCase):
    def setUp(self):
        adapters.LIVE["confirmed"] = False
        self.addCleanup(lambda: adapters.LIVE.update(confirmed=False))

    def test_sends_expected_graph_request(self):
        calls = []
        pub = FacebookPublisher("123", "tok", http=lambda url, data: calls.append((url, data)) or {"id": "123_9"})
        ref = pub.publish({"body": "hello"})
        self.assertEqual(ref, "facebook:123_9")
        self.assertTrue(calls[0][0].endswith("/123/feed"))
        self.assertEqual(calls[0][1], {"message": "hello", "access_token": "tok"})

    def test_missing_id_is_an_error(self):
        with self.assertRaises(RuntimeError):
            FacebookPublisher("1", "t", http=lambda u, d: {}).publish({"body": "x"})

    def test_every_kind_goes_to_feed_as_text(self):
        calls = []
        pub = FacebookPublisher("123", "tok", http=lambda url, data: calls.append((url, data)) or {"id": "t1"})
        for kind in ("reel", "text_post", "carousel"):
            pub.publish({"id": 999, "kind": kind, "body": "x"})
        self.assertTrue(all(url.endswith("/123/feed") for url, _ in calls))

    def test_dry_run_unless_credentials_and_owner_confirmation(self):
        with mock.patch.dict("os.environ", ENV):
            self.assertIsInstance(adapters.get_publisher("facebook"), DryRunPublisher)  # not confirmed
            adapters.LIVE["confirmed"] = True
            self.assertIsInstance(adapters.get_publisher("facebook"), FacebookPublisher)
            self.assertIsInstance(adapters.get_publisher("linkedin"), DryRunPublisher)  # no adapter yet
        adapters.LIVE["confirmed"] = True
        with mock.patch.dict("os.environ", {"FB_PAGE_ID": "", "FB_PAGE_TOKEN": ""}):
            self.assertIsInstance(adapters.get_publisher("facebook"), DryRunPublisher)  # no credentials


if __name__ == "__main__":
    unittest.main()
