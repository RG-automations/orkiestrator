"""The three AI modules as plain functions. `llm` is injected so they can be tested without the API."""
import csv
import json
import re
import threading
from datetime import date

from . import carousel, gate, leads
from .db import ROOT

KNOW = ROOT / "knowledge"
INPUTS = ROOT / "inputs"
BRIEFS = ROOT / "data" / "briefs"
PLATFORMS = ("instagram", "youtube", "linkedin", "tiktok", "facebook")

LANG_NAMES = {"pl": "Polish", "en": "English", "de": "German", "es": "Spanish", "fr": "French"}


def language():
    """`language: pl` in inputs/goals.md (default Polish). Everything the AI writes for the owner and the audience uses it."""
    m = re.search(r"^language:\s*([A-Za-z]{2,})", _read(INPUTS / "goals.md"), re.MULTILINE)
    code = (m.group(1) if m else "pl").lower()
    return LANG_NAMES.get(code, m.group(1) if m else "Polish")


def localized(llm):
    """Wraps an llm callable so every prompt ends with the language order (the last instruction is the one models obey)."""
    def call(sys_prompt, prompt):
        return llm(sys_prompt, f"{prompt}\n\nIMPORTANT: write all text content in {language()}. JSON keys stay in English.")
    return call


def system():
    return (
        "You work for a one-person company. Write in the brand voice. Never invent facts, prices, results or "
        "quotes. No em dashes. Avoid: delve, unlock, game-changer, dive in. If data is missing, say so. "
        f"LANGUAGE: write every text you produce (briefs, hooks, captions, scripts, replies) in {language()}, "
        "even if the input data is in another language. Keep JSON keys exactly as requested, in English."
    )


def _read(path):
    return path.read_text(encoding="utf-8") if path.exists() else ""


def is_placeholder(path):
    text = _read(path)
    return (not text.strip()) or "OWNER:" in text or "SAMPLE DATA" in text


def knowledge_text(folders):
    parts = []
    for folder in folders:
        for f in sorted((KNOW / folder).glob("*.md")):
            parts.append(f"## {folder}/{f.name}\n{_read(f)}")
    return "\n\n".join(parts)


def _json_block(text):
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if not m:
        raise ValueError("Model nie zwrócił listy JSON.")
    return json.loads(m.group(0))


def latest_brief():
    """Most recently written brief (by modification time, not by name: week numbers can sort wrongly)."""
    files = sorted(BRIEFS.glob("*.md"), key=lambda f: f.stat().st_mtime) if BRIEFS.exists() else []
    return files[-1] if files else None


def append_hook_breakdowns(items, path=None):
    """Structured entries: source | first 3 seconds verbatim | formula | why it worked."""
    if not items:
        return
    path = path or KNOW / "hooks" / "hook-breakdowns.md"
    stamp = date.today().isoformat()
    lines = [f"\n## Added {stamp}"]
    for it in items:
        lines.append(f"- source: {it.get('source', '?')} | first 3 seconds: \"{it.get('first_3_seconds', '')}\" | "
                     f"formula: {it.get('formula', '')} | why it worked: {it.get('why', '')}")
    with path.open("a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def run_research(llm):
    llm = localized(llm)
    sources = {f.name: _read(f) for f in sorted(INPUTS.glob("*")) if f.suffix in (".csv", ".md", ".txt")}
    if not sources:
        raise ValueError("Folder inputs/ jest pusty. Wklej dane w zakładce Dane.")
    data = "\n\n".join(f"### {n}\n{t}" for n, t in sources.items()) + "\n\n" + knowledge_text(["audience"])
    prompt = (
        "Write a weekly research brief in markdown from these inputs only. Sections: trends, competitor winners "
        "ranked by views divided by that account's own average (ignore posts at or below 1.0x), top audience "
        "questions with counts, keywords, ranked content ideas with evidence, and a list of missing data. "
        "Describe competitor patterns only, do not quote their wording.\n\n" + data
    )
    text = llm(system(), prompt)
    BRIEFS.mkdir(parents=True, exist_ok=True)
    iso = date.today().isocalendar()
    path = BRIEFS / f"{iso[0]}-W{iso[1]:02d}.md"
    path.write_text(text, encoding="utf-8")

    hooks_prompt = (
        "From the competitor posts below that beat their own account average, return ONLY a JSON array of objects with keys "
        "source (account and post id), first_3_seconds (verbatim from the data), formula (hook pattern name), "
        "why (one sentence on why it worked). Skip posts at or below their average.\n\n" + data
    )
    try:
        append_hook_breakdowns(_json_block(llm(system(), hooks_prompt)))
    except (ValueError, KeyError, TypeError):
        pass  # the brief is the main output; a malformed hooks answer must not lose it
    return path


def compose_body(it):
    """Human-readable text of a structured piece: this is what the owner reads and approves."""
    hooks = sorted(it.get("hooks") or [], key=lambda h: -float(h.get("score", 0)))
    hook = (hooks[0]["text"] if hooks else it.get("hook", "")).strip()
    parts = [hook]
    if it.get("body_text"):
        parts.append(it["body_text"])
    if it.get("script"):
        beats = "\n".join(f"[{b.get('t', '')}] {b.get('say', '')}" + (f"  (on screen: {b['on_screen']})" if b.get("on_screen") else "")
                          for b in it["script"])
        parts.append("SCRIPT\n" + beats)
    if it.get("slides"):
        parts.append("SLIDES\n" + "\n".join(f"{i}. {s.get('title', '')}: {s.get('text', '')}" for i, s in enumerate(it["slides"], 1)))
    if it.get("thumbnail_text"):
        parts.append("THUMBNAIL TEXT: " + it["thumbnail_text"])
    if it.get("caption"):
        parts.append("CAPTION\n" + it["caption"])
    if it.get("facts"):
        parts.append("SOURCES\n" + "\n".join(f"- {f.get('claim', '')} [{f.get('source', '') or 'brak źródła'}]" for f in it["facts"]))
    return "\n\n".join(p for p in parts if p), hook


def brand_check(llm, text):
    """Second AI pass against the brand files. Advisory only: any failure (rate limit, bad JSON) returns no warnings."""
    llm = localized(llm)
    prompt = (
        "Check the post against the brand voice, tone and banned words below. Return ONLY a JSON array of short strings, "
        "one per violation (empty array if it is fine).\n\nPOST\n" + text + "\n\n" + knowledge_text(["voice"])
    )
    try:
        return [str(x) for x in _json_block(llm(system(), prompt))]
    except Exception:  # noqa: BLE001  advisory step must never lose the drafts
        return []


# ---- limits, repeatability and learning from rejections

MAX_DRAFTS = 3
_GENERATION_LOCK = threading.Lock()


class QueueFull(ValueError):
    """Raised when enough drafts already wait for the owner; nothing is generated and no API call is made."""


def waiting_count(conn):
    """Posts (not replies or DMs) waiting for the owner's decision."""
    return conn.execute("SELECT COUNT(*) FROM posts WHERE status='pending_approval' AND archived_at IS NULL "
                        "AND kind NOT IN ('reply','dm')").fetchone()[0]


def _norm_hook(text):
    return re.sub(r"[^\w]+", " ", (text or "").lower()).strip()


def known_hooks(conn):
    """Every hook ever written (any status, archived or not), so a repeated run cannot queue the same idea again."""
    return {_norm_hook(r[0]) for r in conn.execute("SELECT hook FROM posts WHERE hook != ''")}


def recent_rejections(conn, limit=5):
    """The owner's last rejection reasons, newest first, as short lines for the next prompt."""
    rows = conn.execute(
        "SELECT hook, note FROM posts WHERE status='rejected' AND note != '' ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [f"- \"{(r['hook'] or '')[:90]}\": {r['note']}" for r in rows]


# ---- checks that run in code before a draft is saved

_PL_WORDS = set("i w z na się nie że jest dla jak czy po ale co od za tak są być ma tego który już tylko przez bez lub oraz nad pod ich jego "
                "twoje twój masz mam aby gdy kiedy też więc może bardzo każdy firmy klient klienta agencji".split())
_EN_WORDS = set("the and is are of in for you your that with this it be was not but have has if they we our can will how why what when does "
                "from by as at an or just more most all any about into than then them their there been being".split())


def _word_stats(text):
    low = (text or "").lower()
    words = re.findall(r"[a-ząćęłńóśźż]+", low)
    pl = sum(w in _PL_WORDS for w in words) + len(re.findall(r"[ąćęłńóśźż]", low))
    en = sum(w in _EN_WORDS for w in words)
    return len(words), pl, en


def clearly_english(text):
    """True only when the text is evidently English; ambiguous or very short text passes (the check must not block good drafts)."""
    n, pl, en = _word_stats(text)
    return n >= 6 and en >= 4 and en > pl


def _object_block(text):
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise ValueError("Model nie zwrócił obiektu JSON.")
    return json.loads(m.group(0))


def ensure_polish(llm, it):
    """Returns the item in Polish, rewriting it once if it came back in English; None if it is still English after the retry."""
    if language() != "Polish":
        return it
    body, hook = compose_body(it)
    n, pl, en = _word_stats(hook)
    if not clearly_english(body) and not (n >= 4 and en >= 2 and pl == 0):
        return it
    try:
        fixed = _object_block(llm(system(), "Rewrite this JSON object entirely in Polish. Keep every key, number, name, link and "
                                            "source exactly as it is; translate only the human-readable text. Return ONLY the JSON object.\n\n"
                                  + json.dumps(it, ensure_ascii=False)))
    except Exception:  # noqa: BLE001  a failed retry means the draft is dropped, never saved in English
        return None
    fixed.setdefault("platform", it.get("platform"))
    fixed.setdefault("kind", it.get("kind"))
    body2, hook2 = compose_body(fixed)
    return None if clearly_english(body2) else fixed


def source_corpus():
    """Everything a draft is allowed to cite: the latest brief, the owner's inputs and offer files."""
    parts = []
    brief = latest_brief()
    if brief:
        parts.append(_read(brief))
    parts += [_read(f) for f in sorted(INPUTS.glob("*")) if f.suffix in (".csv", ".md", ".txt")]
    parts += [_read(f) for f in sorted((KNOW / "offer").glob("*.md"))]
    return "\n".join(parts)


def source_files():
    """name -> text for every file a draft may cite. Several spellings per file ('faq.md', 'offer/faq.md', 'knowledge/offer/faq.md')."""
    files = {}

    def add(path, extra=()):
        text = _read(path)
        try:
            rel = path.relative_to(ROOT).as_posix().lower()
        except ValueError:
            rel = path.name.lower()
        names = {path.name.lower(), rel, f"{path.parent.name}/{path.name}".lower(), *extra}
        for n in names:
            files[n] = text

    brief = latest_brief()
    if brief:
        add(brief, extra=("brief", "brief tygodnia"))
    for f in sorted(INPUTS.glob("*")):
        if f.suffix in (".csv", ".md", ".txt"):
            add(f)
    for f in sorted((KNOW / "offer").glob("*.md")):
        add(f)
    return files


_URL = re.compile(r"https?://[^\s)\]>\"']+")
_MONEY = re.compile(r"(?:[$€£]\s?\d[\d\s.,]*|\d[\d\s.,]*\s?(?:%|zł|pln|usd|eur|\$|€))", re.IGNORECASE)


def _compact(s):
    return re.sub(r"[\s,]", "", s.lower())


def _claim_text(it):
    """The words the audience will read (no timing marks, no sources block)."""
    parts = [compose_body({**it, "script": [], "slides": [], "facts": []})[0]]
    parts += [f"{s.get('title', '')} {s.get('text', '')}" for s in it.get("slides") or []]
    parts += [f"{b.get('say', '')} {b.get('on_screen', '')}" for b in it.get("script") or []]
    return "\n".join(parts)


def _numbers(text):
    """Numbers as written ('2 500' counts as 2500) plus every plain digit group."""
    out = set()
    for m in re.findall(r"\d+(?:[  ]\d{3})*(?:[.,]\d+)?", text):
        out.add(re.sub(r"[  ]", "", m).replace(",", "."))
    out.update(re.findall(r"\d+", text))
    return out


def _supports(claim, text):
    """True when the cited file really backs the claim: every number in it, and most of its words (by 5-letter stem), appear there."""
    low, compact = text.lower(), _compact(text)
    if any(_compact(n) not in compact for n in re.findall(r"\d[\d ]*\d|\d", claim)):
        return False
    words = [w for w in re.findall(r"[a-ząćęłńóśźż]{4,}", claim.lower())]
    if not words:
        return True
    hit = sum(1 for w in words if w[:5] in low)
    return hit / len(words) >= 0.6


def check_sources(it, corpus, files=None):
    """Returns (blocking, warnings). Links, percentages and amounts that appear nowhere in the data block the draft.
    A fact cites a file name or a link: a link must occur in the data, a file must exist and really contain the claim.
    Anything else, and numbers with no match in the data, only raise a warning for the owner."""
    body, _ = compose_body(it)
    low, compact = corpus.lower(), _compact(corpus)
    files = files or {}
    blocking = []
    for url in {u.rstrip(".,;:!?") for u in _URL.findall(body)}:
        if url.lower() not in low:
            blocking.append(f"link spoza danych: {url}")
    for token in {m.strip() for m in _MONEY.findall(body)}:
        if _compact(token) not in compact:
            blocking.append(f"liczba bez pokrycia w danych: {token}")
    warnings = []
    for f in it.get("facts") or []:
        claim, src = str(f.get("claim", "")), (f.get("source") or "").strip().lower().replace("\\", "/")
        if not src or src == "brak":
            warnings.append(f"fakt bez źródła: {claim[:60]}")
        elif src in low:
            continue  # a link or quote that occurs verbatim in the data
        else:
            names = [n for n in files if n in src]
            if not names:
                warnings.append(f"źródło niezweryfikowane: {claim[:60]}")
            elif not _supports(claim, files[max(names, key=len)]):
                warnings.append(f"źródło nie potwierdza twierdzenia: {claim[:60]}")
    known = _numbers(corpus)
    for n in sorted(_numbers(_claim_text(it)) - known):
        if len(n.replace(".", "")) >= 2:
            warnings.append(f"liczba bez pokrycia w danych: {n}")
    return blocking, warnings


def run_content(conn, llm, actor="agent:content-studio", max_posts=MAX_DRAFTS):
    """Writes at most MAX_DRAFTS drafts. Refuses when drafts already wait; a second click while one run is active is refused too."""
    if not _GENERATION_LOCK.acquire(blocking=False):
        raise ValueError("Generowanie już trwa. Poczekaj na wynik, zamiast klikać ponownie.")
    try:
        return _run_content(conn, localized(llm), actor, max_posts)
    finally:
        _GENERATION_LOCK.release()


def _run_content(conn, llm, actor, max_posts):
    brief = latest_brief()
    if brief is None:
        raise ValueError("Najpierw uruchom Research (brak briefu).")
    waiting = waiting_count(conn)
    room = min(max_posts, MAX_DRAFTS) - waiting
    if room <= 0:
        raise QueueFull(f"W kolejce czeka już {waiting} szkiców do zatwierdzenia. Zatwierdź albo odrzuć je, zanim zlecisz nowe.")
    rejections = recent_rejections(conn)
    prompt = (
        f"From the brief and brand files below draft up to {room} pieces, one per top idea, spread over "
        f"{list(PLATFORMS)}. Mix formats. Return ONLY a JSON array of objects with keys: platform; kind (video, reel, short, "
        "carousel or text_post); hooks (exactly 3 objects {text, score 1-10, formula}, made for this brand, never copied from "
        "competitors); caption; script (videos only: array of {t, say, on_screen, visual} following the video script format in the content files); slides (carousels only: array of {title, text}, "
        "6 to 8 slides); body_text (text posts only); thumbnail_text (youtube only); facts (array of {claim, source}: EVERY statistic, "
        "quote, price, percentage or link used in the text must be listed with an exact source copied from the brief or the inputs "
        "(a file name or a URL that appears there). Do not state any fact you cannot source). Quote only prices that appear in the "
        "pricing file."
        + ("\n\nThe owner rejected earlier drafts for these reasons. Do not repeat these mistakes:\n" + "\n".join(rejections) if rejections else "")
        + f"\n\n## BRIEF\n{_read(brief)}\n\n" + knowledge_text(["voice", "offer", "platforms", "hooks", "content"])
    )
    items = _json_block(llm(system(), prompt))
    corpus, files, seen = source_corpus(), source_files(), known_hooks(conn)
    created, failed = [], []
    for raw in items[:room]:
        if raw.get("platform") not in PLATFORMS:
            failed.append((str(raw.get("hooks", "?"))[:40], "zła platforma"))
            continue
        it = ensure_polish(llm, raw)
        if it is None:
            failed.append((compose_body(raw)[1] or "?", "tekst nie jest po polsku (po jednej poprawce)"))
            continue
        body, hook = compose_body(it)
        if not body.strip():
            failed.append((hook or "?", "pusty tekst"))
            continue
        if _norm_hook(hook) in seen:
            failed.append((hook, "duplikat: taki hak już był"))
            continue
        blocking, source_warnings = check_sources(it, corpus, files)
        if blocking:
            failed.append((hook, "; ".join(blocking)))
            continue
        seen.add(_norm_hook(hook))
        next_id = conn.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM posts").fetchone()[0]
        warnings = brand_check(llm, body) + source_warnings
        pid = gate.add_draft(conn, actor, it["platform"], it.get("kind", "post"), body, hook=hook, brief_ref=str(brief.name),
                             link_id=f"pr-{next_id}", payload=json.dumps(it, ensure_ascii=False), warnings="; ".join(warnings))
        if it.get("slides"):
            carousel.render(pid, it["slides"])
        try:
            gate.transition(conn, pid, "submit", actor)
            created.append(pid)
        except gate.GateError as e:
            failed.append((hook or "?", str(e)))
    return created, failed


def run_replies(conn, llm, actor="agent:posting-community"):
    llm = localized(llm)
    path = INPUTS / "comments.csv"
    if not path.exists():
        raise ValueError("Brak inputs/comments.csv. Wklej komentarze w zakładce Dane.")
    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    worth, escalated = [], []
    for r in rows:
        intent = leads.capture(conn, r["platform"], r["handle"], r["comment"])
        if intent == "negative":
            escalated.append((r, intent))
        elif r["platform"] in PLATFORMS:  # the CSV platform column can be a source type ("blog"), not somewhere we can reply
            worth.append((r, intent))
    if not worth:
        return [], [r for r, _ in escalated], []
    pricing_known = not is_placeholder(KNOW / "offer" / "pricing.md")
    prompt = (
        "Draft replies to the messages below in the brand voice. Skip duplicates and unclear ones. "
        + ("Prices may only come from the pricing file. " if pricing_known else "The pricing file is empty: never quote a price, say details will follow. ")
        + "Return ONLY a JSON array of objects with keys: handle, platform, kind (reply or dm), body.\n\nMESSAGES\n"
        + "\n".join(f"- {r['platform']} {r['handle']} [{i}]: {r['comment']}" for r, i in worth)
        + "\n\n" + knowledge_text(["voice", "community", "offer"])
    )
    created, failed = [], []
    for it in _json_block(llm(system(), prompt)):
        if it.get("platform") not in PLATFORMS:
            failed.append((it.get("handle", "?"), "zła platforma"))
            continue
        pid = gate.add_draft(conn, actor, it["platform"], it.get("kind", "reply"), it["body"], hook=f"reply to {it['handle']}")
        try:
            gate.transition(conn, pid, "submit", actor)
            created.append(pid)
        except gate.GateError as e:
            failed.append((it["handle"], str(e)))
    return created, [r for r, _ in escalated], failed
