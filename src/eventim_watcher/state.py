"""Trwały stan między uruchomieniami: cookies Queue-it i znaczniki czasu.

Moduł odpowiada za :class:`StateStore`, czyli za dwie rzeczy, których
program potrzebuje po restarcie processu:

* **cookies sesji** - bez nich handshake trwa 5 hopów i ok. 1,2 s zamiast
  1 hopu i ok. 0,1 s (ADR-6, architektura §2.6),
* **znaczniki czasu** - bez ``last_error_notified_at`` ostrzeżenia o awarii
  szłyby co godzinę, czyli 24 wiadomości na dobę o tym samym problemie.

Decyzje projektowe (docs/technical-plan.md, sekcja 6):

**Cache jest optymalizacją, nie wymaganiem.** ``load()`` i ``save()`` nie rzucają
wyjątków — ani przy braku pliku, ani przy uszkodzonym JSON-ie, ani przy
katalogu, do którego nie da się zapisać. Program ma działać, gdy cache zniknie
wskutek wygaśnięcia ``actions/cache`` po 7 dniach; to poprawna degradacja,
nie awaria.

**Nieznana ``schema_version`` oznacza pełny reset, nie tylko pominięcie cookies.**
Zmiana nagłówków albo struktury Queue-it unieważnia token — próba odtworzenia
starych cookies skończyłaby się pętlą 403. Odczytywanie częściowego stanu przy
nieznanej wersji byłoby czystym ryzykiem bez żadnej korzyści.

**Zapis atomowy.** Najpierw plik tymczasowy, potem ``os.replace``. Przerwanie
w połowie zapisu nie może zostawić uszkodzonego JSON-a, bo plik uszkodzony
wygląda dokładnie tak samo jak cache wygasły — a w obu przypadkach tracimy
cookies po cichu.

**Token Telegrama nigdy nie trafia tutaj.** W pliku są cookies sesji sklepu,
nie dane dostępowe do konta.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path

__all__ = [
    "AVAILABILITY_STATES",
    "FileStateStore",
    "SCHEMA_VERSION",
    "StateStore",
    "availability_state",
    "initial_state",
    "mark_anomaly_notified",
    "mark_check_failed",
    "mark_check_ok",
    "mark_error_notified",
    "parse_timestamp",
    "should_warn",
    "should_warn_anomaly",
]

log = logging.getLogger("eventim_watcher.state")

#: Wersja ksztaltu zapisywanego JSON-a. Zmiana formatu oznacza bump i reset
#: cache - patrz decyzja o nieznanej wersji w docstringu modulu.
SCHEMA_VERSION = 1

#: Domyslna lokalizacja pliku stanu. `.cache/` jest w `.gitignore`, a
#: `actions/cache` w Fazy 7 wskazywuje na `path:` tego samego katalogu.
DEFAULT_STATE_PATH = ".cache/eventim-state.json"

#: Stany dostepnosci zapisywane w `last_availability_state`. Wartości sa
#: czytelne w logu runa, wylacznie informacyjne - patrz `availability_state`.
AVAILABILITY_STATES = ("none_available", "some_available", "all_unknown")


def initial_state() -> dict:
    """Stan pierwszego uruchomienia: brak cookies, brak historii."""
    return {
        "schema_version": SCHEMA_VERSION,
        "cookies": [],
        "last_check_ok": None,
        "last_check_at": None,
        "last_error_notified_at": None,
        "last_anomaly_notified_at": None,
        "last_availability_state": None,
    }


def parse_timestamp(value: object) -> datetime | None:
    """Zamienia znacznik z JSON-a na ``datetime`` w UTC.

    Brak, ``null``, pusty string i smierc wartosci zwracaja ``None`` - uszkodzony
    znacznik to nie wyjatek. Ostatecznie sprowadza sie do "nie pamietamy", czyli
    do ostrzezenia przy najblizszym uruchomieniu, a nie do cichego wyciszenia.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        log.warning("[state] nieczytelny znacznik czasu %r - traktujemy jako brak", value)
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def format_timestamp(moment: datetime) -> str:
    """Odwrotność :func:`parse_timestamp` — zapis do JSON-a w UTC."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat()


def now_utc() -> datetime:
    """Teraz w UTC. Osobna funkcja, zeby testy mogly podstawic czas."""
    return datetime.now(timezone.utc)


# -- przejscia stanu ---------------------------------------------------------


def availability_state(
    dostepne: int, nieznane: int
) -> str:
    """Opisuje stan dostepnosci jednym slowem do logu i do cache.

    Rozroznia ``all_unknown`` od ``none_available``, bo to dwa calkiem
    rozne komunikaty dla uzytkownika: "nie ma" i "nie wiem".
    """
    if dostepne:
        return "some_available"
    if nieznane:
        return "all_unknown"
    return "none_available"


def mark_check_ok(
    state: dict,
    *,
    moment: datetime,
    stan: str,
    cookies: list[dict] | None = None,
) -> dict:
    """Zapisuje udane sprawdzenie. Zwraca ten sam slownik, dla czytelnosci.

    ``cookies`` podajemy przy kazdym udanym pobraniu, takze wtedy gdy wszystko
    jest wyprzedane - martwy Telegram nie moze powodowac utraty cookies.
    """
    state["last_check_ok"] = True
    state["last_check_at"] = format_timestamp(moment)
    state["last_availability_state"] = stan
    if cookies is not None:
        state["cookies"] = cookies
    return state


def mark_check_failed(state: dict, *, moment: datetime) -> dict:
    """Zapisuje nieudane sprawdzenie.

    Cookies celowo zostawiamy bez zmian - token moze wciaz dzialac, a awaria
    sieciowa nie jest powodem do kazdego razu od nowa budowania handshake'u.
    ``last_error_notified_at`` tez zostaje: to on decyduje o wyciszeniu.
    """
    state["last_check_ok"] = False
    state["last_check_at"] = format_timestamp(moment)
    return state


def mark_error_notified(state: dict, *, moment: datetime) -> dict:
    """Zapisuje czas wyslania ostrzezenia o awarii."""
    state["last_error_notified_at"] = format_timestamp(moment)
    return state


def should_warn(
    state: Mapping[str, object],
    *,
    moment: datetime,
    cooldown: timedelta,
) -> bool:
    """Czy wyslac ostrzezenie o awarii, czy zostac cicho.

    Zasada: ostrzegamy, dopoki poprzednie sprawdzenie nie bylo wlasnie
    nieudane. Po jednym ostrzezeniu wyciszamy sie na ``cooldown``; kazde
    kolejne nieudane sprawdzenie w tym oknie nie generuje nowej wiadomosci.

    Pierwsze ostrzezenie nigdy nie jest wyciszone - ``last_error_notified_at``
    wynoszace ``None`` oznacza, ze problemu jeszcze nie zgloszono. Inaczej
    swiezy blad bylby cichy przy pierwszym uruchomieniu po wdrozeniu.
    """
    if state.get("last_check_ok") is not False:
        # Poprzednie sprawdzenie nie bylo nieudane (albo nie bylo w ogole).
        return True

    ostatnie = parse_timestamp(state.get("last_error_notified_at"))
    if ostatnie is None:
        return True

    if cooldown <= timedelta(0):
        return True

    return moment - ostatnie >= cooldown


def mark_anomaly_notified(state: dict, *, moment: datetime) -> dict:
    """Zapisuje czas zgloszenia anomalii braku pola ``availability``.

    Osobny kanal obok ``last_error_notified_at``, bo anomalia i awaria
    pobierania to dwa rozne problemy. Wspolny cooldown pozwolilby, zeby
    wyciszony komunikat o awarii zamaskowal pierwsze zgloszenie anomalii —
    czyli swiezy problem pozostalby cichy.
    """
    state["last_anomaly_notified_at"] = format_timestamp(moment)
    return state


def should_warn_anomaly(
    state: Mapping[str, object],
    *,
    moment: datetime,
    cooldown: timedelta,
) -> bool:
    """Czy wyslac ostrzezenie o anomalii, czy zostac cicho.

    Swiadomie nie czyta ``last_check_ok``: anomalia to nie awaria pobierania,
    a mieszanie obu kanalow pozwoliloby jednemu wyciszyc drugi.
    """
    ostatnie = parse_timestamp(state.get("last_anomaly_notified_at"))
    if ostatnie is None:
        return True
    if cooldown <= timedelta(0):
        return True
    return moment - ostatnie >= cooldown


# -- magazyn stanu -----------------------------------------------------------


class StateStore:
    """Interfejs magazynu stanu.

    Rozdzielenie od :class:`FileStateStore` pozwala podmienic backend
    (``actions/cache``, baza, s3) bez dotykania logiki orkiestracji. Metody
    sa z założenia odporne na błędy — patrz decyzje w docstringu modułu.
    """

    def load(self) -> dict:  # pragma: no cover - interfejs
        """Zwraca stan wczytany z magazynu albo stan domyślny."""
        raise NotImplementedError

    def save(self, data: dict) -> None:  # pragma: no cover - interfejs
        """Zapisuje stan. Nie rzuca wyjątku przy braku dostępu do magazynu."""
        raise NotImplementedError


class FileStateStore(StateStore):
    """Stan w jednym pliku JSON.

    Ścieżka katalogu jest wskazywana przez ``actions/cache``, wiec zapis musi
    byc atomowy — przerywany run nie moze zostawic pliku, ktory wyglada jak
    uszkodzony, ale nim jest.
    """

    def __init__(self, path: str | Path = DEFAULT_STATE_PATH) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    # -- odczyt -------------------------------------------------------------

    def load(self) -> dict:
        """Wczytuje stan, nigdy nie rzucając wyjątku.

        Każda niepowodzenie prowadzi do stanu domyślnego z wpisem w logu:
        brak pliku to sytuacja normalna (pierwsze uruchomienie, wygasły cache),
        uszkodzony JSON to ciekawostka diagnostyczna.
        """
        try:
            surowy = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            log.info("[state] brak pliku stanu (%s) - zaczynamy od zera", self._path)
            return initial_state()
        except OSError as exc:
            log.warning("[state] nie da sie odczytac %s (%s) - zaczynamy od zera",
                        self._path, exc.__class__.__name__)
            return initial_state()

        try:
            dane = json.loads(surowy)
        except json.JSONDecodeError as exc:
            log.warning("[state] uszkodzony JSON w %s (%s) - zaczynamy od zera", self._path, exc)
            return initial_state()

        if not isinstance(dane, dict):
            log.warning("[state] %s nie zawiera obiektu JSON - zaczynamy od zera", self._path)
            return initial_state()

        wersja = dane.get("schema_version")
        if wersja != SCHEMA_VERSION:
            log.warning(
                "[state] nieznana wersja schematu w %s (jest %r, oczekiwano %r) - "
                "cache pominiety, cookies odtwarzane od nowa",
                self._path, wersja, SCHEMA_VERSION,
            )
            return initial_state()

        stan = initial_state()
        stan.update(dane)
        stan["cookies"] = _valid_cookies(dane.get("cookies"))
        stan["schema_version"] = SCHEMA_VERSION
        return stan

    # -- zapis --------------------------------------------------------------

    def save(self, data: dict) -> None:
        """Zapisuje stan atomowo. Błąd zapisu jest logowany, nie podnoszony.

        Brak zapisanego stanu oznacza tylko utratę cookies i wyciszenia
        ostrzeżeń — obie rzeczy kosztują czas, nie poprawność.
        """
        payload = initial_state()
        payload.update({k: v for k, v in data.items() if k in payload})
        payload["schema_version"] = SCHEMA_VERSION

        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.warning("[state] nie udalo sie utworzyc katalogu %s: %s - pomijam zapis",
                        self._path.parent, exc)
            return

        # Plik tymczasowy w tym samym katalogu, bo os.replace dziala tylko
        # w obrebie jednego systemu plikow.
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self._path.parent,
                prefix=self._path.name + ".",
                suffix=".tmp",
                delete=False,
            ) as uchwyt:
                json.dump(payload, uchwyt, ensure_ascii=False, indent=2, sort_keys=True)
                uchwyt.write("\n")
                tymczasowy = Path(uchwyt.name)
        except (OSError, TypeError, ValueError) as exc:
            log.warning("[state] zapis %s nieudany (%s) - pomijam", self._path, exc)
            return

        try:
            os.replace(tymczasowy, self._path)
        except OSError as exc:
            log.warning("[state] podmiana %s nieudana (%s) - pomijam zapis",
                        self._path, exc)
            _discard(tymczasowy)
            return

        _restrict_permissions(self._path)

        log.debug(
            "[state] zapisano %s (cookies=%d, check_ok=%r, stan=%r)",
            self._path,
            len(payload.get("cookies") or []),
            payload.get("last_check_ok"),
            payload.get("last_availability_state"),
        )


def _valid_cookies(value: object) -> list[dict]:
    """Przepuszcza tylko wplywajace wpisy ciasteczek.

    Cache jest zapisywany przez nas, wiec dziurawy wpis oznacza uszkodzenie
    pliku lub reczna edycje. Oba przypadki prowadza do jednego: rezygnujemy
    z tego wpisu, a nie z calego cache.
    """
    if not isinstance(value, list):
        return []

    poprawne: list[dict] = []
    for wpis in value:
        if not isinstance(wpis, dict):
            continue
        name = wpis.get("name")
        value_cookie = wpis.get("value")
        domain = wpis.get("domain")
        if not (isinstance(name, str) and name):
            continue
        if not (isinstance(value_cookie, str) and value_cookie):
            continue
        if not (isinstance(domain, str) and domain):
            continue
        sciezka = wpis.get("path")
        poprawne.append(
            {
                "domain": domain,
                "path": sciezka if isinstance(sciezka, str) and sciezka else "/",
                "name": name,
                "value": value_cookie,
            }
        )

    if len(poprawne) != len(value):
        log.warning("[state] odrzucono %d uszkodzonych wpisow cookies",
                    len(value) - len(poprawne))

    return poprawne


def _restrict_permissions(path: Path) -> None:
    """Ogranicza plik do wlasciciela. Na Windows bez efektu — i tak tam działa."""
    try:
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover - platformy bez chmod
        pass


def _discard(path: Path) -> None:
    """Usuwa plik tymczasowy po nieudanej podmianie."""
    try:
        path.unlink()
    except OSError:  # pragma: no cover - plik moze juz nie istniec
        pass