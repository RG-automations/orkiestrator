"""The Brain: runs the morning loop in priority order and ends with one summary.

Order: refresh alerts first (they set the priorities), then research, content, replies, then the brief.
Each stage is isolated so one failure does not stop the rest; the summary says what ran and what failed.
Nothing here can publish: every output is a draft waiting for the owner.
"""
import re
from datetime import date, datetime

from . import brief, hooks, interrupts, notify, pipeline
from .db import ROOT


def morning_time(goals_path=None):
    """'morning: 08:00' in inputs/goals.md, default 08:00."""
    path = goals_path or ROOT / "inputs" / "goals.md"
    m = re.search(r"^morning:\s*(\d{1,2}):(\d{2})", path.read_text(encoding="utf-8") if path.exists() else "", re.MULTILINE)
    return (int(m.group(1)), int(m.group(2))) if m else (8, 0)


def morning_loop(conn, llm, panel_url="", report_dir=None):
    steps, failures = [], []

    def stage(name, fn):
        try:
            steps.append(f"{name}: {fn()}")
        except Exception as e:  # noqa: BLE001  isolate stages on purpose
            failures.append(f"{name}: {e}")

    new_alerts = interrupts.store(conn, interrupts.evaluate(conn))
    steps.append(f"Alerty: {len(new_alerts)} nowych")
    routes = {a["route"] for a in interrupts.open_alerts(conn) if a["priority"] in ("highest", "high")}

    # High-priority alerts pull their module to the front; otherwise the default order applies.
    def research():
        return f"brief {pipeline.run_research(llm).name}"

    def content():
        try:
            ok, bad = pipeline.run_content(conn, llm)
        except pipeline.QueueFull as e:  # not a failure: the owner simply has enough to decide on
            return f"pominięto, {e}"
        return f"{len(ok)} szkiców do zatwierdzenia" + (f", {len(bad)} odrzuconych kontrolą stylu" if bad else "")

    def replies():
        ok, esc, bad = pipeline.run_replies(conn, llm)
        return f"{len(ok)} odpowiedzi do zatwierdzenia, {len(esc)} do Twojej decyzji"

    stages = [("Research", research), ("Treści", content), ("Odpowiedzi", replies)]
    if "posting-community" in routes:
        stages.insert(0, stages.pop(2))
    for name, fn in stages:
        stage(name, fn)

    stage("Hooki", lambda: str(hooks.write_winning_patterns(conn).name))
    text = brief.render(conn)
    out = report_dir or ROOT / "data" / "reports"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{date.today().isoformat()}.md"
    path.write_text(text, encoding="utf-8")

    pending = conn.execute("SELECT COUNT(*) FROM posts WHERE status='pending_approval'").fetchone()[0]
    summary = "\n".join(["Poranna pętla zakończona."] + steps + [f"BŁĄD {f}" for f in failures]
                        + [f"Czeka na Twoje zatwierdzenie: {pending}. Raport: {path.name}"])
    notify.send(summary, panel_url)
    return {"summary": summary, "report": path, "failures": failures, "steps": steps}


def due_now(now=None, last_run_day=None):
    """True once per day after the configured morning time."""
    now = now or datetime.now()
    hh, mm = morning_time()
    return now.date() != last_run_day and (now.hour, now.minute) >= (hh, mm)
