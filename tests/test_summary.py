"""Testy podsumowania runu dla ``$GITHUB_STEP_SUMMARY`` (Faza 7).

Podsumowanie w UI GitHub Actions jest jedynym miejscem, gdzie człowiek
spojrzy na monitoring tydzień po wdrożeniu i zapyta „czy on jeszcze żyje".
Dlatego ma odpowiadać na trzy pytania jednym rzutem oka: co widziałem,
co się stało i co powiedziałem na Telegramie.

Najważniejszy kontrakt nie brzmi „ładnie wygląda", tylko **nigdy nie kłamie**.
Dlatego raport powstaje z liczb liczonych w miejscu, gdzie je znamy, a nie
z odtwarzania własnych logów na końcu — i dlatego osobno pilnujemy, że brak
możliwości zapisania podsumowania nie zamienia zielonego runu w czerwony.

**Żaden z tych testów nie uderza w sieć.**
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests
import responses
from conftest import (
    TARGET,
    TELEGRAM_CHAT,
    TELEGRAM_TOKEN,
    register_handshake,
    register_telegram_ok,
    register_telegram_refused,
    shop_page,
)

from eventim_watcher.config import Config
from eventim_watcher.eventim import new_session
from eventim_watcher.main import (
    EXIT_FAILURE,
    EXIT_OK,
    WYSYLKA_BRAK,
    WYSYLKA_BLAD,
    WYSYLKA_WYSLANA,
    WYSYLKA_WYCISZONA,
    WYNIK_ALERT,
    WYNIK_ANOMALIA,
    WYNIK_BLAD_ALERTA,
    WYNIK_BLAD_POBRANIA,
    WYNIK_BRAK,
    WYNIK_KONFIGURACJA,
    Raport,
    _OPIS_WYNIKU,
    _OPIS_WYSYLKI,
    _zapisz_podsumowanie,
    main,
    render_step_summary,
    run,
)
from eventim_watcher.state import FileStateStore, mark_error_notified

T0 = datetime(2026, 10, 4, 19, 17, 3, tzinfo=timezone.utc)

SPRZEDANE = "https://schema.org/SoldOut"
DOSTEPNE = "https://schema.org/InStock"
WSZYSTKIE_SPRZEDANE = [SPRZEDANE] * 6
JEDEN_DOSTEPNY = [DOSTEPNE] + [SPRZEDANE] * 5


# --- osprzet ---------------------------------------------------------------


@pytest.fixture
def konfiguracja() -> Config:
    return Config.from_env(
        {
            "EVENTIM_TARGET_URL": TARGET,
            "TELEGRAM_BOT_TOKEN": TELEGRAM_TOKEN,
            "TELEGRAM_CHAT_ID": TELEGRAM_CHAT,
            "EVENTIM_ERROR_COOLDOWN_HOURS": "6",
            "TELEGRAM_MAX_RETRIES": "0",
        }
    )


@pytest.fixture
def sklep(tmp_path):
    """Czysty magazyn stanu i atrapa HTTP — bez sieci, jak w `test_main.py`."""
    responses.start()
    responses.reset()
    responses.mock.assert_all_requests_are_fired = False
    try:
        yield FileStateStore(tmp_path / "stan.json")
    finally:
        responses.reset()
        responses.stop()


def uruchom(konfiguracja, sklep, *, teraz=T0):
    """Wykonuje `run()` z raportem i zwraca `(kod, raport)`."""
    raport = Raport()
    kod = run(konfiguracja, sklep, new_session(), now=teraz, raport=raport)
    return kod, raport


# --- render_step_summary: ksztalt ----------------------------------------


def test_pusty_raport_daje_odczywalne_podsumowanie():
    tekst = render_step_summary(Raport(), moment=T0)
    assert tekst.startswith("## ")
    assert _OPIS_WYNIKU[WYNIK_BRAK] in tekst
    assert _OPIS_WYNIKU[WYNIK_BRAK] not in tekst.split("\n")[0]


def test_liczby_terminow_sa_w_podsumowaniu():
    raport = Raport(nazwa_serii="Cabriotour", terminow=6, dostepnych=2, nieznanych=1)
    tekst = render_step_summary(raport, moment=T0)
    assert "| Terminów w serii | 6 |" in tekst
    assert "| Dostępnych | 2 |" in tekst
    assert "| Nieznanych (brak pola `availability`) | 1 |" in tekst
    assert "| Seria | Cabriotour |" in tekst


@pytest.mark.parametrize("wynik", sorted(_OPIS_WYNIKU))
def test_kazdy_wynik_ma_opis_po_polsku(wynik: str) -> None:
    """Nagłówek podsumowania to zawsze zdanie dla człowieka, nigdy slug.

    `WYNIK_BLAD_POBRANIA` istnieje tylko dla logów i kodu. W podsumowaniu ma
    być po polsku; sluga nie wolno, bo po roku nikt nie pamięta, co znaczyło
    `blad_pobrania`. Porównujemy **cały** nagłówek, a nie tylko brak sluga —
    samo „nie zawiera" przepuszczałoby `blad_pobrania - blad pobrania`.
    """
    tekst = render_step_summary(Raport(wynik=wynik), moment=T0)
    naglowki = [linia for linia in tekst.splitlines() if linia.startswith("**")]
    assert naglowki == [f"**{_OPIS_WYNIKU[wynik]}**"], naglowki


@pytest.mark.parametrize("wysylka", sorted(_OPIS_WYSYLKI))
def test_kazdy_stan_wysylki_ma_opis_po_polsku(wysylka: str) -> None:
    tekst = render_step_summary(Raport(wysylka=wysylka), moment=T0)
    assert f"| {_OPIS_WYSYLKI[wysylka]} |" in tekst


def test_adres_dolaczamy_tylko_gdy_jest_konfiguracja(konfiguracja) -> None:
    bez = render_step_summary(Raport(), moment=T0)
    assert TARGET not in bez

    z = render_step_summary(Raport(), konfiguracja, moment=T0)
    assert f"`{TARGET}`" in z


def test_szczegol_trafia_do_podsumowania() -> None:
    tekst = render_step_summary(
        Raport(wynik=WYNIK_BLAD_POBRANIA, szczegol="HTTP 403 od akamai"),
        moment=T0,
    )
    assert "HTTP 403 od akamai" in tekst


def test_szczegol_jednolinijkowy_zostaje_w_inline_code() -> None:
    tekst = render_step_summary(Raport(szczegol="HTTP 403 od akamai"), moment=T0)
    assert "Szczegół: `HTTP 403 od akamai`" in tekst
    assert "```" not in tekst


def test_szczegol_wielolinijkowy_laduje_w_bloku_kodu() -> None:
    """Inline code nie przenosi znaków nowej linii.

    Błąd konfiguracji jest wielolinijkowy. Wblokowany w `` ` `` zlepiłby się
    w jeden nieczytelny akapit — czyli dokładnie wtedy, gdy człowiek próbuje
    zrozumieć, co poprawić w sekretach.
    """
    szczegol = "Niepoprawna konfiguracja - problemy:\n  - brak TELEGRAM_BOT_TOKEN"
    tekst = render_step_summary(
        Raport(wynik=WYNIK_KONFIGURACJA, szczegol=szczegol), moment=T0
    )
    assert "```text\n" + szczegol + "\n```" in tekst
    assert f"Szczegół: `{szczegol}`" not in tekst


def test_szczegol_z_backtickiem_tez_nie_wywraca_markdowna() -> None:
    """Treść błędu pochodzi z sieci — może zawierać cokolwiek.

    Jeden ` w treści zamknąłby inline code przed końcem, a połowa opisu
    zniknęłaby w zastępczym stylu.
    """
    szczegol = "bled z odwolaniem `zly_znacznik` w tresci"
    tekst = render_step_summary(Raport(szczegol=szczegol), moment=T0)
    assert "```text" in tekst
    assert f"Szczegół: `{szczegol}`" not in tekst


def test_pusty_szczegol_nie_zostawia_wiszacej_etykiety() -> None:
    assert "Szczegół" not in render_step_summary(Raport(), moment=T0)


def test_alert_dostaje_przypomnienie_o_nieaktualnym_linku() -> None:
    """Najczęstsze źródło złośliwego alarmu: „działa, nie działa" w dwie minuty.

    Dostępność w sklepie znika w minutę, a wiadomość zostaje w czacie. Bez tego
    zdania czytelnik wraca, klika link i zastanawia się, czy program kłamie.
    """
    tekst = render_step_summary(Raport(wynik=WYNIK_ALERT), moment=T0)
    assert "może być już nieaktualny" in tekst


def test_anomalia_wyjasnia_zasade_z_nieznanego_stanu() -> None:
    texto = render_step_summary(Raport(wynik=WYNIK_ANOMALIA), moment=T0)
    assert "ADR-9" in texto
    assert "traktowane jako niedostępne" in texto


def test_zwykly_run_nie_dostaje_dodatkowych_wyjasnien() -> None:
    tekst = render_step_summary(Raport(), moment=T0)
    assert "ADR-9" not in tekst
    assert "nieaktualny" not in tekst


def test_nieznany_wynik_nie_wywraca_renderowania() -> None:
    """Raport może przyjść z przyszłej wersji programu niż ta summary.

    Nieznany klucz ma pokazać się wprost, a nie wywołać `KeyError` w trakcie
    zapisu podsumowania — czyli dokładnie wtedy, gdy ktoś tego pilnuje.
    """
    tekst = render_step_summary(Raport(wynik="cos_nowego"), moment=T0)
    assert "cos_nowego" in tekst


def test_nieznany_stan_wysylki_nie_wywraca_renderowania() -> None:
    """Ta sama tolerancja co dla `wynik`, o ktorym łatwo zapomnieć.

    Oba pola to slugi z tej samego obiektu, dodawanego w tym samym miejscu.
    `KeyError` w jednym a nie w drugim to nie ostrożność, tylko przypadek.
    """
    tekst = render_step_summary(Raport(wysylka="cos_nowego"), moment=T0)
    assert "cos_nowego" in tekst


def test_moment_domyslny_to_teraz() -> None:
    assert render_step_summary(Raport()).rstrip().endswith("`._")


def test_podsumowanie_nie_zawiera_tokenu_telegrama(konfiguracja, sklep) -> None:
    """Podsumowanie trafia do logu runu, czyli do UI repozytorium."""
    register_handshake(final=shop_page(JEDEN_DOSTEPNY))
    register_telegram_ok()
    _, raport = uruchom(konfiguracja, sklep)
    tekst = render_step_summary(raport, konfiguracja, moment=T0)
    assert TELEGRAM_TOKEN not in tekst


# --- _zapisz_podsumowanie: plik ------------------------------------------


def test_brak_zmiennej_github_pisze_nic(tmp_path: Path) -> None:
    _zapisz_podsumowanie({}, Raport())
    assert list(tmp_path.iterdir()) == []


def test_podsumowanie_trafia_do_pliku(tmp_path: Path) -> None:
    plik = tmp_path / "summary.md"
    _zapisz_podsumowanie({"GITHUB_STEP_SUMMARY": str(plik)}, Raport(terminow=6))
    assert "Terminów w serii | 6" in plik.read_text(encoding="utf-8")


def test_zapis_dopisuje_a_nie_nadpisuje(tmp_path: Path) -> None:
    """`workflow_dispatch` z ponowieniem ma dwa kroki w tym samym pliku.

    Nadpisanie skasowałoby podsumowanie pierwszego kroku — a to jest ten
    etap, po którym pada druga próba albo wysyłka do Telegrama.
    """
    plik = tmp_path / "summary.md"
    env = {"GITHUB_STEP_SUMMARY": str(plik)}
    _zapisz_podsumowanie(env, Raport(terminow=6))
    _zapisz_podsumowanie(env, Raport(terminow=7))
    tekst = plik.read_text(encoding="utf-8")
    assert "Terminów w serii | 6" in tekst
    assert "Terminów w serii | 7" in tekst


def test_niedostepny_plik_nie_wywala_runu(tmp_path: Path, caplog) -> None:
    """Najwazniejszy kontrakt tej funkcji: **nigdy nie rzuca**.

    Brak prawa do zapisu nie moze zamienic zielonego runu w czerwony.
    Najgorsze, co moze sie stac, to cicha nieobecnosc podsumowania.
    """
    katalog = tmp_path / "to-jest-katalog"
    katalog.mkdir()
    with caplog.at_level(logging.WARNING, logger="eventim_watcher"):
        _zapisz_podsumowanie({"GITHUB_STEP_SUMMARY": str(katalog)}, Raport())
    assert "nie udalo sie zapisac podsumowania" in caplog.text


def test_pusta_sciezka_jest_ignorowana() -> None:
    _zapisz_podsumowanie({"GITHUB_STEP_SUMMARY": ""}, Raport())


# --- run(): raport na kazdej sciezce --------------------------------------


def test_raport_bez_biletow(sklep, konfiguracja) -> None:
    register_handshake(final=shop_page(WSZYSTKIE_SPRZEDANE))
    register_telegram_ok()
    kod, raport = uruchom(konfiguracja, sklep)
    assert kod == EXIT_OK
    assert raport.wynik == WYNIK_BRAK
    assert raport.wysylka == WYSYLKA_WYSLANA
    assert raport.terminow == 6
    assert raport.dostepnych == 0
    assert raport.nieznanych == 0
    assert raport.nazwa_serii


def test_raport_alertu(sklep, konfiguracja) -> None:
    register_handshake(final=shop_page(JEDEN_DOSTEPNY))
    register_telegram_ok()
    kod, raport = uruchom(konfiguracja, sklep)
    assert kod == EXIT_OK
    assert raport.wynik == WYNIK_ALERT
    assert raport.wysylka == WYSYLKA_WYSLANA
    assert raport.dostepnych == 1


def test_raport_nieudanej_wysylki_alertu(sklep, konfiguracja) -> None:
    """Bilet jest, wiadomość nie dotarła — najgorsza z możliwych sytuacji.

    Dwa osobne pola wyników i wysyłki: inaczej podsumowanie wyglądałoby
    jak zwykły „brak dostępnych terminów", a człowiek by szukał przyczyny
    godzinę.
    """
    register_handshake(final=shop_page(JEDEN_DOSTEPNY))
    register_telegram_refused()
    kod, raport = uruchom(konfiguracja, sklep)
    assert kod == EXIT_FAILURE
    assert raport.wynik == WYNIK_BLAD_ALERTA
    assert raport.wysylka == WYSYLKA_BLAD


def test_raport_awarii_pobrania(sklep, konfiguracja) -> None:
    responses.get(TARGET, body=requests.ConnectionError("siec padla"))
    register_telegram_ok()
    kod, raport = uruchom(konfiguracja, sklep)
    assert kod == EXIT_FAILURE
    assert raport.wynik == WYNIK_BLAD_POBRANIA
    assert raport.wysylka == WYSYLKA_WYSLANA
    assert "siec padla" in raport.szczegol


def test_raport_awarii_wyczyszonej_cooldownem(sklep, konfiguracja) -> None:
    responses.get(TARGET, body=requests.ConnectionError("siec padla"))
    register_telegram_ok()
    uruchom(konfiguracja, sklep)  # pierwsza awaria: ostrzezenie wychodzi
    kod, raport = uruchom(konfiguracja, sklep, teraz=T0 + timedelta(hours=1))
    assert kod == EXIT_FAILURE
    assert raport.wynik == WYNIK_BLAD_POBRANIA
    assert raport.wysylka == WYSYLKA_WYCISZONA


def test_raport_anomalii_ma_liczby_terminow(sklep, konfiguracja) -> None:
    """Ścieżka anomalii nie przechodzi przez główny blok raportu.

    Gdyby liczby wypełniał tylko parse, podsumowanie anomalii pokazałoby
    zera i czytelnik uznałby, że nie ma żadnych terminów. `None` w liście
    stanów oznacza usunięcie pola `availability` (ADR-9).
    """
    stany = [SPRZEDANE, None] + [SPRZEDANE] * 4
    register_handshake(final=shop_page(stany))
    register_telegram_ok()
    kod, raport = uruchom(konfiguracja, sklep)
    assert kod == EXIT_FAILURE
    assert raport.wynik == WYNIK_ANOMALIA
    assert raport.terminow == 6
    assert raport.nieznanych == 1
    assert raport.szczegol == ""


def test_raport_nie_jest_wymagany(sklep, konfiguracja) -> None:
    """`raport` jest opcjonalny — stare wywolania bez niego muszą działać."""
    register_handshake(final=shop_page(WSZYSTKIE_SPRZEDANE))
    register_telegram_ok()
    assert run(konfiguracja, sklep, new_session(), now=T0) == EXIT_OK


# --- main(): podsumowanie powstaje w obu koncach --------------------------


def test_cli_zapisuje_podsumowanie(sklep, tmp_path: Path) -> None:
    register_handshake(final=shop_page(WSZYSTKIE_SPRZEDANE))
    register_telegram_ok()
    plik = tmp_path / "summary.md"
    kod = main(
        [],
        env={
            "EVENTIM_TARGET_URL": TARGET,
            "TELEGRAM_BOT_TOKEN": TELEGRAM_TOKEN,
            "TELEGRAM_CHAT_ID": TELEGRAM_CHAT,
            "TELEGRAM_MAX_RETRIES": "0",
            "GITHUB_STEP_SUMMARY": str(plik),
        },
        store=sklep,
        session=new_session(),
    )
    assert kod == EXIT_OK
    assert "Sprawdzone" in plik.read_text(encoding="utf-8")


def test_cli_bez_zmiennej_nie_tworzy_podsumowania(sklep, tmp_path: Path) -> None:
    """Brak `GITHUB_STEP_SUMMARY` znaczy: żadnego pliku poza stanem.

    Nie sprawdzamy „zero plików", tylko brak podsumowania — `run()` **ma**
    prawo i obowiązek zapisać plik stanu, a to jest jego zapis, nie podsumowania.
    """
    register_handshake(final=shop_page(WSZYSTKIE_SPRZEDANE))
    main(
        [],
        env={
            "EVENTIM_TARGET_URL": TARGET,
            "TELEGRAM_BOT_TOKEN": TELEGRAM_TOKEN,
            "TELEGRAM_CHAT_ID": TELEGRAM_CHAT,
            "TELEGRAM_MAX_RETRIES": "0",
        },
        store=sklep,
        session=new_session(),
    )
    assert [p.name for p in tmp_path.iterdir()] == ["stan.json"]


def test_cli_podsumowuje_nawet_bled_konfiguracji(tmp_path: Path) -> None:
    """Brak sekretu to najczestszy blad wdrozenia — podsumowanie musi go pokazac.

    Bez tego run konczy sie czerwono z komunikatem na `stderr`, a zakladka
    Actions pokazuje pustke: człowiek nie ma gdzie przeczytac, czego brakuje.
    """
    plik = tmp_path / "summary.md"
    kod = main(
        [],
        env={
            "EVENTIM_TARGET_URL": TARGET,
            "GITHUB_STEP_SUMMARY": str(plik),
        },
        store=FileStateStore(tmp_path / "stan.json"),
        session=new_session(),
    )
    assert kod == EXIT_FAILURE
    tekst = plik.read_text(encoding="utf-8")
    assert _OPIS_WYNIKU[WYNIK_KONFIGURACJA] in tekst
    assert "TELEGRAM_BOT_TOKEN" in tekst
