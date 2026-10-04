"""Punkt wejscia CLI.

W Fazie 1 odpowiedzialnosc jest ograniczona do wczytania i walidacji
konfiguracji: pobieranie strony (Faza 2), parsowanie JSON-LD (Faza 3),
powiadomienia (Faza 4) i orkiestracja (Faza 5) dochodza kolejnymi etapami.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Mapping, Sequence

from eventim_watcher import __version__
from eventim_watcher.config import Config, ConfigError

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


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    """Punkt wejscia programu. Zwraca kod wyjscia, nie wywoluje ``sys.exit``."""
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

    # Faza 1 konczy sie na walidacji. Pobieranie strony i powiadomienia
    # sa kolejnymi etapami - nie udajemy, ze juz dzialaja.
    log.info("[start] konfiguracja poprawna; pobieranie strony wchodzi w Fazie 2")
    return EXIT_OK