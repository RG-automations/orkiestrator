import os

from .base import Publisher  # noqa: F401  re-exported for new adapters
from .dryrun import DryRunPublisher
from .facebook import FacebookPublisher

PLATFORMS = ("instagram", "youtube", "linkedin", "tiktok", "facebook")

# In-memory only: the owner switches live mode on in the panel, and it resets on every restart.
LIVE = {"confirmed": False}


def get_publisher(platform, outbox=None):
    """Real client only when credentials exist AND the owner confirmed live mode; otherwise dry-run."""
    if platform not in PLATFORMS:
        raise ValueError(f"unsupported platform {platform!r}")
    if platform == "facebook" and LIVE["confirmed"]:
        page_id, token = os.environ.get("FB_PAGE_ID"), os.environ.get("FB_PAGE_TOKEN")
        if page_id and token:
            return FacebookPublisher(page_id, token)
    return DryRunPublisher(platform, outbox)
