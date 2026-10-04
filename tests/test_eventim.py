"""Testy klienta HTTP: handshake Queue-it, cookies, wykrywanie anomalii.

Wazna ograniczenie biblioteki ``responses``, o ktorym trzeba pamietac:
**nie przekierowuje sama** i **nie przenosi cookies miedzy hopami**.
Dlatego kazdy hop jest rejestrowany recznie - tak jak w realsci.
To jednoczesnie czyni testy dowodem na poprawnosc obslugi przekierowan
i cookies, a nie tylko sprawdzeniem parsowania odpowiedzi.
"""

from __future__ import annotations

import json

import pytest
import requests
import responses
from conftest import FIXTURES, HTML_TEMPLATE, make_html

from eventim_watcher.eventim import (
    BlockedByIpError,
    EventimClient,
    FetchError,
    extract_reload_url,
    has_json_ld,
    is_interstitial,
    looks_like_marketing_page,
    new_session,
)

TARGET = "https://www.eventim-light.com/de/a/org/s/series"
QUEUE_HOST = "eventimlight.queue-it.net"

#: Zwykly wynik: strona sklepu z JSON-LD.
SHOP_HTML = make_html(
    {
        "@type": "EventSeries",
        "name": "Test",
        "url": TARGET,
        "subEvent": [
            {
                "@type": "Event",
                "startDate": "2026-10-09T19:00:00+02:00",
                "offers": {"availability": "https://schema.org/InStock"},
            }
        ],
    }
)

#: Strona posrednia Queue-it (prawdziwa struktura, odtworzona z przechwytu).
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

RELOAD_URL = (
    "https://eventimlight.queue-it.net/"
    "?c=eventimlight&e=shopde&t=https%3A%2F%2Fwww.eventim-light.com%2Fs"
    "&cid=de-DE&tsr=1&tsh=2"
)

QUEUE_ENTRY_URL = (
    f"https://{QUEUE_HOST}/"
    "?c=eventimlight&e=shopde&t=https%3A%2F%2Fwww.eventim-light.com%2Fs"
    "&tsr=1&tsh=2"
)

#: Strona marketingowa zamiast sklepu (objaw odrzucenia adresu IP).
MARKETING_PAGE = (
    "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"UTF-8\">"
    "<title>Tickets verkaufen im eigenen Ticket-Shop | EVENTIM.Light</title>"
    "<meta name=\"robots\" content=\"index,follow\">"
    "</head><body><h1>Ticket-Shop</h1></body></html>"
)


def make_client(user_agent: str = "eventim-watch/1.0 (test)", **kwargs) -> EventimClient:
    return EventimClient(new_session(), user_agent=user_agent, **kwargs)


def register_handshake(*, final: str = SHOP_HTML, final_status: int = 200):
    """Rejestruje pelny, pieciohopowy handshake Queue-it.

    Kolejnosc jest deterministyczna, wiec ``responses`` dopasowuje po
    dokladnym URL - bez dopasowywania po regulach.
    """
    responses.get(
        TARGET,
        status=302,
        headers={"Location": QUEUE_ENTRY_URL},
    )
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


# --- pelny handshake --------------------------------------------------------

@responses.activate
def test_pieciohopowy_handshake_zakonczony_sukcesem():
    register_handshake()

    html = make_client().fetch_html(TARGET)

    assert html == SHOP_HTML
    assert len(responses.calls) == 5, "handshake musi uzyc dokladnie 5 zapytan"


@responses.activate
def test_kolejnosc_hopow_jest_zgodna_z_dokumentacja():
    register_handshake()

    make_client().fetch_html(TARGET)

    hosts = [call.request.url.split("/")[2] for call in responses.calls]
    assert hosts[0] == "www.eventim-light.com"
    assert hosts[1] == hosts[2] == QUEUE_HOST
    assert hosts[3] == hosts[4] == "www.eventim-light.com"


@responses.activate
def test_cookietest_idzie_do_domeny_queue_it():
    """Cookie musi trafic do ``eventimlight.queue-it.net``.

    Umieszczenie go na domenie sklepu nie domyka handshakeu - to najczestsza
    przyczyna zapetlenia petli w nieznanych wariantach (ADR-3).
    """
    register_handshake()

    make_client().fetch_html(TARGET)

    queue_calls = [
        call for call in responses.calls if QUEUE_HOST in call.request.url
    ]
    assert len(queue_calls) == 2
    # Hop 2 (wejscie) nie ma jeszcze cookietest, hop 3 juz musi go wyslac.
    assert "cookietest=1" not in queue_calls[0].request.headers.get("Cookie", "")
    assert "cookietest=1" in queue_calls[1].request.headers.get("Cookie", "")


@responses.activate
def test_cookie_queue_it_token_zapisany_w_jarze():
    register_handshake()
    client = make_client()

    client.fetch_html(TARGET)

    # ``requests`` zapisuje kropke przed domena przy cookie z naglowka
    # ``Set-Cookie`` - tak samo jak robi to Akamai dla ``bm_sz``.
    jar = {(c["domain"], c["name"]): c["value"] for c in client.export_cookies()}
    assert jar[(f".{QUEUE_HOST}", "Queue-it-token")] == "tok123"
    assert jar[(".www.eventim-light.com", "QueueITAccepted-SDFrts345E-V3_shopde")] == "acc"
    assert (QUEUE_HOST, "cookietest") in jar


@responses.activate
def test_referer_przekazywany_do_kolejnego_hopu():
    register_handshake()

    make_client().fetch_html(TARGET)

    hop3 = [c for c in responses.calls if c.request.url == RELOAD_URL][0]
    assert hop3.request.headers["Referer"] == QUEUE_ENTRY_URL


@responses.activate
def test_pierwszy_hop_nie_ma_naglowka_referer():
    register_handshake()

    make_client().fetch_html(TARGET)

    assert "Referer" not in responses.calls[0].request.headers


@responses.activate
def test_kazdy_hop_jest_osobnym_zapytaniem():
    """Klient musi widziec kazdy hop - inaczej nie wykryje strony posredniej.

    Gdyby ``requests`` podazyl sam (``allow_redirects=True``), wykonanie
    skonczyloby sie jednym wywolaniem zamiast pieciu.
    """
    register_handshake()

    make_client().fetch_html(TARGET)

    assert [call.request.url for call in responses.calls] == [
        TARGET,
        QUEUE_ENTRY_URL,
        RELOAD_URL,
        f"{TARGET}?queueittoken=e_shopde~ts_1~ce_true",
        TARGET,
    ]


# --- cieple sesje (cache cookies) -------------------------------------------

@responses.activate
def test_ciepla_sesja_jednym_hopem():
    """Z odtworzonymi cookies sklep odpowiada od razu (ADR-6)."""
    client = make_client()
    client.load_cookies(
        [
            {"domain": "www.eventim-light.com", "path": "/",
             "name": "QueueITAccepted-SDFrts345E-V3_shopde", "value": "acc"},
            {"domain": QUEUE_HOST, "path": "/", "name": "Queue-it-token", "value": "tok"},
            {"domain": QUEUE_HOST, "path": "/", "name": "cookietest", "value": "1"},
        ]
    )
    responses.get(TARGET, status=200, body=SHOP_HTML)

    html = client.fetch_html(TARGET)

    assert html == SHOP_HTML
    assert len(responses.calls) == 1


@responses.activate
def test_ciepla_sesja_wysyla_odtworzone_cookies():
    client = make_client()
    client.load_cookies(
        [{"domain": "www.eventim-light.com", "path": "/",
          "name": "QueueITAccepted-SDFrts345E-V3_shopde", "value": "acc"}]
    )
    responses.get(TARGET, status=200, body=SHOP_HTML)

    client.fetch_html(TARGET)

    assert "QueueITAccepted-SDFrts345E-V3_shopde=acc" in responses.calls[0].request.headers.get("Cookie", "")


@responses.activate
def test_cookie_z_innej_domeny_nie_jest_wysylane():
    """Odzyskanie cookies z cache nie moze przepuszczac ich do innych hostow."""
    client = make_client()
    client.load_cookies(
        [{"domain": ".eventim-light.com", "path": "/", "name": "bm_sz", "value": "x"}]
    )
    responses.get(TARGET, status=200, body=SHOP_HTML)

    client.fetch_html(TARGET)

    sent = responses.calls[0].request.headers.get("Cookie", "")
    assert "bm_sz" in sent  # .eventim-light.com obejmuje www.


# --- eksport i import cookies -----------------------------------------------

def test_eksport_zawiera_domene_sciezke_nazwe_i_wartosc():
    client = make_client()
    client._session.cookies.set("a", "1", domain="x.example", path="/p")

    jar = client.export_cookies()

    assert jar == [{"domain": "x.example", "path": "/p", "name": "a", "value": "1"}]


def test_eksport_pustego_jaru():
    assert make_client().export_cookies() == []


def test_import_pustej_listy():
    assert make_client().load_cookies([]) == 0


@pytest.mark.parametrize(
    "wpis",
    [
        {"name": "a", "value": "1"},                       # brak domeny
        {"domain": "x", "name": "a"},                      # brak wartosci
        {"domain": "x", "value": "1"},                     # brak nazwy
        {"domain": "", "name": "a", "value": "1"},         # pusta domena
        {"name": "", "value": "1", "domain": "x"},         # pusta nazwa
        {"domain": "x", "name": "a", "value": ""},         # pusta wartosc
        "nie-slownik",
        None,
        42,
    ],
)
def test_import_ignoruje_wpisy_uszkodzone(wpis):
    """Cache jest optymalizacja - poprawnosc nie moze od niego zalecec."""
    assert make_client().load_cookies([wpis]) == 0


def test_import_liczy_odtworzone_wpisy():
    client = make_client()
    client.load_cookies(
        [
            {"domain": "x", "name": "a", "value": "1"},
            {"domain": "y", "name": "b", "value": "2"},
            {"domain": "y", "name": "b"},  # uszkodzony, pomijany
        ]
    )

    assert client.load_cookies([]) == 0
    assert len(client.export_cookies()) == 2


def test_import_dominy_z_dot_na_poczatku_zachowana():
    client = make_client()
    client.load_cookies([{"domain": ".eventim-light.com", "name": "bm_sz", "value": "1"}])

    assert client.export_cookies()[0]["domain"] == ".eventim-light.com"


def test_import_brak_sciezki_daje_pusty_string_odtwarza_jako_root():
    client = make_client()
    client.load_cookies([{"domain": "x", "name": "a", "value": "1", "path": ""}])

    assert client.export_cookies()[0]["path"] == "/"


# --- rozpoznawanie rodzaju odpowiedzi ---------------------------------------

def test_rozpoznawanie_json_ld():
    assert has_json_ld(SHOP_HTML) is True
    assert has_json_ld("<html><body>brak</body></html>") is False


def test_rozpoznawanie_strony_posredniej():
    assert is_interstitial(INTERSTITIAL) is True


@pytest.mark.parametrize(
    "tekst",
    [
        "<html>skrypt ustawiajacy cookietest</html>",
        "<html>zmienna sprawdzajaca obsluge plikow</html>",
        "<html>calkiem obce strony</html>",
    ],
)
def test_strona_posrednia_wymaga_oba_markerow(tekst):
    """Sam ``cookietest`` pojawia sie takze w miejscach, ktora nie sa nim."""
    assert is_interstitial(tekst) is False


def test_rozpoznawanie_strony_marketingowej():
    assert looks_like_marketing_page(MARKETING_PAGE) is True


def test_prawdziwa_strona_sklepu_nie_jest_marketingowa():
    assert looks_like_marketing_page(SHOP_HTML) is False


def test_strona_bez_tytulu_nie_jest_marketingowa():
    assert looks_like_marketing_page("<html><body>EVENTIM.Light robots</body></html>") is False


def test_strona_interstitial_nie_jest_marketingowa():
    """Strona posrednia ma ``noindex``, wiec nie moze byc pomylona z marketingowa."""
    assert looks_like_marketing_page(INTERSTITIAL) is False


def test_tytul_bez_robots_nie_jest_marketingowa():
    html = MARKETING_PAGE.replace('content="index,follow"', 'content="noindex"')

    assert looks_like_marketing_page(html) is False


# --- odczytanie adresu przeladowania ----------------------------------------

def test_odczyt_adresu_zdekodowanego_jednokrotnie():
    assert extract_reload_url(INTERSTITIAL, QUEUE_ENTRY_URL) == RELOAD_URL


def test_adres_odczytany_dokladnie_raz():
    """Kluczowy test pulapki: adres jest zakodowany DWUKROTNIE.

    Zdekodowanie tylko jednej warstwy zostawia ``%253A`` i ``%26``, a taki
    URL wraca na strone posrednia - petla zamiast handshakeu.
    """
    matched = extract_reload_url(INTERSTITIAL, QUEUE_ENTRY_URL)

    assert matched == RELOAD_URL
    # Jedna warstwa zdjecia: %3A zostaje jako zakodowanie wartosci `t`,
    # ale %253A (podwojnie zakodowany dwukropek) juz nie.
    assert "%253A" not in matched
    assert "%26" not in matched
    assert "t=https%3A%2F%2Fwww.eventim-light.com%2Fs" in matched


def test_odczyt_adresu_gdy_brak_kodu_js():
    assert extract_reload_url("<html>bez skryptu</html>", QUEUE_ENTRY_URL) is None


def test_odczyt_adresu_gdy_inny_zapis_bez_dekodowania():
    """Brak elastycznosci na zmiane formatu jest ryzykiem, nie dokladoscia."""
    html = "<script>document.location.href = '/?c=eventimlight'</script>"

    assert extract_reload_url(html, QUEUE_ENTRY_URL) is None


def test_prawdziwa_strona_posrednia_z_fixture():
    """Fixture z tests/fixtures musi dac ten sam adres co syntetyczny."""
    html = (FIXTURES / "interstitial.html").read_text(encoding="utf-8")

    assert is_interstitial(html) is True
    assert "document.location.href = decodeURIComponent(" in html

    reload_url = extract_reload_url(html, QUEUE_ENTRY_URL)
    assert reload_url is not None
    assert reload_url.startswith(f"https://{QUEUE_HOST}/?c=eventimlight")
    assert "%253A" not in reload_url


# --- bledne sytuacje --------------------------------------------------------

@responses.activate
def test_blokada_ip_wykryta_jako_osobny_blad():
    register_handshake(final=MARKETING_PAGE)

    with pytest.raises(BlockedByIpError) as excinfo:
        make_client().fetch_html(TARGET)

    assert "Akamai" in str(excinfo.value)


def test_blokada_ip_jest_podklasa_bledu_pobrania():
    assert issubclass(BlockedByIpError, FetchError)


@responses.activate
def test_limit_hopow_przekroczony():
    responses.get(TARGET, status=302, headers={"Location": TARGET})

    with pytest.raises(FetchError, match="limit 3 hopow"):
        make_client(max_hops=3).fetch_html(TARGET)

    assert len(responses.calls) == 3


@responses.activate
def test_limit_hopow_domyślnie_12():
    responses.get(TARGET, status=302, headers={"Location": TARGET})

    with pytest.raises(FetchError, match="limit 12 hopow"):
        make_client().fetch_html(TARGET)

    assert len(responses.calls) == 12


@responses.activate
def test_przekierowanie_bez_adresu_location():
    responses.get(TARGET, status=302)

    with pytest.raises(FetchError, match="bez naglowka Location"):
        make_client().fetch_html(TARGET)


@responses.activate
def test_odpowiedz_403_akamai():
    responses.get(TARGET, status=403, body="Access Denied")

    with pytest.raises(FetchError, match="HTTP 403"):
        make_client().fetch_html(TARGET)


@responses.activate
def test_odpowiedz_404_nie_jest_bledem_marketingowym():
    responses.get(TARGET, status=404, body="<html>nie ma</html>")

    with pytest.raises(FetchError, match="HTTP 404"):
        make_client().fetch_html(TARGET)


@responses.activate
def test_strona_posrednia_bez_adresu_przeladowania():
    """Brak ``document.location.href`` to blad, nie zapetlenie petli."""
    responses.get(
        TARGET,
        status=200,
        body="<html>var cookieEnabled=true; // cookietest</html>",
    )

    with pytest.raises(FetchError, match="bez adresu przeladowania"):
        make_client().fetch_html(TARGET)


@responses.activate
def test_strona_bez_json_ld_i_nie_interstitial():
    responses.get(TARGET, status=200, body="<html><body>czysta strona</body></html>")

    with pytest.raises(FetchError, match="nieoczekiwana odpowiedz HTTP 200"):
        make_client().fetch_html(TARGET)


@responses.activate
def test_komunikat_bledu_zawiera_fragment_odpowiedzi():
    responses.get(TARGET, status=200, body="<html>" + "x" * 5000 + "</html>")

    with pytest.raises(FetchError) as excinfo:
        make_client().fetch_html(TARGET)

    assert "xxxx" in str(excinfo.value)
    assert len(str(excinfo.value)) < 600, "komunikat musi byc obciety"


@responses.activate
def test_blad_sieci_zamieniony_na_fetch_error():
    responses.get(TARGET, body=requests.exceptions.ConnectionError("brak sieci"))

    with pytest.raises(FetchError, match="zadanie HTTP nieudane"):
        make_client().fetch_html(TARGET)


@responses.activate
def test_timeout_zamieniony_na_fetch_error():
    responses.get(TARGET, body=requests.exceptions.ReadTimeout("timeout"))

    with pytest.raises(FetchError) as excinfo:
        make_client().fetch_html(TARGET)

    assert "ReadTimeout" in str(excinfo.value)


def test_interstitial_bez_hosta_w_adresie():
    """Adres względny nie pozwala ustawić ``cookietest`` - blad musi być czytelny.

    To obronna ścieżka: poprawny adres zawsze ma hosta, ale ``urljoin`` może
    zwrócić URL względny, gdyby kiedyś ktoś podał inny format adresu.
    """
    client = make_client()

    with pytest.raises(FetchError, match="brak hosta"):
        client._handle_interstitial(INTERSTITIAL, "relative/path.html", 2)


# --- naglowki ---------------------------------------------------------------

def test_user_agent_nadpisuje_domyslny():
    session = new_session()
    EventimClient(session, user_agent="eventim-watch/9.9 (test)")

    assert session.headers["User-Agent"] == "eventim-watch/9.9 (test)"


def test_naglowki_negocjujace_html():
    session = new_session()
    EventimClient(session)

    assert "text/html" in session.headers["Accept"]
    assert session.headers["Accept-Language"].startswith("de")


@responses.activate
def test_user_agent_wysylany_do_sklepu():
    responses.get(TARGET, status=200, body=SHOP_HTML)

    make_client(user_agent="eventim-watch/7.7 (test)").fetch_html(TARGET)

    assert responses.calls[0].request.headers["User-Agent"] == "eventim-watch/7.7 (test)"


# --- fixture odtwarza prawdziwa strone --------------------------------------

def test_fixture_sklepu_zawiera_json_ld(html_soldout: str):
    assert has_json_ld(html_soldout)
    assert json.loads(
        html_soldout.split('type="application/ld+json">')[1].split("</script>")[0]
    )["@graph"]


def test_szkielet_html_uzywany_w_testach_ma_json_ld():
    assert HTML_TEMPLATE.count("application/ld+json") == 1