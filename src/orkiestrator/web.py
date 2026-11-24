"""Local web panel for the owner. Approval is a click here; only a session holding the owner's token can do it.

The token is printed once in the terminal where the owner started the server. It is never written to disk,
so an agent that only has file access cannot approve anything.
"""
import getpass
import hmac
import html
import json
import os
import re
import secrets
import threading
import webbrowser
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import adapters, ai, attribution, brain, brief, carousel, cleanup, gate, interrupts, leads, pipeline, publisher, scheduler, voiceover
from .db import ROOT, connect

SETUP_FILES = {
    "offer": ("Oferta (co sprzedajesz, komu, jedno wezwanie do działania)", ROOT / "knowledge/offer/offer.md"),
    "pricing": ("Cennik (tylko ceny, które wolno podawać)", ROOT / "knowledge/offer/pricing.md"),
    "faq": ("Najczęstsze pytania i Twoje odpowiedzi", ROOT / "knowledge/offer/faq.md"),
    "voice": ("Głos marki (jak brzmisz, przykłady)", ROOT / "knowledge/voice/voice-guide.md"),
    "goals": ("Cele i możliwości (np. ile postów tygodniowo)", ROOT / "inputs/goals.md"),
}
INPUT_FILES = {
    "trends.csv": "Trendy (kolumny: source,topic,week_growth_pct,note)",
    "competitors.csv": "Konkurencja (account,platform,post_id,views,account_avg_views,first_3_seconds,format)",
    "comments.csv": "Komentarze i wiadomości (platform,post_ref,handle,comment)",
}
STATUS_PL = {"draft": "szkic", "pending_approval": "czeka na Ciebie", "approved": "zatwierdzony",
             "scheduled": "zaplanowany", "published": "opublikowany", "rejected": "odrzucony"}

REJECT_REASONS = ("po angielsku", "zbyt ogólne", "zły ton", "nie moja teza")
LIVE_STATUSES = "archived_at IS NULL"
# tab key -> (label, SQL condition). Only the first tab needs the owner's decision.
QUEUE_TABS = {
    "pending": ("Do zatwierdzenia", "status='pending_approval' AND archived_at IS NULL"),
    "scheduled": ("Zaplanowane", "status IN ('approved','scheduled') AND archived_at IS NULL"),
    "published": ("Opublikowane", "status='published' AND archived_at IS NULL"),
    "rejected": ("Odrzucone", "status='rejected' AND archived_at IS NULL"),
    "draft": ("Szkice", "status='draft' AND archived_at IS NULL"),
    "archive": ("Archiwum", "archived_at IS NOT NULL"),
}

CSS = """
body{font:16px/1.5 system-ui,sans-serif;margin:0;background:#f5f5f7;color:#1d1d1f}
header{background:#1d1d1f;padding:12px 24px;display:flex;gap:18px;flex-wrap:wrap}
header a{color:#fff;text-decoration:none;opacity:.8}header a:hover{opacity:1}
main{max-width:900px;margin:24px auto;padding:0 16px}
.card{background:#fff;border-radius:10px;padding:16px 20px;margin:14px 0;box-shadow:0 1px 3px #0002}
.step{display:flex;gap:12px;align-items:flex-start}.dot{font-size:22px}
button,.btn{background:#0a66c2;color:#fff;border:0;border-radius:8px;padding:9px 16px;font-size:15px;cursor:pointer;text-decoration:none;display:inline-block}
button.ok{background:#1a7f37}button.no{background:#b42318}button.gray{background:#666}
textarea{width:100%;min-height:140px;font:14px/1.4 ui-monospace,monospace;box-sizing:border-box}
.tag{font-size:12px;border-radius:99px;padding:2px 10px;background:#e8e8ed}.msg{background:#fff4ce;padding:10px 14px;border-radius:8px}
.err{background:#fde7e9}pre{white-space:pre-wrap;margin:0;font:inherit}small{color:#666}
"""


def esc(s):
    return html.escape(str(s if s is not None else ""))


def page(title, body, msg="", error=False):
    note = f'<div class="msg{" err" if error else ""}">{esc(msg)}</div>' if msg else ""
    nav = "".join(f'<a href="{h}">{t}</a>' for h, t in [
        ("/", "Start"), ("/brief", "Brief"), ("/alerts", "Alerty"), ("/queue", "Do zatwierdzenia"), ("/setup", "Dane marki"),
        ("/inputs", "Dane wejściowe"), ("/leads", "Leady"), ("/report", "Raport")])
    return (f"<!doctype html><html lang=pl><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{esc(title)} | Orkiestrator</title><style>{CSS}</style><header>{nav}</header><main>{note}{body}</main></html>")


def public_url(port):
    """Panel address used in notifications (no token). In Docker set ORKI_PUBLIC_URL, e.g. http://panel.example.lan/."""
    return os.environ.get("ORKI_PUBLIC_URL") or f"http://127.0.0.1:{port}/"


def make_server(port=8765, llm=None, outbox=None, token=None, db_path=None, host=None):
    token = token or os.environ.get("ORKI_TOKEN") or secrets.token_urlsafe(24)
    owner = "human:" + getpass.getuser()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        # ---- plumbing
        def conn(self):
            return connect(db_path)

        def authed(self):
            c = SimpleCookie(self.headers.get("Cookie", ""))
            return "orki" in c and hmac.compare_digest(c["orki"].value, token)

        def send(self, body, code=200, headers=()):
            data = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            for k, v in headers:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def redirect(self, to, headers=()):
            self.send_response(303)
            self.send_header("Location", to)
            for k, v in headers:
                self.send_header(k, v)
            self.end_headers()

        def form(self):
            n = int(self.headers.get("Content-Length") or 0)
            return {k: v[0] for k, v in parse_qs(self.rfile.read(n).decode("utf-8")).items()}

        def deny(self):
            self.send(page("Dostęp", "<div class=card><h2>Brak dostępu</h2><p>Otwórz adres z tokenem wypisany w terminalu, "
                           "w którym uruchomiłeś panel. Tylko właściciel go widzi.</p></div>"), 401)

        # ---- routing
        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            m = re.fullmatch(r"/r/([A-Za-z0-9_-]{1,64})", u.path)
            if m:  # public on purpose: counts a click, redirects to the fixed offer URL.
                return self.track(m.group(1))
            if u.path == "/healthz":  # public on purpose: uptime monitors. Returns no data.
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                return self.wfile.write(b"ok")
            if "token" in q:
                if hmac.compare_digest(q["token"][0], token):
                    return self.redirect("/", [("Set-Cookie", f"orki={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=7776000")])
                return self.deny()
            if not self.authed():
                return self.deny()
            m = re.fullmatch(r"/assets/(\d+)/([A-Za-z0-9_.-]+)", u.path)
            if m:
                return self.asset(int(m.group(1)), m.group(2))
            msg = q.get("msg", [""])[0]
            self.tab = q.get("tab", ["pending"])[0]
            err = q.get("err", [""])[0] == "1"
            routes = {"/": self.page_home, "/queue": self.page_queue, "/setup": self.page_setup, "/inputs": self.page_inputs,
                      "/leads": self.page_leads, "/report": self.page_report, "/alerts": self.page_alerts, "/brief": self.page_brief}
            fn = routes.get(u.path)
            if fn is None:
                return self.send(page("404", "<div class=card>Nie ma takiej strony.</div>"), 404)
            self.send(fn(msg, err))

        def track(self, link_id):
            conn = self.conn()
            try:
                counted = attribution.record_click(conn, link_id)
            finally:
                conn.close()
            goals = SETUP_FILES["goals"][1]
            m = re.search(r"^offer_url:\s*(https?://\S+)", goals.read_text(encoding="utf-8") if goals.exists() else "", re.MULTILINE)
            if m and counted:
                return self.redirect(m.group(1))
            self.send(page("Link", "<div class=card>Ten link nie jest jeszcze skonfigurowany.</div>"), 404 if not counted else 200)

        def asset(self, post_id, name):
            path = carousel.assets_dir(post_id) / name
            if not path.is_file():
                return self.send("not found", 404)
            kind = {".svg": "image/svg+xml", ".mp3": "audio/mpeg"}.get(path.suffix)
            if kind is None:
                return self.send("not found", 404)
            data = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            if not self.authed():
                return self.deny()
            origin = self.headers.get("Origin")
            if origin and urlparse(origin).netloc != self.headers.get("Host"):
                return self.send("bad origin", 403)
            path = urlparse(self.path).path
            f = self.form()
            conn = self.conn()
            try:
                m = re.fullmatch(r"/(approve|publish-now|reject|edit)/(\d+)", path)
                if m:
                    action, pid = m.group(1), int(m.group(2))
                    if action in ("approve", "publish-now"):
                        row = conn.execute("SELECT platform, kind FROM posts WHERE id=?", (pid,)).fetchone()
                        if row is None:
                            raise gate.GateError(f"post {pid} not found")
                        gate.transition(conn, pid, "approve", owner)
                        immediate = action == "publish-now" or row["kind"] in ("reply", "dm")
                        when = gate.now() if immediate else scheduler.next_slot(row["platform"], taken=scheduler.taken_slots(conn, row["platform"]))
                        gate.transition(conn, pid, "schedule", "agent:scheduler", scheduled_at=when)
                        done = publisher.run_due(conn, outbox=outbox) if immediate else []
                        mode = "LIVE" if publisher.is_live(row["platform"]) else "tryb próbny, zapis w data/outbox"
                        if immediate:
                            return self.back("/queue", f"Zatwierdzone i opublikowane ({len(done)}; {mode}).")
                        return self.back("/queue", f"Zatwierdzone. Zaplanowane na {when} ({mode}).")
                    if action == "reject":
                        reason, extra = f.get("reason", "").strip(), f.get("note", "").strip()
                        gate.transition(conn, pid, "reject", owner, note=": ".join(x for x in (reason, extra) if x))
                        return self.back("/queue", "Odrzucone. Powód trafi do następnych szkiców." if reason or extra else "Odrzucone.")
                    gate.edit_body(conn, pid, owner, f.get("body", ""))
                    gate.transition(conn, pid, "submit", owner)
                    return self.back("/queue", "Zapisano zmianę. Zatwierdzenie trzeba dać ponownie.")
                m = re.fullmatch(r"/(archive|unarchive|requeue)/(\d+)", path)
                if m:
                    action, pid = m.group(1), int(m.group(2))
                    if action == "archive":
                        gate.archive(conn, pid, owner)
                        return self.back("/queue?tab=rejected", "Przeniesione do archiwum.")
                    if action == "unarchive":
                        gate.unarchive(conn, pid, owner)
                        return self.back("/queue?tab=archive", "Przywrócone z archiwum.")
                    gate.transition(conn, pid, "submit", owner)
                    return self.back("/queue", "Wróciło do zatwierdzenia.")
                m = re.fullmatch(r"/alert-done/(\d+)", path)
                if m:
                    interrupts.dismiss(conn, int(m.group(1)))
                    return self.back("/alerts", "Alert zamknięty.")
                m = re.fullmatch(r"/lead/(\d+)/(archive|unarchive)", path)
                if m:
                    (leads.archive_lead if m.group(2) == "archive" else leads.unarchive_lead)(conn, int(m.group(1)))
                    return self.back("/leads", "Gotowe." if m.group(2) == "archive" else "Przywrócone.")
                m = re.fullmatch(r"/lead/(\d+)/(status|client)", path)
                if m:
                    lid = int(m.group(1))
                    if m.group(2) == "status":
                        leads.set_status(conn, lid, f.get("status", ""))
                        return self.back("/leads", "Status zmieniony.")
                    try:
                        amount = float((f.get("amount") or "").replace(",", "."))
                    except ValueError:
                        raise ValueError("Podaj kwotę jako liczbę, np. 490.")
                    leads.mark_client(conn, lid, amount)
                    return self.back("/leads", f"Zapisano klienta ({amount:.2f}). Sprzedaż trafiła do posta, który go przyprowadził.")
                m = re.fullmatch(r"/voiceover/(\d+)", path)
                if m:
                    pid = int(m.group(1))
                    row = conn.execute("SELECT body, hook, payload FROM posts WHERE id=?", (pid,)).fetchone()
                    if row is None:
                        raise ValueError("Nie ma takiego posta.")
                    script = " ".join(b.get("say", "") for b in json.loads(row["payload"] or "{}").get("script", [])) or row["hook"]
                    voiceover.generate(pid, script)
                    return self.back("/queue", "Lektor wygenerowany (odsłuchaj przy poście).")
                if path == "/live":
                    if f.get("confirm", "").strip() != "OPUBLIKUJ NAPRAWDE":
                        raise ValueError("Wpisz dokładnie: OPUBLIKUJ NAPRAWDE")
                    if not (os.environ.get("FB_PAGE_ID") and os.environ.get("FB_PAGE_TOKEN")):
                        raise ValueError("Brak FB_PAGE_ID i FB_PAGE_TOKEN. Ustaw je przed startem.")
                    adapters.LIVE["confirmed"] = True
                    return self.back("/", "Tryb LIVE włączony dla Facebooka do restartu. Zatwierdzone posty będą naprawdę publikowane.")
                if path == "/live-off":
                    adapters.LIVE["confirmed"] = False
                    return self.back("/", "Tryb live wyłączony. Wracasz do trybu próbnego.")
                if path == "/morning-loop":
                    res = brain.morning_loop(conn, llm or ai.default_llm, panel_url=public_url(self.server.server_address[1]))
                    return self.back("/", res["summary"].replace("\n", " | "), bool(res["failures"]))
                if path == "/cleanup":
                    manifest = cleanup.load() or {}
                    if not any(cleanup.preview(conn, manifest).values()):
                        raise ValueError("Brak danych testowych do usunięcia.")
                    res = cleanup.run(conn, manifest)
                    return self.back("/queue", f"Usunięto dane testowe: posty {res['posts']}, leady {res['leads']}, wiadomości {res['inbound']}, "
                                               f"pliki i foldery {res['paths']}. Kopia bazy: {res['backup']}.")
                if path.startswith("/setup/"):
                    key = path.split("/", 2)[2]
                    SETUP_FILES[key][1].write_text(f.get("text", ""), encoding="utf-8")
                    return self.back("/setup", "Zapisane.")
                if path.startswith("/inputs/"):
                    name = path.split("/", 2)[2]
                    if name not in INPUT_FILES:
                        return self.send("nope", 404)
                    (ROOT / "inputs" / name).write_text(f.get("text", ""), encoding="utf-8")
                    return self.back("/inputs", f"Zapisano {name}.")
                if path == "/metrics":
                    tmp = ROOT / "data" / "metrics_upload.csv"
                    tmp.write_text(f.get("text", ""), encoding="utf-8")
                    attribution.import_csv(conn, tmp)
                    return self.back("/report", "Wyniki zaimportowane.")
                if path.startswith("/generate/"):
                    return self.generate(conn, path.rsplit("/", 1)[1])
                self.send("nope", 404)
            except (gate.GateError, ValueError, KeyError, RuntimeError, ai.MissingKey, voiceover.NotConfigured, OSError) as e:
                self.back("/queue" if "/queue" in (self.headers.get("Referer") or "") else "/", str(e), True)
            finally:
                conn.close()

        def back(self, to, msg, err=False):
            from urllib.parse import quote
            self.redirect(f"{to}{'&' if '?' in to else '?'}msg={quote(msg)}" + ("&err=1" if err else ""))

        def generate(self, conn, what):
            call = llm or ai.default_llm
            if what == "research":
                p = pipeline.run_research(call)
                return self.back("/brief", f"Gotowe: brief zapisany ({p.name}). Przeczytaj go, potem wróć na Start i kliknij krok 4.")
            if what == "content":
                ok, bad = pipeline.run_content(conn, call)
                note = f"Dodano {len(ok)} szkiców do zatwierdzenia." + (
                    " Pominięte: " + "; ".join(f"{(h or '?')[:45]} ({why})" for h, why in bad[:3]) + "." if bad else "")
                return self.back("/queue", note)
            if what == "replies":
                ok, esc_, bad = pipeline.run_replies(conn, call)
                note = f"Dodano {len(ok)} odpowiedzi do zatwierdzenia. Do Twojej decyzji (negatywne, bez odpowiedzi): {len(esc_)}."
                return self.back("/queue", note)
            if what == "alerts":
                new = interrupts.store(conn, interrupts.evaluate(conn))
                return self.back("/alerts", f"Nowych alertów: {len(new)}.")
            self.send("nope", 404)

        # ---- pages
        def page_home(self, msg, err):
            conn = self.conn()
            n = lambda s: conn.execute("SELECT COUNT(*) FROM posts WHERE status=?", (s,)).fetchone()[0]
            pending, published = n("pending_approval"), n("published")
            brand_ok = not pipeline.is_placeholder(pipeline.KNOW / "offer" / "offer.md")
            has_inputs = any((ROOT / "inputs").glob("*.csv"))
            has_brief = pipeline.latest_brief() is not None
            open_alerts = interrupts.open_alerts(conn)
            conn.close()

            def step(done, num, title, text, action=""):
                return (f"<div class='card step'><div class=dot>{'✅' if done else num}</div><div><b>{title}</b><br>"
                        f"<small>{text}</small><br><br>{action}</div></div>")

            def gen(what, label):
                return f"<form method=post action=/generate/{what}><button>{label}</button></form>"

            body = "<h1>Twój zespół social media</h1><p>Idź od góry do dołu. Nic nie wyjdzie publicznie, dopóki nie klikniesz „Zatwierdź”.</p>"
            live = adapters.LIVE["confirmed"]
            if live:
                body += ("<div class='card msg err'><b>TRYB LIVE: Facebook publikuje naprawdę.</b> "
                         "<form method=post action=/live-off style='display:inline'><button class=no>Wyłącz</button></form></div>")
            top = f"<b>{len(open_alerts)} alertów czeka</b>" if open_alerts else "Brak alertów"
            body += (f"<div class=card>{top} <a class=btn href=/alerts>Zobacz alerty</a> "
                     "<form method=post action=/morning-loop style='display:inline'><button class=gray>Uruchom pętlę poranną</button></form>"
                     "<br><small>Pętla poranna: alerty, research, treści, odpowiedzi i raport w jednym kliknięciu. "
                     "Działa też sama codziennie o godzinie z inputs/goals.md (morning: 08:00), gdy panel jest uruchomiony.</small></div>")
            body += step(brand_ok, "1️⃣", "Uzupełnij dane marki", "Co sprzedajesz, ceny i jak brzmisz. Bez tego AI nie ma na czym pracować.", "<a class=btn href=/setup>Uzupełnij</a>")
            body += step(has_inputs, "2️⃣", "Wklej dane wejściowe", "Trendy, konkurencja, komentarze (eksporty lub ręcznie).", "<a class=btn href=/inputs>Wklej dane</a>")
            body += step(has_brief, "3️⃣", "Research", "AI robi brief tygodnia z Twoich danych.", gen("research", "Uruchom research"))
            body += step(pending + published > 0, "4️⃣", "Tydzień treści", "AI pisze posty na podstawie briefu i wrzuca je do kolejki.", gen("content", "Napisz tydzień treści"))
            body += step(False, "5️⃣", "Odpowiedzi na komentarze", "AI wyłapuje pytania o cenę (leady) i pisze odpowiedzi do zatwierdzenia.", gen("replies", "Przygotuj odpowiedzi"))
            body += step(published > 0, "6️⃣", f"Zatwierdź ({pending} czeka)", "Przeczytaj, popraw i kliknij Zatwierdź. Tylko Ty to robisz.", "<a class=btn href=/queue>Przejdź do kolejki</a>")
            if not live:
                body += ("<div class=card><b>Publikacja: tryb próbny</b><br><small>Zatwierdzone posty zapisują się w data/outbox/. "
                         "Żeby naprawdę publikować na stronie Facebook: ustaw FB_PAGE_ID i FB_PAGE_TOKEN, uruchom ponownie i potwierdź poniżej.</small>"
                         "<form method=post action=/live><input name=confirm placeholder='wpisz: OPUBLIKUJ NAPRAWDE'> <button class=no>Włącz tryb live</button></form></div>")
            return page("Start", body, msg, err)

        def page_queue(self, msg, err):
            tab = getattr(self, "tab", "pending")
            if tab not in QUEUE_TABS:
                tab = "pending"
            conn = self.conn()
            counts = {key: conn.execute(f"SELECT COUNT(*) FROM posts WHERE {where}").fetchone()[0] for key, (_, where) in QUEUE_TABS.items()}
            rows = conn.execute(f"SELECT * FROM posts WHERE {QUEUE_TABS[tab][1]} ORDER BY id DESC" if tab != "pending"
                                else f"SELECT * FROM posts WHERE {QUEUE_TABS[tab][1]} ORDER BY id").fetchall()
            manifest = cleanup.load() or {}
            body = "<h1>Kolejka</h1>"
            try:
                found = cleanup.preview(conn, manifest)
            except ValueError as e:
                found = {}
                body += f"<div class='card msg err'>Lista danych testowych jest nieprawidłowa i nic nie zostanie usunięte: {esc(e)}</div>"
            conn.close()
            if any(found.values()):
                ids = ", ".join(f"#{i}" for i in found["posts"])
                body += ("<div class='card msg err'><b>Dane testowe do usunięcia</b><br><small>"
                         f"Posty: {esc(ids) or 'brak'}<br>Leady: {len(found['leads'])}, wiadomości: {len(found['inbound'])}<br>"
                         f"Pliki i foldery: {esc(', '.join(found['paths'])) or 'brak'}<br>"
                         "Zostaje wszystko inne (m.in. pozostałe posty, aktualny brief, dane wejściowe). "
                         "Przed usunięciem powstaje kopia bazy w data/backups/.</small><br><br>"
                         "<form method=post action=/cleanup><button class=no>Usuń dane testowe</button></form></div>")
            body += "<p>" + " ".join(
                f"<a class='btn{'' if key == tab else ' gray'}' href='/queue?tab={key}'>{label} ({counts[key]})</a>"
                for key, (label, _) in QUEUE_TABS.items()) + "</p>"
            reject_options = "<option value=''>(bez powodu)</option>" + "".join(f"<option>{esc(r)}</option>" for r in REJECT_REASONS)

            def reject_form(pid):
                return (f"<form method=post action=/reject/{pid} style='display:inline'><select name=reason>{reject_options}</select> "
                        f"<input name=note placeholder='dopisek (opcjonalnie)'> <button class=no>Odrzuć</button></form>")

            for r in rows:
                head = f"<b>#{r['id']} {esc(r['platform'])} / {esc(r['kind'])}</b> <span class=tag>{STATUS_PL[r['status']]}</span>"
                if r["is_test"]:
                    head += " <span class=tag style='background:#fde7e9'>TEST</span>"
                if r["status"] == "scheduled":
                    head += f" <small>zaplanowany na {esc(r['scheduled_at'])}</small>"
                if r["status"] == "rejected" and r["note"]:
                    head += f"<br><small>Powód odrzucenia: {esc(r['note'])}</small>"
                if r["warnings"]:
                    head += f"<br><span class=tag style='background:#fff4ce'>Uwaga: {esc(r['warnings'])}</span>"
                media = ""
                try:
                    pl = json.loads(r["payload"] or "{}")
                except ValueError:
                    pl = {}
                for svg in sorted(carousel.assets_dir(r["id"]).glob("slide-*.svg")):
                    media += f"<img src=/assets/{r['id']}/{svg.name} width=150 style='margin:4px;border:1px solid #ddd'> "
                if (carousel.assets_dir(r["id"]) / "voiceover.mp3").exists():
                    media += f"<br><audio controls src=/assets/{r['id']}/voiceover.mp3></audio>"
                if r["status"] == "pending_approval":
                    if r["kind"] in ("video", "reel", "short") and voiceover.available():
                        media += f"<form method=post action=/voiceover/{r['id']}><button class=gray>Wygeneruj lektora</button></form>"
                    form = (f"<form method=post action=/edit/{r['id']}><textarea name=body>{esc(r['body'])}</textarea>"
                            f"<br><button class=gray>Zapisz poprawkę</button></form>"
                            f"<form method=post action=/approve/{r['id']} style='display:inline'><button class=ok>Zatwierdź (zaplanuj na najlepszą porę)</button></form> "
                            f"<form method=post action=/publish-now/{r['id']} style='display:inline'><button class=ok>Zatwierdź i opublikuj teraz</button></form> "
                            + reject_form(r["id"]))
                else:
                    form = f"<pre>{esc(r['body'])}</pre><br>"
                    if r["archived_at"]:
                        form += f"<form method=post action=/unarchive/{r['id']}><button class=gray>Przywróć z archiwum</button></form>"
                    elif r["status"] == "rejected":
                        form += (f"<form method=post action=/archive/{r['id']} style='display:inline'><button class=gray>Archiwizuj</button></form> "
                                 f"<form method=post action=/requeue/{r['id']} style='display:inline'><button class=gray>Przywróć do zatwierdzenia</button></form>")
                    elif r["status"] == "published":
                        form += f"<form method=post action=/archive/{r['id']}><button class=gray>Archiwizuj</button></form>"
                    elif r["status"] in ("scheduled", "approved", "draft"):
                        form += reject_form(r["id"])
                body += f"<div class=card>{head}<br><i>{esc(r['hook'])}</i><br>{media}<br>{form}</div>"
            if not rows:
                empty = {"pending": "Nic nie czeka na Twoją decyzję. Na Start możesz napisać kolejne szkice (krok 4).",
                         "archive": "Archiwum jest puste."}.get(tab, "Brak pozycji w tej zakładce.")
                body += f"<div class=card>{empty}</div>"
            return page("Kolejka", body, msg, err)

        def page_setup(self, msg, err):
            body = "<h1>Dane marki</h1><p>Wypełnij prostym tekstem. Teksty z „OWNER:” to szablony do zastąpienia.</p>"
            for key, (label, path) in SETUP_FILES.items():
                text = path.read_text(encoding="utf-8") if path.exists() else ""
                body += (f"<div class=card><b>{esc(label)}</b><form method=post action=/setup/{key}>"
                         f"<textarea name=text>{esc(text)}</textarea><br><button>Zapisz</button></form></div>")
            return page("Dane marki", body, msg, err)

        def page_inputs(self, msg, err):
            body = "<h1>Dane wejściowe</h1><p>Wklej zawartość CSV (pierwsza linia to nagłówki).</p>"
            for name, label in INPUT_FILES.items():
                p = ROOT / "inputs" / name
                text = p.read_text(encoding="utf-8") if p.exists() else ""
                body += (f"<div class=card><b>{esc(name)}</b><br><small>{esc(label)}</small><form method=post action=/inputs/{name}>"
                         f"<textarea name=text>{esc(text)}</textarea><br><button>Zapisz</button></form></div>")
            return page("Dane wejściowe", body, msg, err)

        def page_leads(self, msg, err):
            conn = self.conn()
            rows = conn.execute("SELECT * FROM leads WHERE archived_at IS NULL ORDER BY id DESC").fetchall()
            conn.close()
            labels = {"new": "nowy", "replied": "odpowiedziano", "booked": "umówiony", "client": "klient", "lost": "stracony"}
            body = "<h1>Leady</h1>"
            for r in rows:
                opts = "".join(f"<option value={s}{' selected' if s == r['status'] else ''}>{labels[s]}</option>" for s in leads.STATUSES if s != "client")
                client = (f"<form method=post action=/lead/{r['id']}/client style='display:inline'><input name=amount size=8 placeholder='kwota'> "
                          f"<button class=ok>To klient</button></form>") if r["status"] != "client" else "<b>Klient ✅</b>"
                body += (f"<div class=card><b>{esc(r['handle'])}</b> <span class=tag>{esc(r['platform'])}</span> "
                         f"<span class=tag>{labels[r['status']]}</span><br>{esc(r['message'])}<br><br>"
                         f"<form method=post action=/lead/{r['id']}/status style='display:inline'><select name=status>{opts}</select> "
                         f"<button class=gray>Zmień status</button></form> {client} "
                         f"<form method=post action=/lead/{r['id']}/archive style='display:inline'><button class=gray>Gotowe, ukryj</button></form></div>")
            return page("Leady", body if rows else "<h1>Leady</h1><div class=card>Brak leadów.</div>", msg, err)

        def page_brief(self, msg, err):
            b = pipeline.latest_brief()
            hb = pipeline.KNOW / "hooks" / "hook-breakdowns.md"
            body = "<h1>Brief tygodnia</h1>"
            if b is None:
                body += "<div class=card>Nie ma jeszcze briefu. Na Start kliknij „Uruchom research”.</div>"
            else:
                body += f"<div class=card><small>{esc(b.name)}</small><br><br><pre>{esc(b.read_text(encoding='utf-8'))}</pre></div>"
            if hb.exists():
                body += f"<div class=card><b>Rozbicie haków</b><br><small>knowledge/hooks/hook-breakdowns.md</small><br><br><pre>{esc(hb.read_text(encoding='utf-8'))}</pre></div>"
            return page("Brief", body, msg, err)

        def page_alerts(self, msg, err):
            conn = self.conn()
            rows = interrupts.open_alerts(conn)
            conn.close()
            pl = {"highest": "najwyższy", "high": "wysoki", "medium": "średni"}
            body = ("<h1>Alerty</h1><p><small>Co przerywa pętlę: skok trendu, post konkurencji, pytanie o cenę, "
                    "negatywny komentarz, współpraca, skok kliknięć, flop w pierwszej godzinie. Progi w knowledge/reporting/kpi-model.md.</small></p>"
                    "<form method=post action=/generate/alerts><button>Odśwież alerty</button></form>")
            for r in rows:
                body += (f"<div class=card><span class=tag>{pl[r['priority']]}</span> <span class=tag>{esc(r['route'])}</span><br>{esc(r['text'])}<br><br>"
                         f"<form method=post action=/alert-done/{r['id']}><button class=gray>Załatwione</button></form></div>")
            if not rows:
                body += "<div class=card>Brak otwartych alertów.</div>"
            return page("Alerty", body, msg, err)

        def page_report(self, msg, err):
            conn = self.conn()
            text = brief.render(conn)
            conn.close()
            body = (f"<h1>Raport</h1><div class=card><pre>{esc(text)}</pre></div><div class=card><b>Wklej wyniki</b><br>"
                    "<small>CSV: post_id,day,followers_gained,clicks,customers,revenue</small><form method=post action=/metrics>"
                    "<textarea name=text></textarea><br><button>Importuj</button></form></div>")
            return page("Raport", body, msg, err)

    srv = ThreadingHTTPServer((host or os.environ.get("ORKI_HOST") or "127.0.0.1", port), H)
    srv.token = token
    return srv


def serve(port=8765, open_browser=True):
    if os.environ.get("ORKI_HOST", "127.0.0.1") not in ("127.0.0.1", "localhost") and not os.environ.get("ORKI_TOKEN"):
        # Panel wystawiony w sieci (Docker) z losowym tokenem wypisalby go do `docker logs`.
        raise SystemExit("ORKI_HOST wystawia panel w sieci, ale brak ORKI_TOKEN. Ustaw ORKI_TOKEN (GH Secret).")
    srv = make_server(port)
    if os.environ.get("ORKI_TOKEN"):
        # Token z env (Docker): nie wypisujemy go, bo trafilby do `docker logs`.
        url = public_url(srv.server_address[1])
        print(f"\nOrkiestrator działa: {url} (otwórz z ?token=<ORKI_TOKEN> z menedżera haseł)\n")
        open_browser = False
    else:
        url = f"http://127.0.0.1:{srv.server_address[1]}/?token={srv.token}"
        print(f"\nOrkiestrator działa. Otwórz (token tylko dla Ciebie):\n  {url}\nCtrl+C aby zakończyć.\n")
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    stop = publisher.start_loop()
    daily_stop = start_daily_loop(public_url(srv.server_address[1]))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        daily_stop.set()


def start_daily_loop(panel_url, interval=60, llm=None, db_path=None, clock=None):
    """Runs the Brain's morning loop once per day after the configured time, while the panel is running."""
    stop = threading.Event()
    state = {"last": None, "warned": False}

    def tick():
        from datetime import datetime
        now = (clock or datetime.now)()
        if not brain.due_now(now, state["last"]):
            return
        state["last"] = now.date()
        conn = connect(db_path)
        try:
            res = brain.morning_loop(conn, llm or ai.default_llm, panel_url=panel_url)
            print("[poranna pętla]", res["summary"])
        except ai.MissingKey as e:
            if not state["warned"]:
                print(f"[poranna pętla] pominięta: {e}")
                state["warned"] = True
        except Exception as e:  # noqa: BLE001
            print(f"[poranna pętla] błąd: {e}")
        finally:
            conn.close()

    def loop():
        while not stop.wait(interval):
            tick()

    threading.Thread(target=loop, daemon=True).start()
    stop.tick = tick  # exposed so tests can drive one iteration without sleeping
    return stop
