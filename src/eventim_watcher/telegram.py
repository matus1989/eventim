"""Wysyłka komunikatów przez Telegram Bot API.

Tylko ``sendMessage`` i nic ponadto. Klient jest cienki, bo nie ma tu żadnej
logiki domenowej - formatowanie tekstu żyje w :mod:`eventim_watcher.messages`,
a decyzja *co* wysłać w orkiestracji (Faza 5).

Trzy świadome decyzje:

**``parse_mode`` nieustawione** (ADR z planu technicznego). Telegram bez
``parse_mode`` traktuje tekst dosłownie. Gdybyśmy ustawili ``Markdown``, znak
``_`` w nazwie wydarzenia unieważniłby całą wiadomość (``can't parse entities``).
Nazwy w tym sklepie mają spacje i myślniki, więc ryzyko jest realne.

**``disable_web_page_preview`` zawsze.** Bez tego Telegram dokłada podgląd do
linku sklepu i wiadomość wygląda jak reklama zamiast alertu.

**Token nigdy nie trafia do logu ani do treści wyjątku.** Adres API ma token
w ścieżce, więc odpowiedź API przy błędzie może go wyechoować. Komunikat
błędu budujemy z **opisu** HTTP, nigdy z surowego ciała odpowiedzi.
"""

from __future__ import annotations

import logging
import time

import requests

from eventim_watcher.messages import split_message

__all__ = ["NotifyError", "TelegramNotifier", "TELEGRAM_API_BASE"]

log = logging.getLogger("eventim_watcher.telegram")

#: Adres API Telegrama. Token doklejamy dopiero przy budowie URL-a.
TELEGRAM_API_BASE = "https://api.telegram.org"

#: Domyślny limit znaków na jedną wiadomość - niższy niż 4096 z Telegrama,
#: bo jego licznik traktuje znaki spoza ASCII jako wielobajtowe.
DEFAULT_MAX_CHARS = 3500


class NotifyError(Exception):
    """Nie udało się wysłać wiadomości.

    Osobny typ, bo orkiestracja (Faza 5) musi odróżnić „nie ma biletów"
    od „nie umiałem powiedzieć, że nie ma biletów”. Drugi przypadek
    zasługuje na ostrzeżenie, pierwszy na ciszę.
    """


class TelegramNotifier:
    """Wysyła zwykły tekst na czat lub kanał.

    Sesja jest przekazywana z zewnątrz, żeby nie tworzyć jej per wiadomość.
    """

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        *,
        session: requests.Session | None = None,
        timeout: float = 30.0,
        max_retries: int = 1,
        retry_backoff: float = 2.0,
        max_chars: int = DEFAULT_MAX_CHARS,
    ) -> None:
        self._bot_token = bot_token
        self._chat_id = chat_id
        self._session = session if session is not None else requests.Session()
        self._timeout = timeout
        self._max_retries = max(0, max_retries)
        self._retry_backoff = retry_backoff
        self._max_chars = max_chars

    @property
    def chat_id(self) -> str:
        return self._chat_id

    @property
    def max_chars(self) -> int:
        return self._max_chars

    def send(self, text: str) -> int:
        """Wysyła tekst, dzieląc go gdy przekracza limit. Zwraca liczbę wiadomości.

        Rzuca :class:`NotifyError` przy pierwszym niepowodzeniu - nie wysyłamy
        częściowo bez informacji, bo użytkownik dostanie alert, którego reszty
        nie zobaczy.
        """
        if not text.strip():
            log.warning("[telegram] pusta wiadomosc - pomijam")
            return 0

        czesci = split_message(text, self._max_chars)
        if len(czesci) > 1:
            log.info("[telegram] tekst %d B -> %d wiadomosci", len(text), len(czesci))

        for index, czesc in enumerate(czesci, 1):
            self._send_one(czesc, index, len(czesci))

        return len(czesci)

    # -- wewnetrzne ----------------------------------------------------------

    def _send_one(self, text: str, index: int, total: int) -> None:
        """Wysyła jeden fragment z ponowieniem przy awarii sieciowej lub 5xx."""
        ostatni_blad: str = ""

        for attempt in range(self._max_retries + 1):
            if attempt:
                # Krótki backoff. Bez niego chwilowa awaria API gubi alert
                # o biletach, a alert jest jedynym powodem istnienia tego skryptu.
                wait = self._retry_backoff * attempt
                log.info(
                    "[telegram] ponawiam %d/%d (czekam %.1f s)", attempt,
                    self._max_retries, wait,
                )
                time.sleep(wait)

            try:
                response = self._session.post(
                    self._api_url(),
                    json=self._payload(text),
                    timeout=self._timeout,
                )
            except requests.RequestException as exc:
                # Wyjątek sieciowy jest odwracalny, więc ponawiamy.
                ostatni_blad = f"{exc.__class__.__name__}: {exc}"
                log.warning("[telegram] fragment %d/%d: %s", index, total, ostatni_blad)
                continue

            if response.ok:
                log.info("[telegram] wyslano fragment %d/%d", index, total)
                return

            opis = self._describe(response)
            ostatni_blad = opis

            if self._is_retryable(response.status_code):
                log.warning("[telegram] fragment %d/%d: %s", index, total, opis)
                continue

            # 400/401/403/404 nie ustąpią przed ponowieniem - to błąd
            # konfiguracji albo nieistniejący czat, nie chwilowa awaria.
            raise NotifyError(
                f"Telegram odrzucil wiadomosc (HTTP {response.status_code}): {opis}"
            )

        raise NotifyError(
            f"Telegram nie odpowiedzial po {self._max_retries + 1} probach: {ostatni_blad}"
        )

    def _api_url(self) -> str:
        """Buduje adres API. Token wchodzi tu i tylko tu."""
        return f"{TELEGRAM_API_BASE}/bot{self._bot_token}/sendMessage"

    def _payload(self, text: str) -> dict[str, object]:
        """Buduje ciało żądania.

        ``parse_mode`` celowo nieobecne - patrz docstring modułu.
        """
        return {
            "chat_id": self._chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }

    @staticmethod
    def _is_retryable(status_code: int) -> bool:
        """Ponawiamy tylko błędy przejściowe i limitę."""
        return status_code == 429 or 500 <= status_code < 600

    @staticmethod
    def _describe(response: requests.Response) -> str:
        """Opis błędu bez treści odpowiedzi.

        Telegram potrafi echoować token w ciele błędu, więc **nie** wklejamy
        odpowiedzi do komunikatu. ``description`` z JSON-a jest bezpieczne,
        gdyśli API je zwróci.
        """
        opis = ""
        try:
            dane = response.json()
        except ValueError:
            dane = {}

        if isinstance(dane, dict):
            opis = str(dane.get("description", "")).strip()

        if not opis:
            opis = f"serwer odpowiedzial pustym statusem {response.status_code}"

        return opis