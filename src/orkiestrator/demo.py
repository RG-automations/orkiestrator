"""Offline stand-in for an LLM (ORKI_PROVIDER=demo). No network, no key, no cost.

Returns fixed answers shaped like the real prompts expect, so the whole flow (research, drafts, replies,
approval, dry-run publishing) can be shown without any external service. It does not read or judge the
prompt beyond picking the answer type, so the texts are canned, not generated.
"""
import json
import re

BRIEF = """# Brief tygodnia (tryb demo, odpowiedź stała)

## Trendy
- "kawa z kawiarki" rośnie najszybciej w danych z inputs/trends.csv.

## Zwycięzcy konkurencji
- @demo_palarnia_a (instagram, p1): hak o błędzie przy kawiarce, wyraźnie ponad średnią konta.

## Pytania odbiorców
- Ile kosztuje subskrypcja, jaka kawa do kawiarki, skąd jest kawa.

## Pomysły na treści
1. Jeden błąd przy kawiarce i jak go naprawić.
2. Jak czytać opis smaku bez żargonu.
3. Od ziarna do paczki: jak wygląda palenie.

## Brakujące dane
- Wyniki własnych postów (import metryk).
"""

HOOKS = [{
    "source": "@demo_palarnia_a p1",
    "first_3_seconds": "Twoja kawiarka nie jest zepsuta. Robisz jeden błąd.",
    "formula": "mistake warning",
    "why": "Zdejmuje winę ze sprzętu i obiecuje jedną prostą poprawkę.",
}]

ITEMS = [
    {
        "platform": "instagram", "kind": "carousel",
        "hooks": [{"text": "Kawa z kawiarki wychodzi gorzka? Zmień jedną rzecz.", "score": 8, "formula": "mistake warning"},
                  {"text": "Nie kupuj nowej kawiarki, zanim to sprawdzisz.", "score": 7, "formula": "contrarian claim"},
                  {"text": "Gorzka kawa to zwykle wina mielenia.", "score": 6, "formula": "secret revealed"}],
        "slides": [{"title": "Gorzka kawa?", "text": "Zwykle to nie wina kawiarki."},
                   {"title": "Sprawdź mielenie", "text": "Za drobne mielenie daje goryczkę."},
                   {"title": "Sprawdź ogień", "text": "Mały płomień, kawa ma się nie gotować."},
                   {"title": "Zdejmij w porę", "text": "Gdy kawa zaczyna syczeć, zdejmij kawiarkę."},
                   {"title": "Wybierz palenie", "text": "Średnie palenie jest najbardziej wyrozumiałe."},
                   {"title": "Nie wiesz, od czego zacząć?", "text": "Napisz w komentarzu KAWA, a podpowiemy, od której zacząć."}],
        "caption": "Zanim kupisz nową kawiarkę, sprawdź te cztery rzeczy. Napisz w komentarzu KAWA, a podpowiemy, od której kawy zacząć.",
        "facts": [],
    },
    {
        "platform": "facebook", "kind": "text_post",
        "hooks": [{"text": "Opis smaku na paczce brzmi jak zagadka? Oto tłumaczenie.", "score": 8, "formula": "question the audience already asks"},
                  {"text": "Nuty bergamotki, czyli herbata z cytryną.", "score": 7, "formula": "before/after"},
                  {"text": "Nie musisz znać żargonu, żeby wybrać dobrą kawę.", "score": 6, "formula": "contrarian claim"}],
        "body_text": ("Na paczkach kawy piszemy o smaku po ludzku. Kwiatowa znaczy lekka i delikatna. Czekoladowa znaczy pełna "
                      "i łagodna. Owocowa znaczy rześka, trochę jak sok.\n\nJaką kawę pijesz najczęściej? Napisz w komentarzu, "
                      "podpowiemy jedną na start."),
        "facts": [],
    },
    {
        "platform": "tiktok", "kind": "video",
        "hooks": [{"text": "Tak wygląda kawa, zanim trafi do Twojej paczki.", "score": 7, "formula": "behind the scenes"},
                  {"text": "Zielone ziarno pachnie jak groszek. Serio.", "score": 7, "formula": "secret revealed"},
                  {"text": "Palenie kawy w małej palarni od środka.", "score": 5, "formula": "behind the scenes"}],
        "script": [{"t": "0-3", "say": "Zielone ziarno kawy pachnie jak groszek.", "on_screen": "Tak, jak groszek", "visual": "dłoń z zielonymi ziarnami"},
                   {"t": "3-10", "say": "Dopiero palenie zmienia je w kawę, którą znasz.", "on_screen": "Palenie zmienia wszystko", "visual": "ziarna w bębnie palarki"},
                   {"t": "10-18", "say": "Palimy małe partie, żeby kawa jechała do Ciebie świeża.", "on_screen": "Małe partie", "visual": "ziarna stygną na sicie"},
                   {"t": "18-25", "say": "Napisz w komentarzu KAWA, a podpowiemy, od której zacząć.", "on_screen": "Napisz KAWA", "visual": "paczka kawy na stole"}],
        "caption": "Od zielonego ziarna do paczki. Napisz KAWA, a podpowiemy, od której zacząć.",
        "facts": [],
    },
]

REPLIES = {
    "lead": "Dzięki za pytanie! Subskrypcja to 49 zł za paczkę 250 g co dwa tygodnie, wstrzymasz ją w każdej chwili. Czym parzysz kawę? Podpowiemy pierwszą paczkę.",
    "collab": "Dziękujemy za wiadomość! Przekażemy ją właścicielowi, który odezwie się w sprawie współpracy.",
    "other": "Dzięki za komentarz! Jeśli chcesz, podpowiemy kawę do Twojej metody parzenia.",
}
MESSAGE = re.compile(r"^- (\S+) (\S+) \[(\w+)\]: ", re.MULTILINE)


def llm(system, prompt):
    if "weekly research brief" in prompt:
        return BRIEF
    if "From the competitor posts below" in prompt:
        return json.dumps(HOOKS, ensure_ascii=False)
    if "draft up to" in prompt:
        m = re.search(r"draft up to (\d+)", prompt)
        return json.dumps(ITEMS[: int(m.group(1)) if m else len(ITEMS)], ensure_ascii=False)
    if "Draft replies" in prompt:
        return json.dumps([{"handle": h, "platform": p, "kind": "reply", "body": REPLIES.get(i, REPLIES["other"])}
                           for p, h, i in MESSAGE.findall(prompt)], ensure_ascii=False)
    if "Rewrite this JSON object" in prompt:
        m = re.search(r"\{.*\}", prompt, re.DOTALL)
        return m.group(0) if m else "{}"
    return "[]"  # brand check and anything else: no warnings
