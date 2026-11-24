import json
import urllib.error
import urllib.parse
import urllib.request

from .base import Publisher

GRAPH = "https://graph.facebook.com/v19.0"


def _http_post(url, data):
    req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode(), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Facebook API error {e.code}: {e.read().decode(errors='replace')[:300]}") from e


class FacebookPublisher(Publisher):
    """Publishes a text post to a Facebook Page through the Graph API (/{page_id}/feed). `http` is injectable for tests."""

    platform = "facebook"

    def __init__(self, page_id, token, http=None, base=GRAPH):
        self.page_id, self.token = page_id, token
        self.http = http or _http_post
        self.base = base

    def publish(self, post):
        resp = self.http(f"{self.base}/{self.page_id}/feed", {"message": post["body"], "access_token": self.token})
        if "id" not in resp:
            raise RuntimeError(f"Facebook did not return a post id: {resp}")
        return f"facebook:{resp['id']}"
