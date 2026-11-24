"""Scheduler and publisher. Only ever reads posts the gate has marked approved by a human."""
import threading

from . import gate
from .adapters import get_publisher
from .db import connect


def is_live(platform):
    """True when this platform would really post (opt-in), not dry-run."""
    return get_publisher(platform).__class__.__name__ != "DryRunPublisher"


def due_posts(conn, as_of=None):
    as_of = as_of or gate.now()
    return conn.execute(
        "SELECT * FROM posts WHERE status IN ('approved','scheduled') AND approved_by LIKE 'human:%'"
        " AND scheduled_at IS NOT NULL AND scheduled_at <= ? ORDER BY scheduled_at",
        (as_of,),
    ).fetchall()


def run_due(conn, actor="agent:publisher", as_of=None, outbox=None):
    """Publishes every due, human-approved post exactly once, even if two processes call this at once.

    due_posts() is a plain read and can return the same row to two concurrent callers (two publisher
    loops, or a loop racing a manual `publish-due`). claim_for_publish() is the only thing that actually
    decides who gets to publish a given post: it is a single atomic UPDATE, so only one of two racing
    callers can win it for a given post. The platform call always uses a fresh read taken right after the
    claim, not the (possibly stale) row from due_posts(), because that is the content the claim just froze
    (edit_body refuses to touch a 'publishing' post).
    """
    published = []
    for candidate in due_posts(conn, as_of):
        # Recomputed from the candidate's own fields, not read back from its approved_hash column: a forged
        # approved_hash (set without actually re-approving the real content) must not satisfy the claim.
        if not gate.claim_for_publish(conn, candidate["id"], gate.content_hash(candidate)):
            continue  # another process already claimed it, or its content/approval changed since this read
        post = conn.execute("SELECT * FROM posts WHERE id=?", (candidate["id"],)).fetchone()
        try:
            ref = get_publisher(post["platform"], outbox).publish(post)
        except Exception as e:
            gate.release_claim(conn, post["id"], actor, note=str(e))
            raise
        gate.transition(conn, post["id"], "publish", actor, note=ref)
        published.append((post["id"], ref))
    return published


def start_loop(db_path=None, outbox=None, interval=60):
    """Background thread: publishes due posts every `interval` seconds. Returns a stop Event."""
    stop = threading.Event()

    def loop():
        while not stop.wait(interval):
            conn = connect(db_path)
            try:
                run_due(conn, outbox=outbox)
            except Exception as e:  # keep the loop alive; a failed post stays scheduled and is retried
                print(f"[publisher] {e}")
            finally:
                conn.close()

    threading.Thread(target=loop, daemon=True).start()
    return stop
