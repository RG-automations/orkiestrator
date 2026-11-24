import json
from pathlib import Path

from ..db import ROOT
from .base import Publisher

FIELDS = ("id", "platform", "kind", "title", "hook", "body", "link_id", "approved_by", "approved_at")


class DryRunPublisher(Publisher):
    """Writes the would-be post to data/outbox/ instead of calling a platform API."""

    def __init__(self, platform, outbox=None):
        self.platform = platform
        self.outbox = Path(outbox or ROOT / "data" / "outbox")

    def publish(self, post):
        self.outbox.mkdir(parents=True, exist_ok=True)
        path = self.outbox / f"{self.platform}-{post['id']}.json"
        payload = {k: post[k] for k in FIELDS}
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        return str(path)
