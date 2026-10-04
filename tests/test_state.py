"""Testy magazynu stanu (plan techniczny, sekcja 6).

Kluczowa zasada, ktora przechodzi przez caly ten plik: **cache jest
optymalizacja, nie wymaganiem.** Kazde ``load()`` musi zwrocic sensowny stan
zamiast wyjatku - inaczej wygasniecie ``actions/cache`` po 7 dniach zamienia
dzialajacy monitoring w czerwony run.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from eventim_watcher.state import (
    AVAILABILITY_STATES,
    SCHEMA_VERSION,
    FileStateStore,
    StateStore,
    availability_state,
    initial_state,
    mark_anomaly_notified,
    mark_check_failed,
    mark_check_ok,
    mark_error_notified,
    parse_timestamp,
    should_warn,
    should_warn_anomaly,
)

UTC = timezone.utc
CHWILA = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)


def cookies(n: int = 2) -> list[dict]:
    return [
        {
            "domain": ".www.eventim-light.com",
            "path": "/",
            "name": f"cookie-{i}",
            "value": f"wartosc-{i}",
        }
        for i in range(n)
    ]


# --- roundtrip ---------------------------------------------------------------


def test_zapis_i_odczyt_odtwarzaja_identyczny_stan(tmp_path):
    store = FileStateStore(tmp_path / "stan.json")

    zapisany = initial_state()
    mark_check_ok(
        zapisany,
        moment=CHWILA,
        stan="some_available",
        cookies=cookies(3),
    )
    mark_error_notified(zapisany, moment=CHWILA)
    store.save(zapisany)

    wczytany = store.load()

    assert wczytany == zapisany


def test_zapis_nadpisuje_poprzedni_stan(tmp_path):
    store = FileStateStore(tmp_path / "stan.json")
    store.save(initial_state() | {"last_availability_state": "some_available"})

    nowy = initial_state()
    mark_check_failed(nowy, moment=CHWILA)
    store.save(nowy)

    assert store.load()["last_availability_state"] is None
    assert store.load()["last_check_ok"] is False


# --- odpornosc na brak i uszkodzenia ---------------------------------------


def test_brak_pliku_daje_stan_domyslny(tmp_path):
    store = FileStateStore(tmp_path / "nie-ma-mnie.json")

    stan = store.load()

    assert stan == initial_state()


def test_uszkodzony_json_daje_stan_domyslny(tmp_path):
    sciezka = tmp_path / "stan.json"
    sciezka.write_text("{to nie jest json", encoding="utf-8")

    stan = FileStateStore(sciezka).load()

    assert stan == initial_state()


def test_json_nie_bedacy_slownikiem_daje_stan_domyslny(tmp_path):
    sciezka = tmp_path / "stan.json"
    sciezka.write_text("[1, 2, 3]", encoding="utf-8")

    assert FileStateStore(sciezka).load() == initial_state()


def test_katalog_zamiast_pliku_daje_stan_domyslny(tmp_path):
    katalog = tmp_path / "stan.json"
    katalog.mkdir()

    assert FileStateStore(katalog).load() == initial_state()


# --- wersjonowanie schematu -------------------------------------------------


@pytest.mark.parametrize("wersja", [0, 2, 99, "1", None, -1])
def test_nieznana_wersja_pomija_cache_w_calosci(tmp_path, wersja):
    """Nieznana wersja to pelny reset, nie tylko pominięcie cookies.

    Odtworzenie części stanu z nieznanej wersji byloby ryzykiem bez korzyści:
    zmiana struktury Queue-it uniewaznia token, wiec proba rezygnacji tylko
    z cookies i tak skonczylaby sie petla 403.
    """
    sciezka = tmp_path / "stan.json"
    sciezka.write_text(
        json.dumps(
            {
                "schema_version": wersja,
                "cookies": cookies(),
                "last_check_ok": True,
                "last_availability_state": "some_available",
            }
        ),
        encoding="utf-8",
    )

    stan = FileStateStore(sciezka).load()

    assert stan == initial_state()


def test_poprawna_wersja_przechodzi_bez_resetu(tmp_path):
    sciezka = tmp_path / "stan.json"
    sciezka.write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "cookies": cookies(2),
                "last_check_ok": True,
            }
        ),
        encoding="utf-8",
    )

    stan = FileStateStore(sciezka).load()

    assert stan["cookies"] == cookies(2)
    assert stan["last_check_ok"] is True


# --- odpornosc na uszkodzone cookies ----------------------------------------


def test_dziurawy_wpis_cookies_jest_odrzucany_bez_utraty_pozostalych(tmp_path):
    sciezka = tmp_path / "stan.json"
    sciezka.write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "cookies": [
                    {"name": "", "value": "x", "domain": "d"},  # brak nazwy
                    {"name": "n", "value": "", "domain": "d"},  # brak wartosci
                    {"name": "n", "value": "v"},  # brak domeny
                    {"name": "n", "value": "v", "domain": "d"},  # poprawny
                    "to nie jest slownik",
                ],
            }
        ),
        encoding="utf-8",
    )

    stan = FileStateStore(sciezka).load()

    assert stan["cookies"] == [{"name": "n", "value": "v", "domain": "d", "path": "/"}]


def test_cookies_nie_slownik_daje_pusta_liste(tmp_path):
    sciezka = tmp_path / "stan.json"
    sciezka.write_text(
        json.dumps({"schema_version": SCHEMA_VERSION, "cookies": "abc"}),
        encoding="utf-8",
    )

    assert FileStateStore(sciezka).load()["cookies"] == []


# --- odpornosc na zapis -----------------------------------------------------


def test_zapis_do_miejsca_ktorego_nie_da_sie_utworzyc_nie_wywraca_programu(tmp_path):
    """Plik w miejscu katalogu - jedyny sposob na pewny brak uprawnien.

    Katalog zrodlowy w `tmp_path` bylby zapisywalny, wiec test na
    uprawnieniach przechodzilby z powodu niezwiazanym z celem.
    """
    przeszkoda = tmp_path / "plik"
    przeszkoda.write_text("x", encoding="utf-8")

    store = FileStateStore(przeszkoda / "pod" / "stan.json")
    store.save(initial_state())  # nie moze rzucic

    assert not store.path.exists()


def test_zapis_tworzy_katalogi_posrednie(tmp_path):
    store = FileStateStore(tmp_path / "a" / "b" / "c" / "stan.json")

    store.save(initial_state())

    assert store.path.exists()


def test_nieznane_klucze_sa_odrzucane_przy_zapisie(tmp_path):
    """Tajemnica bota nie moze trafic do pliku stanu nawet przypadkiem."""
    store = FileStateStore(tmp_path / "stan.json")

    store.save(initial_state() | {"telegram_bot_token": "tajemnica", "cos": 1})

    surowy = store.path.read_text(encoding="utf-8")
    assert "tajemnica" not in surowy
    assert store.load().get("telegram_bot_token") is None


def test_zapis_jest_atomowy_nie_zostawia_plikow_tymczasowych(tmp_path):
    store = FileStateStore(tmp_path / "stan.json")

    store.save(initial_state())
    store.save(initial_state())

    smieci = [p.name for p in tmp_path.iterdir() if p.name != "stan.json"]
    assert not smieci, f"pliki tymczasowe po zapisie: {smieci}"


def test_plik_stanu_nie_jest_czytelny_dla_innych(tmp_path):
    """Plik z tokenem sesji sklepu — uprawnienia 600 tam, gdzie system to umie."""
    store = FileStateStore(tmp_path / "stan.json")

    store.save(initial_state())

    if os.name != "nt":
        assert oct(store.path.stat().st_mode)[-3:] == "600"


# --- znaczniki czasu --------------------------------------------------------


def test_parse_timestamp_odwraca_formatowanie():
    zapisany = mark_check_ok(initial_state(), moment=CHWILA, stan="none_available")

    assert parse_timestamp(zapisany["last_check_at"]) == CHWILA


@pytest.mark.parametrize("wartosc", [None, "", "   ", "nie-data", 12345, [], {}])
def test_usunekiwy_znacznik_daje_none_i_nie_wywraca_programu(wartosc):
    assert parse_timestamp(wartosc) is None


def test_znacznik_bej_strefy_czasowej_jest_traktowany_jako_utc():
    """Brak ``tzinfo`` to blad zapisu, nie blad programu."""
    parsed = parse_timestamp("2026-10-04T12:00:00")

    assert parsed == CHWILA
    assert parsed.tzinfo is not None


# --- przejscia stanu --------------------------------------------------------


def test_znacznik_czasu_jest_zapisywany_w_utc():
    """Znaczniki porownujemy miedzy uruchomieniami na roznych runnerach."""
    berlin = datetime(2026, 10, 4, 14, 0, tzinfo=timezone(timedelta(hours=2)))

    stan = mark_check_ok(initial_state(), moment=berlin, stan="some_available")

    assert stan["last_check_at"].endswith("+00:00")
    assert parse_timestamp(stan["last_check_at"]) == datetime(2026, 10, 4, 12, 0, tzinfo=UTC)


def test_moje_znaczniki_nie_zostaja_bez_zmiany():
    stan = mark_check_ok(initial_state(), moment=CHWILA, stan="none_available")
    poprzednie = stan["last_availability_state"]

    nowy = mark_check_ok(stan, moment=CHWILA, stan="some_available")

    assert poprzednie == "none_available"
    assert nowy["last_availability_state"] == "some_available"


def test_nieudane_sprawdzenie_nie_kasuje_cookies():
    """Token moze wciaz dzialac; awaria sieci nie jest powodem do resetu."""
    stan = initial_state()
    mark_check_ok(stan, moment=CHWILA, stan="none_available", cookies=cookies(3))

    mark_check_failed(stan, moment=CHWILA)

    assert len(stan["cookies"]) == 3


def test_nieudane_sprawdzenie_zachowuje_czas_ostatniego_ostrzezenia():
    """Ten znacznik decyduje o wyciszeniu - nie moze zniknac przy bledzie."""
    stan = mark_error_notified(initial_state(), moment=CHWILA)
    zapisany = stan["last_error_notified_at"]

    mark_check_failed(stan, moment=CHWILA + timedelta(hours=1))

    assert stan["last_error_notified_at"] == zapisany


# --- opis stanu dostepnosci -------------------------------------------------


def test_stan_dostepnosci_rozroznia_wiedzy_od_braku():
    assert availability_state(1, 0) == "some_available"
    assert availability_state(0, 0) == "none_available"
    assert availability_state(0, 3) == "all_unknown"


def test_dostepny_termin_przywaza_nieznanym():
    """Dostepnosc wygrywa: jak cos jest, to jest - nieznane resyty to osobny raport."""
    assert availability_state(1, 5) == "some_available"


def test_wszystkie_opisane_stany_sa_uzyte():
    """Kazdy stan zwracany przez `availability_state` ma byc zadeklarowany.

    Inaczej dodanie nowego przypadku przepisze cicho kontrakt pola
    `last_availability_state` i nikt nie zobaczy rozbieżności.
    """
    for dostepne in (0, 1, 5):
        for nieznane in (0, 1, 5):
            assert availability_state(dostepne, nieznane) in AVAILABILITY_STATES


# --- cooldown awarii --------------------------------------------------------


def test_pierwsze_ostrzezenie_nie_jest_wyciszone():
    assert should_warn(initial_state(), moment=CHWILA, cooldown=timedelta(hours=6))


def test_po_poprzednim_sukcesie_ostrzegamy_mimo_czego():
    """Poprzednie sprawdzenie udane = nie ciag awarii = nie wyciszamy."""
    stan = mark_check_ok(initial_state(), moment=CHWILA, stan="none_available")

    assert should_warn(stan, moment=CHWILA + timedelta(hours=1), cooldown=timedelta(hours=6))


def test_druga_awaria_w_trakcie_cooldownu_jest_wyciszona():
    stan = mark_error_notified(initial_state(), moment=CHWILA)
    mark_check_failed(stan, moment=CHWILA)

    assert not should_warn(
        stan, moment=CHWILA + timedelta(hours=1), cooldown=timedelta(hours=6)
    )


def test_po_wykonaniu_cooldownu_ostrzegamy_znowu():
    stan = mark_error_notified(initial_state(), moment=CHWILA)
    mark_check_failed(stan, moment=CHWILA)

    assert should_warn(
        stan, moment=CHWILA + timedelta(hours=6), cooldown=timedelta(hours=6)
    )


def test_zero_cooldownu_wyznacza_czestotliwosc():
    stan = mark_error_notified(initial_state(), moment=CHWILA)
    mark_check_failed(stan, moment=CHWILA)

    assert should_warn(stan, moment=CHWILA, cooldown=timedelta(0))


def test_ciag_awarii_kroi_tylko_pierwsze_ostrzezenie():
    """Noc awarii nie moze zasypac Telegrama.

    Symulacja 10 kolejnych uruchomien co godzine z cooldownem 6 h.
    Oczekujemy **dwóch** ostrzeżeń (w godzinie 0 i 6), nie dziesięciu.

    Kolejnosc w petli odwzorowuje orkiestracje: `should_warn` czyta stan z
    **poprzedniego** uruchomienia, dopiero potem stan oznaczamy jako nieudany.
    Odwrotna kolejnosc symulacji dawalaby wynik zupelnie inny — i wlasnie
    dlatego jest testowana.
    """
    stan = initial_state()
    ostrzezenia = []

    for godzina in range(10):
        chwila = CHWILA + timedelta(hours=godzina)
        if should_warn(stan, moment=chwila, cooldown=timedelta(hours=6)):
            ostrzezenia.append(godzina)
            mark_error_notified(stan, moment=chwila)
        mark_check_failed(stan, moment=chwila)

    assert ostrzezenia == [0, 6], f"cooldown 6 h przy 10 awariach godzinowych: {ostrzezenia}"


def test_sukres_przerywa_ciag_awarii():
    """Po jednym udanym sprawdzeniu kolejna awaria zglasza sie od nowa.

    `last_check_ok` opisuje **poprzednie** sprawdzenie, wiec sukres zerwuje
    wyciszenie nawet wtedy, gdy od ostatniego ostrzezenia minelo mniej niz
    cooldown. Inaczej po jednym naprawionym runie dalibysmy sie w ciszy
    calej kolejnej awarii.
    """
    stan = initial_state()

    # Run w godzinie 0 nieudany - ostrzezenie leci.
    assert should_warn(stan, moment=CHWILA, cooldown=timedelta(hours=6))
    mark_error_notified(stan, moment=CHWILA)
    mark_check_failed(stan, moment=CHWILA)

    # Run w godzinie 1 nieudany - wyciszony cooldownem.
    assert not should_warn(
        stan, moment=CHWILA + timedelta(hours=1), cooldown=timedelta(hours=6)
    )
    mark_check_failed(stan, moment=CHWILA + timedelta(hours=1))

    # Run w godzinie 2 udany.
    mark_check_ok(stan, moment=CHWILA + timedelta(hours=2), stan="none_available")

    # Run w godzinie 3 nieudany - poprzedni byl udany, wiec ostrzegamy,
    # mimo ze od ostatniego ostrzezenia minely tylko 3 h.
    assert should_warn(stan, moment=CHWILA + timedelta(hours=3), cooldown=timedelta(hours=6))


# --- cooldown anomalii ------------------------------------------------------


def test_pierwsza_anomalia_nie_jest_wyciszona():
    assert should_warn_anomaly(initial_state(), moment=CHWILA, cooldown=timedelta(hours=6))


def test_powtorka_anomalii_w_trakcie_cooldownu_jest_wyciszona():
    stan = mark_anomaly_notified(initial_state(), moment=CHWILA)

    assert not should_warn_anomaly(
        stan, moment=CHWILA + timedelta(hours=1), cooldown=timedelta(hours=6)
    )


def test_kanaly_anomalii_i_awarii_sa_niezalezne():
    """Kluczowe: wyciszona awaria nie może zamaskować świeżej anomalii.

    Bez osobnego kanału świeży problem znikłby cicho, bo właśnie wysłaliśmy
    ostrzeżenie o czymś zupełnie innym.
    """
    stan = initial_state()
    # Swieza awaria pobierania, ostrzezenie wyslane godzine temu.
    mark_check_failed(stan, moment=CHWILA)
    mark_error_notified(stan, moment=CHWILA)

    # Godzine pozniej sklep odpowiada, ale bez pola availability.
    chwila = CHWILA + timedelta(hours=1)
    assert should_warn_anomaly(stan, moment=chwila, cooldown=timedelta(hours=6))
    assert not should_warn(stan, moment=chwila, cooldown=timedelta(hours=6))


def test_znacznik_anomalii_przezywa_nieudane_sprawdzenie():
    stan = mark_anomaly_notified(initial_state(), moment=CHWILA)

    mark_check_failed(stan, moment=CHWILA + timedelta(hours=1))

    assert not should_warn_anomaly(
        stan, moment=CHWILA + timedelta(hours=2), cooldown=timedelta(hours=6)
    )


# --- interfejs --------------------------------------------------------------


def test_interfejs_nie_ma_domyslnej_implementacji():
    """`StateStore` to kontrakt; pomylenie sie z brakiem implementacji ma byc glosne."""
    with pytest.raises(NotImplementedError):
        StateStore().load()

    with pytest.raises(NotImplementedError):
        StateStore().save(initial_state())


def test_plikowy_magazyn_jest_magazynem_stanu(tmp_path):
    assert isinstance(FileStateStore(tmp_path / "stan.json"), StateStore)