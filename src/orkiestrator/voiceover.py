"""Optional voiceover through ElevenLabs. Needs ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID; without them it explains why."""
import json
import os
import urllib.error
import urllib.request

from .carousel import assets_dir


class NotConfigured(Exception):
    pass


def _http(url, headers, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"ElevenLabs error {e.code}: {e.read().decode(errors='replace')[:200]}") from e


def available():
    return bool(os.environ.get("ELEVENLABS_API_KEY") and os.environ.get("ELEVENLABS_VOICE_ID"))


def generate(post_id, text, http=None, root=None):
    key, voice = os.environ.get("ELEVENLABS_API_KEY"), os.environ.get("ELEVENLABS_VOICE_ID")
    if not key or not voice:
        raise NotConfigured("Lektor wymaga zmiennych ELEVENLABS_API_KEY i ELEVENLABS_VOICE_ID.")
    audio = (http or _http)(
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice}",
        {"xi-api-key": key, "content-type": "application/json", "accept": "audio/mpeg"},
        {"text": text, "model_id": "eleven_multilingual_v2"},
    )
    out = assets_dir(post_id, root)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "voiceover.mp3"
    path.write_bytes(audio)
    return path
