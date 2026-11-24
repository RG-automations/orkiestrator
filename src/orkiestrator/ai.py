"""LLM clients (stdlib only). The pipeline takes any callable llm(system, prompt) -> str.

Provider: ORKI_PROVIDER=gemini|claude, otherwise Gemini when GEMINI_API_KEY (or GOOGLE_API_KEY) is set,
else Claude when ANTHROPIC_API_KEY is set. ORKI_MODEL overrides the default model of either provider.
"""
import json
import os
import time
import urllib.error
import urllib.request

CLAUDE_URL = "https://api.anthropic.com/v1/messages"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
DEFAULT_GEMINI_MODEL = "gemini-flash-latest"
DEFAULT_CLAUDE_MODEL = "claude-sonnet-5-5"


class MissingKey(Exception):
    pass


RETRY_CODES = (429, 500, 503)
RETRY_WAITS = (8, 20, 45)  # seconds; free tiers hit per-minute limits often


def _post(url, headers, body, sleep=time.sleep):
    for attempt in range(len(RETRY_WAITS) + 1):
        req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in RETRY_CODES and attempt < len(RETRY_WAITS):
                sleep(RETRY_WAITS[attempt])
                continue
            hint = " Limit zapytań wyczerpany: poczekaj minutę albo sprawdź limity/rozliczenia w AI Studio." if e.code == 429 else ""
            raise RuntimeError(f"Błąd API {e.code}.{hint} {e.read().decode(errors='replace')[:200]}") from e


def gemini_key():
    return os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")


def provider():
    """Returns 'gemini', 'claude' or None when no key is configured."""
    forced = os.environ.get("ORKI_PROVIDER", "").lower()
    if forced in ("gemini", "claude"):
        return forced
    if gemini_key():
        return "gemini"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "claude"
    return None


def gemini_llm(system, prompt, http=None):
    key = gemini_key()
    if not key:
        raise MissingKey("Brak klucza GEMINI_API_KEY (klucz z aistudio.google.com/api-keys).")
    model = os.environ.get("ORKI_MODEL") or DEFAULT_GEMINI_MODEL
    data = (http or _post)(
        GEMINI_URL.format(model=model),
        {"x-goog-api-key": key, "content-type": "application/json"},
        {"systemInstruction": {"parts": [{"text": system}]}, "contents": [{"role": "user", "parts": [{"text": prompt}]}]},
    )
    cands = data.get("candidates") or []
    if not cands:
        reason = (data.get("promptFeedback") or {}).get("blockReason", "brak odpowiedzi")
        raise RuntimeError(f"Gemini nie zwrócił tekstu ({reason}).")
    return "".join(p.get("text", "") for p in cands[0].get("content", {}).get("parts", []))


def claude_llm(system, prompt, http=None):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise MissingKey("Brak klucza ANTHROPIC_API_KEY.")
    data = (http or _post)(
        CLAUDE_URL,
        {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        {"model": os.environ.get("ORKI_MODEL") or DEFAULT_CLAUDE_MODEL, "max_tokens": 8000, "system": system,
         "messages": [{"role": "user", "content": prompt}]},
    )
    return "".join(b.get("text", "") for b in data.get("content", []))


def default_llm(system, prompt):
    which = provider()
    if which == "gemini":
        return gemini_llm(system, prompt)
    if which == "claude":
        return claude_llm(system, prompt)
    raise MissingKey("Brak klucza AI. Ustaw GEMINI_API_KEY (aistudio.google.com/api-keys) albo ANTHROPIC_API_KEY.")
