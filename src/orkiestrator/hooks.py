"""Hook scoring and A/B comparison, judged by customers and revenue, not likes."""
from .attribution import funnel
from .db import ROOT
from .gate import now

MIN_CLICKS = 20
START, END = "<!-- generated:start -->", "<!-- generated:end -->"


def score(conn):
    """Returns (repeat, retire): hooks to repeat (made money) and to retire (traffic but no customers, or flopped)."""
    rows = funnel(conn)
    flops = {r[0] for r in conn.execute("SELECT p.id FROM posts p JOIN hourly h ON h.post_id=p.id WHERE h.views < 100")}
    repeat = [r for r in rows if r["revenue"] > 0 or r["customers"] > 0]
    retire = [r for r in rows if r not in repeat and (r["clicks"] >= MIN_CLICKS or r["id"] in flops)]
    return repeat, retire


def write_winning_patterns(conn, path=None):
    path = path or ROOT / "knowledge" / "hooks" / "winning-patterns.md"
    repeat, retire = score(conn)
    lines = [START, f"Updated {now()}", "", "## Repeat (brought customers)"]
    lines += [f"- {r['hook'] or r['title']} ({r['platform']}): {r['customers']} customers, {r['revenue']:.2f} revenue" for r in repeat] or ["- Not enough data yet."]
    lines += ["", "## Retire (traffic or reach without customers)"]
    lines += [f"- {r['hook'] or r['title']} ({r['platform']}): {r['clicks']} clicks, 0 customers" for r in retire] or ["- None."]
    lines.append(END)
    block = "\n".join(lines)
    text = path.read_text(encoding="utf-8") if path.exists() else "# Winning patterns\n"
    if START in text and END in text:
        text = text[: text.index(START)] + block + text[text.index(END) + len(END):]
    else:
        text = text.rstrip() + "\n\n" + block + "\n"
    path.write_text(text, encoding="utf-8")
    return path


def create_experiment(conn, name, post_a, post_b):
    cur = conn.execute("INSERT INTO experiments (name, post_a, post_b, created_at) VALUES (?,?,?,?)", (name, post_a, post_b, now()))
    conn.commit()
    return cur.lastrowid


def compare(conn, exp_id):
    """Returns ('a'|'b'|'tie'|'not enough data', details). Winner by revenue, then customers, then clicks."""
    e = conn.execute("SELECT * FROM experiments WHERE id=?", (exp_id,)).fetchone()
    if e is None:
        raise ValueError(f"experiment {exp_id} not found")
    by_id = {r["id"]: r for r in funnel(conn)}
    a, b = by_id.get(e["post_a"]), by_id.get(e["post_b"])
    if not a or not b or min(a["clicks"], b["clicks"]) < MIN_CLICKS:
        return "not enough data", (a, b)

    def key(r):
        return (r["revenue"], r["customers"], r["clicks"])

    if key(a) == key(b):
        return "tie", (a, b)
    return ("a" if key(a) > key(b) else "b"), (a, b)
