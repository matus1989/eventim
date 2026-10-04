"""Orkiestracja jednego sprawdzenia dostępności.

Sekwencja z :mod:`docs/technical-plan.md`, sekcja 7. Kolejność kroków jest
częścią kontraktu, a nie kwestią stylu — trzy miejsca są tam ułożone celowo:

**Cookies zapisujemy PRZED wysyłką do Telegrama.** W przeciwnym razie martwy
Telegram powodowałby utratę tokenu Queue-it, a każde kolejne uruchomienie
znowu robiłoby 5 hopów zamiast jednego.

**Ostrzeżenie o awarii wyciszamy na podstawie stanu PRZED bieżącym uruchomieniem.**
``should_warn`` czyta ``last_check_ok`` z poprzedniego sprawdzenia. Gdybyśmy
najpierw zapisali ``last_check_ok=False``, pierwsze ostrzeżenie po świeżym
problemie zostałoby wyciszone jako „kolejne" — czyli cicho.

**``last_availability_state`` nie steruje wysyłką alertu.** To pole służy
wyłącznie do logowania przejść stanów. Alarm o biletach leci przy **każdym**
uruchomieniu, w którym cokolwiek jest dostępne, bez limitu i bez porównania
z poprzednim runem — taka była decyzja właściciela. Pole wygląda na bramkę
dla powiadomień; gdyby kiedyś zaczęło nią sterować, monitoring przestałby
powiadamiać o biletach, które nadal są.

Kody wyjścia:

* ``0`` — sprawdzono i nie ma biletów, albo sprawdzono i wysłano alert,
* ``1`` — nie udało się sprawdzić (awaria pobierania, błąd parsowania,
  anomalia ``availability``, nieudana wysyłka alertu).

Rozróżnienie „nie ma" od „nie wiem" jest celowe: ``0`` przy braku dostępności
nie zaśmieca powiadomień, a ``1`` przy awarii nie udaje, że monitoring działa.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta

import requests

from eventim_watcher import __version__
from eventim_watcher.config import Config, ConfigError
from eventim_watcher.eventim import (
    BlockedByIpError,
    EventimClient,
    FetchError,
    new_session,
)
from eventim_watcher.messages import (
    format_available,
    format_fetch_error,
    format_unknown_availability,
)
from eventim_watcher.models import Series
from eventim_watcher.parser import ParseError, parse_series
from eventim_watcher.state import (
    FileStateStore,
    StateStore,
    availability_state,
    mark_anomaly_notified,
    mark_check_failed,
    mark_check_ok,
    mark_error_notified,
    now_utc,
    should_warn,
    should_warn_anomaly,
)
from eventim_watcher.telegram import NotifyError, TelegramNotifier

log = logging.getLogger("eventim_watcher")

#: Kod wyjscia: konfiguracja poprawna i sprawdzenie wykonane.
EXIT_OK = 0

#: Kod wyjscia: nie udalo sie sprawdzic (konfiguracja lub blad pobierania).
#: Rozroznienie "nie ma biletow" (0) od "nie wiem" (1) jest celowe - inaczej
#: cicha awaria monitoringu wygladalaby jak zwykly run.
EXIT_FAILURE = 1


def _configure_logging(level_name: str) -> None:
    """Konfiguruje logi w formacie czytelnym w UI GitHub Actions."""
    level = getattr(logging, level_name.upper(), logging.INFO)
    if not isinstance(level, int):
        level = logging.INFO

    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)


# -- orkiestracja -----------------------------------------------------------


def run(
    config: Config,
    store: StateStore,
    session: requests.Session,
    *,
    now: datetime | None = None,
) -> int:
    """Wykonuje jedno sprawdzenie i zwraca kod wyjscia.

    Nie wywoluje ``sys.exit`` i nie czyta ``os.environ`` — dzieki temu testy
    moga podac wlasne ``store`` i ``session``. Sesja jest wspolna dla klienta
    HTTP i notyfikatora: nie zakladamy, ze ``requests`` udostepnia pulenie
    poza wlasnymi sesjami.
    """
    chwila = now if now is not None else now_utc()
    cooldown = timedelta(hours=config.error_cooldown_hours)

    stan = store.load()
    client = EventimClient(
        session,
        user_agent=config.user_agent,
        max_hops=config.max_hops,
        timeout=config.timeout,
    )
    notifier = TelegramNotifier(
        config.telegram_bot_token,
        config.telegram_chat_id,
        session=session,
        timeout=config.timeout,
        max_retries=config.telegram_max_retries,
    )

    client.load_cookies(stan.get("cookies") or [])

    log.info("[start] sprawdzam %s", config.target_url)

    # -- pobranie ----------------------------------------------------------

    try:
        html = client.fetch_html(config.target_url)
    except BlockedByIpError as exc:
        # Osobno, bo komunikat intryguje niz zwykly blad sieciowy: adres
        # runnera jest odrzucany i samo ponowienie nic nie da.
        log.error("[blad] %s", exc)
        log.error(
            "[blad] sprawdz dostepne materialy planu awaryjnego "
            "(docs/technical-plan.md 11.1) - to nie jest chwilowa awaria sieci"
        )
        return _handle_fetch_failure(store, notifier, config, stan, chwila, cooldown, str(exc))
    except FetchError as exc:
        log.error("[blad] pobieranie nieudane: %s", exc)
        return _handle_fetch_failure(store, notifier, config, stan, chwila, cooldown, str(exc))

    # -- parsowanie --------------------------------------------------------

    try:
        series = parse_series(html, fallback_url=config.target_url)
    except ParseError as exc:
        # Plan traktuje blad parsowania jak blad pobrania: strone przyniosli,
        # ale nie da sie z niej korzystac, wiec obie sciezki koncza sie tak samo.
        log.error("[blad] parsowanie nieudane: %s", exc)
        log.error("[blad] sklep zwrocil strone bez czytelnego JSON-LD - mozliwa zmiana ukladu")
        return _handle_fetch_failure(store, notifier, config, stan, chwila, cooldown, str(exc))

    # -- stan po udanym sprawdzeniu (PRZED wysylka) ------------------------

    stan_dostepnosci = availability_state(
        len(series.available_terms), len(series.unknown_terms)
    )
    poprzedni_stan = stan.get("last_availability_state")
    mark_check_ok(
        stan,
        moment=chwila,
        stan=stan_dostepnosci,
        cookies=client.export_cookies(),
    )
    store.save(stan)

    if poprzedni_stan is not None and poprzedni_stan != stan_dostepnosci:
        # Pole jest informacyjne. Logujemy przejscia, bo przy awarii w cichu
        # pytanie "co program ostatnio widzial?" jest pierwszym pytaniem.
        #
        # `poprzedni_stan is not None` — pierwsze uruchomienie nie ma z czego
        # przechodzic. Bez tego warunku kazde wdrozenie zapisuje w logu
        # "zmiana stanu: None -> none_available", a potem cisza, i czytelnik
        # nie odróżni wstepu od rzeczywistej zmiany.
        log.info(
            "[stan] zmiana stanu dostepnosci: %r -> %r",
            poprzedni_stan,
            stan_dostepnosci,
        )

    log.info(
        "[wynik] %s: terminow=%d dostepnych=%d nieznanych=%d",
        series.name,
        len(series.terms),
        len(series.available_terms),
        len(series.unknown_terms),
    )

    # -- alert o dostepnosci -----------------------------------------------

    if series.any_available:
        # Bez limitu powtorzen i bez porownania z poprzednim runem: decyzja
        # wlasciciela brzmi "alert przy kazdym dostepnym terminie".
        tekst = format_available(series, series.available_terms, now=chwila)
        try:
            wyslane = notifier.send(tekst)
        except NotifyError as exc:
            # Nie wyciszamy: bilety sa, komunikat nie dotarl. To sytuacja
            # wymagajaca reakcji, wiec run musi byc widoczny jako blad.
            log.error("[telegram] alert o dostepnosci nie zostal wyslany: %s", exc)
            return EXIT_FAILURE
        log.info("[telegram] alert wyslany (%d wiadomosci)", wyslane)
    else:
        log.info("[telegram] brak dostepnych terminow - nie wysylam alertu")

    # -- anomalia braku pola availability (ADR-9) ---------------------------

    if series.has_unknown:
        return _handle_unknown_availability(
            store, notifier, series, stan, chwila, cooldown
        )

    return EXIT_OK


def _handle_fetch_failure(
    store: StateStore,
    notifier: TelegramNotifier,
    config: Config,
    stan: dict,
    chwila: datetime,
    cooldown: timedelta,
    przyczyna: str,
) -> int:
    """Ostrzega o awarii z cooldownem i zwraca ``EXIT_FAILURE``.

    Kolejność jest tu kluczowa: :func:`should_warn` czyta ``last_check_ok`` z
    **poprzedniego** uruchomienia, wiec stan oznaczamy jako nieudany dopiero
    po decyzji. Odwrotna kolejność wyciszyłaby pierwsze ostrzeżenie po awarii.
    """
    ostrzec = should_warn(stan, moment=chwila, cooldown=cooldown)
    mark_check_failed(stan, moment=chwila)
    # Cookies zostawiamy bez zmian - token moze wciaz dzialac.

    if not ostrzec:
        log.warning(
            "[awaria] powtorka w trakcie cooldownu (%g h) - wysylka wyciszona",
            config.error_cooldown_hours,
        )

    if ostrzec:
        tekst = format_fetch_error(
            config.target_url,
            przyczyna,
            cooldown_hours=config.error_cooldown_hours,
            now=chwila,
        )
        try:
            notifier.send(tekst)
        except NotifyError as exc:
            # Nie podnosimy: pierwotny blad pobrania jest wazniejszy i jest
            # juz w logu. Rzucanie tutaj zgubiloby przyczyne.
            log.error("[telegram] ostrzezenia o awarii nie udalo sie wyslac: %s", exc)
        else:
            mark_error_notified(stan, moment=chwila)
            log.info("[telegram] ostrzezenie o awarii wyslane")

    store.save(stan)
    return EXIT_FAILURE


def _handle_unknown_availability(
    store: StateStore,
    notifier: TelegramNotifier,
    series: Series,
    stan: dict,
    chwila: datetime,
    cooldown: timedelta,
) -> int:
    """Ostrzega o anomalii braku pola ``availability`` i zwraca ``EXIT_FAILURE``.

    Kanal ostrzegania jest osobny od kanału awarii pobierania — patrz
    :func:`eventim_watcher.state.should_warn_anomaly`.
    """
    if not should_warn_anomaly(stan, moment=chwila, cooldown=cooldown):
        log.warning("[anomalia] powtorka w trakcie cooldownu - wysylka wyciszona")
    else:
        tekst = format_unknown_availability(series, now=chwila)
        try:
            notifier.send(tekst)
        except NotifyError as exc:
            # Nie podnosimy — run i tak jest czerwony, a przyczyna anomalii
            # jest juz w logu parsera.
            log.error("[telegram] ostrzezenia o anomalii nie udalo sie wyslac: %s", exc)
        else:
            mark_anomaly_notified(stan, moment=chwila)
            log.info("[telegram] ostrzezenie o anomalii wyslane")

    store.save(stan)
    # Run jest czerwony: monitoring dziala, ale nie dla wszystkich terminow.
    return EXIT_FAILURE


# -- wejscie CLI ------------------------------------------------------------


def main(
    argv: Sequence[str] | None = None,
    env: Mapping[str, str] | None = None,
    *,
    store: StateStore | None = None,
    session: requests.Session | None = None,
) -> int:
    """Punkt wejscia programu. Zwraca kod wyjscia, nie wywoluje ``sys.exit``.

    ``store`` i ``session`` sa punktem wstrzykniecia dla testow. Bez nich
    testy CLI musialyby wykonac prawdziwy pieciohopowy handshake ze sklepem,
    czyli test jednostkowy trafialby do sieci - i wolniej by 15-krotnie
    przy kazdym uruchomieniu. Domyslnie sa tworzone wylacznie wtedy, gdy
    program naprawde ma cos zrobic.
    """
    parser = argparse.ArgumentParser(
        prog="eventim-watch",
        description="Sprawdza dostepnosc biletow na Eventim Light i powiadamia przez Telegram.",
    )
    parser.add_argument(
        "--log-level",
        default=(env or os.environ).get("EVENTIM_LOG_LEVEL", "info"),
        help="poziom szczegolowosci logow (debug, info, warning, error)",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    args = parser.parse_args(argv)
    _configure_logging(args.log_level)

    try:
        config = Config.from_env(env)
    except ConfigError as exc:
        # Czytelny komunikat na stderr i kod 1. Nigdy nie KeyError.
        print(exc, file=sys.stderr)
        log.error("koniec: konfiguracja niepoprawna")
        return EXIT_FAILURE

    log.info("[config] %s", config.summary())
    if config.uses_default_user_agent:
        # Swiadomie nie namawiamy do podania adresu e-mail: pomiar 4.10.2026
        # pokazal, ze kazdy UA bez frazy "github.com" konczy sie 403 (ADR-2).
        log.info(
            "[config] EVENTIM_USER_AGENT nieustawiony - uzywamy wartosci domyslnej"
        )
    elif "github.com" not in config.user_agent:
        log.warning(
            "[config] EVENTIM_USER_AGENT nie zawiera 'github.com' - Akamai prawdopodobnie "
            "zwroci 403 przy kazdym sprawdzeniu. Wpisz UA z adresem wlasciwego repozytorium "
            "(architecture.md, sekcja 2.5)."
        )

    magazyn = store if store is not None else FileStateStore(config.state_path)
    sesja = session if session is not None else new_session()

    try:
        return run(config, magazyn, sesja)
    except KeyboardInterrupt:  # pragma: no cover - przerwanie przez operatora
        log.warning("koniec: przerwano przez uzytkownika")
        return EXIT_FAILURE