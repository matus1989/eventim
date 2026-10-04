"""Faza 0 - bramka: czy runner GitHub Actions pobiera strone sklepu?

Ten skrypt jest **tymczasowy**. Odpowiada na jedno pytanie, ktorego nie da
sie rozstrzygnac na maszynie lokalnej: czy adres IP hostowanego runnera
GitHub Actions (a wiec puli Azure) jest przyjmowany przez Akamai i Queue-it
przed sklepem Eventim Light.

Wynik badania z 4.10.2026 na IP centrum danych: handshake konczy sie na
stronie marketingowej (~16 KB) zamiast sklepu (~152 KB). Nie wiadomo, czy
ten sam wynik dostaniemy na runnerze, bo runner ma inne warunki sieciowe
i inny User-Agent HTTP.

Metryka sukcesu z roadmap.md: ``hops == 5`` **oraz** znaleziony wezel
``EventSeries`` z terminami.

Zasady, ktore tu obowiazuja:

**Awaria probe'a to nie awaria programu.** Kod wyjscia 2 oznacza "bramka
nie przeszla", a nie "skrypt jest zly". Oba kody sa widoczne w UI, ale
nie moga byc pomylone.

**Kazdy wynik zapisujemy jako artefakt.** Jesli runner dostanie strone
marketingowa albo zmieniony JSON-LD, chcemy zobaczyc co dokladnie przyszlo -
z logu nie da sie tego odtworzyc.

**Matryca User-Agentow tylko przy porazce.** Gdy handshake przeszedl,
nie ma po co ruszac 4 warianty UA: bramka przeszla, a kolejne zadania
moglyby tylko sprowokowac Queue-it. Gdy handshake padnie, matryca
rozdziela "Akamai blokuje ten UA" od "Akamai blokuje ten adres IP", a to
sa dwa zupełnie rozne problemy o roznych naprawach.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import platform
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests

from eventim_watcher.config import DEFAULT_USER_AGENT
from eventim_watcher.eventim import (
    BlockedByIpError,
    EventimClient,
    FetchError,
    has_json_ld,
    looks_like_marketing_page,
    new_session,
)
from eventim_watcher.models import Series
from eventim_watcher.parser import ParseError, parse_series

log = logging.getLogger("probe")

#: Wydarzenie probowane w Fazie 0. Ten sam adres sluzy do calej bramki,
#: wiec wynik mowi cos o produkcyjnym przypadku, a nie o sztucznym URL.
DEFAULT_TARGET = (
    "https://www.eventim-light.com/de/a/65488918b266253e892eaec1/s/692425419d93a5650a708f21"
)

#: Katalog na odpowiedzi - wrzucany jako artefakt runa.
ARTIFACTS = Path("probe-artifacts")

#: Bramka przeszla: strone sklepu da sie pobrac i sparsowac.
EXIT_OK = 0

#: Bramka nie przeszla. Swiadomy, inny kod niz 1 - probe nie jest programem,
#: a 1 zwykle oznacza "cos sie wywrocilo".
EXIT_FAIL = 2

# --- werdykty ---------------------------------------------------------------
# Jedna nazwa per przyczyna, bo "nie zadzialalo" nie pozwala wybrac
# nastepnego kroku z planu awaryjnego (technical-plan.md, sekcja 11.1).

V_OK = "OK"
V_BEZ_TERMINOW = "SZKICZ_BEZ_TERMINOW"
V_IP = "BLOKADA_IP_STRONA_MARKETINGOWA"
V_AKAMAI = "BLOKADA_AKAMAI_403"
V_HANDSHAKE = "HANDSHAKE_NIEUKONCZONY"
V_PARSE = "JSON_LD_NIEPARSOWALNE"
V_NET = "BLAD_SIECI"
V_INNE = "INNY_BLAD"

#: Warianty UA do matrycy diagnostycznej. Kolejnosc ma znaczenie:
#: produkcyjny na poczatku, bo to jest jedyny, ktory nas interesuje.
#: "obca_domena" i "chrome" to kontrole negatywne z pomiaru 4.10.2026
#: (architecture.md 2.4) - jesli na runnerze przepuszczaja, to bialka
#: lista UA nie jest prawidlowa i trzeba przemyslec decyzje ADR-2.
UA_WARIANTS: tuple[tuple[str, str], ...] = (
    ("produkcyjny", DEFAULT_USER_AGENT),
    ("bez_wersji", "eventim-watch/1.0"),
    ("obca_domena", "eventim-watch/1.0 (+https://example.com/; probe)"),
    (
        "chrome",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
    ),
)

#: Markery strony blokady Akamai. Sprawdzamy je tylko w odpowiedzi 403,
#: bo "access denied" w innym kontekście jest niejednoznaczne.
_AKAMAI_MARKERS = ("access denied", "akamai", "reference #", "_abck")

#: Numer hopu w komunikacie klienta. Zbieramy **najwyzszy** numer, a nie
#: liczymy linie: klient loguje hop 2 dwa razy (odpowiedz HTTP i rozpoznanie
#: strony posredniej), wiec zliczanie linii dawalo 6 zamiast wymaganych 5.
_HOP_RE = re.compile(r"^\[fetch\] hop (\d+):")


class HopCounter(logging.Handler):
    """Odczytuje liczbe hopow handshake'u z komunikatow klienta HTTP.

    Klient loguje ``[fetch] hop N: ...`` dla kazdego zapytania, ale nie zwraca
    liczby. Zamiast modyfikowac kontrakt klienta tylko dla probe'a, czytamy
    jego log - liczba hopow jest tam juz wypisywana i jest jedynym miejscem,
    gdzie da sie ja odczytac bez powtorzenia calego handshake'u.
    """

    def __init__(self) -> None:
        super().__init__()
        self.hops = 0
        self.trace: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        self.trace.append(message)
        dopasowanie = _HOP_RE.match(message)
        if dopasowanie:
            self.hops = max(self.hops, int(dopasowanie.group(1)))


@dataclass
class Report:
    """Wynik probe'a w formie gotowej do logu, JSON-a i podsumowania runa."""

    verdict: str = V_INNE
    detail: str = ""
    hops: int = 0
    sekundy: float = 0.0
    url: str = ""
    user_agent: str = ""
    znaleziono_json_ld: bool = False
    nazwa_serii: str | None = None
    liczba_terminow: int = 0
    liczba_dostepnych: int = 0
    liczba_nieznanych: int = 0
    stany: dict[str, int] = field(default_factory=dict)
    handshake: list[dict[str, object]] = field(default_factory=list)
    srodowisko: dict[str, str] = field(default_factory=dict)
    pliki: dict[str, str] = field(default_factory=dict)

    @property
    def bramka_przeszla(self) -> bool:
        """Czy strona sklepu dała się pobrać i sparsować.

        ``SZKICZ_BEZ_TERMINOW`` to formalnie sukces: IP nie jest blokowane,
        a więc Faza 7 zadziała. Niepusty ``subEvent`` jest jednak warunkiem
        z roadmap.md, więc trzeba to zobaczyć w werdykcie.
        """
        return self.verdict in (V_OK, V_BEZ_TERMINOW)

    def linie(self) -> list[str]:
        """Czytelne podsumowanie - ta sama treść trafia do logu i do JSON-a."""
        out = [
            f"WERDYKT={self.verdict}",
            f"bramka={'TAK' if self.bramka_przeszla else 'NIE'}",
            f"hops={self.hops}",
            f"sukces_json_ld={'TAK' if self.znaleziono_json_ld else 'NIE'}",
            f"terminy={self.liczba_terminow}",
            f"dostepne={self.liczba_dostepnych}",
            f"nieznane={self.liczba_nieznanych}",
            f"czas={self.sekundy:.1f}s",
        ]
        if self.nazwa_serii:
            out.append(f"seria={self.nazwa_serii!r}")
        if self.stany:
            out.append("stany=" + ", ".join(f"{k}:{v}" for k, v in sorted(self.stany.items())))
        if self.detail:
            out.append(f"szczegoly={self.detail}")
        return out


# --- pobieranie -------------------------------------------------------------


def fetch_shop(url: str, user_agent: str, timeout: float, max_hops: int) -> tuple[str, Report, HopCounter]:
    """Przechodzi handshake produkcyjnym klientem i opisuje wynik.

    Zwraca ``(html, report, licznik_hopow)``. ``html`` jest pusty przy błędzie,
    a werdykt w raporcie już wskazuje przyczynę.
    """
    counter = HopCounter()
    fetch_log = logging.getLogger("eventim_watcher.fetch")
    fetch_log.addHandler(counter)
    fetch_log.setLevel(logging.INFO)

    report = Report(url=url, user_agent=user_agent)

    client = EventimClient(
        new_session(),
        user_agent=user_agent,
        max_hops=max_hops,
        timeout=timeout,
    )

    started = time.monotonic()
    try:
        html = client.fetch_html(url)
    except BlockedByIpError as exc:
        report.verdict = V_IP
        report.detail = str(exc)
        return "", report, counter
    except FetchError as exc:
        # Rozroznienie 403 od reszty ma znaczenie: 403 to Akamai (warstwa
        # filtrujaca User-Agent), a FetchError innego rodzaju to juz
        # warstwa Queue-it albo zmiana markupu.
        report.verdict = V_AKAMAI if "403" in str(exc) else V_HANDSHAKE
        report.detail = str(exc)
        return "", report, counter
    except requests.RequestException as exc:  # pragma: no cover - zalezne od sieci
        report.verdict = V_NET
        report.detail = f"{exc.__class__.__name__}: {exc}"
        return "", report, counter
    finally:
        report.sekundy = time.monotonic() - started
        report.hops = counter.hops
        fetch_log.removeHandler(counter)

    report.znaleziono_json_ld = has_json_ld(html)
    return html, report, counter


def describe_series(html: str, report: Report) -> Series | None:
    """Parsuje pobrany HTML i dopisuje liczniki do raportu.

    Błąd parsowania jest wynikiem sam w sobie: strona mogła się załadować,
    a JSON-LD mógł zmienić strukturę (ryzyko R2). Dlatego rozdzielamy
    "nie da się pobrać" od "nie da się zrozumieć".
    """
    try:
        series = parse_series(html, fallback_url=report.url)
    except ParseError as exc:
        report.verdict = V_PARSE
        report.detail = str(exc)
        return None

    report.nazwa_serii = series.name
    report.liczba_terminow = len(series.terms)
    report.liczba_dostepnych = len(series.available_terms)
    report.liczba_nieznanych = len(series.unknown_terms)
    report.stany = _count_states(series)
    report.verdict = V_OK if series.terms else V_BEZ_TERMINOW
    if series.terms:
        report.detail = f"znaleziono {len(series.terms)} terminow"
    else:
        # Wspolne @graph z wezlem EventSeries, ale pustym subEvent: bramka
        # przeszla, ale parser ma cos do poprawienia.
        report.detail = "wezel EventSeries bez subEvent - markup mogl sie zmienic"
    return series


def _count_states(series: Series) -> dict[str, int]:
    """Zlicza stany dostepnosci terminow - skrocone podgladanie danych."""
    counts: dict[str, int] = {}
    for term in series.terms:
        counts[term.availability] = counts.get(term.availability, 0) + 1
    return counts


# --- matryca diagnostyczna -------------------------------------------------


def first_hop(url: str, user_agent: str, timeout: float) -> dict[str, object]:
    """Jedno zapytanie bez poscigania przekierowan.

    Celowo **jeden** hop: hop 1 jest jedynym miejscem, na którym decyduje
    Akamai. Pelny handshake dla kazdego wariantu UA bylby 5x drozszy i
    moglby sprowokowac dodatkowe wyzwania Queue-it.
    """
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
        }
    )

    wynik: dict[str, object] = {"user_agent": user_agent}
    try:
        response = session.get(url, allow_redirects=False, timeout=timeout)
    except requests.RequestException as exc:
        wynik["blad"] = f"{exc.__class__.__name__}: {exc}"
        return wynik

    body = response.text
    location = response.headers.get("Location", "")
    wynik.update(
        {
            "status": response.status_code,
            "location_host": urlparse(location).hostname or "",
            "bajty": len(body),
            "ma_json_ld": has_json_ld(body),
            "strona_marketingowa": looks_like_marketing_page(body),
            "akamai": _looks_like_akamai_block(response.status_code, body),
            "serwer": response.headers.get("Server", ""),
        }
    )
    return wynik


def _looks_like_akamai_block(status: int, body: str) -> bool:
    """Czy odpowiedź to strona blokady Akamai.

    Sam ``403`` nie wystarczy: Queue-it tez potrafi odpowiedziec 403 na
    częste zapytania i wtedy problem jest inny (zadanie P3).
    """
    if status != 403:
        return False
    lower = body.lower()
    return any(marker in lower for marker in _AKAMAI_MARKERS)


def run_matrix(url: str, timeout: float, pause: float) -> list[dict[str, object]]:
    """Sprawdza warstwe Akamai dla wszystkich wariantow User-Agent.

    Wynik rozróżnia trzy sytuacje, które wygladają tak samo („nie działa"),
    ale mają różne naprawy:

    * produkcyjny UA przechodzi → blokada jest głębiej (Queue-it albo warstwa
      marketingowa), więc problem nie jest w User-Agent;
    * produkcyjny UA nie przechodzi, ale kontrole negatywne też nie →
      biała lista UA nie działa **na tym adresie IP**;
    * kontrole negatywne przechodzą → biała lista jest inna niż na maszynie
      lokalnej i ADR-2 wymaga ponownego pomiaru.
    """
    log.info("--- matryca User-Agentow (tylko hop 1) ---")
    wyniki: list[dict[str, object]] = []

    for index, (nazwa, ua) in enumerate(UA_WARIANTS):
        if index and pause > 0:
            # Pauza miedzy wariantami: zbyt szybka seria zdan z jednego adresu
            # wyglada jak skanowanie i tylko utrudnia odpowiedz.
            time.sleep(pause)

        wynik = first_hop(url, ua, timeout)
        wynik["wariant"] = nazwa
        wyniki.append(wynik)

        status = wynik.get("status", wynik.get("blad", "-"))
        log.info(
            "UA[%s] -> %s host=%s %s B json_ld=%s akamai=%s",
            nazwa,
            status,
            wynik.get("location_host") or "-",
            wynik.get("bajty", "-"),
            wynik.get("ma_json_ld", "-"),
            wynik.get("akamai", "-"),
        )

    return wyniki


def diagnose_matrix(wyniki: list[dict[str, object]]) -> str:
    """Wniosek z matrycy UA, jednym zdaniem.

    Wypisujemy **fakty**, a nie wybieramy jedna z kilku mutually-exclusive
    gałęzi. Kombinacja wyników jest jednocześnie kilku wariantów naraz
    (np. produkcyjny odrzucony, ale Chrome przepuszczony), a wybór jednej
    przyczyny zgubiłby informację, która zmienia decyzję o ADR-2.
    """
    by_name = {str(w.get("wariant")): w for w in wyniki}
    produkcyjny = by_name.get("produkcyjny", {})

    if produkcyjny.get("status") != 403:
        return "warstwa Akamai przepuszcila UA produkcyjny - blokada jest pozniej w lancuchu"

    czesci = ["UA produkcyjny odrzucony 403 na tym IP"]
    kontrole = ("bez_wersji", "obca_domena", "chrome")
    przeszly = [n for n in kontrole if by_name.get(n, {}).get("status") not in (None, 403)]

    if przeszly:
        czesci.append(
            "przeszly: " + ", ".join(przeszly) + " - biala lista jest inna niz na maszynie"
        )
    odrzucone = [n for n in kontrole if by_name.get(n, {}).get("status") == 403]
    if odrzucone:
        czesci.append("odrzucone: " + ", ".join(odrzucone))

    return "; ".join(czesci)


# --- srodowisko -------------------------------------------------------------


def describe_environment(url: str, timeout: float) -> dict[str, str]:
    """Zbiera fakty o runnerze potrzebne do interpretacji wyniku.

    Adres wychodzący i organizacja są kluczowe: bez nich „runner nie
    dostał strony” jest nieopisywalne, bo nie wiadomo, jaki to zakres IP.
    """
    info: dict[str, str] = {
        "os": f"{platform.system()} {platform.release()}",
        "python": platform.python_version(),
        "runner": os.environ.get("RUNNER_OS", "-"),
        "architektura": platform.machine(),
    }
    info.update(_egress_info(timeout))
    info["cel"] = url
    return info


def _egress_info(timeout: float) -> dict[str, str]:
    """Adres IP wychodzący i jego organizacja - best effort, bez wyjatku.

    Zewnętrzne uslugi odpowiadaja wolniej i bywają chwilowo niedostępne.
    Brak tych danych nie moze uniewaznic probe'a, bo wersja i system
    runnera wystarcza do interpretacji wyniku.
    """
    out: dict[str, str] = {}
    sesja = requests.Session()
    sesja.headers["User-Agent"] = DEFAULT_USER_AGENT

    try:
        response = sesja.get("https://api.ipify.org", timeout=min(timeout, 8.0))
        if response.ok:
            out["ip_wychodzace"] = response.text.strip()
    except requests.RequestException:
        out["ip_wychodzace"] = "nieokretlone"

    try:
        response = sesja.get("https://ipinfo.io/json", timeout=min(timeout, 8.0))
        if response.ok:
            dane = response.json()
            for klucz, etykieta in (("ip", "ip"), ("org", "organizacja"), ("country", "kraj")):
                if dane.get(klucz):
                    out[etykieta] = str(dane[klucz])
    except (requests.RequestException, ValueError):
        out["organizacja"] = "nieokretlona"

    return out


# --- raportowanie -----------------------------------------------------------


def save_html(name: str, html: str, report: Report) -> None:
    """Zapisuje odpowiedź jako artefakt runa."""
    if not html:
        return
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    sciezka = ARTIFACTS / f"{name}.html"
    sciezka.write_text(html, encoding="utf-8")
    report.pliki[name] = str(sciezka)
    log.info("zapisano %s (%d B)", sciezka, len(html))


def write_report(report: Report) -> None:
    """Zapisuje raport jako JSON i jako tekst - oba wchodza w artefakt.

    JSON jest dla maszyn (pozniejsze porownanie runow), tekst dla czytania
    bez otwierania edytora w przegladarce runa.
    """
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    dane = {
        "znacznik_czasu": datetime.now(timezone.utc).isoformat(),
        "werdykt": report.verdict,
        "bramka_przeszla": report.bramka_przeszla,
        "hops": report.hops,
        "sekundy": round(report.sekundy, 1),
        "url": report.url,
        "user_agent": report.user_agent,
        "znaleziono_json_ld": report.znaleziono_json_ld,
        "nazwa_serii": report.nazwa_serii,
        "liczba_terminow": report.liczba_terminow,
        "liczba_dostepnych": report.liczba_dostepnych,
        "liczba_nieznanych": report.liczba_nieznanych,
        "stany": report.stany,
        "handshake": report.handshake,
        "srodowisko": report.srodowisko,
        "pliki": report.pliki,
        "szczegoly": report.detail,
    }
    (ARTIFACTS / "probe-report.json").write_text(
        json.dumps(dane, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (ARTIFACTS / "probe-report.txt").write_text(
        "\n".join(report.linie()) + "\n", encoding="utf-8"
    )


def write_step_summary(report: Report) -> None:
    """Wpisuje werdykt na górę strony runa w UI GitHub Actions.

    Log przy 200 liniach jest przewijany; podsumowanie na górze strony to
    miejsce, w ktorym wynik bramki faktycznie zostanie zobaczony.
    """
    sciezka = os.environ.get("GITHUB_STEP_SUMMARY")
    if not sciezka:
        return

    znacznik = "✅" if report.bramka_przeszla else "❌"
    wiersze = [
        f"## {znacznik} Faza 0 — {'bramka przeszła' if report.bramka_przeszla else 'bramka NIE przeszła'}",
        "",
        "| Metryka | Wartość |",
        "|---|---|",
        f"| Werdykt | `{report.verdict}` |",
        f"| Hopy (wymagane 5) | {report.hops} |",
        f"| JSON-LD | {'znaleziony' if report.znaleziono_json_ld else 'BRAK'} |",
        f"| Terminów | {report.liczba_terminow} |",
        f"| Dostępnych | {report.liczba_dostepnych} |",
        f"| Stanów nieznanych | {report.liczba_nieznanych} |",
        f"| Czas | {report.sekundy:.1f} s |",
    ]
    if report.nazwa_serii:
        wiersze.append(f"| Seria | {report.nazwa_serii} |")
    if report.detail:
        wiersze.extend(["", "**Szczegóły:**", "", report.detail])

    wiersze.extend(["", f"Kod wyjścia: {EXIT_OK if report.bramka_przeszla else EXIT_FAIL}", ""])
    Path(sciezka).write_text("\n".join(wiersze), encoding="utf-8")


# --- wejscie ----------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="probe",
        description="Faza 0: sprawdza, czy runner pobiera strone sklepu Eventim Light.",
    )
    parser.add_argument(
        "--url", default=os.environ.get("EVENTIM_TARGET_URL") or DEFAULT_TARGET
    )
    parser.add_argument("--user-agent", default=os.environ.get("EVENTIM_USER_AGENT") or DEFAULT_USER_AGENT)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--max-hops", type=int, default=12)
    parser.add_argument(
        "--pauza", type=float, default=8.0, help="pauza miedzy wariantami UA w sekundach"
    )
    parser.add_argument(
        "--bez-matrycy",
        action="store_true",
        help="nie uruchamiaj diagnostyki UA po porazce (domyslnie i tak nie rusza)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="[%(levelname)s] %(message)s",
        stream=sys.stdout,
    )

    args = parse_args(argv)

    log.info("=== Faza 0: proba pobrania strony sklepu na runnerze ===")
    log.info("cel: %s", args.url)
    log.info("User-Agent: %s", args.user_agent)

    # Srodowisko zbieramy do zmiennej, a nie do `report`, bo `fetch_shop`
    # zwraca **nowy** obiekt Report i przypisanie `report, ... = fetch_shop(...)`
    # po cichu gubilo caly opis runnera. Wykryte dopiero po pierwszym
    # prawdziwym runie - w logu bylo, w artefakcie juz nie.
    srodowisko = describe_environment(args.url, args.timeout)
    for klucz, wartosc in srodowisko.items():
        log.info("srodowisko: %s = %s", klucz, wartosc)

    html, report, counter = fetch_shop(
        args.url, args.user_agent, args.timeout, args.max_hops
    )
    report.srodowisko = srodowisko
    report.handshake = [m for m in counter.trace if m.startswith("[fetch]")]

    if html:
        save_html("shop", html, report)
        describe_series(html, report)
    else:
        log.error("pobieranie nieudane: %s", report.detail)

    if not report.bramka_przeszla and not args.bez_matrycy:
        report.handshake = run_matrix(args.url, args.timeout, args.pauza)
        wniosek = diagnose_matrix(report.handshake)
        report.detail = f"{report.detail} || matryca UA: {wniosek}"
        log.error("wniosek diagnostyczny: %s", wniosek)

    write_report(report)
    write_step_summary(report)

    for linia in report.linie():
        log.info("raport | %s", linia)
    log.info(
        "WYNIK FAZY 0: %s (kod wyjscia %d)",
        "bramka przeszla" if report.bramka_przeszla else "bramka NIE przeszla",
        EXIT_OK if report.bramka_przeszla else EXIT_FAIL,
    )

    return EXIT_OK if report.bramka_przeszla else EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
