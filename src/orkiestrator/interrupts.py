"""What interrupts the loop: turns data into prioritised alerts routed to the right module.

Rules from the diagram: trend spike, competitor post blows up, "how much?", link-click spike,
negative comment, brand DM, post flops in the first hour. Thresholds live in knowledge/reporting/kpi-model.md.
"""
import csv
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .db import ROOT
from .gate import now

DEFAULTS = {"trend_growth_pct": 100.0, "competitor_multiple": 5.0, "click_spike_multiple": 3.0, "first_hour_views": 100.0}
PRIORITY_ORDER = {"highest": 0, "high": 1, "medium": 2}


@dataclass(frozen=True)
class Alert:
    kind: str
    priority: str
    route: str
    text: str
    ref: str


def thresholds(path=None):
    path = path or ROOT / "knowledge" / "reporting" / "kpi-model.md"
    out = dict(DEFAULTS)
    if path.exists():
        m = re.search(r"^thresholds:\s*(.+)$", path.read_text(encoding="utf-8"), re.MULTILINE)
        if m:
            for pair in m.group(1).split(","):
                k, _, v = pair.strip().partition("=")
                if k in out:
                    out[k] = float(v)
    return out


def _rows(path):
    return list(csv.DictReader(path.open(encoding="utf-8"))) if path.exists() else []


def _num(value):
    """First number in a messy cell: '99000 (480 likes)' -> 99000, '~45000' -> 45000, 'brak danych' -> None."""
    m = re.search(r"\d[\d,]*(?:\.\d+)?", str(value or ""))
    return float(m.group(0).replace(",", "")) if m else None


def _is_night(ts):
    # shortcut: 21:00-05:00 UTC (about 23:00-07:00 in Poland), ignores the winter hour shift
    h = datetime.fromisoformat(ts).astimezone(timezone.utc).hour
    return h >= 21 or h < 5


def evaluate(conn, inputs_dir=None, th=None, as_of=None):
    inputs_dir = inputs_dir or ROOT / "inputs"
    th = th or thresholds()
    as_of = as_of or datetime.now(timezone.utc)
    alerts = []

    for r in _rows(inputs_dir / "trends.csv"):
        growth = _num(r.get("week_growth_pct"))
        if growth is not None and growth >= th["trend_growth_pct"]:
            alerts.append(Alert("trend_spike", "high", "research",
                                f"Skok trendu: \"{r['topic']}\" +{r['week_growth_pct']}% ({r['source']}). Dodaj do briefu i napisz materiał.",
                                f"{r['source']}:{r['topic']}"))

    for r in _rows(inputs_dir / "competitors.csv"):
        views, avg = _num(r.get("views")), _num(r.get("account_avg_views"))
        if views is None or not avg:
            continue  # unknown or zero average: no basis for "beat its own average"
        mult = views / avg
        if mult >= th["competitor_multiple"]:
            alerts.append(Alert("competitor_win", "high", "research",
                                f"Post konkurenta {r['account']} ({r['platform']}) {mult:.1f}x ponad swoją średnią. Rozbij hak: \"{r['first_3_seconds']}\".",
                                f"{r['account']}:{r['post_id']}"))

    for r in conn.execute("SELECT * FROM inbound WHERE archived_at IS NULL ORDER BY id"):
        who = f"{r['handle']} ({r['platform']})"
        ref = str(r["id"])
        if r["intent"] == "lead":
            night = " To nocne zapytanie: szkic czeka na Twoje zatwierdzenie, nic nie wyjdzie samo." if _is_night(r["created_at"]) else ""
            alerts.append(Alert("buy_intent", "highest", "posting-community", f"Pytanie o zakup od {who}: \"{r['message']}\". Odpowiedz teraz.{night}", ref))
        elif r["intent"] == "negative":
            alerts.append(Alert("negative_comment", "high", "posting-community",
                                f"Negatywny komentarz od {who}: \"{r['message']}\". Zdecyduj, czy odpowiadać (agent nie odpisze sam).", ref))
        elif r["intent"] == "collab":
            alerts.append(Alert("collab", "medium", "posting-community", f"Propozycja współpracy od {who}: \"{r['message']}\".", ref))

    day_ago = (as_of - timedelta(hours=24)).isoformat(timespec="seconds")
    for r in conn.execute(
        "SELECT link_id, SUM(ts >= ?) AS last24, SUM(ts < ?) AS prev, MIN(ts) AS first FROM clicks GROUP BY link_id", (day_ago, day_ago)
    ):
        days_before = max(1.0, (as_of - datetime.fromisoformat(r["first"])).total_seconds() / 86400 - 1)
        baseline = max(1.0, (r["prev"] or 0) / days_before)
        if r["last24"] >= 5 and r["last24"] >= th["click_spike_multiple"] * baseline:
            alerts.append(Alert("click_spike", "medium", "growth",
                                f"Skok kliknięć w link {r['link_id']}: {r['last24']} w 24 h (zwykle ok. {baseline:.0f}/dzień). Sprawdź, który post go napędza.",
                                f"{r['link_id']}:{as_of.date()}"))

    for r in conn.execute(
        "SELECT p.id, p.platform, p.hook, h.views FROM posts p JOIN hourly h ON h.post_id = p.id "
        "WHERE p.status='published' AND h.views < ?", (th["first_hour_views"],)
    ):
        alerts.append(Alert("first_hour_flop", "medium", "growth",
                            f"Flop w pierwszej godzinie: #{r['id']} ({r['platform']}) {r['views']} wyświetleń. Wycofaj hak \"{r['hook']}\".",
                            str(r["id"])))

    alerts.sort(key=lambda a: PRIORITY_ORDER[a.priority])
    return alerts


def store(conn, alerts):
    """Inserts new alerts (deduplicated by kind+ref). Returns the ones that were new."""
    new = []
    for a in alerts:
        cur = conn.execute(
            "INSERT OR IGNORE INTO alerts (kind, priority, route, text, ref, created_at) VALUES (?,?,?,?,?,?)",
            (a.kind, a.priority, a.route, a.text, a.ref, now()),
        )
        if cur.rowcount:
            new.append(a)
    conn.commit()
    return new


def open_alerts(conn):
    rows = conn.execute("SELECT * FROM alerts WHERE status='open'").fetchall()
    return sorted(rows, key=lambda r: (PRIORITY_ORDER[r["priority"]], -r["id"]))


def dismiss(conn, alert_id):
    conn.execute("UPDATE alerts SET status='done' WHERE id=?", (alert_id,))
    conn.commit()
