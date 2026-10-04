"""Testy na prawdziwym sklepie - uruchamiane tylko na żądanie.

Te testy są jedynym miejscem w repozytorium, które dotyka sieci. Ręczne
symulacje (`tests/conftest.py`) potrafią sprawdzić logikę, ale nie potrafią
sprawdzić dwóch rzeczy, które w tym projekcie są najważniejsze:

* czy Akamai wciąż przepuszcza nasz User-Agent z runnerów GitHub Actions
  (ADR-2) - zmiana po stronie Eventim jest dla nas niewidoczna,
* czy handshake Queue-it nadal ma pięć hopów i czy markup nadal zawiera
  `availability` (ADR-9).

Dlatego osobny marker, osobny plik i wymagany przełącznik.

Trzy zasady, których nie łamiemy nawet tutaj:

* **`EVENTIM_LIVE=1` w wymagany.** Sam marker `live` nie wystarczy - błąd
  literówki w `pytest.ini` nie może odpalić kilkunastu żądań do cudzego
  serwera.
* **Nigdy nie wysyłamy Telegrama.** Testy live kończą się na parsowaniu.
  Wysyłka jest jedyną nieodwracalną rzeczą w tym repozytorium, a cel
  tych testów to nie „sprawdź, czy Telegram działa”, tylko „sprawdź, czy
  sklep odpowiada”.
* **Jeden handshake na sesję.** Pobranie jest kosztowne i obciąża cudzy
  serwer, więc drogie testy dzielą jeden wynik zamiast odpytywać osobno.

Testy **nie** asertywują, że biletów nie ma. To nie jest ich zadaniem, a
twarda asercja zrobiłaby z tego testu, który krzyczy „sukces!” w chwili,
gdy monitoring przestaje działać. Ich zadaniem jest: dać się uruchomić,
wykryć zmianę po stronie Eventim i nie zepsuć się cicho.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from contextlib import contextmanager
import json
import logging
import os
import re
from dataclasses import dataclass, field

import pytest

from conftest import ld_from_file
from eventim_watcher.eventim import (
    BlockedByIpError,
    EventimClient,
    FetchError,
    new_session,
)
from eventim_watcher.models import UNKNOWN
from eventim_watcher.parser import parse_series

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.getenv("EVENTIM_LIVE") != "1",
        reason="wymagane EVENTIM_LIVE=1 - testy dotykaja prawdziwego sklepu",
    ),
]

#: Prawdziwy cel. Można nadpisać zmienną środowiskową, ale domyślnie test
#: sprawdza **ten** event, o który chodzi monitoring - inaczej walidacja
#: potwierdzałaby coś innego, niż to, co zostanie wdrożone.
ADRES = (
    "https://www.eventim-light.com/de/a/65488918b266253e892eaec1/s/692425419d93a5650a708f21"
)

#: Zapisany zrzut z 4.10.2026. Traktowany jako kontrakt **kształtu**, nie treści:
#: identyfikatory terminów zmieniają się z każdą edycją oferty.
ZRZUT = "shop_soldout.html"

#: Dopasowanie wiersza logu klienta: ``[fetch] hop 5: 200 host (12050 B)``.
_HOP_RE = re.compile(r"\bhop (\d+):")

#: Wystarczająco dużo na wolny handshake z obcą kontynentu, wystarczająco mało,
#: żeby nie wisieć kwadrans, gdy coś jest zepsute.
TIMEOUT_S = 30.0

#: Ile hopów ma handshake według ADR-3. Wartość górna, nie dokładna: zmiana
#: po stronie Queue-it nie jest naszą regresją, ale zmniejszenie poniżej
#: połowy tej liczby oznaczałoby, że warm cache przestał działać.
HOPOW_PELNY_HANDSHAKE = 5


class LicznikHopow(logging.Handler):
    """Zapisuje numery hopów z logów klienta.

    Klient nie wystawia licznika publicznie - i nie powinien, bo liczba
    przekierowań to szczegół implementacji handshake'u, nie kontrakt.
    Do testu wystarczy podejrzeć log, więc handler jest mniej inwazyjny
    niż nowe API.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.numery: list[int] = []

    def emit(self, record: logging.LogRecord) -> None:
        dopasowanie = _HOP_RE.search(record.getMessage())
        if dopasowanie:
            self.numery.append(int(dopasowanie.group(1)))

    @property
    def ostatni_hop(self) -> int:
        """Najwyższy numer hopu, **nie** liczba linii.

        Log klienta nie zawiera jednego wiersza na hop: strona pośrednia
        loguje dodatkowo linię bez numeru. Liczenie linii dałoby 6
        zamiast 5 i test padałby bez powodu.
        """
        return max(self.numery, default=0)


@contextmanager
def _obserwuj_hopy() -> Iterator[LicznikHopow]:
    """Podlacza licznik do loggera klienta i sprzata po sobie.

    Poziom loggera trzeba ustawic jawnie. W zwyklym przebiegu ``pytest``
    dziedziczy po rocie ``WARNING``, wiec ``logger.info(...)`` nigdzie nie
    dociera i licznik widzi zero rekordow - czyli zero hopow. Testy liczace
    hopy przechodzilyby wtedy w prozni: ``0 < 5`` jest prawda, niezaleznie
    od tego, czy handshake w ogole dziala. To najgorszy rodzaj testu
    diagnostycznego - nie bledzie, tylko klamie.
    """
    log = logging.getLogger("eventim_watcher.fetch")
    poprzedni = log.level
    log.setLevel(logging.INFO)
    licznik = LicznikHopow()
    log.addHandler(licznik)
    try:
        yield licznik
    finally:
        log.removeHandler(licznik)
        log.setLevel(poprzedni)


@dataclass
class Pobranie:
    """Wynik pojedynczego rzeczywistego handshake'u, dzielony przez testy."""

    html: str
    licznik: LicznikHopow
    adres: str
    cookies: list[dict[str, str]] = field(default_factory=list)


def _adres() -> str:
    """Adres sklepu: z env, inaczej domyślny z planu."""
    return os.getenv("EVENTIM_TARGET_URL") or ADRES


@pytest.fixture(scope="session")
def pobrano() -> Pobranie:
    """Jeden rzeczywisty fetch na cala sesje testow."""
    adres = _adres()
    session = new_session()
    client = EventimClient(session, timeout=TIMEOUT_S)
    with _obserwuj_hopy() as licznik:
        try:
            html = client.fetch_html(adres)
        except BlockedByIpError as exc:  # pragma: no cover - tylko przy blokadzie
            pytest.fail(
                f"Sklep odrzucil runnera ({exc}).\n"
                "To nie jest awaria sieci - nie pomoze ponowienie. Sprawdz ADR-2 "
                "(User-Agent musi zawierac slowo 'github.com'; Chrome UA daje 403) "
                "oraz plan awaryjny w docs/technical-plan.md 11.1."
            )
        except FetchError as exc:  # pragma: no cover - tylko przy awarii sieci
            pytest.fail(f"Pobieranie nieudane: {exc}")
    return Pobranie(
        html=html,
        licznik=licznik,
        adres=adres,
        cookies=client.export_cookies(),
    )



@pytest.fixture(scope="session")
def seria(pobrano: Pobranie):
    """Rzeczywista seria wydarzenia sparsowana z pobranej strony."""
    return parse_series(pobrano.html, fallback_url=pobrano.adres)


# --- pobieranie ------------------------------------------------------------


def test_strona_ma_json_ld(pobrano: Pobranie) -> None:
    """Bez JSON-LD nie ma czego parsować - i nie ma po czym poznać zmianę."""
    assert "<script" in pobrano.html
    assert "ld+json" in pobrano.html
    assert len(pobrano.html) > 5000, "strona wygląda na zbyt krótką"


def test_handshake_przeszedl(pobrano: Pobranie) -> None:
    """Pobranie musi przejść rękę, nie wrócić ze strony pośredniej.

    ``max_hops`` w konfiguracji to 12, a handshake ma 5. Gdyby klient
    wrócił „za dużo przekierowań", oznaczałoby to zmianę po stronie
    Queue-it - i wtedy ta liczba jest jedyną rzeczą, która to zobaczy.
    """
    assert pobrano.licznik.ostatni_hop > 0, "klient nie zalogował żadnego hopu"


def test_czysty_cache_skraca_handshake(pobrano: Pobranie) -> None:
    """ADR-6: z cookies handshake skraca się do jednego żądania.

    Bez cache wracamy do 5 hopów (~1,2 s zamiast ~0,1 s). Test nie
    asertywuje „dokładnie 1" - asertywuje „wyraźnie mniej niż pełny
    handshake". Dokładna liczba zależy od tego, co Queue-it zrobi dziś,
    a rozbicie testu na każdą z tych wartości zamieniłoby walidację
    w kruchy test na zmianę, której nie kontrolujemy.
    """
    if not pobrano.cookies:
        pytest.skip("brak cookies w sesji - nie da się sprawdzić ciepłego cache")

    cieply = EventimClient(new_session(), timeout=TIMEOUT_S)
    cieply.load_cookies(pobrano.cookies)
    with _obserwuj_hopy() as licznik:
        cieply.fetch_html(pobrano.adres)

    assert licznik.ostatni_hop < HOPOW_PELNY_HANDSHAKE, (
        f"ciepły cache dał {licznik.ostatni_hop} hopów, powinien wyraźnie mniej "
        f"niż {HOPOW_PELNY_HANDSHAKE} - cookies przestały działać (ADR-6)"
    )


def test_wczytanie_cookies_nie_podwaja_ich(pobrano: Pobranie) -> None:
    """`load_cookies` musi być idempotentne.

    Bez tego każdy kolejny run dołożyłby te same cookies do słoika, a sklep
    zacząłby albo odrzucać żądanie, albo wracać do pełnego handshake'u -
    cicho, bo kod nadal działa.
    """
    klient = EventimClient(new_session(), timeout=TIMEOUT_S)
    klient.load_cookies(pobrano.cookies)
    pierwsza = len(klient.export_cookies())
    klient.load_cookies(pobrano.cookies)
    assert len(klient.export_cookies()) == pierwsza


def test_pusty_cieply_cache_nadal_dziala(pobrano: Pobranie) -> None:
    """Wygasły cache to normalna sytuacja, nie awaria (ADR-6).

    `actions/cache` jest ewiktowany po 7 dniach bez trafienia, więc pierwszy
    run po przerwie zawsze wchodzi z pustym cache. Gdyby to był wyjątek,
    monitoring cicho przestałby działać po wakacjach.
    """
    klient = EventimClient(new_session(), timeout=TIMEOUT_S)
    klient.load_cookies([])
    html = klient.fetch_html(pobrano.adres)
    assert "ld+json" in html


# --- parsowanie ------------------------------------------------------------


def test_seria_ma_nazwe_i_terminy(seria) -> None:
    """Puste `terms` nie jest sukcesem - znaczyłoby, że wróciła pusta strona."""
    assert seria.name
    assert seria.terms, "zero terminów - coś jest nie tak ze sklepem"
    assert seria.url.startswith("https://www.eventim-light.com/")


def test_kazdy_termin_ma_date_i_link(seria) -> None:
    """Termin bez daty jest bezużyteczny w alarmie, a termin bez linku
    jest bezużyteczny dla człowieka, który chce kupić."""
    for termin in seria.terms:
        assert termin.start, f"termin bez daty: {termin.name!r}"
        assert termin.url.startswith("http"), f"termin bez linku: {termin.name!r}"


def test_brak_terminow_o_nieznanej_dostepnosci(seria) -> None:
    """ADR-9 zakłada, że `availability` jest zawsze obecne.

    Założenie nigdy nie zostało sprawdzone na żywo dla stanu innego niż
    `SoldOut` (6/6 prawdziwych terminów było `SoldOut`). Jeśli Eventim
    przestanie wysyłać to pole, monitoring przejdzie z „wyprzedane" na
    „nie wiem" - i zrobi to **cicho**, chyba że ktoś tu patrzy.
    Ten test jest tym kimś.
    """
    nieznane = seria.unknown_terms
    assert not nieznane, (
        f"{len(nieznane)}/{len(seria.terms)} terminów bez pola 'availability'. "
        "Jeśli to zmiana jednorazowa - zapisz ją w ADR-9. Jeśli markup "
        "zmienił nazwę pola, oczekiwana wartość to "
        f"{UNKNOWN!r}, a nie brak wpisu."
    )


def test_kazdy_termin_ma_rozpoznawalny_stan(seria) -> None:
    """Stan spoza czarnej listy **nie** jest błędem (ADR-5).

    Test nie wymusza `SoldOut` - wymusza tylko to, że program wie, co
    widzi. Nowa wartość schema.org ma dać alert, nie awarię.
    """
    for termin in seria.terms:
        assert termin.availability, "pusty stan - błąd parsowania"
        assert termin.label, f"brak etykiety dla {termin.availability!r}"


def test_dostepne_terminy_daja_poprawny_alert(seria) -> None:
    """Ścieżka alertu musi działać na prawdziwych danych, nie tylko na fixture.

    Budujemy tekst alertu **bez wysyłki** - sprawdzamy formatowanie na
    realnym odsyłaczu do terminu i realnej nazwie wydarzenia. Jeśli
    pojawi, ten test powinien zobaczyć to pierwszy, zanim wyśle się
    cokolwiek.
    """
    from eventim_watcher.messages import format_available  # noqa: PLC0415

    dostepne = seria.available_terms
    if not dostepne:
        pytest.skip("wszystkie terminy wyprzedane - brak realnego alertu do zbadania")

    tekst = format_available(seria)
    assert "DOSTĘPNE BILETY" in tekst
    assert len(tekst) < 3500, "alert przekracza limit Telegrama"


# --- ksztalt markupu -------------------------------------------------------


def test_liczba_terminow_sie_zgadza(pobrano: Pobranie, seria) -> None:
    """Porównanie z zapisanym zrzutem - wykrywa dryf przed awarią.

    Zrzut ma 6 terminów, zapisany 4.10.2026. Porównujemy **liczbę**, nie
    identyfikatory: oferta może mieć inną liczbę terminów po rozbudowie,
    i wtedy zrzut trzeba odświeżyć ręcznie - ale to nie jest awaria
    monitoringu i nie powinno wyglądać jak taka.
    """
    zapisany = ld_from_file(ZRZUT)
    wezel = next(
        (n for n in zapisany.get("@graph", []) if n.get("@type") == "EventSeries"),
        None,
    )
    assert wezel is not None, f"zrzut {ZRZUT} nie ma węzła EventSeries"
    zapisane_terminy = wezel.get("subEvent") or []
    assert len(zapisane_terminy), "zrzut nie zawiera terminów"
    assert len(seria.terms) == len(zapisane_terminy), (
        f"liczba terminów zmieniła się: zapisano {len(zapisane_terminy)}, "
        f"teraz {len(seria.terms)}. Jeśli to celowa zmiana oferty - "
        "odśwież zrzut i ADR-9; jeśli nie - sprawdź, czy sklep nie "
        "odpowiada na inną stronę niż seria."
    )


def test_ten_sam_html_daje_ten_sam_wynik(pobrano: Pobranie, seria) -> None:
    """Ta sama strona musi dać ten sam wynik - inaczej mamy losowość.

    Nie testuje `parse_series` (to robi `test_parser.py` na fixture).
    Sprawdza, że **ta** strona jest jednoznaczna dla parsera.
    """
    powtorka = parse_series(pobrano.html, fallback_url=pobrano.adres)
    assert [t.availability for t in powtorka.terms] == [
        t.availability for t in seria.terms
    ]
    assert [t.url for t in powtorka.terms] == [t.url for t in seria.terms]


def test_json_ld_jest_w_jednym_bloku(pobrano: Pobranie) -> None:
    """Dwa bloki JSON-LD z danymi o dostępności to niejednoznaczność.

    Parser bierze pierwszy znaleziony węzeł `EventSeries`. Jeśli sklep
    zacznie wysyłać drugi (np. dla wersji mobilnej), wynik stanie się
    zależny od kolejności skryptów na stronie.
    """
    bloki = re.findall(
        r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>',
        pobrano.html,
        re.S | re.I,
    )
    assert bloki, "brak bloków JSON-LD"
    z_serializowane = [json.dumps(json.loads(b)) for b in bloki]
    serie_w_blokach = [b for b in z_serializowane if '"EventSeries"' in b]
    assert len(serie_w_blokach) == 1, (
        f"{len(serie_w_blokach)} bloków JSON-LD zawiera EventSeries, a parser "
        "bierze pierwszy - wynik zależy od kolejności skryptów na stronie"
    )


# --- higiena ---------------------------------------------------------------


def test_plik_live_nie_wola_telegrama() -> None:
    """Zabezpieczenie przed przypadkowa wysylka wiadomosci.

    Sprawdzamy **kod**, nie tekst pliku. Wersja wczytujaca zrodlo i szukajaca
    napisu byla bezbronna: znajdowala wlasna linie asercji, wiec nie mogla
    przejsc nigdy. A test, ktory z definicji pada, to nie test, tylko
    zawodzenie, ktore uczy czytelnika ignorowac ten plik.

    ``ast`` widzi importy i wywolania, a nie komentarze ani docstringi,
    w ktorych zasade pewnie opisujemy dla czytelnika.
    """
    drzewo = ast.parse(open(__file__, encoding="utf-8").read())

    nazwy_importow: set[str] = set()
    for wezel in ast.walk(drzewo):
        if isinstance(wezel, ast.ImportFrom) and wezel.module:
            nazwy_importow.add(wezel.module)
        elif isinstance(wezel, ast.Import):
            nazwy_importow.update(alias.name for alias in wezel.names)

    podejrzane = {n for n in nazwy_importow if ".telegram" in n}
    assert not podejrzane, f"test live nie moze importowac {podejrzane}"

    wysylajace = {
        wezel.func.attr
        for wezel in ast.walk(drzewo)
        if isinstance(wezel, ast.Call) and isinstance(wezel.func, ast.Attribute)
    } & {"send", "post", "notify"}
    assert not wysylajace, f"test live wywoluje {wysylajace} - wysylka jest wykluczona"
