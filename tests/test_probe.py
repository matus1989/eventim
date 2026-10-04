"""Testy skryptu proby z Fazy 0.

Skrypt proby **decyduje** o tym, czy projekt w ogole jest wykonalny na
runnerze GitHub Actions. Blad w tym skrypcie daje falszywy werdykt, a
falszywy werdykt jest gorszy niz brak odpowiedzi - prowadzi do decyzji
o zmianie planu na podstawie bledu pomocniczego kodu.

Dlatego testujemy wszystkie **werdykty**, nie tylko sciezke sukcesu. Do
tego sam log i kody wyjscia, bo to one decyduja o tym, co zobaczy
uzytkownik w UI GitHub Actions.

Atrapy HTTP sa te same co w ``test_eventim.py`` - pelny pieciohopowy
handshake Queue-it, bo proba musi uzywac produkcyjnego klienta, a nie
uproszczonego zapytania.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import pathlib
import sys

import pytest
import responses
from conftest import make_html

# Skrypt nie jest pakietem (jest jednorazowy), wiec ladujemy go wprost
# z pliku. Instalacja w trybie edytowalnym nie obejmuje katalogu scripts/.
_SPEC = importlib.util.spec_from_file_location(
    "probe", pathlib.Path(__file__).resolve().parents[1] / "scripts" / "probe.py"
)
assert _SPEC and _SPEC.loader, "nie znaleziono scripts/probe.py"
probe = importlib.util.module_from_spec(_SPEC)
# Wpis do sys.modules jest wymagany przez dataclass: dekorator szuka modulu
# po `cls.__module__` przy rozstrzyganiu typu pola z adnotacja.
sys.modules["probe"] = probe
_SPEC.loader.exec_module(probe)

TARGET = "https://www.eventim-light.com/de/a/org/s/series"
QUEUE_HOST = "eventimlight.queue-it.net"

QUEUE_ENTRY_URL = (
    f"https://{QUEUE_HOST}/"
    "?c=eventimlight&e=shopde&t=https%3A%2F%2Fwww.eventim-light.com%2Fs"
    "&tsr=1&tsh=2"
)
RELOAD_URL = (
    "https://eventimlight.queue-it.net/"
    "?c=eventimlight&e=shopde&t=https%3A%2F%2Fwww.eventim-light.com%2Fs"
    "&cid=de-DE&tsr=1&tsh=2"
)

INTERSTITIAL = (
    "<!DOCTYPE html><html><head><meta name=\"robots\" content=\"noindex\">"
    "<script type='text/javascript'>"
    "var cookieEnabled = navigator.cookieEnabled;"
    "document.cookie = 'cookietest=1';"
    "document.location.href = decodeURIComponent("
    "'%2F%3Fc%3Deventimlight%26e%3Dshopde%26t%3Dhttps%253A%252F%252F"
    "www.eventim-light.com%252Fs%26cid%3Dde-DE%26tsr%3D1%26tsh%3D2');"
    "</script></head><body>"
    "<div class=\"nocookies alert alert-error hidden\"><p></p></div>"
    "</body></html>"
)

SHOP_HTML = make_html(
    {
        "@type": "EventSeries",
        "name": "Test",
        "url": TARGET,
        "subEvent": [
            {
                "@type": "Event",
                "startDate": "2026-10-09T19:00:00+02:00",
                "offers": {"availability": "https://schema.org/SoldOut"},
            }
        ],
    }
)

MARKETING_PAGE = (
    "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"UTF-8\">"
    "<title>Tickets verkaufen im eigenen Ticket-Shop | EVENTIM.Light</title>"
    "<meta name=\"robots\" content=\"index,follow\">"
    "</head><body><h1>Ticket-Shop</h1></body></html>"
)

AKAMAI_PAGE = (
    "<html><head><title>Access Denied</title></head><body>"
    "<h1>Access Denied</h1><p>You don't have permission to access "
    '"/de/a/org" on this server. Reference #18.1f2a3b4c5d6e7f80</p>'
    "</body></html>"
)


def register_handshake(final: str = SHOP_HTML, final_status: int = 200) -> None:
    """Rejestruje pelny, pieciohopowy handshake Queue-it."""
    responses.get(TARGET, status=302, headers={"Location": QUEUE_ENTRY_URL})
    responses.get(QUEUE_ENTRY_URL, status=200, body=INTERSTITIAL)
    responses.get(
        RELOAD_URL,
        status=302,
        headers={
            "Location": f"{TARGET}?queueittoken=e_shopde~ts_1~ce_true",
            "Set-Cookie": f"Queue-it-token=tok123; Path=/; Domain={QUEUE_HOST}",
        },
    )
    responses.get(
        f"{TARGET}?queueittoken=e_shopde~ts_1~ce_true",
        status=302,
        headers={
            "Location": TARGET,
            "Set-Cookie": "QueueITAccepted-SDFrts345E-V3_shopde=acc; Path=/; "
            "Domain=www.eventim-light.com",
        },
    )
    responses.get(TARGET, status=final_status, body=final)


def prawdziwa_strona() -> str:
    """Prawdziwa strona sklepu z ``tests/fixtures`` (6 terminow)."""
    return pathlib.Path(__file__).parent.joinpath("fixtures", "shop_soldout.html").read_text(
        "utf-8"
    )


@pytest.fixture
def proba(tmp_path, monkeypatch):
    """Srodowisko proby: artefakty w katalogu tymczasowym, zero sieci.

    Dwie rzeczy musza byc odciete: ``describe_environment`` woła dwa
    zewnetrzne serwisy (adres IP i organizacja), a ``ARTIFACTS`` jest
    zwykla sciezka wzgledna, wiec bez tego proba zapisalaby pliki w
    repozytorium.
    """
    monkeypatch.setattr(probe, "ARTIFACTS", tmp_path / "probe-artifacts")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
    monkeypatch.setattr(probe, "describe_environment", lambda url, timeout: {"os": "test"})

    def uruchom(*, pauza: float = 0.0, url: str = TARGET) -> int:
        return probe.main(["--url", url, "--pauza", str(pauza), "--timeout", "5"])

    return uruchom


def report_from_disk(tmp_path) -> dict:
    return json.loads((tmp_path / "probe-artifacts" / "probe-report.json").read_text("utf-8"))


# --- sciezka sukcesu --------------------------------------------------------


@responses.activate
def test_pieciohopowy_handshake_daje_werdykt_ok(proba, tmp_path):
    """Metryka sukcesu z roadmap: strona sklepu z 6 terminami.

    Uzywamy prawdziwego payloadu, bo proba ma oceniac to samo, co program
    produkcyjny - atrapa z jednym terminem nie wykryje zmiany formatu.
    """
    register_handshake(final=prawdziwa_strona())

    assert proba() == probe.EXIT_OK

    dane = report_from_disk(tmp_path)
    assert dane["werdykt"] == probe.V_OK
    assert dane["hops"] == 5, "wymagane jest dokladnie 5 hopow"
    assert dane["znaleziono_json_ld"] is True
    assert dane["liczba_terminow"] == 6
    assert dane["stany"] == {"SoldOut": 6}


@responses.activate
def test_sukces_nie_uruchamia_matrycy_user_agentow(proba, tmp_path):
    """Kolejne zapytania do sklepu moglyby tylko sprowokowac Queue-it.

    Bramka przeszla, wiec diagnozowanie warstwy Akamai nie ma sensu.
    """
    register_handshake()

    assert proba() == probe.EXIT_OK
    assert len(responses.calls) == 5, "matryca UA nie powinna dodawac zapytan"


@responses.activate
def test_pobrana_strona_zapisana_jako_artefakt(proba, tmp_path):
    """Bez zapisanej odpowiedzi nie da sie zdiagnozowac zmiany markupu."""
    register_handshake()

    proba()

    zapisany = tmp_path / "probe-artifacts" / "shop.html"
    assert zapisany.read_text("utf-8") == SHOP_HTML


# --- strona marketingowa (odrzucony adres IP) -------------------------------


@responses.activate
def test_strona_marketingowa_daje_werdykt_o_blokadzie_ip(proba, tmp_path):
    """To glowna hipoteza do zweryfikowania w Fazie 0.

    Handshake konczy sie na stronie promocyjnej zamiast sklepu, a program
    konczy sie bez wyjatku - bez tego rozpoznania monitoring wygladalby
    na sprawny, a nie dziala.
    """
    register_handshake(final=MARKETING_PAGE)

    assert proba() == probe.EXIT_FAIL

    dane = report_from_disk(tmp_path)
    assert dane["werdykt"] == probe.V_IP
    assert dane["bramka_przeszla"] is False
    assert "marketingowej" in dane["szczegoly"]


@responses.activate
def test_blokada_ip_uruchamia_matryce_user_agentow(proba, tmp_path):
    """Dla blokady IP trzeba wiedziec, czy winny jest UA czy adres."""
    register_handshake(final=MARKETING_PAGE)

    proba()

    dane = report_from_disk(tmp_path)
    assert len(dane["handshake"]) == len(probe.UA_WARIANTS)


# --- blokada Akamai 403 -----------------------------------------------------


@responses.activate
def test_odpowiedz_403_daje_werdykt_o_blokadzie_akamai(proba, tmp_path):
    """403 na pierwszym hopie to inna przyczyna niz strona marketingowa."""
    responses.get(TARGET, status=403, body=AKAMAI_PAGE)

    assert proba() == probe.EXIT_FAIL

    dane = report_from_disk(tmp_path)
    assert dane["werdykt"] == probe.V_AKAMAI
    assert "403" in dane["szczegoly"]


@responses.activate
def test_matryca_sprawdza_kazdy_wariant_user_agent(proba, tmp_path):
    responses.get(TARGET, status=403, body=AKAMAI_PAGE)

    proba()

    dane = report_from_disk(tmp_path)
    warianty = [w["wariant"] for w in dane["handshake"]]
    assert warianty == ["produkcyjny", "bez_wersji", "obca_domena", "chrome"]


def test_warianty_uzywaja_repozytorium_jako_kontaktu():
    """UA produkcyjny musi zawierac `github.com` - inaczej Akamai zwraca 403.

    Test chroni przed zmiana wartosci domyslnej na adres e-mail: wyglada
    to jak poprawka, a konczy cichym 403 przy kazdym sprawdzeniu.
    """
    assert "github.com" in probe.DEFAULT_USER_AGENT
    assert "github.com" in dict(probe.UA_WARIANTS)["produkcyjny"]
    assert "github.com" not in dict(probe.UA_WARIANTS)["obca_domena"]


# --- bledna zawartosc strony ------------------------------------------------


@responses.activate
def test_json_ld_bez_terminow_daje_szkicz(proba, tmp_path):
    """Pusta lista terminow nie blokuje adresu IP, ale wymaga poprawki parsera.

    Bramka dotyczy dostepnosci do sklepu. Brak terminow to juz problem
    parsera (ryzyko R2), wiec werdykt jest osobny - i nadal "przeszla".
    """
    pusty = make_html({"@type": "EventSeries", "name": "Test", "url": TARGET, "subEvent": []})
    register_handshake(final=pusty)

    proba()

    dane = report_from_disk(tmp_path)
    assert dane["werdykt"] == probe.V_BEZ_TERMINOW
    assert dane["bramka_przeszla"] is True, "IP nie jest blokowane"


@responses.activate
def test_json_ld_bez_wezla_series_daje_werdykt_o_parsowaniu(proba, tmp_path):
    """Strona sie zaladowala, ale nie ma w niej EventSeries - inny problem."""
    bez_series = make_html({"@type": "WebSite", "name": "Eventim Light"})
    register_handshake(final=bez_series)

    assert proba() == probe.EXIT_FAIL

    dane = report_from_disk(tmp_path)
    assert dane["werdykt"] == probe.V_PARSE
    assert dane["znaleziono_json_ld"] is True


@responses.activate
def test_nieparsowalny_json_ld_daje_werdykt_o_parsowaniu(proba, tmp_path):
    zepsute = (
        '<html><head><script type="application/ld+json">'
        "{to nie jest json}"
        "</script></head><body></body></html>"
    )
    register_handshake(final=zepsute)

    proba()

    dane = report_from_disk(tmp_path)
    assert dane["werdykt"] == probe.V_PARSE


# --- matryca diagnostyczna --------------------------------------------------


@responses.activate
def test_pierwszy_hop_rozpoznaje_strone_akamai():
    responses.get(TARGET, status=403, body=AKAMAI_PAGE)

    wynik = probe.first_hop(TARGET, "eventim-watch/1.0", timeout=5)

    assert wynik["status"] == 403
    assert wynik["akamai"] is True
    assert wynik["ma_json_ld"] is False


@responses.activate
def test_pierwszy_hop_nie_poscig_przekierowania():
    """Jeden hop zamiast pelnego handshake'u - inaczej matryca bylaby 5x drozsza."""
    responses.get(TARGET, status=302, headers={"Location": QUEUE_ENTRY_URL})

    wynik = probe.first_hop(TARGET, "eventim-watch/1.0", timeout=5)

    assert wynik["status"] == 302
    assert wynik["location_host"] == QUEUE_HOST
    assert wynik["akamai"] is False
    assert len(responses.calls) == 1


@responses.activate
def test_403_bez_markerow_akamai_nie_jest_blokada_akamai():
    """Queue-it tez zwraca 403. Odróżnienie ma znaczenie (pytanie P3)."""
    responses.get(TARGET, status=403, body="<html>Zbyt wiele zapytań</body>")

    wynik = probe.first_hop(TARGET, "eventim-watch/1.0", timeout=5)

    assert wynik["status"] == 403
    assert wynik["akamai"] is False


def test_ua_produkcyjny_jako_pierwszy_wariant():
    """Wariant produkcyjny jest jedynym, ktory uzywa program produkcyjny.

    Kolejnosc ma znaczenie: jego wynik musi byc widoczny przed kontrolami
    negatywnymi, a nie pomieszany z nimi.
    """
    assert probe.UA_WARIANTS[0][0] == "produkcyjny"
    assert probe.UA_WARIANTS[0][1] == probe.DEFAULT_USER_AGENT


@responses.activate
def test_ua_produkcyjny_pierwszy_w_trybie_diagnostycznym():
    responses.get(TARGET, status=403, body=AKAMAI_PAGE)

    wyniki = probe.run_matrix(TARGET, timeout=5, pause=0)

    assert wyniki[0]["wariant"] == "produkcyjny"
    assert wyniki[0]["user_agent"] == probe.DEFAULT_USER_AGENT


@responses.activate
def test_matryca_rozroznia_ua_po_naglowku():
    """``responses`` dopasowuje po URL, wiec rozroznienie wariantow robi callback."""

    def callback(request):
        agent = request.headers.get("User-Agent", "")
        if "Chrome" in agent:
            return (302, {"Location": QUEUE_ENTRY_URL}, "")
        return (403, {}, AKAMAI_PAGE)

    responses.add_callback(responses.GET, TARGET, callback=callback)

    wyniki = probe.run_matrix(TARGET, timeout=5, pause=0)

    po_nazwie = {w["wariant"]: w["status"] for w in wyniki}
    assert po_nazwie["chrome"] == 302
    assert po_nazwie["produkcyjny"] == 403


# --- wnioski z matrycy ------------------------------------------------------


def test_wniosek_gdy_ua_produkcyjny_przeszedl():
    """Blokada jest pozniej w lancuchu - UA nie jest winny."""
    wyniki = [
        {"wariant": "produkcyjny", "status": 302},
        {"wariant": "chrome", "status": 403},
    ]

    assert "przepuszcila UA produkcyjny" in probe.diagnose_matrix(wyniki)


def test_wniosek_gdy_chrome_przeszedl_a_produkcyjny_nie():
    """Biala lista UA na runnerze jest inna niz na maszynie lokalnej.

    To najcenniejszy wynik diagnostyczny: uniewaznia ADR-2 i wymaga
    ponownego pomiaru zamiast dyskusji o naglowkach.
    """
    wyniki = [
        {"wariant": "produkcyjny", "status": 403},
        {"wariant": "obca_domena", "status": 403},
        {"wariant": "chrome", "status": 302},
    ]

    wniosek = probe.diagnose_matrix(wyniki)

    assert "UA produkcyjny odrzucony" in wniosek
    assert "biala lista jest inna niz na maszynie" in wniosek
    assert "chrome" in wniosek


def test_wniosek_wymienia_warianty_odrzucone():
    wyniki = [
        {"wariant": "produkcyjny", "status": 403},
        {"wariant": "bez_wersji", "status": 403},
        {"wariant": "obca_domena", "status": 403},
        {"wariant": "chrome", "status": 403},
    ]

    wniosek = probe.diagnose_matrix(wyniki)

    assert "odrzucone: bez_wersji, obca_domena, chrome" in wniosek
    assert "biala lista jest inna" not in wniosek


def test_wniosek_podaje_fakty_a_nie_jedna_przyczyne():
    """Kombinacja wynikow bywa wielokrotna - wybor jednej gubi informacje.

    Produkcyjny odrzucony i Chrome przepuszczony to dwa fakty naraz;
    raport musi podac oba.
    """
    wyniki = [
        {"wariant": "produkcyjny", "status": 403},
        {"wariant": "bez_wersji", "status": 302},
        {"wariant": "obca_domena", "status": 403},
        {"wariant": "chrome", "status": 302},
    ]

    wniosek = probe.diagnose_matrix(wyniki)

    assert "odrzucony 403" in wniosek
    assert "przeszly: bez_wersji, chrome" in wniosek
    assert "odrzucone: obca_domena" in wniosek


# --- raportowanie -----------------------------------------------------------


@responses.activate
def test_raport_json_zawiera_werdykt_i_liczniki(proba, tmp_path):
    """JSON jest dla maszyn - pozniejszego porownania kolejnych runow."""
    register_handshake()

    proba()

    dane = report_from_disk(tmp_path)
    for klucz in ("werdykt", "bramka_przeszla", "hops", "liczba_terminow", "handshake"):
        assert klucz in dane
    assert "znacznik_czasu" in dane


@responses.activate
def test_raport_json_zawiera_opis_runnera(proba, tmp_path):
    """Adres IP i organizacja mowia, z jakiej puli przyszedl run.

    Pierwsza wersja proby gubila to pole: `report, ... = fetch_shop(...)`
    zastapilo obiekt, a srodowisko bylo przypisane do poprzedniego.
    W logu bylo, w artefakcie juz nie - czyli cicho, bez zadnego bledu.
    """
    register_handshake()

    proba()

    dane = report_from_disk(tmp_path)
    assert dane["srodowisko"] == {"os": "test"}, "opis runnera zginal w artefakcie"


@responses.activate
def test_raport_tekstowy_zawiera_znacznik_wyjscia(proba, tmp_path):
    register_handshake()

    proba()

    tekst = (tmp_path / "probe-artifacts" / "probe-report.txt").read_text("utf-8")
    assert "WERDYKT=OK" in tekst
    assert "hops=5" in tekst
    assert "bramka=TAK" in tekst


@responses.activate
def test_raport_json_ma_ten_sam_werdykt_co_podsumowanie(proba, tmp_path):
    """Roznica miedzy plikami oznaczalaby, ze ktorys jest zapisany pozniej."""
    register_handshake(final=MARKETING_PAGE)

    proba()

    dane = report_from_disk(tmp_path)
    podsumowanie = (tmp_path / "summary.md").read_text("utf-8")
    assert dane["werdykt"] in podsumowanie


@responses.activate
def test_podsumowanie_runa_idzie_na_gore_strony(proba, tmp_path):
    """Log przy 200 liniach jest przewijany - podsumowanie musi byc na gorze.

    To jedyne miejsce, w ktorym wynik bramki naprawde zostanie zobaczony.
    """
    register_handshake()

    proba()

    podsumowanie = (tmp_path / "summary.md").read_text("utf-8")
    assert "bramka przeszła" in podsumowanie
    assert "| Hopy (wymagane 5) | 5 |" in podsumowanie
    assert "| Terminów | 1 |" in podsumowanie


@responses.activate
def test_podsumowanie_pokazuje_niepowodzenie(proba, tmp_path):
    register_handshake(final=MARKETING_PAGE)

    proba()

    podsumowanie = (tmp_path / "summary.md").read_text("utf-8")
    assert "bramka NIE przeszła" in podsumowanie
    assert "BLOKADA_IP_STRONA_MARKETINGOWA" in podsumowanie


def test_brak_gITHUB_STEP_SUMMARY_nie_wywala():
    """Probe moze byc uruchomione recznie, bez sekretow i bez runa Actions."""
    assert probe.write_step_summary(probe.Report(verdict=probe.V_OK)) is None


# --- licznik hopow ----------------------------------------------------------


def _emit(licznik, message: str) -> None:
    licznik.emit(logging.LogRecord("x", logging.INFO, "f", 1, message, None, None))


def test_licznik_hopow_bierze_najwyzszy_numer():
    """Klient loguje hop 2 dwa razy: odpowiedz HTTP i strona posrednia.

    Zliczanie linii dawalo 6 zamiast 5, co uniewaznilo by metryke bramki.
    """
    licznik = probe.HopCounter()

    for message in (
        "[fetch] hop 1: 302 www.eventim-light.com (260 B)",
        "[fetch] hop 2: 200 eventimlight.queue-it.net (2378 B)",
        "[fetch] hop 2: strona posrednia, cookietest=1 dla eventimlight.queue-it.net",
        "[fetch] hop 3: 302 eventimlight.queue-it.net (0 B)",
        "[fetch] hop 4: 302 www.eventim-light.com (115 B)",
        "[fetch] hop 5: 200 www.eventim-light.com (152044 B)",
        "[fetch] JSON-LD znalezione po 5 hopach",
    ):
        _emit(licznik, message)

    assert licznik.hops == 5
    assert len(licznik.trace) == 7


def test_licznik_hopow_pomija_komunikaty_bez_hopu():
    licznik = probe.HopCounter()
    _emit(licznik, "[parse] seria='U-Bahn-Cabriotour 2026' terminow=6")

    assert licznik.hops == 0


def test_licznik_hopow_pomija_inny_format():
    """Nieznany format komunikatu nie moze zglosic fałszywego hopa."""
    licznik = probe.HopCounter()
    _emit(licznik, "[fetch] hopX: cos innego")

    assert licznik.hops == 0


# --- kontrakt programu ------------------------------------------------------


def test_kody_wyjscia_sa_rozne():
    """Blad probe'a nie moze wygladac jak blad programu.

    0 to "bramka przeszla", 2 to "nie przeszla". Gdyby obie byly takie
    same, nie daloby sie rozroznic awarii skryptu od wyniku testu.
    """
    assert probe.EXIT_OK == 0
    assert probe.EXIT_FAIL == 2


def test_bramka_przeszla_dla_obu_sciezek_sukcesu():
    assert probe.Report(verdict=probe.V_OK).bramka_przeszla is True
    assert probe.Report(verdict=probe.V_BEZ_TERMINOW).bramka_przeszla is True


@pytest.mark.parametrize(
    "werdykt",
    [
        probe.V_IP,
        probe.V_AKAMAI,
        probe.V_HANDSHAKE,
        probe.V_PARSE,
        probe.V_NET,
        probe.V_INNE,
    ],
)
def test_bramka_nie_przeszla_dla_kazdej_awarii(werdykt: str):
    assert probe.Report(verdict=werdykt).bramka_przeszla is False


def test_domyślny_cel_to_wydarzenie_z_planu():
    """Cel probe'a musi byc produkcyjnym adresem, nie przykladem.

    Inaczej wynik mowilby o dowolnym sklepie, a nie o tym, ktorym sie
    interesujemy.
    """
    assert probe.DEFAULT_TARGET == (
        "https://www.eventim-light.com/de/a/65488918b266253e892eaec1/s/692425419d93a5650a708f21"
    )
    assert probe.parse_args(["--url", TARGET]).url == TARGET


def test_werdykty_sa_stałymi_znanymi_w_dokumentacji():
    """Nazwy werdyktow trafiaja do runa i do dokumentacji - nie zmieniaja sie po cichu."""
    assert probe.V_OK == "OK"
    assert probe.V_IP == "BLOKADA_IP_STRONA_MARKETINGOWA"
    assert probe.V_AKAMAI == "BLOKADA_AKAMAI_403"
