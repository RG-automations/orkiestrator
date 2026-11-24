# Orkiestrator: zespół social media dla jednej osoby

AI robi research, pisze treści, przygotowuje odpowiedzi, pilnuje alertów i raportuje. **Nic nie wychodzi publicznie, dopóki sam nie klikniesz „Zatwierdź".**

## Jak tego używać (5 minut)

1. **Jednorazowo:** zainstaluj Pythona 3.10+ i ustaw klucz AI w zmiennej środowiskowej: `GEMINI_API_KEY` (klucz z aistudio.google.com/api-keys) albo `ANTHROPIC_API_KEY` (Claude). Najwygodniej: skopiuj `local.env.example` jako `local.env` i wpisz klucz, `start.bat` wczyta go sam. Jeśli są oba, wygrywa Gemini; wymusisz wybór przez `ORKI_PROVIDER=claude`.
2. **Uruchom:** dwuklik na `start.bat`. W terminalu pojawi się adres z tokenem i otworzy się przeglądarka. Token widzisz tylko Ty, nie jest nigdzie zapisany.
3. **Strona Start prowadzi krok po kroku:**
   1. *Dane marki*: oferta, cennik, głos, cele.
   2. *Dane wejściowe*: trendy, konkurencja, komentarze (CSV).
   3. *Research*, 4. *Tydzień treści*, 5. *Odpowiedzi*: jeden przycisk każdy.
   6. *Zatwierdź*: w zakładce „Do zatwierdzenia" czytasz, poprawiasz, klikasz.
   
   Albo jednym przyciskiem **Pętla poranna** (alerty, research, treści, odpowiedzi, raport). Uruchamia się też sama codziennie o godzinie z `inputs/goals.md` (`morning: 08:00`), dopóki panel działa.
4. **Zatwierdzanie ma dwa przyciski:** *Zatwierdź* planuje post na najbliższą dobrą porę (godziny w `knowledge/platforms/<platforma>-rules.md`, linia `slots:`), *Zatwierdź i opublikuj teraz* publikuje od razu. Odpowiedzi na komentarze i DM-y idą od razu.

   **Kolejka i archiwum.** Zakładka „Do zatwierdzenia” pokazuje tylko to, co czeka na Twoją decyzję. Zaplanowane, opublikowane, odrzucone i szkice są w osobnych zakładkach, a „Archiwizuj” chowa gotowe pozycje do Archiwum (z możliwością przywrócenia, nic nie jest kasowane). Odrzucając post, wybierasz powód („po angielsku”, „zbyt ogólne”, „zły ton”, „nie moja teza”), a ostatnie 5 powodów trafia do następnego zapytania do AI. Leady i komentarze chowasz przyciskiem „Gotowe, ukryj”.

   **Limit szkiców.** „Napisz tydzień” tworzy najwyżej 3 szkice naraz i nie ruszy, gdy 3 lub więcej już czeka (odpowiedzi na komentarze się nie liczą). Powtórne kliknięcie podczas pracy jest odrzucane, a ten sam hak nie wejdzie do kolejki drugi raz. Przy błędzie limitu API (429) aplikacja ponawia zapytanie po 8, 20 i 45 sekundach.

   **Sprawdzanie przed zapisem szkicu.** Tekst wyraźnie po angielsku jest raz przepisywany po polsku, a jeśli nadal nie jest, szkic w ogóle nie trafia do kolejki. Link, procent albo kwota, których nie ma w Twoich danych, blokują szkic. Fakt z nieweryfikowalnym źródłem zostaje, ale z żółtym ostrzeżeniem.

   **Kopie zapasowe.** Przed zmianą schematu bazy (np. po aktualizacji) powstaje kopia w `data/backups/`. Dane oznaczone jako testowe (`is_test`) usuwa przycisk „Usuń dane testowe”.

## Co dostajesz

| Moduł | Co robi |
|---|---|
| Research | Brief tygodnia z Twoich danych, ranking konkurencji względem własnej średniej konta, pytania odbiorców, rozbicie haków (pierwsze 3 s i dlaczego działa) do `knowledge/hooks/hook-breakdowns.md` |
| Treści | Do 7 materiałów na tydzień: 3 warianty haka z oceną, podpis, scenariusz z czasem i tekstem na ekranie, karuzele jako pliki SVG 1080x1350, tekst miniatury, kontrola marki (stały filtr stylu + ostrzeżenia AI) |
| Posting i community | Harmonogram po godzinach per platforma, odpowiedzi w głosie marki, wykrywanie pytań o cenę (lead), negatywnych komentarzy i propozycji współpracy |
| Alerty | Skok trendu, post konkurencji, pytanie o cenę, negatywny komentarz, współpraca, skok kliknięć, flop w pierwszej godzinie (progi w `knowledge/reporting/kpi-model.md`) |
| Lejek | Własne linki `/r/<id>` liczące kliknięcia, leady ze statusami, „to klient" z kwotą, raport post → obserwujący → kliknięcie → klient, scoring haków (powtórz / wycofaj) i porównanie A/B |
| Powiadomienia | Telegram lub e-mail o tym, że coś czeka. Wiadomość ma tylko link do panelu, zatwierdzać można wyłącznie w panelu |

## Zmienne środowiskowe (wszystkie opcjonalne poza pierwszą)
`GEMINI_API_KEY` lub `ANTHROPIC_API_KEY` (przyciski AI), `FB_PAGE_ID` + `FB_PAGE_TOKEN` (prawdziwa publikacja na stronie Facebook), `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` albo `SMTP_HOST` + `SMTP_TO` (+ `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM`), `ELEVENLABS_API_KEY` + `ELEVENLABS_VOICE_ID` (lektor), `ORKI_MODEL` (zmiana modelu; domyślnie `gemini-flash-latest` dla Gemini, `claude-sonnet-5-5` dla Claude), `ORKI_PROVIDER` (`gemini` albo `claude`).
W `inputs/goals.md` możesz dodać linie `morning: 08:00` i `offer_url: https://twoja-oferta` (dokąd przekierowuje `/r/<id>`).

## Publikacja: co jest prawdziwe
- **Facebook (strona):** prawdziwy adapter. Włączasz go świadomie: ustaw `FB_PAGE_ID` i `FB_PAGE_TOKEN`, uruchom panel, na Start wpisz `OPUBLIKUJ NAPRAWDE`. Tryb live resetuje się przy każdym restarcie.
- **Instagram, YouTube, LinkedIn, TikTok:** na razie tryb próbny. Zatwierdzony post zapisuje się w `data/outbox/`. Adapter to jeden plik (patrz niżej).
- Adapter Facebooka jest przetestowany na atrapie serwera, **nie** na prawdziwym API. Pierwszą publikację zrób na testowej stronie.
- Kliknięcia `/r/<id>` liczą się naprawdę tylko wtedy, gdy panel jest osiągalny z internetu (tunel lub hosting). Lokalnie działa do testów.

## Czego nie ma (świadomie)
Edycja wideo i klipy, b-roll, ads manager, miniatury jako obrazy, Notion/Airtable jako baza (jest SQLite), liczba obserwujących z API platform (wpisujesz ją w CSV wyników), publikacja na Instagram/YouTube/LinkedIn/TikTok. Jakość tekstów od AI oceń sam przed zatwierdzeniem: ostrzeżenia kontroli marki są tylko podpowiedzią.

## Dla inżynierów
```
start.bat / python -m orkiestrator serve   panel (127.0.0.1, token w terminalu, cookie HttpOnly, SameSite=Strict)
src/orkiestrator/gate.py        jedyna maszyna stanów: draft > pending_approval > approved > scheduled > published | rejected
src/orkiestrator/web.py         panel; approve/reject tylko z sesją właściciela; jedyna publiczna ścieżka to GET /r/<link_id>
src/orkiestrator/brain.py       pętla poranna w kolejności priorytetów, etapy izolowane, codzienny wyzwalacz
src/orkiestrator/interrupts.py  reguły alertów (czyste funkcje, progi z kpi-model.md)
src/orkiestrator/scheduler.py   sloty z linii `slots:` w knowledge/platforms/*.md
src/orkiestrator/publisher.py   publikuje tylko to, co bramka oznaczyła jako zatwierdzone przez człowieka; pętla w tle co 60 s
src/orkiestrator/pipeline.py    research / content / replies (llm wstrzykiwany, testowalny bez API)
src/orkiestrator/attribution.py, hooks.py, leads.py, lint.py, carousel.py, voiceover.py, notify.py, ai.py (Gemini i Claude)
src/orkiestrator/adapters/      Publisher; DryRunPublisher + FacebookPublisher
knowledge/ inputs/ data/        wiedza marki, dane wejściowe, stan (kolejka, briefy, raporty, outbox, assets)
.claude/ CLAUDE.md              tryb pracy w Claude Code (subagenci); agenci mają zakaz approve/reject
tests/                          python -m unittest discover -s tests
```
**Gwarancje:** `approve` i `reject` wymagają aktora `human:`. `schedule` i `publish` ponownie sprawdzają zapisane zatwierdzenie, więc podrobiona kolumna `status` nie wystarczy. Edycja posta unieważnia zatwierdzenie. Każdy `submit` przechodzi filtr stylu. Prawdziwy adapter wymaga kluczy oraz jawnego potwierdzenia w panelu. Każde przejście jest w tabeli `events`.

Nowy adapter: zaimplementuj `Publisher.publish(post)` i zwróć go z `get_publisher` w `adapters/__init__.py`.
CLI nadal działa (`python -m orkiestrator list|add|submit|brief|lead ...`); `approve` w CLI wymaga interaktywnego terminala.
