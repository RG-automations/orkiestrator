"""The approval gate. The only place post status changes.

Actors are strings: "agent:<name>" for the AI team, "human:<name>" for the owner.
Agents may create drafts and submit them for review. Only a human can approve or
reject, and only an approved post can be scheduled or published.
"""
import hashlib
import json
from datetime import datetime, timezone

from . import lint

STATUSES = ("draft", "pending_approval", "approved", "scheduled", "published", "rejected")

# action -> (allowed from-statuses, to-status, human required)
TRANSITIONS = {
    "submit": (("draft", "rejected"), "pending_approval", False),
    "approve": (("pending_approval",), "approved", True),
    "reject": (("draft", "pending_approval", "approved", "scheduled"), "rejected", True),
    "schedule": (("approved",), "scheduled", False),
    "publish": (("approved", "scheduled"), "published", False),
}


class GateError(Exception):
    pass


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def is_human(actor):
    return actor.startswith("human:") and len(actor) > len("human:")


CONTENT_FIELDS = ("platform", "kind", "title", "hook", "body", "payload", "link_id")


def content_hash(row):
    """Fingerprint of everything that goes out. Integrity check, not a signature: whoever can write the DB can recompute it."""
    data = json.dumps([row[k] for k in CONTENT_FIELDS], ensure_ascii=False)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def verify_approval(row):
    """Raises unless a human approved exactly this content. Called before any platform call and again on the transition."""
    if not row["approved_by"] or not is_human(row["approved_by"]) or not row["approved_at"]:
        raise GateError(f"post {row['id']} has no human approval on record")
    if row["approved_hash"] != content_hash(row):
        raise GateError(f"post {row['id']} changed after approval (or was approved before content hashing): approve it again")


def add_draft(conn, actor, platform, kind, body, title="", hook="", brief_ref="", link_id="", payload="", warnings="", is_test=False):
    """Drafts are the only thing that can be inserted; there is no way to create any other status."""
    cur = conn.execute(
        "INSERT INTO posts (platform, kind, title, hook, body, brief_ref, link_id, payload, warnings, status, created_by, created_at, is_test)"
        " VALUES (?,?,?,?,?,?,?,?,?, 'draft', ?, ?, ?)",
        (platform, kind, title, hook, body, brief_ref, link_id, payload, warnings, actor, now(), 1 if is_test else 0),
    )
    post_id = cur.lastrowid
    _log(conn, post_id, actor, "create", None, "draft", "")
    conn.commit()
    return post_id


def transition(conn, post_id, action, actor, note="", scheduled_at=None):
    if action not in TRANSITIONS:
        raise GateError(f"unknown action {action!r}")
    allowed_from, to_status, human_only = TRANSITIONS[action]
    if human_only and not is_human(actor):
        raise GateError(f"{action} requires a human actor, got {actor!r}")
    row = conn.execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()
    if row is None:
        raise GateError(f"post {post_id} not found")
    if row["status"] not in allowed_from:
        raise GateError(f"cannot {action} post {post_id} in status {row['status']!r}")
    if action in ("schedule", "publish"):
        verify_approval(row)
    if action == "submit":
        problems = lint.check(f"{row['hook']}\n{row['body']}")
        if problems:
            raise GateError(f"voice lint failed for post {post_id}: " + "; ".join(problems))
    if action == "schedule" and not scheduled_at:
        raise GateError("schedule requires scheduled_at")

    fields = {"status": to_status}
    if action == "submit":
        fields["archived_at"] = None  # a rejected, archived post that is sent back for review is a working post again
    if action == "approve":
        fields.update(approved_by=actor, approved_at=now(), approved_hash=content_hash(row))
    if action == "reject":
        fields.update(approved_by=None, approved_at=None, approved_hash=None, note=note)
    if action == "schedule":
        fields["scheduled_at"] = scheduled_at
    if action == "publish":
        fields["published_at"] = now()
    sets = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE posts SET {sets} WHERE id=?", (*fields.values(), post_id))
    _log(conn, post_id, actor, action, row["status"], to_status, note)
    conn.commit()


def edit_body(conn, post_id, actor, body):
    """Any edit voids a prior approval: the owner must sign off on the text that goes out."""
    row = conn.execute("SELECT status FROM posts WHERE id=?", (post_id,)).fetchone()
    if row is None:
        raise GateError(f"post {post_id} not found")
    if row["status"] == "published":
        raise GateError("cannot edit a published post")
    to_status = "draft" if row["status"] in ("approved", "scheduled", "pending_approval") else row["status"]
    conn.execute(
        "UPDATE posts SET body=?, status=?, approved_by=NULL, approved_at=NULL, approved_hash=NULL, scheduled_at=NULL WHERE id=?",
        (body, to_status, post_id),
    )
    note = "approval voided" if to_status != row["status"] else ""
    _log(conn, post_id, actor, "edit", row["status"], to_status, note)
    conn.commit()


ARCHIVABLE = ("rejected", "published")  # only finished posts can leave the working views


def archive(conn, post_id, actor):
    """Hides a rejected or published post from the working views. The post and its log stay; status is unchanged."""
    row = conn.execute("SELECT status, archived_at FROM posts WHERE id=?", (post_id,)).fetchone()
    if row is None:
        raise GateError(f"post {post_id} not found")
    if row["status"] not in ARCHIVABLE:
        raise GateError(f"cannot archive post {post_id} in status {row['status']!r}: only rejected or published posts")
    if row["archived_at"]:
        return
    conn.execute("UPDATE posts SET archived_at=? WHERE id=?", (now(), post_id))
    _log(conn, post_id, actor, "archive", row["status"], row["status"], "")
    conn.commit()


def unarchive(conn, post_id, actor):
    row = conn.execute("SELECT status, archived_at FROM posts WHERE id=?", (post_id,)).fetchone()
    if row is None:
        raise GateError(f"post {post_id} not found")
    if not row["archived_at"]:
        return
    conn.execute("UPDATE posts SET archived_at=NULL WHERE id=?", (post_id,))
    _log(conn, post_id, actor, "unarchive", row["status"], row["status"], "")
    conn.commit()


def _log(conn, post_id, actor, action, from_status, to_status, note):
    conn.execute(
        "INSERT INTO events (post_id, ts, actor, action, from_status, to_status, note) VALUES (?,?,?,?,?,?,?)",
        (post_id, now(), actor, action, from_status, to_status, note),
    )
