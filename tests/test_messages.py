"""Testy formatowania komunikatów Telegram.

Moduł ``messages`` jest czysty (bez I/O), więc te testy sprawdzają **dokładny
tekst** bez sieci i bez atrap HTTP. To najtańsze miejsce na wyłapanie literówki
w komunikacie, którego użytkownik nie zobaczy w logach.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from conftest import make_html, set_availability, set_sub_events

from eventim_watcher.messages import (
    ERROR_EXCERPT_CHARS,
    format_available,
    format_fetch_error,
    format_price,
    format_term_date,
    format_unknown_availability,
    split_message,
)
from eventim_watcher.models import Series, Term
from eventim_watcher.parser import parse_series

#: Stały punkt odniesienia - testy komunikatów muszą być powtarzalne,
#: a ``datetime.now()`` za każdym razem dawałby inne stopyki.
STALA_CHWILA = datetime(2026, 10, 4, 18, 30, 5, tzinfo=timezone.utc)


def term(**kwargs) -> Term:
    """Termin z sensownymi wartościami domyślnymi."""
    dane = {
        "name": "U-Bahn-Cabriotour 2026",
        "start": "2026-10-09T19:00:00+02:00",
        "url": "https://www.eventim-light.com/de/a/x/e/y",
        "availability": "InStock",
        "low_price": 40.0,
        "high_price": 58.0,
        "currency": "EUR",
    }
    dane.update(kwargs)
    return Term(**dane)


def series_z_terminami(*terminy: Term) -> Series:
    return Series(name="U-Bahn-Cabriotour 2026", url="https://x/s", terms=terminy)


# --- formatowanie dat -------------------------------------------------------

@pytest.mark.parametrize(
    ("start", "oczekiwane"),
    [
        ("2026-10-09T19:00:00+02:00", "pt, 09.10.2026, 19:00 (UTC+02:00)"),
        ("2026-10-16T22:30:00+02:00", "pt, 16.10.2026, 22:30 (UTC+02:00)"),
        ("2026-10-17T19:00:00+01:00", "sob, 17.10.2026, 19:00 (UTC+01:00)"),
        ("2026-10-07T09:15:00+02:00", "śr, 07.10.2026, 09:15 (UTC+02:00)"),
        ("2026-10-12T20:00:00+01:00", "pon, 12.10.2026, 20:00 (UTC+01:00)"),
        ("2026-10-11T20:00:00+02:00", "ndz, 11.10.2026, 20:00 (UTC+02:00)"),
        ("2026-10-08T20:00:00+02:00", "czw, 08.10.2026, 20:00 (UTC+02:00)"),
        ("2026-10-13T20:00:00+02:00", "wt, 13.10.2026, 20:00 (UTC+02:00)"),
        ("2026-10-14T20:00:00+02:00", "śr, 14.10.2026, 20:00 (UTC+02:00)"),
    ],
)
def test_formatowanie_daty(start: str, oczekiwane: str):
    assert format_term_date(start) == oczekiwane


def test_dzien_tygodnia_nie_zalezy_od_lokalizacji_procesu():
    """``strftime("%a")`` zwróciłoby angielskie skróty na runnerze Actions.

    Dlatego skróty dni są policzone ręcznie, a nie z formatowania daty.
    """
    wynik = format_term_date("2026-10-09T19:00:00+02:00")

    assert wynik.startswith("pt")
    assert "Fri" not in wynik


def test_offset_ujemny_ma_znak_minus():
    assert "(UTC-05:00)" in format_term_date("2026-10-09T19:00:00-05:00")


def test_offset_zero_z_kolami_dla_czytelnosci():
    assert "(UTC+00:00)" in format_term_date("2026-10-09T19:00:00+00:00")


def test_offset_o_minucie_nie_jest_zaokraglany():
    """Strefa +05:30 to realny przypadek - dzielenie przez 60 gubiłoby minutę."""
    assert "(UTC+05:30)" in format_term_date("2026-10-09T19:00:00+05:30")


def test_data_bez_offsetu_oznaczona_jako_nieznana():
    assert "czas nieznany" in format_term_date("2026-10-09T19:00:00")


def test_data_uszkodzona_pokazana_surowo_bez_wyjatku():
    """Uszkodzona data nie może wywrócić monitoringu."""
    assert format_term_date("to-nie-jest-data") == "to-nie-jest-data"


def test_data_pusta_daje_myslnik():
    assert format_term_date("") == "-"


# --- formatowanie cen -------------------------------------------------------

def test_zakres_cen():
    assert format_price(term(low_price=40.0, high_price=58.0)) == "40-58 EUR"


def test_jedna_cena_bez_zakresu():
    """„40-40 EUR" wygląda jak błąd."""
    assert format_price(term(low_price=40.0, high_price=40.0)) == "40 EUR"


def test_cena_uzywa_kropki_dziesietnej():
    """Calkowita cena nie powinna mieć „45.00" - to szum w komunikacie."""
    assert format_price(term(low_price=45.0, high_price=45.0)) == "45 EUR"


def test_cena_ulamkowa_bez_zer():
    assert format_price(term(low_price=12.5, high_price=12.5)) == "12.5 EUR"


def test_brak_ceny_jawne_nie_pominiety():
    """Odbiorca musi wiedzieć, że cena jest nieznana, a nie żadna."""
    assert format_price(term(low_price=None, high_price=None)) == "cena niedostępna"


def test_tylko_dolna_cena():
    assert format_price(term(low_price=40.0, high_price=None)) == "od 40 EUR"


def test_tylko_gorna_cena():
    assert format_price(term(low_price=None, high_price=58.0)) == "do 58 EUR"


def test_brak_waluty_bez_ogonka():
    assert format_price(term(currency=None, low_price=40.0, high_price=40.0)) == "40"


def test_waluta_inna_niz_eur():
    assert format_price(term(currency="PLN")) == "40-58 PLN"


# --- alert o dostepnosci ----------------------------------------------------

def test_alert_zawiera_nazwe_serii_i_zliczenie():
    """Zliczenie dotyczy całej serii, lista pozycji tylko dostępnych terminów."""
    seria = series_z_terminami(
        term(availability="SoldOut"), term(), term(availability="SoldOut")
    )

    tekst = format_available(seria, seria.available_terms, now=STALA_CHWILA)

    assert "DOSTĘPNE BILETY: U-Bahn-Cabriotour 2026" in tekst
    assert "1 z 3 terminów dostępnych" in tekst
    assert "1. pt" in tekst
    assert "2. pt" not in tekst


def test_alert_zawiera_date_cene_stan_i_link():
    seria = series_z_terminami(term())

    tekst = format_available(seria, seria.available_terms, now=STALA_CHWILA)

    assert "pt, 09.10.2026, 19:00 (UTC+02:00)" in tekst
    assert "40-58 EUR" in tekst
    assert "stan: dostepne" in tekst
    assert "https://www.eventim-light.com/de/a/x/e/y" in tekst


def test_alert_konczy_sie_czasem_sprawdzenia():
    seria = series_z_terminami(term())

    tekst = format_available(seria, seria.available_terms, now=STALA_CHWILA)

    assert tekst.rstrip().endswith("Sprawdzono: 2026-10-04 18:30:05 UTC")


def test_alert_przelicza_czas_na_utc():
    """Godzina lokalna procesu na runnerze to UTC, ale stopka ma być jednoznaczna."""
    chwila = datetime(2026, 10, 4, 20, 30, 5, tzinfo=timezone(timedelta(hours=2)))
    seria = series_z_terminami(term())

    tekst = format_available(seria, seria.available_terms, now=chwila)

    assert "Sprawdzono: 2026-10-04 18:30:05 UTC" in tekst


def test_alert_ma_numerowane_pozycje():
    seria = series_z_terminami(term(), term(start="2026-10-16T22:30:00+02:00"))

    tekst = format_available(seria, seria.available_terms, now=STALA_CHWILA)

    assert "1. pt, 09.10.2026" in tekst
    assert "2. pt, 16.10.2026" in tekst


def test_alert_nie_escapuje_znacznikow_markdown():
    """Brak ``parse_mode`` oznacza, że znaczniki są dosłowne - nie wolno ich ruszać.

    Zmiana treści przy escapowaniu byłaby fałszywa: użytkownik dostałby
    komunikat inny niż ten, który testujemy.
    """
    seria = series_z_terminami(term(name="Rundfahrt_Sonderfahrt *EXTREM*"))

    tekst = format_available(seria, seria.available_terms, now=STALA_CHWILA)

    assert "Rundfahrt_Sonderfahrt *EXTREM*" in tekst
    assert "\\*" not in tekst
    assert "\\_" not in tekst


def test_alert_gdy_brak_dostepnych_terminow_nie_wymysla_linkow():
    """Pusty alert to błąd orkiestracji, nie przypadek formatowania."""
    seria = series_z_terminami(term(availability="SoldOut"))

    tekst = format_available(seria, (), now=STALA_CHWILA)

    assert "0 z 1 terminów dostępnych" in tekst
    assert "/e/" not in tekst


def test_alert_bz_argumentu_czasu_uzywa_teraz():
    seria = series_z_terminami(term())

    tekst = format_available(seria, seria.available_terms)

    assert "Sprawdzono: " in tekst
    assert tekst.rstrip().endswith("UTC")


# --- ostrzezenie o awarii ---------------------------------------------------

def test_awaria_rozroznia_nieudane_sprawdzenie_od_braku_biletow():
    """Rozróżnienie ma znaczenie operacyjne: pierwsze wymaga reakcji."""
    tekst = format_fetch_error(
        "https://www.eventim-light.com/de/a/x/s/y",
        "HTTP 403",
        cooldown_hours=6.0,
        now=STALA_CHWILA,
    )

    assert "nie udało się sprawdzić dostępności" in tekst
    assert "NIE znaczy, że biletów braku" in tekst
    assert "Przyczyna: HTTP 403" in tekst
    assert "Sklep: https://www.eventim-light.com/de/a/x/s/y" in tekst


def test_awaria_podaje_cooldown():
    tekst = format_fetch_error(
        "https://x", "błąd", cooldown_hours=6.0, now=STALA_CHWILA
    )

    assert "za około 6 h" in tekst


def test_awaria_bez_cooldownu_mowi_o_nastepnym_uruchomieniu():
    tekst = format_fetch_error(
        "https://x", "błąd", cooldown_hours=0.0, now=STALA_CHWILA
    )

    assert "kolejnym uruchomieniu" in tekst


def test_awaria_obcina_dlugi_komunikat():
    tekst = format_fetch_error(
        "https://x", "x" * 5000, cooldown_hours=6.0, now=STALA_CHWILA
    )

    linia = next(l for l in tekst.split("\n") if l.startswith("Przyczyna:"))
    assert len(linia) < ERROR_EXCERPT_CHARS + 40
    assert linia.endswith("…")


def test_awaria_scalza_wieloliniowy_komunikat():
    """Traceback w logu jest wieloliniowy - w wiadomości musi być jedna linia."""
    tekst = format_fetch_error(
        "https://x", "linia1\nlinia2\nlinia3", cooldown_hours=6.0, now=STALA_CHWILA
    )

    assert "linia1 linia2 linia3" in tekst


def test_awaria_bez_argumentu_czasu_uzywa_teraz():
    tekst = format_fetch_error("https://x", "błąd", cooldown_hours=6.0)

    assert "Sprawdzono: " in tekst


# --- anomalia braku danych ---------------------------------------------------

def test_anomalia_mowi_ze_nie_wiemy_a_nie_ze_brak():
    """Ostrzeżenie musi odróżniać 'nie wiem' od 'nie ma'."""
    seria = series_z_terminami(
        Term("A", "2026-10-09T19:00:00+02:00", "https://x/e/1", "Unknown"),
        term(availability="SoldOut"),
    )

    tekst = format_unknown_availability(seria, now=STALA_CHWILA)

    assert "brak danych o dostępności" in tekst
    assert "1 z 2 terminów" in tekst
    assert "Nie wysyłamy alarmu o biletach" in tekst
    assert "nieznana" in tekst


def test_anomalia_pokazuje_maksymalnie_trzy_terminy():
    """Pełna lista 200 terminów nie zmieściłaby się w czytelnym ostrzeżeniu."""
    nieznane = tuple(
        term(url=f"https://x/e/{i}", availability="Unknown") for i in range(5)
    )

    tekst = format_unknown_availability(series_z_terminami(*nieznane), now=STALA_CHWILA)

    assert "https://x/e/0" in tekst
    assert "https://x/e/2" in tekst
    assert "https://x/e/3" not in tekst
    assert "i 2 więcej" in tekst


def test_anomalia_gdy_tyle_sie_zgadza_nie_dodaje_licznika():
    nieznane = tuple(term(url=f"https://x/e/{i}", availability="Unknown") for i in range(3))

    tekst = format_unknown_availability(series_z_terminami(*nieznane), now=STALA_CHWILA)

    assert "więcej" not in tekst


# --- dzielenie długich wiadomości -------------------------------------------

def test_krotka_wiadomosc_nie_dzieli_sie():
    assert split_message("jedna linia") == ["jedna linia"]


def test_dlugie_wiadomosci_dzieli_sie_bez_utraty_tresci():
    tekst = "\n".join(f"linia {i}" for i in range(2000))

    czesci = split_message(tekst)

    assert len(czesci) > 1
    assert all(len(c) <= 3500 for c in czesci)
    assert "\n".join(czesci) == tekst


def test_podzial_nie_lamie_url():
    """Ucięcie w połowie adresu daje link, którego nie da się kliknąć."""
    tekst = "\n".join(f"https://www.eventim-light.com/de/a/x/e/{i}" for i in range(200))

    for czesc in split_message(tekst):
        for linia in czesc.split("\n"):
            assert linia.split("/")[-1].isdigit(), f"urwany URL: {linia}"


def test_sama_dluga_linia_nie_jest_dzielona_bez_uzasadnienia():
    """Linia za długa sama w sobie musi zostać w całości.

    Nie ma gdzie jej uciąć - lepiej wysłać za długą wiadomość (Telegram ją
    odrzuci z czytelnym błędem) niż rozsypać URL na kawałki.
    """
    tekst = "https://x/" + "a" * 5000

    assert split_message(tekst) == [tekst]


def test_limit_musi_byc_dodatni():
    with pytest.raises(ValueError, match="dodatni"):
        split_message("tekst", 0)


# --- integracja z prawdziwymi danymi ----------------------------------------

def _z_json(ld: dict) -> str:
    return make_html(ld)


def test_alert_na_prawdziwych_danych_z_jednym_dostepnym(real_ld: dict):
    """Buduje alert z prawdziwego JSON-LD, zmieniając tylko stan dostępności."""
    ld = set_availability(real_ld, ["SoldOut", "InStock"] + ["SoldOut"] * 4)

    seria = parse_series(_z_json(ld))
    tekst = format_available(seria, seria.available_terms, now=STALA_CHWILA)

    assert seria.name == "U-Bahn-Cabriotour 2026"
    assert "1 z 6 terminów dostępnych" in tekst
    assert "22:30 (UTC+02:00)" in tekst
    assert "/e/" in tekst


def test_alert_na_prawdziwych_danych_gdy_wszystko_wyprzedane(real_ld: dict):
    seria = parse_series(_z_json(real_ld))

    tekst = format_available(seria, seria.available_terms, now=STALA_CHWILA)

    assert "0 z 6 terminów dostępnych" in tekst
    assert "DOSTĘPNE BILETY" in tekst, "nagłówek jest, lista terminów nie"


def test_anomalia_na_prawdziwych_danych_gdy_brak_pola(real_ld: dict):
    ld = set_availability(real_ld, [None] * 6)

    seria = parse_series(_z_json(ld))
    tekst = format_unknown_availability(seria, now=STALA_CHWILA)

    assert "6 z 6 terminów" in tekst
    assert "brak danych o dostępności" in tekst


def test_alert_z_dlugim_unicode_nie_psuje_sie(real_ld: dict):
    """Nazwy z niestandardowymi znakami muszą przetrwać formatowanie."""
    ld = set_sub_events(
        real_ld,
        [
            {
                "@type": "Event",
                "name": "Cabriotour — SONDERFAHRT «Sonder» 日本語",
                "startDate": "2026-10-09T19:00:00+02:00",
                "offers": {
                    "availability": "InStock",
                    "lowPrice": 40,
                    "highPrice": 58,
                    "priceCurrency": "EUR",
                },
            }
        ],
    )

    seria = parse_series(_z_json(ld))
    tekst = format_available(seria, seria.available_terms, now=STALA_CHWILA)

    assert "SONDERFAHRT «Sonder» 日本語" in tekst
    assert len(tekst) < 3500


def test_alert_dlugiej_serii_dzieli_sie_na_wiadomosci(real_ld: dict):
    """Duża seria z dostępnymi terminami musi zmieścić się w limicie Telegrama."""
    ld = set_availability(real_ld, ["InStock"] * 6)
    for node in ld["@graph"]:
        if node.get("@type") == "EventSeries":
            node["subEvent"] = [
                {**ev, "name": f"U-Bahn-Cabriotour 2026 - SONDERFAHRT {i} " + "x" * 40}
                for i, ev in enumerate(node["subEvent"] * 40)
            ]

    seria = parse_series(_z_json(ld))
    tekst = format_available(seria, seria.available_terms, now=STALA_CHWILA)

    czesci = split_message(tekst)

    assert len(seria.available_terms) == 240
    assert len(czesci) > 1
    assert all(len(c) <= 3500 for c in czesci)
    assert "\n".join(czesci) == tekst