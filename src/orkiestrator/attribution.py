"""Post -> follower -> click -> customer attribution.

Followers come from imported daily metrics (no platform API yet). Clicks come from the /r/<link_id> tracker
plus any imported click counts. Customers come from leads marked as clients plus any imported counts.
"""
import csv

from .gate import now


def import_csv(conn, path):
    """CSV columns: post_id, day, followers_gained, clicks, customers, revenue [, first_hour_views]."""
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            conn.execute(
                "INSERT INTO metrics (post_id, day, followers_gained, clicks, customers, revenue) VALUES (?,?,?,?,?,?)",
                (int(r["post_id"]), r["day"], int(r["followers_gained"]), int(r["clicks"]), int(r["customers"]), float(r["revenue"])),
            )
            if (r.get("first_hour_views") or "").strip():
                conn.execute("INSERT INTO hourly (post_id, views, ts) VALUES (?,?,?)", (int(r["post_id"]), int(r["first_hour_views"]), now()))
    conn.commit()


def record_click(conn, link_id):
    """Logs a click only for a link_id that belongs to a post. Returns True if counted."""
    if not conn.execute("SELECT 1 FROM posts WHERE link_id=? AND link_id!=''", (link_id,)).fetchone():
        return False
    conn.execute("INSERT INTO clicks (link_id, ts) VALUES (?,?)", (link_id, now()))
    conn.commit()
    return True


def funnel(conn):
    """Per published post: totals down the funnel, ranked by revenue then customers then clicks."""
    rows = conn.execute(
        "SELECT p.id, p.platform, p.title, p.hook, p.link_id,"
        " COALESCE((SELECT SUM(followers_gained) FROM metrics WHERE post_id=p.id),0) AS followers,"
        " COALESCE((SELECT SUM(clicks) FROM metrics WHERE post_id=p.id),0)"
        "   + (SELECT COUNT(*) FROM clicks c WHERE c.link_id=p.link_id AND p.link_id!='') AS clicks,"
        " COALESCE((SELECT SUM(customers) FROM metrics WHERE post_id=p.id),0)"
        "   + (SELECT COUNT(*) FROM customers u WHERE u.post_id=p.id) AS customers,"
        " COALESCE((SELECT SUM(revenue) FROM metrics WHERE post_id=p.id),0)"
        "   + COALESCE((SELECT SUM(amount) FROM customers u WHERE u.post_id=p.id),0) AS revenue"
        " FROM posts p WHERE p.status='published' ORDER BY revenue DESC, customers DESC, clicks DESC"
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["click_to_customer"] = round(d["customers"] / d["clicks"], 4) if d["clicks"] else 0.0
        out.append(d)
    return out
