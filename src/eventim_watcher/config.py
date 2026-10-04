"""Wczytywanie i walidacja konfiguracji.

Decyzje projektowe (dokumentacja: docs/technical-plan.md, sekcja 2):

* **Wszystkie braki naraz.** ``ConfigError`` wymienia komplet problemow znalezionych
  przy starcie, a nie tylko pierwszy. Poprawianie konfiguracji po jednej zmiennej
  na runnerze marnuje i limit crona, i czas.
* **Sekrety bez wartosci domyslnej.** Brak tokenu Telegram musi byc glosny,
  a nie cichy - inaczej monitoring wyglada na sprawny, a nie dziala.
* **Walidacja typu i zakresu przy starcie.** ``EVENTIM_TIMEOUT=0`` ma zglosic blad
  teraz, a nie jako ``TimeoutError`` o 3 w nocy na runnerze.
* **Ograniczenie domeny URL.** Odrzucamy URL spoza eventim-light.com - lepszy
  czytelny blad konfiguracji niz ciche pobieranie cudzej strony.
* ``strip()`` na wszystkich wartosciach. Kopiuj-wklej bardzo czesto dokleja
  spacje, a ``TELEGRAM_BOT_TOKEN`` z bledami na koncu konczy sie 401 u Telegrama.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlparse

from eventim_watcher.state import DEFAULT_STATE_PATH

__all__ = [
    "ALLOWED_HOST_SUFFIX",
    "Config",
    "ConfigError",
    "DEFAULT_USER_AGENT",
    "DEFAULT_STATE_PATH",
    "OPTIONAL_VARS",
    "REQUIRED_VARS",
]


#: Wlasny User-Agent zamiast podszywania sie pod przeglądarke.
#: Akamai zwraca 403 dla klientow udajacych Chrome (ADR-2).
#:
#: UWAGA: przepustka jest zawieszona na frazie ``github.com`` w naglowku.
#: Pomiar 4.10.2026 (13 wariantow, swieza sesja kazdorazowo):
#:
#:     DZIALA  eventim-watch/1.0 (+https://github.com/; ...)
#:     DZIALA  Mozilla/5.0 (+https://github.com/; bot)      <- nawet prefiks Chrome
#:     DZIALA  curl/8.5.0 (+https://github.com/)
#:     DZIALA  eventim-watch/2.0 (+https://github.com/; ...)
#:     DZIALA  eventim-watch/1.0                            <- sam token produktu
#:     403     eventim-watch/1.0 (+https://gitlab.com/; ...) <- inna domena
#:     403     eventim-watch/1.0 (+https://example.com/; ...)
#:     403     eventim-watch/1.0 (+mailto:kontakt@example.com)
#:     403     eventim-watch/1.0 (kontakt: ktos@example.com)
#:
#: Wniosek: ``github.com`` w dowolnej postaci przechodzi, inna domena kontaktowa
#: nie. Dlatego **nie zamieniaj tej wartosci na adres e-mail** - zmiana
#: wygladna na poprawe konczy sie cichym 403 przy kazdym uruchomieniu.
#:
#: Adres repozytorium jest jednoczesnie kontaktem dla wlasciciela sklepu:
#: to realny kod, do ktorego moze zajrzec osoba w adminsitracji Eventim.
#: Pelne wyniki: docs/architecture.md, sekcja 2.4.
DEFAULT_USER_AGENT = (
    "eventim-watch/1.0 (+https://github.com/matus1989/eventim; ticket-availability-monitor)"
)

#: Jedyna dozwolona domena strony sklepu.
ALLOWED_HOST_SUFFIX = "eventim-light.com"

#: Zmienne bez wartosci domyslnej - brak ktorej jest bledem.
REQUIRED_VARS = (
    "EVENTIM_TARGET_URL",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
)

#: Zmienne opcjonalne - maja sensowne domyslne.
OPTIONAL_VARS = (
    "EVENTIM_USER_AGENT",
    "EVENTIM_TIMEOUT",
    "EVENTIM_MAX_HOPS",
    "EVENTIM_ERROR_COOLDOWN_HOURS",
    "EVENTIM_STATE_FILE",
    "TELEGRAM_MAX_RETRIES",
)

#: Token z @BotFather ma postac "<id>:<secret>".
_TOKEN_RE = re.compile(r"^\d+:[A-Za-z0-9_-]{20,}$")

#: ID czatu to "@kanal" albo liczba (moze byc ujemna dla supergrup).
_CHAT_ID_RE = re.compile(r"^(?:@[A-Za-z0-9_]{5,}|-?\d+)$")

_HINT = (
    "Sekrety ustaw w repozytorium: Settings -> Secrets and variables -> Actions.\n"
    "Zmienna EVENTIM_TARGET_URL moze byc zmienna (Settings -> Variables)."
)


class ConfigError(Exception):
    """Konfiguracja brakujaca lub niepoprawna.

    Komunikat zawiera komplet znalezionych problemow, aby nie trzeba bylo
    uruchamiać programu wielokrotnie.
    """

    def __init__(self, problems: list[str]) -> None:
        self.problems = tuple(problems)
        super().__init__(self._render())

    def _render(self) -> str:
        lines = [f"Niepoprawna konfiguracja - znaleziono {len(self.problems)} problemow:"]
        lines.extend(f"  - {p}" for p in self.problems)
        lines.append("")
        lines.append(_HINT)
        return "\n".join(lines)


@dataclass(frozen=True)
class Config:
    """Zwalidowana konfiguracja uruchomienia."""

    target_url: str
    telegram_bot_token: str
    telegram_chat_id: str

    user_agent: str = DEFAULT_USER_AGENT
    timeout: float = 30.0
    max_hops: int = 12
    error_cooldown_hours: float = 6.0
    telegram_max_retries: int = 1
    state_path: str = DEFAULT_STATE_PATH

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Config:
        """Buduje ``Config`` ze srodowiska.

        Zglasza jeden ``ConfigError`` opisujacy wszystkie znalezione problemy.
        """
        source: Mapping[str, str] = os.environ if env is None else env
        raw = {name: (source.get(name) or "").strip() for name in REQUIRED_VARS + OPTIONAL_VARS}

        problems: list[str] = []

        missing = [name for name in REQUIRED_VARS if not raw[name]]
        if missing:
            problems.append(
                "brak wymaganych zmiennych srodowiskowych: " + ", ".join(missing)
            )

        target_url = _validate_target_url(raw["EVENTIM_TARGET_URL"], problems)
        token = _validate_token(raw["TELEGRAM_BOT_TOKEN"], problems)
        chat_id = _validate_chat_id(raw["TELEGRAM_CHAT_ID"], problems)

        timeout = _number(raw, "EVENTIM_TIMEOUT", float, 30.0, 1.0, 300.0, problems)
        max_hops = _number(raw, "EVENTIM_MAX_HOPS", int, 12, 1, 50, problems)
        cooldown = _number(raw, "EVENTIM_ERROR_COOLDOWN_HOURS", float, 6.0, 0.0, 168.0, problems)
        retries = _number(raw, "TELEGRAM_MAX_RETRIES", int, 1, 0, 5, problems)

        user_agent = raw["EVENTIM_USER_AGENT"] or DEFAULT_USER_AGENT

        # Sciezka cache nie jest sekretem ani wartoscia wymagana - cache to
        # optymalizacja, wiec brak zmiennej ma byc cichy i bezpieczny.
        state_path = raw["EVENTIM_STATE_FILE"] or DEFAULT_STATE_PATH

        if problems:
            raise ConfigError(problems)

        return cls(
            target_url=target_url,
            telegram_bot_token=token,
            telegram_chat_id=chat_id,
            user_agent=user_agent,
            timeout=timeout,
            max_hops=max_hops,
            error_cooldown_hours=cooldown,
            telegram_max_retries=retries,
            state_path=state_path,
        )

    def summary(self) -> str:
        """Jedna linia do logu. Swiadomie nie zawiera tokenu Telegram."""
        cooldown = f"{self.error_cooldown_hours:g}h"
        return (
            f"target={self.target_url} "
            f"timeout={self.timeout:g}s "
            f"max_hops={self.max_hops} "
            f"cooldown={cooldown} "
            f"chat_id={self.telegram_chat_id}"
        )

    @property
    def uses_default_user_agent(self) -> bool:
        """Czy User-Agent pochodzi z wartosci domyslnej (ogolny kontakt)."""
        return self.user_agent == DEFAULT_USER_AGENT


def _validate_target_url(url: str, problems: list[str]) -> str:
    """Sprawdza, ze URL prowadzi do sklepu na dozwolonej domenie."""
    if not url:
        return url

    parsed = urlparse(url)

    if parsed.scheme != "https":
        problems.append(
            f"EVENTIM_TARGET_URL: wymagany schemat https, podano {parsed.scheme or '(brak)'!r}"
        )

    host = (parsed.hostname or "").lower()
    if not host:
        problems.append("EVENTIM_TARGET_URL: brak hosta w adresie")
    elif not (host == ALLOWED_HOST_SUFFIX or host.endswith("." + ALLOWED_HOST_SUFFIX)):
        problems.append(
            f"EVENTIM_TARGET_URL: dozwolona domena *.{ALLOWED_HOST_SUFFIX}, podano {host!r}"
        )

    if parsed.path in ("", "/"):
        problems.append(
            "EVENTIM_TARGET_URL: brak sciezki - podaj adres strony sklepu, "
            "np. https://www.eventim-light.com/de/a/<organizer>/s/<shop>"
        )

    return url


def _validate_token(token: str, problems: list[str]) -> str:
    """Sprawdza format tokenu z @BotFather."""
    if not token:
        return token

    if not _TOKEN_RE.match(token):
        problems.append(
            "TELEGRAM_BOT_TOKEN: oczekiwany format '<id>:<token>' z @BotFather "
            "- sprawdź, czy nie ma spacji ani łamki wiersza po kopiowaniu"
        )

    return token


def _validate_chat_id(chat_id: str, problems: list[str]) -> str:
    """Sprawdza, ze ID czatu ma postac '@kanal' albo liczby."""
    if not chat_id:
        return chat_id

    if not _CHAT_ID_RE.match(chat_id):
        problems.append(
            "TELEGRAM_CHAT_ID: oczekiwany format '@kanal' (bez spacji!) albo liczbowe ID "
            "czatu, np. -1001234567890"
        )

    return chat_id


def _number(
    raw: Mapping[str, str],
    name: str,
    kind: type[float] | type[int],
    default: float,
    minimum: float,
    maximum: float,
    problems: list[str],
) -> float:
    """Wczytuje liczbe, sprawdzajac typ i zakres. Zwraca ``default`` przy bledzie."""
    value = raw[name]
    if not value:
        return default

    try:
        parsed = kind(value)
    except ValueError:
        kind_name = "liczba calkowita" if kind is int else "liczba"
        problems.append(f"{name}: oczekiwano {kind_name}, podano {value!r}")
        return default

    if not minimum <= parsed <= maximum:
        problems.append(f"{name}: wartosc {parsed} poza zakresem [{minimum}, {maximum}]")
        return default

    return parsed