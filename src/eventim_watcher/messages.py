"""Formatowanie komunikatów Telegram po polsku.

Moduł jest **czysty**: brak I/O, brak zależności od `requests`. Dzięki temu
testy sprawdzają dokładny tekst komunikatu bez sieci i bez atrap HTTP.

Trzy decyzje warte odnotowania:

**Nazwy dni liczymy ręcznie, nie przez ``strftime``.** ``%a`` zależy od
lokalizacji procesu, a runner GitHub Actions zwraca angielskie skróty
(``Fri``) zamiast polskiego ``pt``. Tabela w :data:`DNI` jest jedynym
źródłem prawdy.

**Brak ``parse_mode``** (patrz ``telegram.py``) oznacza, że nie wolno używać
znaczników Markdown. Nazwy wydarzeń zawierają ``-`` i spacje, a przy
``parse_mode`` znak ``_`` w nazwie zepsułby całą wiadomość.

**Czas sprawdzenia w UTC**, nie w lokalnej strefie procesu. Alerty przychodzą
o różnych porach i w różnych strefach; UTC jest jedynym zapisem porównywalnym
między uruchomieniami.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from eventim_watcher.models import Series, Term

__all__ = [
    "MAX_MESSAGE_CHARS",
    "format_available",
    "format_fetch_error",
    "format_no_tickets",
    "format_unknown_availability",
    "format_price",
    "format_term_date",
    "split_message",
]

#: Telegram odrzuca wiadomości dłuższe niż 4096 znaków. Dzielimy z zapasem,
#: bo licznik Telegram uwzględnia też znaki spoza ASCII liczone jako 2 bajty.
MAX_MESSAGE_CHARS = 3500

#: Najdłuższy fragment przyczyny błędu w ostrzeżeniu. Pełny komunikat
#: trafia do logu; na Telegrama idzie tylko to, co da się przeczytać.
ERROR_EXCERPT_CHARS = 300

#: Skróty dni po polsku. ``strftime("%a")`` zależy od lokalizacji procesu.
DNI = ("poniedziałek", "wtorek", "środa", "czwartek", "piątek", "sobota", "niedziela")

#: Skróty dni do komunikatu - krótsze i czytelniejsze w Telegramie.
DNI_SKROT = ("pon", "wt", "śr", "czw", "pt", "sob", "ndz")

MIESIACE = (
    "stycznia", "lutego", "marca", "kwietnia", "maja", "czerwca",
    "lipca", "sierpnia", "września", "października", "listopada", "grudnia",
)


def format_term_date(start: str) -> str:
    """Zamienia ``2026-10-09T19:00:00+02:00`` na ``piątek, 09.10.2026, 19:00 (UTC+02:00)``.

    Zachowujemy offset z danych źródłowych. Konwersja do UTC przesunęłaby
    „19:00" w Berlinie na „17:00", a godzina rozpoczęcia wydarzenia jest
    istotna dla kupującego.
    """
    try:
        moment = datetime.fromisoformat(start)
    except (TypeError, ValueError):
        # Uszkodzona data nie może wywracać monitoringu - pokazujemy surową.
        return start or "-"

    if moment.tzinfo is None:
        return f"{_date_part(moment)}, {_time_part(moment)} (czas nieznany)"

    offset = moment.utcoffset() or timedelta(0)
    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    magnitude = abs(total_minutes)
    offset_text = f"UTC{sign}{magnitude // 60:02d}:{magnitude % 60:02d}"

    return f"{_date_part(moment)}, {_time_part(moment)} ({offset_text})"


def _date_part(moment: datetime) -> str:
    """``piątek, 09.10.2026`` z ręcznie policzonym skrótem dnia."""
    dzien = DNI_SKROT[moment.weekday()]
    return f"{dzien}, {moment.day:02d}.{moment.month:02d}.{moment.year}"


def _time_part(moment: datetime) -> str:
    return f"{moment.hour:02d}:{moment.minute:02d}"


def format_price(term: Term) -> str:
    """Zakres cen jednego terminu, np. ``40-58 EUR``.

    ``low == high`` to jedna liczba, bo „40-40 EUR" wygląda jak błąd.
    Brak danych o cenie to jawny „cena niedostępna", nie pominięcie sekcji —
    odbiorca musi wiedzieć, że cena jest nieznana, a nie żadna.
    """
    if term.low_price is None and term.high_price is None:
        return "cena niedostępna"

    currency = f" {term.currency}" if term.currency else ""
    low = term.low_price
    high = term.high_price

    if low is not None and high is not None:
        if low == high:
            return f"{low:g}{currency}"
        return f"{low:g}-{high:g}{currency}"
    if low is not None:
        return f"od {low:g}{currency}"
    return f"do {high:g}{currency}"


def format_available(
    series: Series,
    terms: tuple[Term, ...],
    *,
    now: datetime | None = None,
) -> str:
    """Alert o dostępności biletów.

    Zawiera nazwę serii, liczbę dostępnych terminów, listę z datą, ceną
    i linkiem do zakupu oraz stopkę z czasem sprawdzenia w UTC.
    """
    chwila = _as_utc(now)
    naglowek = (
        f"DOSTĘPNE BILETY: {series.name}\n"
        f"{len(terms)} z {len(series.terms)} terminów dostępnych"
    )

    bloki = [_format_term_block(term, index) for index, term in enumerate(terms, 1)]
    stopka = f"Sprawdzono: {format_timestamp(chwila)} UTC"

    return _join(naglowek, *bloki, stopka)


def format_no_tickets(
    series: Series,
    *,
    now: datetime | None = None,
) -> str:
    """Informacja o braku dostępnych biletów.

    Wysyłana nawet gdy nie ma dostępnych terminów — użytkownik prosił
    o wiadomość przy każdym sprawdzeniu, nie tylko przy alertach.
    """
    chwila = _as_utc(now)
    return "\n".join(
        [
            "BRAK DOSTĘPNYCH BILETÓW",
            "",
            f"Seria: {series.name}",
            f"Wszystkie terminy ({len(series.terms)}) są obecnie niedostępne.",
            f"Sprawdzono: {format_timestamp(chwila)} UTC",
        ]
    )


def _format_term_block(term: Term, index: int) -> str:
    """Jeden termin w alertzie. Nazwa tylko gdy różni się od reszty serii."""
    linie = [
        "",
        f"{index}. {format_term_date(term.start)}",
        f"   {term.name}",
        f"   stan: {term.label}",
        f"   {format_price(term)}",
        f"   {term.url}",
    ]
    return "\n".join(linie)


def format_fetch_error(
    target: str,
    error: str,
    *,
    cooldown_hours: float,
    now: datetime | None = None,
) -> str:
    """Ostrzeżenie, że **nie udało się sprawdzić** dostępności.

    Rozróżnienie „nie umiem sprawdzić" od „nie ma biletów" ma znaczenie
    operacyjne: pierwsze wymaga reakcji, drugie nie.
    """
    chwila = _as_utc(now)
    przyczyna = _excerpt(error)
    powtorka = (
        f"Następna próba za około {cooldown_hours:g} h"
        if cooldown_hours > 0
        else "Następna próba przy kolejnym uruchomieniu"
    )

    return "\n".join(
        [
            "OSTRZEŻENIE: nie udało się sprawdzić dostępności biletów.",
            "",
            f"To NIE znaczy, że biletów braku - sprawdzenie nie doszło do sklepu.",
            "",
            f"Sklep: {target}",
            f"Przyczyna: {przyczyna}",
            "",
            f"{powtorka}.",
            f"Sprawdzono: {format_timestamp(chwila)} UTC",
        ]
    )


def format_unknown_availability(
    series: Series,
    *,
    now: datetime | None = None,
) -> str:
    """Ostrzeżenie o anomalii: brak pola ``availability`` (ADR-9).

    Nie jest to alert o biletach. Nieobecne pole oznacza, że **nie wiemy**,
    a nie że nic nie ma — bez tego ostrzeżenia cicha utrata monitoringu
    wyglądałaby jak zwykłe „wyprzedane".
    """
    chwila = _as_utc(now)
    nieznane = series.unknown_terms
    przyklady = ", ".join(
        f"{format_term_date(term.start)} ({term.url})" for term in nieznane[:3]
    )
    if len(nieznane) > 3:
        przyklady += f" i {len(nieznane) - 3} więcej"

    return "\n".join(
        [
            "OSTRZEŻENIE: brak danych o dostępności.",
            "",
            f"Seria: {series.name}",
            f"{len(nieznane)} z {len(series.terms)} terminów nie zawiera pola "
            "availability w danych strony.",
            "",
            "Możliwe, że Eventim zmienił układ strony. Nie wysyłamy alarmu "
            "o biletach, bo ich dostępność jest nieznana.",
            "",
            f"Terminy: {przyklady}",
            f"Sprawdzono: {format_timestamp(chwila)} UTC",
        ]
    )


def split_message(text: str, limit: int = MAX_MESSAGE_CHARS) -> list[str]:
    """Dzieli tekst na wiadomości mieszczące się w limicie Telegrama.

    Dzielimy po **liniach**, nie po znakach: ucięcie w połowie adresu URL
    daje link, którego nie da się kliknąć.
    """
    if limit <= 0:
        raise ValueError("limit musi byc dodatni")

    linie = text.split("\n")
    czesci: list[str] = []
    biezaca: list[str] = []

    for linia in linie:
        kandydat = "\n".join([*biezaca, linia])
        if biezaca and len(kandydat) > limit:
            czesci.append("\n".join(biezaca))
            biezaca = [linia]
        else:
            biezaca.append(linia)

    if biezaca:
        czesci.append("\n".join(biezaca))

    return czesci


def format_timestamp(moment: datetime) -> str:
    """``2026-10-04 18:30:05`` - do stopki komunikatu."""
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def _join(*bloki: str) -> str:
    return "\n".join(bloki)


def _as_utc(now: datetime | None) -> datetime:
    """Zamienia podany czas na UTC; brak oznacza 'teraz' w UTC."""
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def _excerpt(text: str, limit: int = ERROR_EXCERPT_CHARS) -> str:
    """Jednolinijkowy, obcięty fragment przyczyny."""
    collapsed = " ".join(str(text).split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1] + "…"