"""Testy odczytu JSON-LD i modelu dostępności.

Warianty stanów są generowane z **prawdziwego** payloadu
(``tests/fixtures/shop_soldout.html``), więc testy zmieniają wyłącznie
stan dostępności, a struktura zawsze jest taka jak na żywo.
"""

from __future__ import annotations

import json

import pytest
from conftest import make_html, set_availability, set_sub_events

from eventim_watcher.models import UNAVAILABLE, UNKNOWN, Series, Term
from eventim_watcher.parser import ParseError, parse_series


# --- prawdziwe dane ---------------------------------------------------------


def test_prawdziwa_strona_odczytana_poprawnie(html_soldout: str):
    series = parse_series(html_soldout)

    assert series.name == "U-Bahn-Cabriotour 2026"
    assert len(series.terms) == 6
    assert series.any_available is False
    assert series.has_unknown is False
    assert series.price_range == (40.0, 58.0)


def test_prawdziwe_terminy_maja_linki_i_ceny(html_soldout: str):
    series = parse_series(html_soldout)

    for term in series.terms:
        assert term.name
        assert term.start.startswith("2026-10-")
        assert "/e/" in term.url, "termin musi linkowac do strony zakupu"
        assert term.low_price == 40.0
        assert term.high_price == 58.0
        assert term.currency == "EUR"


def test_offset_czasu_zachowany(html_soldout: str):
    """Daty maja offset +02:00 - konwersja do UTC przesunelaaby godzine."""
    series = parse_series(html_soldout)

    assert all(term.start.endswith("+02:00") for term in series.terms)


def test_nazwy_terminow_nie_sa_normalizowane(html_soldout: str):
    series = parse_series(html_soldout)

    assert any("SONDERFAHRT" in term.name for term in series.terms)
    assert any("in Englisch" in term.name for term in series.terms)


# --- stany dostepnosci ------------------------------------------------------

@pytest.mark.parametrize(
    "schema_uri",
    [
        "https://schema.org/InStock",
        "https://schema.org/LimitedAvailability",
        "https://schema.org/PreOrder",
        "https://schema.org/PreSale",
        "https://schema.org/OnlineOnly",
        "https://schema.org/BackOrder",
    ],
)
def test_dostepne_stany_wyzwalaja_alert(real_ld: dict, schema_uri: str):
    """Czarna lista (ADR-5): nowe wartosci schema.org tez niosą dostepnosc."""
    html = make_html(set_availability(real_ld, [schema_uri] + ["SoldOut"] * 5))

    series = parse_series(html)

    assert series.any_available is True
    assert len(series.available_terms) == 1
    assert series.available_terms[0].availability == schema_uri.rsplit("/", 1)[-1]


@pytest.mark.parametrize("stan", sorted(UNAVAILABLE))
def test_niedostepne_stany_nie_wyzwalaja_alertu(real_ld: dict, stan: str):
    html = make_html(set_availability(real_ld, [f"https://schema.org/{stan}"] * 6))

    series = parse_series(html)

    assert series.any_available is False
    assert series.available_terms == ()


def test_jeden_dostepny_termin_wystarcza(real_ld: dict):
    """Alert leci, gdy KTÓRYKOLWIEK termin ma bilety (nie wszystkie)."""
    html = make_html(set_availability(real_ld, ["SoldOut", "SoldOut", "InStock"]))

    series = parse_series(html)

    assert series.any_available is True
    assert len(series.available_terms) == 1


def test_brak_pola_availability_to_stan_nieznany_nie_dostepny(real_ld: dict):
    """Brak pola to anomalia, nie potwierdzenie dostępności.

    Gdyby traktowac to jako dostepne, kazda zmiana markupu zasypywalaby
    uzytkownika falszywymi alarmami o biletach.
    """
    html = make_html(set_availability(real_ld, [None] + ["SoldOut"] * 5))

    series = parse_series(html)

    assert series.any_available is False
    assert series.has_unknown is True
    assert len(series.unknown_terms) == 1
    assert series.unknown_terms[0].availability == UNKNOWN
    assert series.unknown_terms[0].is_unknown is True


def test_stan_nieznany_raportowany_w_logu(real_ld: str, caplog: pytest.LogCaptureFixture):
    html = make_html(set_availability(real_ld, [None] * 6))

    with caplog.at_level("WARNING"):
        parse_series(html)

    assert "anomalia markupu" in caplog.text


def test_uri_schema_org_obcinane_do_nazwy(real_ld: dict):
    html = make_html(set_availability(real_ld, ["https://schema.org/InStock"]))

    term = parse_series(html).terms[0]

    assert term.availability == "InStock"
    assert not term.availability.startswith("http")


def test_nieznana_nazwa_stanu_zachowana_jako_taka(real_ld: dict):
    """Nieznana wartosc schema.org nie jest ani dostepna, ani niedostepna."""
    html = make_html(set_availability(real_ld, ["https://schema.org/CośNowego"]))

    term = parse_series(html).terms[0]

    assert term.availability == "CośNowego"
    assert term.available is True  # czarna lista, nie biała


# --- odpornosc na brakujace dane -------------------------------------------

def test_brak_subEvent_nie_jest_bledem(real_ld: dict):
    series = parse_series(make_html(set_sub_events(real_ld, None)))

    assert series.terms == ()
    assert series.any_available is False


def test_pusty_subEvent_nie_jest_bledem(real_ld: dict):
    series = parse_series(make_html(set_sub_events(real_ld, [])))

    assert series.terms == ()
    assert series.any_available is False


def test_subEvent_nie_lista_nie_jest_bledem(real_ld: dict):
    series = parse_series(make_html(set_sub_events(real_ld, "cos-nie-oczekiwanego")))

    assert series.terms == ()


def test_subEvent_liczba_nie_jest_bledem(real_ld: dict):
    series = parse_series(make_html(set_sub_events(real_ld, 42)))

    assert series.terms == ()


def test_subEvent_bez_obiektu_pominiety(real_ld: dict):
    series = parse_series(
        make_html(set_sub_events(real_ld, ["tekst", 42, None, {"@type": "Event", "startDate": "x"}]))
    )

    assert len(series.terms) == 1


def test_brak_offers_daje_stan_nieznany(real_ld: dict):
    series = parse_series(
        make_html(
            set_sub_events(
                real_ld,
                [{"@type": "Event", "name": "Bez ofert",
                  "startDate": "2026-10-09T19:00:00+02:00"}],
            )
        )
    )

    term = series.terms[0]

    assert term.availability == UNKNOWN
    assert term.low_price is None


def test_brak_nazwy_daje_myslnik(real_ld: dict):
    series = parse_series(
        make_html(
            set_sub_events(
                real_ld, [{"@type": "Event", "startDate": "2026-01-01T10:00:00+01:00"}]
            )
        )
    )

    assert series.terms[0].name == "-"


def test_url_terminu_fallback_do_url_serii(real_ld: dict):
    series = parse_series(
        make_html(
            set_sub_events(
                real_ld, [{"@type": "Event", "startDate": "2026-01-01T10:00:00+01:00"}]
            )
        )
    )

    assert series.terms[0].url == (
        "https://www.eventim-light.com/de/a/65488918b266253e892eaec1/s/692425419d93a5650a708f21"
    )


def test_url_fallback_z_argumentu_gdy_seria_go_nie_ma():
    html = make_html(
        {"@type": "EventSeries", "name": "Bez URL",
         "subEvent": [{"@type": "Event", "startDate": "2026-01-01T10:00:00+01:00"}]}
    )

    series = parse_series(html, fallback_url="https://www.eventim-light.com/de/a/x/s/y")

    assert series.terms[0].url == "https://www.eventim-light.com/de/a/x/s/y"


def test_cena_tekstowa_odczytana(real_ld: dict):
    series = parse_series(
        make_html(
            set_sub_events(
                real_ld,
                [{"@type": "Event", "startDate": "2026-01-01T10:00:00+01:00",
                  "offers": {"lowPrice": "12,50", "highPrice": 99,
                             "availability": "InStock"}}],
            )
        )
    )

    term = series.terms[0]

    assert term.low_price == 12.5   # przecinek dziesiętny
    assert term.high_price == 99.0


def test_cena_bezsensowna_daje_none_nie_zero(real_ld: dict):
    """Brak ceny jest lepszy niż ciche wyzerowanie oferty."""
    series = parse_series(
        make_html(
            set_sub_events(
                real_ld,
                [{"@type": "Event", "startDate": "2026-01-01T10:00:00+01:00",
                  "offers": {"lowPrice": "ile?", "availability": "InStock"}}],
            )
        )
    )

    term = series.terms[0]

    assert term.low_price is None
    assert term.available is True


def test_cena_zerowa_nie_jest_uznana_brakiem(real_ld: dict):
    series = parse_series(
        make_html(
            set_sub_events(
                real_ld,
                [{"@type": "Event", "startDate": "2026-01-01T10:00:00+01:00",
                  "offers": {"lowPrice": 0, "highPrice": 0, "availability": "SoldOut"}}],
            )
        )
    )

    assert series.terms[0].low_price == 0.0   # 0 to poprawna cena, nie brak


# --- bledy parsowania -------------------------------------------------------

def test_brak_bloku_json_ld_to_blad():
    with pytest.raises(ParseError, match="nie znaleziono bloku"):
        parse_series("<html><body>Strona bez danych</body></html>")


def test_uszkodzony_json_to_blad():
    html = '<script type="application/ld+json">{zepsute: json,}</script>'

    with pytest.raises(ParseError, match="nie jest poprawnym obiektem JSON"):
        parse_series(html)


def test_json_ld_lista_to_blad():
    with pytest.raises(ParseError, match="poprawnym obiektem JSON"):
        parse_series('<script type="application/ld+json">[1, 2, 3]</script>')


def test_brak_event_series_to_blad():
    html = make_html({"@graph": [{"@type": "WebSite"}, {"@type": "BreadcrumbList"}]})

    with pytest.raises(ParseError, match="EventSeries"):
        parse_series(html)


def test_komunikat_bledu_wymienia_znalezione_typy():
    html = make_html({"@graph": [{"@type": "WebSite"}, {"@type": "Event"}]})

    with pytest.raises(ParseError) as excinfo:
        parse_series(html)

    assert "WebSite" in str(excinfo.value)
    assert "Event" in str(excinfo.value)


def test_event_series_bez_grafu_dziala():
    """Czasem wezel jest w obiekcie glownym, bez @graph."""
    html = make_html(
        {"@type": "EventSeries", "name": "Bez grafu", "url": "https://x/y",
         "subEvent": [{"@type": "Event", "startDate": "2026-01-01T10:00:00+01:00",
                       "offers": {"availability": "InStock"}}]}
    )

    series = parse_series(html)

    assert series.name == "Bez grafu"
    assert series.any_available is True


def test_json_ld_w_kilku_blokach_znajduje_wlasciwy():
    """Strona moze miec kilka blokow JSON-LD - bierzemy ten z EventSeries."""
    html = (
        '<script type="application/ld+json">{"@type":"BreadcrumbList"}</script>\n'
        + make_html(
            {"@type": "EventSeries", "name": "Wlasciwy", "url": "https://x/y",
             "subEvent": [{"@type": "Event", "startDate": "2026-01-01T10:00:00+01:00",
                           "offers": {"availability": "InStock"}}]}
        )
    )

    assert parse_series(html).name == "Wlasciwy"


def test_atrybuty_script_w_innej_kolejnosci():
    html = (
        "<script data-x='1' type='application/ld+json'>"
        '{"@type":"EventSeries","name":"Atrybuty","subEvent":[]}'
        "</script>"
    )

    assert parse_series(html).name == "Atrybuty"


def test_json_ld_wieloliniowy_znaleziony():
    """re.S jest krytyczne - JSON-LD zawsze jest wieloliniowy."""
    html = (
        '<script type="application/ld+json">\n{\n  "@type": "EventSeries",\n'
        '  "name": "Wieloliniowy",\n  "subEvent": []\n}\n</script>'
    )

    assert parse_series(html).name == "Wieloliniowy"


# --- model ------------------------------------------------------------------


def test_series_pusta_nie_dostepna():
    assert Series(name="X", url="u", terms=()).any_available is False


def test_seria_niezmienna():
    series = Series(name="X", url="u", terms=())

    with pytest.raises(AttributeError):
        series.name = "Y"  # type: ignore[misc]


def test_termin_niezmienny():
    term = Term(name="X", start="s", url="u", availability="SoldOut")

    with pytest.raises(AttributeError):
        term.availability = "InStock"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("stan", "etykieta"),
    [
        ("SoldOut", "wyprzedane"),
        ("InStock", "dostepne"),
        ("LimitedAvailability", "ostatnie sztuki"),
        ("PreOrder", "przedsprzedaz"),
        (UNKNOWN, "stan nieznany"),
    ],
)
def test_etykiety_stanow(stan: str, etykieta: str):
    term = Term(name="X", start="s", url="u", availability=stan)

    assert term.label == etykieta


def test_nieznana_etykieta_fallback_do_wartosci():
    term = Term(name="X", start="s", url="u", availability="CosNowego")

    assert term.label == "CosNowego"


def test_cena_przy_nieznanym_stanie_nie_liczona_jako_dostepna():
    """Zakres cen liczony ze wszystkich terminow, także niedostępnych."""
    series = Series(
        name="X",
        url="u",
        terms=(
            Term("a", "s1", "u1", "SoldOut", 40.0, 58.0),
            Term("b", "s2", "u2", "InStock", 45.0, 45.0),
        ),
    )

    assert series.price_range == (40.0, 58.0)


def test_zakres_cen_none_gdy_brak_danych():
    series = Series(name="X", url="u", terms=(Term("a", "s1", "u1", "SoldOut"),))

    assert series.price_range is None