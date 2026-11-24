"""Picks the next publishing slot per platform from the `slots:` line in knowledge/platforms/<p>-rules.md."""
import re
from datetime import datetime, timedelta, timezone

from .db import ROOT

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DEFAULT_SLOTS = ([0, 1, 2, 3, 4], [(10, 0)])
_LINE = re.compile(r"^slots:\s*([a-z]{3})(?:-([a-z]{3}))?\s+(.+)$", re.MULTILINE | re.IGNORECASE)


def parse_slots(text):
    """'slots: mon-fri 11:00, 19:00' -> ([0..4], [(11,0),(19,0)]). Falls back to a default."""
    m = _LINE.search(text or "")
    if not m:
        return DEFAULT_SLOTS
    a = DAYS.index(m.group(1).lower())
    b = DAYS.index((m.group(2) or m.group(1)).lower())
    days = list(range(a, b + 1)) if a <= b else list(range(a, 7)) + list(range(0, b + 1))
    times = []
    for part in m.group(3).split(","):
        hh, mm = part.strip().split(":")
        times.append((int(hh), int(mm)))
    return days, sorted(times)


def slots_for(platform):
    path = ROOT / "knowledge" / "platforms" / f"{platform}-rules.md"
    return parse_slots(path.read_text(encoding="utf-8") if path.exists() else "")


def next_slot(platform, after=None, taken=(), horizon_days=21):
    """Earliest future slot for the platform that is not in `taken` (iso strings). Times are local, returned as UTC iso."""
    after = after or datetime.now(timezone.utc)
    local_after = after.astimezone()
    days, times = slots_for(platform)
    taken = set(taken)
    for offset in range(horizon_days):
        day = (local_after + timedelta(days=offset)).replace(second=0, microsecond=0)
        if day.weekday() not in days:
            continue
        for hh, mm in times:
            cand = day.replace(hour=hh, minute=mm)
            if cand <= local_after:
                continue
            iso = cand.astimezone(timezone.utc).isoformat(timespec="seconds")
            if iso not in taken:
                return iso
    raise ValueError(f"no free slot for {platform} in {horizon_days} days")


def taken_slots(conn, platform):
    return [r[0] for r in conn.execute(
        "SELECT scheduled_at FROM posts WHERE platform=? AND scheduled_at IS NOT NULL AND status IN ('scheduled','approved')",
        (platform,))]
