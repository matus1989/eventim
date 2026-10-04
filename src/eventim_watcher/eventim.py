"""Klient HTTP obslugujacy handshake Queue-it przed sklepem Eventim Light.

Strona sklepu nie odpowiada na zwykle ``GET``: za kazdym razem przechodzi
przez pieciohopowy handshake (dokumentacja: docs/architecture.md, sekcja 2.3).

    hop 1  GET {cel}                  -> 302 do eventimlight.queue-it.net
    hop 2  GET queue-it              -> 200 strona posrednia "cookieEnabled"
    hop 3  GET queue-it + cookietest -> 302 z tokenem + cookie Queue-it-token
    hop 4  GET {cel}?queueittoken=   -> 302 + cookie QueueITAccepted
    hop 5  GET {cel}                 -> 200 prawdziwa strona z JSON-LD

Strona posrednia z hopu 2 to NIE challenge anty-botowy, tylko test "czy
przegladarka obsluguje cookies" - wystarczy ustawic ``cookietest=1``
i przejsc pod wskazany adres.

Decyzje projektowe (docs/technical-plan.md, sekcja 3):

* **Przekierowania obsluguje klient, nie ``requests``** (``allow_redirects=False``).
  Przekierowania sa czescia handshake'u, a nie zwyklym nawigowaniem - gdyby
  ``requests`` podazyl automatycznie, wykrycie strony posredniej byloby niemozliwe.
* **Wlasny User-Agent.** Akamai zwraca ``403`` dla klientow udajacych Chrome
  (potwierdzone badanem), ale przepuszcza zwykle klienty programistyczne.
* **Sklep vs strona marketingowa.** Z IP centrum danych handshake konczy sie
  na stronie promocyjnej zamiast sklepie. To nie jest zwykly blad - wykrywamy
  go osobno (:class:`BlockedByIpError`), bo oznacza, ze monitoring nie dziala.
"""

from __future__ import annotations

import logging
import re
from http.cookiejar import Cookie
from urllib.parse import unquote, urljoin, urlparse

import requests

from eventim_watcher.config import DEFAULT_USER_AGENT

__all__ = [
    "BlockedByIpError",
    "EventimClient",
    "FetchError",
    "REDIRECT_STATUSES",
    "extract_reload_url",
    "has_json_ld",
    "is_interstitial",
    "looks_like_marketing_page",
    "new_session",
]

log = logging.getLogger("eventim_watcher.fetch")

#: Statusy przekierowan HTTP, ktore klient realizuje sam.
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

#: Ograniczenie dlugosci fragmentu w tresci bledu - 152 KB HTML w logu runa
#: czyni go nieczytelnym, a szczegoly nie pomagaja w diagnozie.
SNIPPET_CHARS = 200

_JSON_LD_MARKER = "application/ld+json"

#: Adres przeładowania jest zakodowany PODWÓJNIE (form-urlencoded raz przez
#: ``document.location.href``, raz przez ``decodeURIComponent``). Brak
#: ``unquote`` jest najczestrza przyczyna zapetlenia sie petli.
_RELOAD_RE = re.compile(r"document\.location\.href\s*=\s*decodeURIComponent\('([^']+)'\)")

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)

#: Dlugosc ciaza strony sklepu z JSON-LD. Uzywana tylko w logach diagnostycznych.
_SHOP_PAGE_MIN_CHARS = 20_000


class FetchError(Exception):
    """Nie udalo sie pobrac strony sklepu.

    Komunikat zawiera liczbe hopow, ostatni URL, ostatni kod HTTP i obciety
    fragment odpowiedzi.
    """


class BlockedByIpError(FetchError):
    """Handshake doszedl do strony marketingowej zamiast do sklepu.

    Osobny typ, bo to nie jest zwykly blad sieciowy - oznacza, ze adres IP
    klienta jest odrzucany (patryt z architecture.md, sekcja 2.5). Monitoring
    w takiej sytuacji nie dziala, mimo ze program konczy sie bez wyjatku.
    """


def has_json_ld(text: str) -> bool:
    """Czy odpowiedz zawiera dane JSON-LD, czyli prawdziwa strone sklepu."""
    return _JSON_LD_MARKER in text


def is_interstitial(text: str) -> bool:
    """Czy odpowiedz to strona posrednia testujaca obsluge cookies.

    Wymagamy dwóch markerow naraz - samo ``cookietest`` pojawia sie takze
    w miejscach, ktora nie sa strona posrednia.
    """
    return "cookieEnabled" in text and "cookietest" in text


def looks_like_marketing_page(text: str) -> bool:
    """Czy to strona promocyjna EVENTIM.Light zamiast sklepu.

    Objaw blokady adresu IP: handshake konczy sie na stronie~
    16 KB zamiast sklepu~ 152 KB. Orozroznia ja para cech - tytul oraz
    ``robots=index,follow`` (strona posrednia ma ``noindex``).
    """
    title_match = _TITLE_RE.search(text)
    if not title_match:
        return False
    return "EVENTIM.Light" in title_match.group(1) and (
        'content="index,follow"' in text or 'content="index, follow"' in text
    )


def extract_reload_url(text: str, base_url: str) -> str | None:
    """Zwraca adres, na ktory strona posrednia probuje nas przekierowac."""
    match = _RELOAD_RE.search(text)
    if not match:
        return None
    return urljoin(base_url, unquote(match.group(1)))


def _snippet(text: str) -> str:
    """Obciety, jednolinijkowy fragment odpowiedzy do komunikatu bledu."""
    collapsed = " ".join(text.split())
    return collapsed[:SNIPPET_CHARS]


class EventimClient:
    """Pobiera strone sklepu, przechodzic handshake Queue-it.

    Sesja jest przekazywana z zewnatrz, dzieki czemu ten sam obiekt
    :class:`requests.Session` obsluguje cookies miedzy uruchomieniami.
    """

    def __init__(
        self,
        session: requests.Session,
        *,
        user_agent: str = DEFAULT_USER_AGENT,
        max_hops: int = 12,
        timeout: float = 30.0,
    ) -> None:
        self._session = session
        self._max_hops = max_hops
        self._timeout = timeout
        self._session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
                ),
                "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
            }
        )

    # -- API ---------------------------------------------------------------

    def fetch_html(self, url: str) -> str:
        """Zwraca HTML strony sklepu.

        Rzuca :class:`BlockedByIpError`, gdy handshake trafia na strone
        marketingowa, albo :class:`FetchError` przy kazdym innym niepowodzeniu.
        """
        current = url
        referer: str | None = None
        last_url = url
        last_status: int | None = None
        last_text = ""

        for hop in range(1, self._max_hops + 1):
            headers = {"Referer": referer} if referer else {}
            try:
                response = self._session.get(
                    current,
                    headers=headers,
                    allow_redirects=False,
                    timeout=self._timeout,
                )
            except requests.RequestException as exc:
                raise FetchError(
                    f"zadanie HTTP nieudane po {hop - 1} hopach "
                    f"(url={current[:120]}): {exc.__class__.__name__}: {exc}"
                ) from exc

            last_url = current
            last_status = response.status_code
            text = response.text
            last_text = text
            log.info(
                "[fetch] hop %d: %d %s (%d B)",
                hop,
                response.status_code,
                urlparse(current).netloc,
                len(text),
            )

            if has_json_ld(text):
                log.info("[fetch] JSON-LD znalezione po %d hopach", hop)
                return text

            if is_interstitial(text):
                next_url = self._handle_interstitial(text, current, hop)
                if next_url is None:
                    raise FetchError(
                        f"strona posrednia bez adresu przeladowania po {hop} hopach "
                        f"(url={current[:120]})"
                    )
                referer, current = current, next_url
                continue

            if response.status_code in REDIRECT_STATUSES:
                location = response.headers.get("Location")
                if not location:
                    raise FetchError(
                        f"przekierowanie {response.status_code} bez naglowka Location "
                        f"po {hop} hopach (url={current[:120]})"
                    )
                referer, current = current, urljoin(current, location)
                continue

            if looks_like_marketing_page(text):
                raise BlockedByIpError(
                    f"handshake zakonczyl sie na stronie marketingowej zamiast sklepu "
                    f"po {hop} hopach (url={current[:120]}, http={response.status_code}, "
                    f"{len(text)} B). Adres IP jest najpewniej odrzucany przez Akamai - "
                    f"sprawdzenie dostepnosci nie dziala. Szczegoly: architecture.md 2.5"
                )

            raise FetchError(
                f"nieoczekiwana odpowiedz HTTP {response.status_code} po {hop} hopach "
                f"(url={current[:120]}, {len(text)} B): {_snippet(text)}"
            )

        raise FetchError(
            f"przekroczono limit {self._max_hops} hopow "
            f"(ostatni url={last_url[:120]}, http={last_status}): {_snippet(last_text)}"
        )

    def export_cookies(self) -> list[dict[str, str]]:
        """Zamienia ciasteczka sesji na strukture gotowa do zapisu w cache.

        Zapisujemy domene i sciezke, bo ``Queue-it-token`` i ``cookietest``
        naleza do ``eventimlight.queue-it.net``, a ``QueueITAccepted-*`` do
        ``www.eventim-light.com``. Bez tego odtworzenie cookies nie zadziala.
        """
        return [
            {
                "domain": cookie.domain,
                "path": cookie.path,
                "name": cookie.name,
                "value": cookie.value,
            }
            for cookie in self._session.cookies
        ]

    def load_cookies(self, jar: list[dict[str, str]]) -> int:
        """Odtwarza ciasteczka z cache. Zwraca liczbe odtworzonych wpisow.

        Nieznane lub uszkodzone wpisy sa pomijane - cache jest optymalizacja,
        a poprawnosc nie moze od niego zaleiec.
        """
        restored = 0
        for item in jar:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            value = item.get("value")
            domain = item.get("domain")
            if not name or not value or not domain:
                continue
            self._session.cookies.set(
                name, value, domain=domain, path=item.get("path") or "/"
            )
            restored += 1
        if restored:
            log.info("[cache] odtworzono %d cookies", restored)
        return restored

    # -- wnetrze ------------------------------------------------------------

    def _handle_interstitial(
        self, text: str, current_url: str, hop: int
    ) -> str | None:
        """Ustawia ``cookietest`` i zwraca adres nastepnego zapytania.

        Cookie musi trafic do domeny ``eventimlight.queue-it.net``, a nie do
        ``www.eventim-light.com`` - inaczej handshake sie nie domyka.
        """
        host = urlparse(current_url).hostname
        if not host:
            raise FetchError(
                f"brak hosta w adresie strony posredniej (url={current_url[:120]})"
            )

        self._session.cookies.set("cookietest", "1", domain=host, path="/")
        next_url = extract_reload_url(text, current_url)
        log.info("[fetch] hop %d: strona posrednia, cookietest=1 dla %s", hop, host)
        return next_url


def new_session() -> requests.Session:
    """Tworzy sesje z rozsadnymi domyslnymi ustawieniami."""
    session = requests.Session()
    session.max_redirects = 10
    return session


def cookies_to_header(cookies: list[Cookie]) -> str:  # pragma: no cover - pomocnicze
    """Zamienia ciasteczka na naglowek ``Cookie`` (uzywane wylacznie w diagnostyce)."""
    return "; ".join(f"{c.name}={c.value}" for c in cookies)