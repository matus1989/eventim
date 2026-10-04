"""Wspolne narzedzia testowe.

Warianty danych dostępności **nie są** osobnymi plikami fixture, tylko
sa generowane z prawdziwego JSON-LD zapisanego w ``tests/fixtures/``.

Powod: osobny fixture z wpisanym recznie ``InStock`` z czasem rozjedzie
sie z prawdziwymi danymi i przestanie je testowac. Wygenerowanie wariantu
z prawdziwego payloadu gwarantuje, ze testy zmieniaja **tylko** stan
dostepnosci, a struktura zawsze pozostaje taka, jak na zywo.
"""

from __future__ import annotations

import json
import pathlib
import re
from typing import Any

import pytest
import responses

FIXTURES = pathlib.Path(__file__).parent / "fixtures"

_LD_JSON_RE = re.compile(
    r'<script[^>]*type\s*=\s*["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.S | re.I,
)

#: Szkielet HTML uzywany przez testy. Odwzorowuje strukture prawdziwej strony,
#: ale jest maly - pelna strona sklepu ma ~152 KB i nie wnosi nic do testow.
HTML_TEMPLATE = (
    "<!DOCTYPE html>\n"
    '<html lang="de">\n'
    "<head>\n"
    '  <meta charset="utf-8">\n'
    "  <title>Tickets | Ticket-Shop</title>\n"
    "</head>\n"
    "<body>\n"
    '  <script type="application/ld+json">\n{ld}\n  </script>\n'
    "</body>\n"
    "</html>\n"
)


def make_html(ld: Any, *, template: str = HTML_TEMPLATE) -> str:
    """Buduje fragment strony z danymi JSON-LD."""
    return template.format(ld=json.dumps(ld, ensure_ascii=False, indent=2))


def ld_from_file(name: str) -> dict:
    """Wczytuje prawdziwy JSON-LD z fixture HTML."""
    html = (FIXTURES / name).read_text(encoding="utf-8")
    match = _LD_JSON_RE.search(html)
    assert match, f"fixture {name} nie zawiera bloku JSON-LD"
    return json.loads(match.group(1))


def set_availability(ld: dict, states: list[str | None]) -> dict:
    """Zwraca kopie payloadu z podmienionymi stanami dostępności.

    ``states`` działa pozycyjnie na ``subEvent``. ``None`` oznacza **usunięcie**
    pola ``availability``, czyli scenariusz anomalii markupu.
    """
    clone = json.loads(json.dumps(ld))
    for sub_event, state in zip(_series(clone)["subEvent"], states):
        offers = sub_event.get("offers") or {}
        if state is None:
            offers.pop("availability", None)
        else:
            offers["availability"] = state
        sub_event["offers"] = offers
    return clone


def _series(ld: dict) -> dict:
    """Zwraca wezel ``EventSeries`` z payloadu."""
    for node in ld.get("@graph", []):
        if node.get("@type") == "EventSeries":
            return node
    raise AssertionError("fixture nie zawiera wezla EventSeries")


def without_field(ld: dict, *path: str) -> dict:
    """Zwraca kopie payloadu z usunietym polem (``*path`` to sciezka kropkowa)."""
    clone = json.loads(json.dumps(ld))
    cursor: Any = clone
    for key in path[:-1]:
        cursor = cursor[key]
    cursor.pop(path[-1], None)
    return clone


def set_sub_events(ld: dict, value: Any) -> dict:
    """Zwraca kopie payloadu z podmieniona lista ``subEvent``.

    ``value`` moze byc lista, czymkolwiek nie-lista (scenariusz odpornosci)
    albo ``None`` - wtedy pole jest usuwane.
    """
    clone = json.loads(json.dumps(ld))
    series = _series(clone)
    if value is None:
        series.pop("subEvent", None)
    else:
        series["subEvent"] = value
    return clone


# --- atrapa Telegrama -------------------------------------------------------

#: Token fikcyjny. Format zgodny z walidacja `Config` (`<id>:<secret>`),
#: wiec konfiguracja przechodzi bez dodatkowych zabiegow.
TELEGRAM_TOKEN = "1234567890:AAHkQ1exampleTOKENvalue_do_not_use_1"

TELEGRAM_CHAT = "-1001234567890"

TELEGRAM_SEND = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"


def register_telegram(*, status: int = 200, body: object = None) -> None:
    """Rejestruje odpowiedz API Telegrama.

    ``body`` jako obiekt wyjatku powoduje jego rzucenie — do symulacji zerwanego
    polaczenia bez pytania o kod HTTP.
    """
    responses.post(
        TELEGRAM_SEND,
        status=status,
        json=body if body is not None else {"ok": True, "result": {}},
    )


def register_telegram_ok() -> None:
    register_telegram(status=200)


def register_telegram_refused() -> None:
    """Zerwane polaczenie z API.

    Rzuca ``requests.ConnectionError``, a nie dowolny wyjatek — inaczej
    program potraktowalby to jako awarie programu, nie jako ponowienia
    warty, ktora test ma wlasnie sprawdzic.
    """
    import requests as _requests

    responses.post(
        TELEGRAM_SEND,
        body=_requests.ConnectionError("polaczenie zerwane"),
    )


def telegram_attempts() -> list[str]:
    """Treści wszystkich **prób** wysłania, włącznie z odrzuconymi przez atrape.

    Do sprawdzania, że program w ogóle próbował — nie do stwierdzenia, że
    wiadomość doszła. Do tego drugiego służy :func:`telegram_sends`.
    """
    import json as _json

    teksty: list[str] = []
    for call in responses.calls:
        if "api.telegram.org" in call.request.url:
            teksty.append(_body_text(call.request.body))
    return teksty


def telegram_sends() -> list[str]:
    """Teksty faktycznie **przyjęte** przez Telegrama, w kolejności.

    Czyta ciała żądań z ``responses`` zamiast z pamięci programu, więc test
    potwierdza to, co **wyszłoby** w sieci, a nie to, co kod twierdzi, że
    wysłał. Odrzucone próby (brak atrapy, błąd API) nie są zliczane.
    """
    teksty: list[str] = []
    for call in responses.calls:
        if "api.telegram.org" not in call.request.url:
            continue
        odpowiedz = getattr(call, "response", None)
        if odpowiedz is None or not odpowiedz.ok:
            continue
        teksty.append(_body_text(call.request.body))
    return teksty


def _body_text(raw: object) -> str:
    """Wyciąga pole ``text`` z ciała żądania."""
    import json as _json

    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    try:
        return str(_json.loads(raw)["text"])
    except (ValueError, KeyError, TypeError):  # pragma: no cover - diagnostyka
        return f"<nieparsowalne cialo: {raw!r}>"


def shop_page(stany: list[str | None], *, name: str = "Test") -> str:
    """Buduje stronę sklepu o podanych stanach dostępności.

    ``None`` w ``stany`` oznacza **usunięcie** pola ``availability``, czyli
    scenariusz anomalii markupu (ADR-9).
    """
    ld = set_availability(ld_from_file("shop_soldout.html"), stany)
    return make_html(ld)


# --- atrapa pieciohopowego handshake Queue-it -------------------------------

#: Adres sklepu uzywany w testach. Zgodny z domyslnym probe'a, zeby ladunek
#: testowy i rzeczywisty cel pokrywaly sie.
TARGET = "https://www.eventim-light.com/de/a/org/s/series"

QUEUE_HOST = "eventimlight.queue-it.net"

QUEUE_ENTRY_URL = (
    f"https://{QUEUE_HOST}/"
    "?c=eventimlight&e=shopde&t=https%3A%2F%2Fwww.eventim-light.com%2Fs"
    "&tsr=1&tsh=2"
)

RELOAD_URL = (
    "https://eventimlight.queue-it.net/"
    "?c=eventimlight&e=shopde&t=https%3A%2F%2Fwww.eventim-light.com%2Fs"
    "&cid=de-DE&tsr=1&tsh=2"
)

#: Prawdziwa struktura strony posredniej, odtworzona z przechwytu 4.10.2026.
#: Adres przeladowania jest zakodowany **podwojnie**, co jest sednem ADR-3.
INTERSTITIAL = (
    "<!DOCTYPE html><html><head><meta name=\"robots\" content=\"noindex\">"
    "<script type='text/javascript'>"
    "var cookieEnabled = navigator.cookieEnabled;"
    "document.cookie = 'cookietest=1';"
    "document.location.href = decodeURIComponent("
    "'%2F%3Fc%3Deventimlight%26e%3Dshopde%26t%3Dhttps%253A%252F%252F"
    "www.eventim-light.com%252Fs%26cid%3Dde-DE%26tsr%3D1%26tsh%3D2');"
    "</script></head><body>"
    "<div class=\"nocookies alert alert-error hidden\"><p></p></div>"
    "</body></html>"
)

#: Strona marketingowa zamiast sklepu - objaw odrzucenia adresu IP.
MARKETING_PAGE = (
    "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"UTF-8\">"
    "<title>Tickets verkaufen im eigenen Ticket-Shop | EVENTIM.Light</title>"
    "<meta name=\"robots\" content=\"index,follow\">"
    "</head><body><h1>Ticket-Shop</h1></body></html>"
)

#: Prosty wynik: strona sklepu z JSON-LD, jeden termin dostepny.
SHOP_HTML = make_html(
    {
        "@type": "EventSeries",
        "name": "Test",
        "url": TARGET,
        "subEvent": [
            {
                "@type": "Event",
                "startDate": "2026-10-09T19:00:00+02:00",
                "offers": {"availability": "https://schema.org/InStock"},
            }
        ],
    }
)


def register_handshake(
    *, final: str = SHOP_HTML, final_status: int = 200
) -> None:
    """Rejestruje pelny, pieciohopowy handshake Queue-it w ``responses``.

    Kolejnosc jest deterministyczna, wiec ``responses`` dopasowuje po dokladnym
    URL - bez dopasowywania po regulach.

    ``final_status`` pozwala zasymulowac ``403`` z Akamai w ostatnim hopie.
    """
    responses.get(TARGET, status=302, headers={"Location": QUEUE_ENTRY_URL})
    responses.get(QUEUE_ENTRY_URL, status=200, body=INTERSTITIAL)
    responses.get(
        RELOAD_URL,
        status=302,
        headers={
            "Location": f"{TARGET}?queueittoken=e_shopde~ts_1~ce_true",
            "Set-Cookie": f"Queue-it-token=tok123; Path=/; Domain={QUEUE_HOST}",
        },
    )
    responses.get(
        f"{TARGET}?queueittoken=e_shopde~ts_1~ce_true",
        status=302,
        headers={
            "Location": TARGET,
            "Set-Cookie": "QueueITAccepted-SDFrts345E-V3_shopde=acc; Path=/; "
            "Domain=www.eventim-light.com",
        },
    )
    responses.get(TARGET, status=final_status, body=final)


def register_shop_only(final: str = SHOP_HTML, *, status: int = 200) -> None:
    """Rejestruje pojedynczy skok do sklepu - bezposrednio, bez Queue-it.

    Odpowiada cieplej sesji z odtworzonymi cookies (ADR-6): poprawny cache
    skraca handshake z 5 hopow do jednego.
    """
    responses.get(TARGET, status=status, body=final)


# --- fixture ---------------------------------------------------------------


@pytest.fixture(scope="session")
def real_ld() -> dict:
    """Prawdziwy JSON-LD strony sklepu (zapisany 4.10.2026, 6 terminow)."""
    return ld_from_file("shop_soldout.html")


@pytest.fixture(scope="session")
def html_soldout() -> str:
    """Prawdziwa strona sklepu - wszystkie terminy ``SoldOut``."""
    return (FIXTURES / "shop_soldout.html").read_text(encoding="utf-8")


@pytest.fixture(scope="session")
def interstitial_html() -> str:
    """Prawdziwa strona posrednia Queue-it (zapisana na zywo)."""
    return (FIXTURES / "interstitial.html").read_text(encoding="utf-8")


@pytest.fixture(scope="session")
def event_detail_html() -> str:
    """Prawdziwy JSON-LD strony terminu (``/e/``) z oferta per typ biletu."""
    return (FIXTURES / "event_detail.html").read_text(encoding="utf-8")