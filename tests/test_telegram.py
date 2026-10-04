"""Testy wysyłki przez Telegram Bot API.

Sprawdzamy **treść żądania**, nie tylko to, że wyszło 200 - to jedyny
sposób, by wychwycić ``parse_mode``, podgląd linku albo wyciek tokenu.

Uwaga o atrapach: ``responses`` nie ma własnych połączeń sieciowych, ale
też nie ma limitów Bot API. Dlatego limity (4096 znaków, 429, 5xx)
symulujemy ręcznie odpowiedziami serwera.
"""

from __future__ import annotations

import json

import pytest
import responses

from eventim_watcher.messages import format_available, format_fetch_error
from eventim_watcher.models import Series, Term
from eventim_watcher.telegram import (
    TELEGRAM_API_BASE,
    NotifyError,
    TelegramNotifier,
)

TOKEN = "1234567890:AAHkQ1exampleTOKENvalue_do_not_use_1"
CHAT_ID = "-1001234567890"
API_URL = f"{TELEGRAM_API_BASE}/bot{TOKEN}/sendMessage"

#: Odpowiedź udanego wysłania - krótka, bo Bot API zwraca dużo metadanych,
#: a klient interesuje tylko status HTTP.
SUKCES = {"status": 200, "json": {"ok": True, "result": {"message_id": 1}}}


def notifier(**kwargs) -> TelegramNotifier:
    """Notyfikator z krótkim backoffem - testy nie powinny czekać sekund."""
    defaults = {"retry_backoff": 0.0}
    defaults.update(kwargs)
    return TelegramNotifier(TOKEN, CHAT_ID, **defaults)


def series() -> Series:
    return Series(
        name="U-Bahn-Cabriotour 2026",
        url="https://www.eventim-light.com/de/a/x/s/y",
        terms=(
            Term(
                "U-Bahn-Cabriotour 2026",
                "2026-10-09T22:30:00+02:00",
                "https://www.eventim-light.com/de/a/x/e/y",
                "InStock",
                40.0,
                58.0,
                "EUR",
            ),
        ),
    )


def body_of(call_index: int = 0) -> dict:
    return json.loads(responses.calls[call_index].request.body)


# --- treść żądania ----------------------------------------------------------

@responses.activate
def test_wyslanie_na_poprawny_url():
    responses.post(API_URL, **SUKCES)

    notifier().send("tekst")

    assert responses.calls[0].request.method == "POST"
    assert responses.calls[0].request.url == API_URL


@responses.activate
def test_zadanie_ma_chat_id_tekst_i_wylaczenie_podgladu():
    responses.post(API_URL, **SUKCES)

    notifier().send("tekst alarmu")

    body = body_of()
    assert body["chat_id"] == CHAT_ID
    assert body["text"] == "tekst alarmu"
    assert body["disable_web_page_preview"] is True


@responses.activate
def test_zadanie_nie_ustawia_parse_mode():
    """Brak ``parse_mode`` chroni przed zepsuciem komunikatu przez znaki markdown.

    Nazwa wydarzenia z ``_`` przy ``parse_mode: Markdown`` unieważniłaby
    całą wiadomość błędem ``can't parse entities``.
    """
    responses.post(API_URL, **SUKCES)

    notifier().send("Rundfahrt_Sonderfahrt *EXTREM*")

    assert "parse_mode" not in body_of()


@responses.activate
def test_zadanie_nie_zawiera_nadzwyczajnych_pol():
    """Nadmiarowe pola API potrafi odrzucić - wysyłamy tylko to, co trzeba."""
    responses.post(API_URL, **SUKCES)

    notifier().send("tekst")

    assert set(body_of()) == {"chat_id", "text", "disable_web_page_preview"}


@responses.activate
def test_poprawny_typ_content_type():
    responses.post(API_URL, **SUKCES)

    notifier().send("tekst")

    assert responses.calls[0].request.headers["Content-Type"] == "application/json"


# --- powodzenie -------------------------------------------------------------

@responses.activate
def test_poprawne_wyslanie_zwraca_liczbe_wiadomosci():
    responses.post(API_URL, **SUKCES)

    assert notifier().send("tekst") == 1


@responses.activate
def test_pusty_tekst_nie_wysyla_nic():
    """Pusta wiadomość to błąd orkiestracji - nie ma sensu odpytywać API."""
    assert notifier().send("   \n  ") == 0
    assert len(responses.calls) == 0


@responses.activate
def test_dluga_wiadomosc_idzie_w_kilku_czescciach():
    responses.post(API_URL, **SUKCES)
    tekst = "\n".join(f"linia {i}" for i in range(1000))

    wyslane = notifier().send(tekst)

    assert wyslane > 1
    assert len(responses.calls) == wyslane
    for index in range(wyslane):
        assert len(body_of(index)["text"]) <= 3500


@responses.activate
def test_kazda_czesc_jest_wysylana_odzielnie():
    responses.post(API_URL, **SUKCES)

    notifier().send("\n".join(f"linia {i}" for i in range(1000)))

    teksty = [body_of(i)["text"] for i in range(len(responses.calls))]
    assert "\n".join(teksty) == "\n".join(f"linia {i}" for i in range(1000))


@responses.activate
def test_realny_alert_dostepnosci_dosiega_do_telegrama():
    """Pełna ścieżka: parser → formatowanie → wysyłka."""
    responses.post(API_URL, **SUKCES)
    dane = series()

    notifier().send(format_available(dane, dane.available_terms))

    body = body_of()
    assert "DOSTĘPNE BILETY: U-Bahn-Cabriotour 2026" in body["text"]
    assert "https://www.eventim-light.com/de/a/x/e/y" in body["text"]
    assert body["disable_web_page_preview"] is True


@responses.activate
def test_ostrzezenie_o_awarii_dosiega_do_telegrama():
    responses.post(API_URL, **SUKCES)

    notifier().send(
        format_fetch_error("https://x/s", "HTTP 403", cooldown_hours=6.0)
    )

    assert "nie udało się sprawdzić" in body_of()["text"]


# --- błędy API --------------------------------------------------------------

@responses.activate
def test_blad_400_bez_ponowienia():
    """Zly token albo nieistniejacy czat nie ustapi przed ponowieniem."""
    responses.post(
        API_URL, status=400, json={"ok": False, "description": "chat not found"}
    )

    with pytest.raises(NotifyError, match="400"):
        notifier().send("tekst")

    assert len(responses.calls) == 1


@responses.activate
def test_blad_401_bez_ponowienia():
    responses.post(
        API_URL, status=401, json={"ok": False, "description": "Unauthorized"}
    )

    with pytest.raises(NotifyError, match="Unauthorized"):
        notifier().send("tekst")

    assert len(responses.calls) == 1


@responses.activate
def test_blad_403_bez_ponowienia():
    responses.post(
        API_URL, status=403, json={"ok": False, "description": "bot was blocked"}
    )

    with pytest.raises(NotifyError, match="bot was blocked"):
        notifier().send("tekst")

    assert len(responses.calls) == 1


@responses.activate
def test_tekst_za_dlugi_nie_powoduje_wyslania_od_nowa():
    """Telegram odrzuca 400 dla za dlugiej wiadomosci - ponawianie jest bezcelowe."""
    responses.post(
        API_URL,
        status=400,
        json={"ok": False, "description": "Bad Request: message is too long"},
    )

    with pytest.raises(NotifyError, match="too long"):
        notifier().send("tekst")

    assert len(responses.calls) == 1


@responses.activate
def test_limit_429_jedno_ponowienie():
    responses.post(
        API_URL,
        status=429,
        json={"ok": False, "description": "Too Many Requests: retry after 5"},
    )
    responses.post(API_URL, **SUKCES)

    notifier().send("tekst")

    assert len(responses.calls) == 2


@responses.activate
def test_blad_500_jedno_ponowienie():
    responses.post(API_URL, status=500, json={"ok": False, "description": "server"})
    responses.post(API_URL, **SUKCES)

    notifier().send("tekst")

    assert len(responses.calls) == 2


@responses.activate
def test_blad_503_jedno_ponowienie():
    responses.post(API_URL, status=503, json={"ok": False, "description": "unavailable"})
    responses.post(API_URL, **SUKCES)

    notifier().send("tekst")

    assert len(responses.calls) == 2


@responses.activate
def test_ponowienia_sa_ograniczone_do_jednego():
    """Lawina przy awarii API musi mieć koniec."""
    responses.post(API_URL, status=500, json={"ok": False, "description": "server"})
    responses.post(API_URL, status=500, json={"ok": False, "description": "server"})

    with pytest.raises(NotifyError, match="po 2 probach"):
        notifier().send("tekst")

    assert len(responses.calls) == 2


@responses.activate
def test_brak_ponowien_gdy_wylaczone():
    responses.post(API_URL, status=500, json={"ok": False, "description": "server"})

    with pytest.raises(NotifyError, match="po 1 probach"):
        notifier(max_retries=0).send("tekst")

    assert len(responses.calls) == 1


@responses.activate
def test_wyjatak_sieciowy_jest_ponawiany():
    import requests

    responses.post(API_URL, body=requests.exceptions.ConnectionError("brak sieci"))
    responses.post(API_URL, **SUKCES)

    notifier().send("tekst")

    assert len(responses.calls) == 2


@responses.activate
def test_wyjatek_sieciowy_po_wykorzystaniu_ponowien():
    import requests

    responses.post(API_URL, body=requests.exceptions.ReadTimeout("timeout"))
    responses.post(API_URL, body=requests.exceptions.ReadTimeout("timeout"))

    with pytest.raises(NotifyError, match="ReadTimeout"):
        notifier().send("tekst")

    assert len(responses.calls) == 2


@responses.activate
def test_timeout_wyjatkiem_nie_przecieka():
    import requests

    responses.post(API_URL, body=requests.exceptions.Timeout("timeout"))

    with pytest.raises(NotifyError):
        notifier().send("tekst")


@responses.activate
def test_komunikat_bledu_nie_zawiera_tokenu():
    """Token siedzi w URL - opis odpowiedzi mógłby go wyciągnąć do logu."""
    responses.post(API_URL, status=400, json={"ok": False, "description": "zły token"})

    with pytest.raises(NotifyError) as excinfo:
        notifier().send("tekst")

    komunikat = str(excinfo.value)
    assert TOKEN not in komunikat
    assert "1234567890:" not in komunikat


@responses.activate
def test_komunikat_bledu_nie_wkleja_cala_odpowiedzi():
    """Ciało odpowiedzi bywa zapisane w logu runa - nie wchodzi do wyjatku."""
    responses.post(
        API_URL,
        status=400,
        json={"ok": False, "description": "zly format", "echo": f"TOKEN={TOKEN}"},
    )

    with pytest.raises(NotifyError) as excinfo:
        notifier().send("tekst")

    assert TOKEN not in str(excinfo.value)


@responses.activate
def test_odpowiedz_bez_json_nie_wywala():
    """Nietypowe ciało odpowiedzi nie może zamienić błędu w wyjątek parsowania."""
    responses.post(API_URL, status=502, body="<html>502 Bad Gateway</html>")

    with pytest.raises(NotifyError) as excinfo:
        notifier().send("tekst")

    assert "502" in str(excinfo.value)


@responses.activate
def test_wyslanie_drugiej_czesci_konczy_wyslanie():
    """Utrata drugiej czesci bez zgloszenia bylaby cisza zamiast alarmu."""
    responses.post(API_URL, **SUKCES)
    responses.post(
        API_URL, status=400, json={"ok": False, "description": "chat not found"}
    )
    tekst = "\n".join(f"linia {i}" for i in range(1000))

    with pytest.raises(NotifyError, match="chat not found"):
        notifier().send(tekst)

    assert len(responses.calls) > 1


# --- ekspozycja --------------------------------------------------------------

def test_chat_id_dostepny_do_odczytu():
    assert notifier().chat_id == CHAT_ID


def test_limit_znakow_konfigurowalny():
    assert notifier().max_chars == 3500
    assert notifier(max_chars=100).max_chars == 100


def test_ujemna_liczba_ponowien_redukuje_sie_do_zera():
    """Wartownik chroni przed ``range(-1)`` - brak ponowień, nie wyjątek."""
    assert notifier(max_retries=-3)._max_retries == 0