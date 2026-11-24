import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from orkiestrator import ai  # noqa: E402

NONE = {"GEMINI_API_KEY": "", "GOOGLE_API_KEY": "", "ANTHROPIC_API_KEY": "", "ORKI_PROVIDER": "", "ORKI_MODEL": ""}


class ProviderTests(unittest.TestCase):
    def env(self, **kw):
        return mock.patch.dict("os.environ", {**NONE, **kw})

    def test_provider_selection(self):
        with self.env():
            self.assertIsNone(ai.provider())
        with self.env(ANTHROPIC_API_KEY="a"):
            self.assertEqual(ai.provider(), "claude")
        with self.env(GEMINI_API_KEY="g", ANTHROPIC_API_KEY="a"):
            self.assertEqual(ai.provider(), "gemini")
        with self.env(GOOGLE_API_KEY="g"):
            self.assertEqual(ai.provider(), "gemini")
        with self.env(GEMINI_API_KEY="g", ANTHROPIC_API_KEY="a", ORKI_PROVIDER="claude"):
            self.assertEqual(ai.provider(), "claude")

    def test_gemini_request_and_response(self):
        calls = []
        reply = {"candidates": [{"content": {"parts": [{"text": "he"}, {"text": "llo"}]}}]}
        with self.env(GEMINI_API_KEY="gk"):
            out = ai.gemini_llm("SYS", "PROMPT", http=lambda url, h, b: calls.append((url, h, b)) or reply)
        self.assertEqual(out, "hello")
        url, headers, body = calls[0]
        self.assertIn("generativelanguage.googleapis.com/v1beta/models/gemini-flash-latest:generateContent", url)
        self.assertEqual(headers["x-goog-api-key"], "gk")
        self.assertNotIn("gk", url)  # key goes in a header, never in the URL
        self.assertEqual(body["systemInstruction"]["parts"][0]["text"], "SYS")
        self.assertEqual(body["contents"][0]["parts"][0]["text"], "PROMPT")

    def test_gemini_blocked_response_is_an_error(self):
        with self.env(GEMINI_API_KEY="gk"):
            with self.assertRaises(RuntimeError):
                ai.gemini_llm("s", "p", http=lambda u, h, b: {"promptFeedback": {"blockReason": "SAFETY"}})

    def test_model_override(self):
        calls = []
        with self.env(GEMINI_API_KEY="gk", ORKI_MODEL="gemini-x"):
            ai.gemini_llm("s", "p", http=lambda u, h, b: calls.append(u) or {"candidates": [{"content": {"parts": [{"text": "x"}]}}]})
        self.assertIn("models/gemini-x:generateContent", calls[0])

    def test_claude_path_unchanged(self):
        calls = []
        with self.env(ANTHROPIC_API_KEY="ak"):
            out = ai.claude_llm("s", "p", http=lambda u, h, b: calls.append((u, h, b)) or {"content": [{"text": "ok"}]})
        self.assertEqual(out, "ok")
        self.assertEqual(calls[0][1]["x-api-key"], "ak")

    def test_default_llm_without_keys_explains(self):
        with self.env():
            with self.assertRaises(ai.MissingKey) as cm:
                ai.default_llm("s", "p")
        self.assertIn("GEMINI_API_KEY", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
