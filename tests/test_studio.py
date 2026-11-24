import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from orkiestrator import carousel, pipeline, voiceover  # noqa: E402


class StudioTests(unittest.TestCase):
    def test_compose_body_uses_best_hook_and_all_parts(self):
        body, hook = pipeline.compose_body({
            "hooks": [{"text": "low", "score": 2}, {"text": "TOP", "score": 9}],
            "script": [{"t": "0-3s", "say": "say it", "on_screen": "BIG"}],
            "slides": [{"title": "One", "text": "first"}],
            "thumbnail_text": "WOW", "caption": "cap"})
        self.assertEqual(hook, "TOP")
        for needle in ("TOP", "[0-3s] say it", "(on screen: BIG)", "1. One: first", "THUMBNAIL TEXT: WOW", "CAPTION"):
            self.assertIn(needle, body)

    def test_carousel_writes_valid_escaped_svg_files(self):
        root = Path(tempfile.mkdtemp())
        paths = carousel.render(7, [{"title": "A & B <x>", "text": "word " * 40}, {"title": "Two", "text": "t"}], root=root)
        self.assertEqual([p.name for p in paths], ["slide-01.svg", "slide-02.svg"])
        import xml.dom.minidom
        for p in paths:
            xml.dom.minidom.parse(str(p))  # raises if not well-formed
        text = paths[0].read_text(encoding="utf-8")
        self.assertIn('width="1080" height="1350"', text)
        self.assertIn("A &amp; B &lt;x&gt;", text)
        self.assertIn("1/2", text)

    def test_every_prompt_ends_with_the_language_order(self):
        seen = []
        wrapped = pipeline.localized(lambda s, p: seen.append((s, p)) or "[]")
        wrapped("sys", "do the thing")
        self.assertTrue(seen[0][1].startswith("do the thing"))
        self.assertIn("write all text content in Polish", seen[0][1].splitlines()[-1])
        self.assertIn("in Polish", pipeline.system())

    def test_latest_brief_is_newest_by_time_not_by_name(self):
        import os
        d = Path(tempfile.mkdtemp())
        newer_name, older_name = d / "2026-W43.md", d / "2026-W42.md"
        newer_name.write_text("old", encoding="utf-8")
        older_name.write_text("new", encoding="utf-8")
        os.utime(newer_name, (1_000_000, 1_000_000))
        os.utime(older_name, (2_000_000, 2_000_000))
        with mock.patch.object(pipeline, "BRIEFS", d):
            self.assertEqual(pipeline.latest_brief(), older_name)

    def test_brand_check_is_advisory_and_tolerates_garbage(self):
        self.assertEqual(pipeline.brand_check(lambda s, p: json.dumps(["too salesy"]), "text"), ["too salesy"])
        self.assertEqual(pipeline.brand_check(lambda s, p: "not json at all", "text"), [])

    def test_hook_breakdowns_are_appended_in_structure(self):
        p = Path(tempfile.mkdtemp()) / "hb.md"
        pipeline.append_hook_breakdowns([{"source": "@a c1", "first_3_seconds": "You are undercharging", "formula": "call-out", "why": "names the pain"}], p)
        text = p.read_text(encoding="utf-8")
        self.assertIn("first 3 seconds: \"You are undercharging\"", text)
        self.assertIn("formula: call-out", text)

    def test_voiceover_requires_config_and_sends_request(self):
        root = Path(tempfile.mkdtemp())
        with mock.patch.dict("os.environ", {"ELEVENLABS_API_KEY": "", "ELEVENLABS_VOICE_ID": ""}):
            self.assertFalse(voiceover.available())
            with self.assertRaises(voiceover.NotConfigured):
                voiceover.generate(1, "hi", root=root)
        calls = []
        with mock.patch.dict("os.environ", {"ELEVENLABS_API_KEY": "k", "ELEVENLABS_VOICE_ID": "v1"}):
            path = voiceover.generate(1, "hello", http=lambda url, h, b: calls.append((url, h, b)) or b"MP3", root=root)
        self.assertEqual(path.read_bytes(), b"MP3")
        self.assertTrue(calls[0][0].endswith("/v1/text-to-speech/v1"))
        self.assertEqual(calls[0][1]["xi-api-key"], "k")
        self.assertEqual(calls[0][2]["text"], "hello")


if __name__ == "__main__":
    unittest.main()
