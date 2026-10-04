"""Odczyt dostepnosci biletow z JSON-LD osadzonym w stronie sklepu.

Eventim Light nie udostepnia anonimowego API dla dostepnosci
(dokumentacja: docs/architecture.md, sekcja 2.1). Jedynym zrodlem maszynowym
jest ``<script type="application/ld+json">`` zgodny ze ``schema.org``, wbudowany
w renderowana serwerowo strone:

* strona serii ``/s/{id}``  -> ``EventSeries.subEvent[].offers.availability``
* strona terminu ``/e/{id}`` -> ``Event`` z zagniezdzona lista ``offers``

Ta faza obsluguje strone serii. Zagregowane ``AggregateOffer.availability``
znaczy, ze *wszystkie* typy biletow terminu sa wyprzedane; granulacja per typ
biletu wymaga strony terminu i pozostaje poza zakresem (ADR-4).

Decyzje projektowe (docs/technical-plan.md, sekcja 4):

* **Wyszukujemy wezel po ``@type``, nie bierzemy ``@graph[0]``** - lista zaczyna
  sie obecnie od ``WebSite``.
* **Brak ``subEvent`` to nie blad** - znaczy "brak terminow", czyli "brak biletow".
  Bladem jest dopiero brak parsowalnego JSON-LD.
* **Kazde zagniezdzone pole jest opcjonalne.** Zmiana markupu nie moze wywrocic
  monitoringu, wiec brakujace pola dostaja wartosci domyslne, a nie wyjatki.
"""

from __future__ import annotations

import json
import logging
import re

from eventim_watcher.models import UNKNOWN, Series, Term

__all__ = ["ParseError", "parse_series"]

log = logging.getLogger("eventim_watcher.parse")

#: ``re.S`` jest KRYTYCZNY - JSON-LD jest wieloliniowy, bez tej flagi
#: wyrazenie nie znajdzie nic.
_LD_JSON_RE = re.compile(
    r'<script[^>]*type\s*=\s*["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.S | re.I,
)

_SERIES_TYPE = "EventSeries"

#: Ile znakow kontekstu pokazujemy w komunikacie bledu parsowania.
_CONTEXT_CHARS = 120


class ParseError(Exception):
    """Nie udalo sie odczytac JSON-LD ze strony sklepu."""


def parse_series(html: str, *, fallback_url: str = "") -> Series:
    """Odczytuje serie wydarzen z HTML-a strony sklepu.

    ``fallback_url`` jest uzywany dla terminow bez wlasnego adresu URL.

    Rzuca :class:`ParseError`, gdy nie ma parsowalnego JSON-LD lub nie ma w nim
    wezla ``EventSeries``. Brak ``subEvent`` nie jest bledem.
    """
    payloads = _find_ld_payloads(html)
    series_node = _find_series_node(payloads)
    if series_node is None:
        raise ParseError(
            "w JSON-LD nie znaleziono wezla @type=EventSeries "
            f"(znalezione typy: {', '.join(_collect_types(payloads)) or 'brak'})"
        )

    series_name = _as_text(series_node.get("name")) or "-"
    series_url = _as_text(series_node.get("url")) or fallback_url

    raw_terms = series_node.get("subEvent")
    if not isinstance(raw_terms, list):
        if raw_terms is not None:
            log.warning(
                "[parse] pole subEvent nie jest lista (%s) - traktujemy jako brak terminow",
                type(raw_terms).__name__,
            )
        raw_terms = []

    terms = tuple(
        term
        for raw in raw_terms
        if (term := _build_term(raw, fallback_url=series_url)) is not None
    )

    series = Series(name=series_name, url=series_url, terms=terms)

    if series.has_unknown:
        log.warning(
            "[parse] %d z %d terminow bez pola availability - to anomalia markupu, "
            "nie potwierdzenie dostepnosci",
            len(series.unknown_terms),
            len(terms),
        )

    log.info(
        "[parse] seria=%r terminow=%d dostepnych=%d nieznanych=%d",
        series_name,
        len(terms),
        len(series.available_terms),
        len(series.unknown_terms),
    )

    return series


# -- wewnetrzne -------------------------------------------------------------


def _find_ld_payloads(html: str) -> list[dict]:
    """Zwraca wszystkie parsowalne slowniki JSON-LD ze strony.

    Strona moze zawierac kilka blokow JSON-LD i wlasciwy z seria wydarzen nie
    musi byc pierwszym - dlatego zbieramy wszystkie, a nie tylko pierwszy.
    """
    blocks = _LD_JSON_RE.findall(html)
    if not blocks:
        raise ParseError(
            "na stronie nie znaleziono bloku <script type=\"application/ld+json\"> "
            "- uklad strony mogl sie zmienic albo pobrano strone inna niz sklep"
        )

    payloads: list[dict] = []
    for index, block in enumerate(blocks):
        try:
            parsed = json.loads(block)
        except json.JSONDecodeError as exc:
            log.warning("[parse] blok JSON-LD #%d nie jest poprawnym JSON-em: %s", index, exc)
            continue
        if isinstance(parsed, dict):
            payloads.append(parsed)

    if not payloads:
        raise ParseError(
            f"znaleziono {len(blocks)} blokow JSON-LD, ale zaden nie jest poprawnym obiektem JSON"
        )

    return payloads


def _find_series_node(payloads: list[dict]) -> dict | None:
    """Szuka wezla ``EventSeries`` we wszystkich blokach JSON-LD."""
    for payload in payloads:
        if payload.get("@type") == _SERIES_TYPE:
            return payload
        graph = payload.get("@graph")
        if isinstance(graph, list):
            for node in graph:
                if isinstance(node, dict) and node.get("@type") == _SERIES_TYPE:
                    return node
    return None


def _collect_types(payloads: list[dict]) -> list[str]:
    """Zbiera typy ze JSON-LD do komunikatu bledu (diagnostyka)."""
    types: list[str] = []
    for payload in payloads:
        if isinstance(payload.get("@type"), str):
            types.append(payload["@type"])
        graph = payload.get("@graph")
        if isinstance(graph, list):
            for node in graph:
                if isinstance(node, dict) and isinstance(node.get("@type"), str):
                    types.append(node["@type"])
    return types


def _build_term(raw: object, *, fallback_url: str) -> Term | None:
    """Buduje :class:`Term` z pojedynczego ``subEvent``."""
    if not isinstance(raw, dict):
        log.warning("[parse] pominieto subEvent niebędący obiektem (%s)", type(raw).__name__)
        return None

    offers = raw.get("offers")
    if not isinstance(offers, dict):
        offers = {}

    # Brak pola availability to anomalia, nie potwierdzenie dostepnosci.
    raw_availability = _as_text(offers.get("availability"))
    if raw_availability:
        availability = raw_availability.rsplit("/", 1)[-1].strip() or UNKNOWN
    else:
        availability = UNKNOWN

    url = _as_text(raw.get("url")) or _as_text(offers.get("url")) or fallback_url

    return Term(
        name=_as_text(raw.get("name")) or "-",
        start=_as_text(raw.get("startDate")) or "",
        url=url,
        availability=availability,
        low_price=_as_float(offers.get("lowPrice")),
        high_price=_as_float(offers.get("highPrice")),
        currency=_as_text(offers.get("priceCurrency")) or None,
    )


def _as_text(value: object) -> str:
    """Zwraca obciety string albo pusty tekst dla nieoczekiwanych typow."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return ""


def _as_float(value: object) -> float | None:
    """Zamienia wartosc na ``float`` albo ``None``, gdy nie da sie odczytac.

    ``schema.org`` dopuszcza cene jako liczbe; przy zmianie formatu na
    tekst wolimy brak ceny niz ciche zerowanie oferty.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip().replace(",", "."))
        except ValueError:
            log.warning("[parse] nie udalo sie odczytac ceny z %r", value[:_CONTEXT_CHARS])
            return None
    return None