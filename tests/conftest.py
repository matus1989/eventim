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


def _series(ld: dict) -> dict:
    """Zwraca wezel ``EventSeries`` z payloadu."""
    for node in ld.get("@graph", []):
        if node.get("@type") == "EventSeries":
            return node
    raise AssertionError("fixture nie zawiera wezla EventSeries")


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
    for node in ld.get("@graph", []):
        if node.get("@type") == "EventSeries":
            return node
    raise AssertionError("brak wezla EventSeries")


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