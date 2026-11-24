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
    published = []
    for post in due_posts(conn, as_of):
        ref = get_publisher(post["platform"], outbox).publish(post)
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
