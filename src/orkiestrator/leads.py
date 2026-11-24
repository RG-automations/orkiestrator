"""Intent detection for inbound comments and DMs: buying signals become leads, not inbox noise."""
import re

from .gate import now

BUY_PATTERNS = (
    r"\bhow much\b", r"\bwhat('s| is) the (price|cost)\b", r"\bhow much does it cost\b", r"\bjak\w* cen\w*", r"\bile\b[^.?!]{0,20}\bkoszt\w*", r"\b(cena|cennik|wycen\w*)\b",
    r"\bwhere (can|do) i (buy|sign up|get)\b", r"\b(send|share|drop) (me )?(the |a )?link\b", r"\bis there a link\b",
    r"\binterested\b", r"\bsign me up\b",
    r"\bdo you (offer|have|sell)\b", r"\bjestem zainteresowan\w*\b", r"\bchcę kupić\b",
)
COLLAB_PATTERNS = (
    r"\bcollab\w*\b", r"\bpartnership\b", r"\bsponsor\w*\b", r"\bbrand ambassador\b", r"\bwspółprac\w*\b",
    r"\bwork together\b", r"\bpaid promo\w*\b",
)
_BUY = re.compile("|".join(BUY_PATTERNS), re.IGNORECASE)
_COLLAB = re.compile("|".join(COLLAB_PATTERNS), re.IGNORECASE)
_NEGATIVE = re.compile(r"\b(scam|fake|rip.?off|terrible|worst|oszust\w*)\b", re.IGNORECASE)


def classify(message):
    """Returns 'negative', 'collab', 'lead' or 'other'. Negative first so it is escalated to the owner."""
    if _NEGATIVE.search(message):
        return "negative"
    if _COLLAB.search(message):
        return "collab"
    if _BUY.search(message):
        return "lead"
    return "other"


def capture(conn, platform, handle, message, post_id=None, source_link_id=""):
    """Logs every inbound message; buying signals also become leads. Returns the intent.

    Repeatable: the same message from the same handle on the same platform is stored once, so re-running
    a step over the same CSV does not duplicate messages, leads or alerts."""
    intent = classify(message)
    if conn.execute("SELECT 1 FROM inbound WHERE platform=? AND handle=? AND message=?", (platform, handle, message)).fetchone():
        return intent
    conn.execute(
        "INSERT INTO inbound (platform, handle, message, intent, post_id, created_at) VALUES (?,?,?,?,?,?)",
        (platform, handle, message, intent, post_id, now()),
    )
    if intent == "lead":
        conn.execute(
            "INSERT INTO leads (platform, handle, message, intent, post_id, source_link_id, created_at) VALUES (?,?,?,?,?,?,?)",
            (platform, handle, message, intent, post_id, source_link_id, now()),
        )
    conn.commit()
    return intent


STATUSES = ("new", "replied", "booked", "client", "lost")


def set_status(conn, lead_id, status):
    if status not in STATUSES:
        raise ValueError(f"unknown lead status {status!r}")
    if not conn.execute("SELECT 1 FROM leads WHERE id=?", (lead_id,)).fetchone():
        raise ValueError(f"lead {lead_id} not found")
    conn.execute("UPDATE leads SET status=? WHERE id=?", (status, lead_id))
    conn.commit()


def mark_client(conn, lead_id, amount, post_id=None):
    """Lead became a paying customer: record the sale against the post that brought them."""
    row = conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
    if row is None:
        raise ValueError(f"lead {lead_id} not found")
    if amount < 0:
        raise ValueError("amount must not be negative")
    post_id = post_id or row["post_id"]
    link_id = row["source_link_id"]
    if post_id is None and link_id:
        p = conn.execute("SELECT id FROM posts WHERE link_id=?", (link_id,)).fetchone()
        post_id = p["id"] if p else None
    if link_id == "" and post_id is not None:
        p = conn.execute("SELECT link_id FROM posts WHERE id=?", (post_id,)).fetchone()
        link_id = p["link_id"] if p else ""
    conn.execute(
        "INSERT INTO customers (lead_id, post_id, link_id, amount, ts) VALUES (?,?,?,?,?)",
        (lead_id, post_id, link_id, amount, now()),
    )
    conn.execute("UPDATE leads SET status='client' WHERE id=?", (lead_id,))
    conn.commit()
    return post_id


def archive_lead(conn, lead_id):
    """Marks a lead as handled and hides it from the Leads page. Nothing is deleted."""
    if not conn.execute("SELECT 1 FROM leads WHERE id=?", (lead_id,)).fetchone():
        raise ValueError(f"lead {lead_id} not found")
    conn.execute("UPDATE leads SET archived_at=? WHERE id=?", (now(), lead_id))
    conn.commit()


def unarchive_lead(conn, lead_id):
    conn.execute("UPDATE leads SET archived_at=NULL WHERE id=?", (lead_id,))
    conn.commit()


def archive_inbound(conn, inbound_id):
    """A handled comment or DM stops raising alerts."""
    conn.execute("UPDATE inbound SET archived_at=? WHERE id=?", (now(), inbound_id))
    conn.commit()
