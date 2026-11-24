"""One-page morning summary."""
from datetime import date

from . import attribution


def render(conn, day=None):
    day = day or date.today().isoformat()
    lines = [f"# Daily brief {day}", ""]

    from . import interrupts
    alerts = interrupts.open_alerts(conn)
    lines.append(f"## Alerts ({len(alerts)})")
    lines += [f"- [{a['priority']}] {a['text']}" for a in alerts[:8]] or ["- None."]
    lines.append("")

    pending = conn.execute(
        "SELECT id, platform, kind, hook FROM posts WHERE status = 'pending_approval' ORDER BY id"
    ).fetchall()
    lines.append(f"## Needs your approval ({len(pending)})")
    lines += [f"- #{p['id']} [{p['platform']}/{p['kind']}] {p['hook'] or '(no hook)'}" for p in pending] or ["- Nothing waiting."]

    leads = conn.execute("SELECT platform, handle, message FROM leads WHERE status = 'new' ORDER BY id").fetchall()
    lines += ["", f"## New leads ({len(leads)})"]
    lines += [f"- {l['handle']} on {l['platform']}: {l['message']}" for l in leads] or ["- None."]

    funnel = attribution.funnel(conn)
    lines += ["", "## Money, not likes (post -> follower -> click -> customer)"]
    if funnel:
        lines.append("| Post | Platform | Followers | Clicks | Customers | Revenue |")
        lines.append("|---|---|---|---|---|---|")
        for f in funnel[:5]:
            lines.append(f"| #{f['id']} {f['hook'] or f['title']} | {f['platform']} | {f['followers']} | {f['clicks']} | {f['customers']} | {f['revenue']:.2f} |")
    else:
        lines.append("No published posts with metrics yet.")
    return "\n".join(lines) + "\n"
