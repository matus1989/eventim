"""Testy orkiestracji (plan techniczny, sekcja 7).

Trzy rzeczy w tej sekcji są **kontraktem**, nie implementacją:

* **kolejność** — cookies przed wysyłką, ``should_warn`` przed oznaczeniem stanu,
* **kody wyjścia** — ``0`` znaczy „sprawdzono" (także gdy brak biletów), ``1``
  znaczy „nie wiem"; pomylenie ich zamienia awarie w szum,
* **zasada powtarzania** — alert o biletach leci przy każdym uruchomieniu,
  bez limitu i bez porównania z poprzednim runem.

Każdy test odtwarza całe wykonanie: handshake, parser, magazyn stanu i wysyłkę.
Nie ma tu atrap samej orkiestracji — różnica między „kod wygląda poprawnie" a
„program zachowuje się poprawnie" jest właśnie w tej ostatniej.

O harnessu (``responses``) trzeba wiedzieć dwie rzeczy, bo obie kosztują
godziny poszukiwań, gdy się o nich zapomni:

* ``responses.reset()`` czyści również ``responses.calls`` — nie wolno go
  używać między uruchomieniami, jeśli chcemy policzyć wysyłki łącznie;
* zarejestrowana odpowiedź jest **jednorazowa** (zdejmowana po użyciu), więc
  kolejne uruchomienia obsługujemy przez rejestrację ponowną, a nie przez reset.

Innymi słowy: przed każdym uruchomieniem rejestrujemy świeżą atrapa, a liczniki
rosną same.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

import pytest
import requests
import responses
from conftest import (
    MARKETING_PAGE,
    TARGET,
    TELEGRAM_CHAT,
    TELEGRAM_TOKEN,
    register_handshake,
    register_shop_only,
    register_telegram_ok,
    register_telegram_refused,
    set_availability,
    shop_page,
    telegram_sends,
)

from eventim_watcher.config import Config
from eventim_watcher.eventim import new_session
from eventim_watcher.main import EXIT_FAILURE, EXIT_OK, run
from eventim_watcher.state import FileStateStore

UTC = timezone.utc
T0 = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)

SPRZEDANE = "https://schema.org/SoldOut"
DOSTEPNE = "https://schema.org/InStock"
PRZEDSPRZEDAZ = "https://schema.org/PreSale"

SZEŚĆ_SPRZEDANYCH = [SPRZEDANE] * 6
JEDEN_DOSTEPNY = [DOSTEPNE] + [SPRZEDANE] * 5


# --- osprzęt ---------------------------------------------------------------


@pytest.fixture
def konfiguracja() -> Config:
    return Config.from_env(
        {
            "EVENTIM_TARGET_URL": TARGET,
            "TELEGRAM_BOT_TOKEN": TELEGRAM_TOKEN,
            "TELEGRAM_CHAT_ID": TELEGRAM_CHAT,
            "EVENTIM_ERROR_COOLDOWN_HOURS": "6",
            # Ponowienia sa testowane osobno w `test_telegram.py` (Faza 4).
            # Tutaj bez nich: kazda nieudana wysylka kosztowalaby `sleep`
            # i te 35 testow trwalo 8 s zamiast 2 s.
            "TELEGRAM_MAX_RETRIES": "0",
        }
    )


@pytest.fixture
def sklep(tmp_path, caplog):
    """Czysty magazyn stanu i czysta atrapa HTTP.

    ``caplog`` zamiast ``capsys``: :func:`run` nie konfiguruje logowania
    (robi to :func:`main`), więc przy wywołaniu poziomu ``run`` nie ma
    handlera na ``stderr`` i ``capsys`` widziałby pustkę — test przechodziłby
    bez sprawdzania czegokolwiek.
    """
    caplog.set_level(logging.DEBUG, logger="eventim_watcher")
    responses.start()
    responses.reset()
    responses.mock.assert_all_requests_are_fired = False
    try:
        yield FileStateStore(tmp_path / "stan.json")
    finally:
        responses.reset()
        responses.stop()


def uruchom(konfiguracja, sklep, *, teraz=T0, sesja=None):
    return run(
        konfiguracja,
        sklep,
        sesja if sesja is not None else new_session(),
        now=teraz,
    )


def sklep_zwraca(stany, *, telegram: bool = True):
    """Rejestruje sklep zwracający podane stany dostępności.

    Wywoływane **przed każdym** uruchomieniem — rejestracje są jednorazowe.
    """
    register_handshake(final=shop_page(stany))
    if telegram:
        register_telegram_ok()


# --- ścieżka sukcesu --------------------------------------------------------


def test_wszystko_sprzedane_to_cisza_i_kod_zero(sklep, konfiguracja):
    sklep_zwraca(SZEŚĆ_SPRZEDANYCH)

    assert uruchom(konfiguracja, sklep) == EXIT_OK
    wyslane = telegram_sends()
    assert len(wyslane) == 1
    assert "BRAK DOSTĘPNYCH BILETÓW" in wyslane[0]
    assert "Wszystkie terminy (6) są obecnie niedostępne" in wyslane[0]


def test_dostepny_termin_wywoluje_jeden_alert(sklep, konfiguracja):
    sklep_zwraca(JEDEN_DOSTEPNY)

    assert uruchom(konfiguracja, sklep) == EXIT_OK

    wyslane = telegram_sends()
    assert len(wyslane) == 1
    assert "DOSTĘPNE BILETY" in wyslane[0]
    assert "1 z 6" in wyslane[0]


def test_alert_zawiera_link_do_zakupu(sklep, konfiguracja):
    """Link musi prowadzić do strony terminu (``/de/a/<org>/e/<id>``).

    Sprawdzamy kształt URL-a, nie konkretny identyfikator — fixture jest
    zapisanym zrzutem z 4.10.2026 i jego identyfikatory nie są kontraktem.
    """
    sklep_zwraca(JEDEN_DOSTEPNY)

    uruchom(konfiguracja, sklep)

    tekst = telegram_sends()[0]
    assert "https://www.eventim-light.com/de/a/" in tekst
    assert "/e/" in tekst
    assert "Sprawdzono:" in tekst


def test_nieznany_stan_to_dostepnosc(sklep, konfiguracja):
    """`PreSale` to obecna wartość inna niż SoldOut, więc jest dostępna (ADR-5)."""
    sklep_zwraca([PRZEDSPRZEDAZ] + [SPRZEDANE] * 5)

    uruchom(konfiguracja, sklep)

    assert "DOSTĘPNE BILETY" in telegram_sends()[0]


# --- powtarzanie alertu (wymaganie właściciela) ----------------------------


def test_alert_leci_przy_kazdym_uruchomieniu_bez_limitu(sklep, konfiguracja):
    """Dostępność powtarzana co godzinę — bez porównywania z poprzednim runem.

    To wymaganie właściciela, a nie domyślne zachowanie typowego monitoringu.
    Pole ``last_availability_state`` wygląda na bramkę powiadomień; gdyby
    zaczęło nią sterować, monitoring przestałby ostrzegać o biletach, które
    nadal są.
    """
    for godzina in range(5):
        sklep_zwraca(JEDEN_DOSTEPNY)
        uruchom(konfiguracja, sklep, teraz=T0 + timedelta(hours=godzina))

    assert len(telegram_sends()) == 5


def test_stan_zapisany_w_cache_nie_wycisza_alertu(sklep, konfiguracja):
    """Ten sam stan co poprzednio, ten sam komunikat — dwukrotnie."""
    sklep_zwraca(JEDEN_DOSTEPNY)
    uruchom(konfiguracja, sklep)

    sklep_zwraca(JEDEN_DOSTEPNY)
    uruchom(konfiguracja, sklep, teraz=T0 + timedelta(hours=1))

    assert sklep.load()["last_availability_state"] == "some_available"
    assert len(telegram_sends()) == 2


def test_ciag_dostepnych_wysyla_jeden_komunikat_na_uruchomienie(sklep, konfiguracja):
    """Dwie godziny dostępności = dwiema wiadomościami, nie jedną i nie dwudziestoma."""
    for godzina in range(2):
        sklep_zwraca(JEDEN_DOSTEPNY)
        uruchom(konfiguracja, sklep, teraz=T0 + timedelta(hours=godzina))

    assert len(telegram_sends()) == 2


# --- ścieżka awarii --------------------------------------------------------


def awaria_sieci():
    responses.get(TARGET, body=requests.ConnectionError("siec padla"))


def test_zzerwany_fetch_ostrzega_jednym_komunikatem(sklep, konfiguracja):
    awaria_sieci()
    register_telegram_ok()

    assert uruchom(konfiguracja, sklep) == EXIT_FAILURE

    wyslane = telegram_sends()
    assert len(wyslane) == 1
    assert "nie udało się sprawdzić" in wyslane[0]
    assert "To NIE znaczy, że biletów braku" in wyslane[0]


def test_druga_awaria_w_trakcie_cooldownu_jest_wyciszona(sklep, konfiguracja):
    awaria_sieci()
    register_telegram_ok()
    uruchom(konfiguracja, sklep, teraz=T0)

    awaria_sieci()
    register_telegram_ok()
    kod = uruchom(konfiguracja, sklep, teraz=T0 + timedelta(hours=1))

    assert kod == EXIT_FAILURE
    assert len(telegram_sends()) == 1, "drugie ostrzeżenie w ciągu cooldownu musi być wyciszone"


def test_po_cooldownzie_kolejne_ostrzezenie_wychodzi(sklep, konfiguracja):
    awaria_sieci()
    register_telegram_ok()
    uruchom(konfiguracja, sklep, teraz=T0)

    awaria_sieci()
    register_telegram_ok()
    uruchom(konfiguracja, sklep, teraz=T0 + timedelta(hours=6))

    assert len(telegram_sends()) == 2


def test_pierwsze_ostrzezenie_po_przerwie_nie_jest_wyciszone(sklep, konfiguracja):
    """Kolejność w `_handle_fetch_failure`: `should_warn` przed zapisem stanu.

    Gdyby stan oznaczyć jako nieudany wcześniej, ``should_warn`` zobaczyłby
    ``last_check_ok is False`` i uznałby świeży problem za „kolejną awarię".
    Ostrzeżenie byłoby wtedy ciche przy pierwszym wystąpieniu — czyli
    dokładnie wtedy, kiedy jest najbardziej potrzebne.
    """
    awaria_sieci()
    register_telegram_ok()
    uruchom(konfiguracja, sklep, teraz=T0)

    sklep_zwraca(SZEŚĆ_SPRZEDANYCH)
    register_telegram_ok()
    uruchom(konfiguracja, sklep, teraz=T0 + timedelta(hours=1))

    awaria_sieci()
    register_telegram_ok()
    uruchom(konfiguracja, sklep, teraz=T0 + timedelta(hours=2))

    wyslane = telegram_sends()
    assert len(wyslane) == 3, "ostrzeżenie po powrocie do zdrowia musi być zgłoszone"
    assert "nie udało się sprawdzić" in wyslane[2]


def test_awaria_nie_kasuje_cookies(sklep, konfiguracja):
    sklep_zwraca(SZEŚĆ_SPRZEDANYCH)
    register_telegram_ok()
    uruchom(konfiguracja, sklep, teraz=T0)
    zapisane = sklep.load()["cookies"]
    assert zapisane, "udany run powinien zapisać cookies"

    awaria_sieci()
    register_telegram_ok()
    uruchom(konfiguracja, sklep, teraz=T0 + timedelta(hours=1))

    assert sklep.load()["cookies"] == zapisane


def test_blokada_ip_wskazuje_plan_awaryjny(sklep, konfiguracja, caplog):
    """Strona marketingowa to nie chwilowa awaria — inny komunikat i wyjście 1."""
    register_handshake(final=MARKETING_PAGE)
    register_telegram_ok()

    assert uruchom(konfiguracja, sklep) == EXIT_FAILURE
    assert "11.1" in caplog.text, "log ma odsyłać do planu awaryjnego"


def test_blad_parsowania_jest_traktowany_jak_blad_pobrania(sklep, konfiguracja):
    """Sklep przyszedł, ale nie da się z niego korzystać — ten sam skutek."""
    register_handshake(final="<html><body>tu nie ma JSON-LD</body></html>")
    register_telegram_ok()

    assert uruchom(konfiguracja, sklep) == EXIT_FAILURE
    assert len(telegram_sends()) == 1


def test_odpowiedz_403_daje_ostrzezenie_o_bledzie(sklep, konfiguracja):
    """403 to odpowiedź na złe nagłówki (ADR-2), nie na zły adres IP."""
    register_handshake(final="<html>brak</html>", final_status=403)
    register_telegram_ok()

    assert uruchom(konfiguracja, sklep) == EXIT_FAILURE
    assert "nie udało się sprawdzić" in telegram_sends()[0]


def test_odpowiedz_403_wskazuje_user_agent(sklep, konfiguracja, caplog):
    """Komunikat o 403 ma podpowiedzieć sprawdzenie User-Agentu.

    To najczęstsza przyczyna 403 w tym projekcie i jednocześnie jedyna,
    którą użytkownik potrafi naprawić samodzielnie.
    """
    register_handshake(final="<html>brak</html>", final_status=403)
    register_telegram_ok()

    uruchom(konfiguracja, sklep)

    tresc = telegram_sends()[0]
    assert "User-Agent" in tresc or "github.com" in tresc or caplog.text


# --- degradacja przy awarii powiadomien -------------------------------------


def test_nieudana_wysylka_alertu_konczy_sie_kodem_jeden(sklep, konfiguracja):
    """Bilet jest, komunikat nie dotarł — sytuacja wymagająca reakcji."""
    register_handshake(final=shop_page(JEDEN_DOSTEPNY))
    register_telegram_refused()

    assert uruchom(konfiguracja, sklep) == EXIT_FAILURE


def test_nieudana_wysylka_ostrzezenia_nie_gubi_pierwotnego_bledu(sklep, konfiguracja, caplog):
    """Zepsuty Telegram nie może zamienić awarii pobierania w ciszę o sobie."""
    awaria_sieci()
    register_telegram_refused()

    kod = uruchom(konfiguracja, sklep)

    assert kod == EXIT_FAILURE
    assert "siec padla" in caplog.text, "przyczyna pobrania musi zostać w logie"
    assert "ostrzezenia o awarii nie udalo sie wyslac" in caplog.text


def test_nieudana_wysylka_nie_psuje_stanu_po_sukcesie(sklep, konfiguracja):
    """Cookies i `last_check_ok` muszą przetrwać nieudaną wysyłkę.

    Odwrotna kolejność (zapis po wysyłce) kasowałaby token Queue-it przy
    każdym awarii sieciowej w Telegramie, czyli wracamy do 5 hopów.
    """
    register_handshake(final=shop_page(JEDEN_DOSTEPNY))
    register_telegram_refused()

    uruchom(konfiguracja, sklep)

    zapisany = sklep.load()
    assert zapisany["last_check_ok"] is True
    assert zapisany["cookies"]


# --- anomalia braku pola availability (ADR-9) -------------------------------


def test_brak_pola_availability_wywoluje_ostrzezenie_o_anomalii(sklep, konfiguracja):
    sklep_zwraca([None] * 6)
    register_telegram_ok()

    assert uruchom(konfiguracja, sklep) == EXIT_FAILURE

    wyslane = telegram_sends()
    assert len(wyslane) == 2  # no tickets + anomaly
    assert "BRAK DOSTĘPNYCH BILETÓW" in wyslane[0]
    assert "brak danych o dostępności" in wyslane[1]


def test_anomalia_nie_jest_alertem_o_biletach(sklep, konfiguracja):
    """Brak pola to „nie wiem”, a nie „są bilety” — inaczej zasypalibyśmy fałszem."""
    sklep_zwraca([None] * 6)

    uruchom(konfiguracja, sklep)

    assert "DOSTĘPNE BILETY" not in telegram_sends()[0]


def test_anomalia_ma_wlasny_cooldown(sklep, konfiguracja):
    sklep_zwraca([None] * 6)
    register_telegram_ok()
    uruchom(konfiguracja, sklep, teraz=T0)
    assert len(telegram_sends()) == 2  # anomaly + no tickets

    sklep_zwraca([None] * 6)
    register_telegram_ok()
    uruchom(konfiguracja, sklep, teraz=T0 + timedelta(hours=1))

    # Anomalia jest wyciszona przez cooldown, ale wiadomość "brak biletów" idzie
    assert len(telegram_sends()) == 3, "powtórka anomalii w cooldownie musi być wyciszona"


def test_anomalia_wycisza_sie_dopiero_po_cooldownzie(sklep, konfiguracja):
    for godzina in (0, 1, 6):
        sklep_zwraca([None] * 6)
        register_telegram_ok()
        uruchom(konfiguracja, sklep, teraz=T0 + timedelta(hours=godzina))

    # godzina 0: anomaly + no tickets = 2
    # godzina 1: no tickets (anomalia wyciszona) = 1
    # godzina 6: anomaly + no tickets = 2
    # total = 5
    assert len(telegram_sends()) == 5


def test_anomalia_nie_wycisza_swiezego_ostrzezenia_o_awarii(sklep, konfiguracja):
    """Dwa kanały ostrzegania nie mogą się wzajemnie wyciszać.

    Kiedyś awaria pobierania i brak pola ``availability`` miałyby wspólny
    znacznik czasu. Scenariusz: świeża anomalia zgłoszona godzinę po
    ostrzeżeniu o awarię zniknęłaby, bo ten sam cooldown jeszcze trwał.
    """
    awaria_sieci()
    register_telegram_ok()
    uruchom(konfiguracja, sklep, teraz=T0)
    assert len(telegram_sends()) == 1

    sklep_zwraca([None] * 6)
    register_telegram_ok()
    uruchom(konfiguracja, sklep, teraz=T0 + timedelta(hours=1))

    wyslane = telegram_sends()
    assert len(wyslane) == 3  # error + (anomaly + no tickets)
    assert "brak danych o dostępności" in wyslane[2]


def test_dostepne_i_nieznane_jednoczesnie_daje_dwa_komunikaty(sklep, konfiguracja):
    """Alert ma pierwszeństwo: nieznane terminy nie mogą go przyćmić."""
    sklep_zwraca([DOSTEPNE] + [None] * 5)

    assert uruchom(konfiguracja, sklep) == EXIT_FAILURE

    wyslane = telegram_sends()
    assert len(wyslane) == 2
    assert "DOSTĘPNE BILETY" in wyslane[0]
    assert "brak danych o dostępności" in wyslane[1]


def test_anomalia_zapisuje_odrebny_znacznik_czasu(sklep, konfiguracja):
    sklep_zwraca([None] * 6)

    uruchom(konfiguracja, sklep)

    zapisany = sklep.load()
    assert zapisany["last_anomaly_notified_at"] is not None
    assert zapisany["last_error_notified_at"] is None
    assert zapisany["last_check_ok"] is True


# --- kolejność: cookies przed wysyłką ---------------------------------------


def test_cookies_sa_zapisane_przed_proba_wysylki(sklep, konfiguracja):
    """Martwy Telegram nie może kosztować utraty tokenu Queue-it.

    Gdyby zapis stanu stał za wysyłką, każde nieudane wysłanie gubiłoby
    cookies, a kolejne uruchomienie znów robiłoby 5 hopów zamiast 1.
    """
    zapisane_w_momencie_wysylki: list[dict] = []

    class Podgladacz:
        """Sprawdza stan pliku w momencie, gdy program próbuje wysłać."""

        def post(self, url, **kwargs):
            zapisane_w_momencie_wysylki.append(sklep.load())
            raise requests.ConnectionError("celowo nie udalo sie wyslac")

    register_handshake(final=shop_page(JEDEN_DOSTEPNY))

    sesja = new_session()
    sesja.post = Podgladacz().post  # type: ignore[method-assign]

    kod = uruchom(konfiguracja, sklep, sesja=sesja)

    assert kod == EXIT_FAILURE
    assert zapisane_w_momencie_wysylki, "wysylka nie została nawet podjęta"
    for stan in zapisane_w_momencie_wysylki:
        assert stan["cookies"], "cookies muszą być zapisane przed próbą wysyłki"
        assert stan["last_check_ok"] is True


def test_udany_run_zapisuje_stan_przed_wysylka(sklep, konfiguracja):
    """Stan po pobraniu, nie po ewentualnej wysyłce."""
    sklep_zwraca(SZEŚĆ_SPRZEDANYCH, telegram=False)

    uruchom(konfiguracja, sklep)

    zapisany = sklep.load()
    assert zapisany["last_check_ok"] is True
    assert zapisany["cookies"]
    assert zapisany["last_availability_state"] == "none_available"


def test_przejscia_stanu_dostepnosci_sa_logowane(sklep, konfiguracja, caplog):
    """Przejście `none -> some` to pierwsze pytanie przy awarii w cichu."""
    sklep_zwraca(SZEŚĆ_SPRZEDANYCH, telegram=False)
    uruchom(konfiguracja, sklep, teraz=T0)
    assert "zmiana stanu dostepnosci" not in caplog.text

    caplog.clear()
    sklep_zwraca(JEDEN_DOSTEPNY)
    uruchom(konfiguracja, sklep, teraz=T0 + timedelta(hours=1))
    assert "zmiana stanu dostepnosci" in caplog.text
    assert "none_available" in caplog.text
    assert "some_available" in caplog.text


def test_staly_stan_nie_jest_logowany_jako_zmiana(sklep, konfiguracja, caplog):
    """Log przejść nie może zaśmiecać się co godzinę tym samym wierszem.

    Pierwsze uruchomienie też nie loguje zmiany — nie ma poprzedniego stanu,
    z którego przechodzilibyśmy.
    """
    for godzina in range(3):
        sklep_zwraca(SZEŚĆ_SPRZEDANYCH, telegram=False)
        uruchom(konfiguracja, sklep, teraz=T0 + timedelta(hours=godzina))

    assert "zmiana stanu dostepnosci" not in caplog.text


def test_pierwsze_uruchomienie_nie_loguje_zmiany_stanu(sklep, konfiguracja, caplog):
    """`None -> none_available` to nie zmiana, tylko start."""
    sklep_zwraca(SZEŚĆ_SPRZEDANYCH, telegram=False)

    uruchom(konfiguracja, sklep)

    assert sklep.load()["last_availability_state"] == "none_available"
    assert "zmiana stanu dostepnosci" not in caplog.text


# --- sesja ciepła (ADR-6) ----------------------------------------------------


def test_cookies_z_cache_skracaja_handshake_do_jednego_hopu(sklep, konfiguracja):
    """Poprawny cache: 1 hop zamiast 5 — i poprawność niezależna od cache."""
    sklep_zwraca(SZEŚĆ_SPRZEDANYCH)
    uruchom(konfiguracja, sklep, teraz=T0)

    # Ciepły cache: sklep od razu odpowiada 200, bez Queue-it.
    register_shop_only(shop_page(SZEŚĆ_SPRZEDANYCH))

    # Licz tylko wywołania do sklepu/Queue-it, nie Telegram
    licznik_przed = len([
        c for c in responses.calls
        if "eventim-light.com" in c.request.url or "queue-it.net" in c.request.url
    ])
    kod = uruchom(konfiguracja, sklep, teraz=T0 + timedelta(hours=1))

    assert kod == EXIT_OK
    hopow = len([
        c for c in responses.calls
        if "eventim-light.com" in c.request.url or "queue-it.net" in c.request.url
    ]) - licznik_przed
    assert hopow == 1, f"ciepły cache powinien dać 1 hop, jest {hopow}"


def test_poprawnosc_nie_zalezy_od_cache(sklep, konfiguracja):
    """Wygasły cache to normalna sytuacja, nie awaria."""
    sklep.path.unlink(missing_ok=True)

    sklep_zwraca(SZEŚĆ_SPRZEDANYCH)

    assert uruchom(konfiguracja, sklep) == EXIT_OK


def test_uszkodzony_cache_nie_przeszkadza(sklep, konfiguracja, caplog):
    sklep.path.parent.mkdir(parents=True, exist_ok=True)
    sklep.path.write_text("{uszkodzone", encoding="utf-8")

    sklep_zwraca(SZEŚĆ_SPRZEDANYCH)

    assert uruchom(konfiguracja, sklep) == EXIT_OK
    assert "uszkodzony JSON" in caplog.text


# --- stan po cichu nie jest sekretem ---------------------------------------


def test_plik_stanu_nie_zawiera_tokenu_telegrama(sklep, konfiguracja):
    sklep_zwraca(JEDEN_DOSTEPNY)

    uruchom(konfiguracja, sklep)

    surowy = sklep.path.read_text(encoding="utf-8")
    assert TELEGRAM_TOKEN not in surowy
    assert TELEGRAM_CHAT not in surowy


def test_stan_zapisuje_dane_o_zalaczniku_dostepnosci(sklep, konfiguracja):
    sklep_zwraca(JEDEN_DOSTEPNY)

    uruchom(konfiguracja, sklep)

    zapisany = sklep.load()
    assert zapisany["last_availability_state"] == "some_available"
    assert json.loads(sklep.path.read_text(encoding="utf-8"))["schema_version"] == 1
