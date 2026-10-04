"""Testy punktu wejscia CLI.

Kryterium ukonczenia Fazy 1: ``python -m eventim_watcher`` konczy sie czytelnym
komunikatem o brakujacym sekrecie - a nie ``KeyError`` z tracebackiem.

**Zaden z tych testow nie uderza w siec.** Od Fazy 5 ``main()`` nie ogranicza
sie do walidacji konfiguracji - wykonuje cale sprawdzenie, wiec bez atrapy
handshake'u osiem testow wykonywalo prawdziwy pieciohopowy handshake ze
sklepem przy kazdym uruchomieniu pytesta. Atrapa jest w ``conftest.py``.
"""

from __future__ import annotations

import pytest
import requests
import responses
from conftest import SHOP_HTML, TARGET, register_handshake, register_telegram_ok

from eventim_watcher.eventim import new_session
from eventim_watcher.main import EXIT_FAILURE, EXIT_OK, main
from eventim_watcher.state import FileStateStore

VALID_TOKEN = "1234567890:AAHkQ1exampleTOKENvalue_do_not_use_1"
VALID_URL = (
    "https://www.eventim-light.com/de/a/65488918b266253e892eaec1/s/692425419d93a5650a708f21"
)


def env(**overrides: str) -> dict[str, str]:
    base = {
        "EVENTIM_TARGET_URL": TARGET,
        "TELEGRAM_BOT_TOKEN": VALID_TOKEN,
        "TELEGRAM_CHAT_ID": "-1001234567890",
    }
    for key, value in overrides.items():
        if value is None:
            base.pop(key, None)
        else:
            base[key] = value
    return {k: v for k, v in base.items() if v is not None}


@pytest.fixture(autouse=True)
def bez_sieci(tmp_path, html_soldout):
    """Zastępuje siec atrapa handshake i izoluje plik stanu.

    Uzywamy **prawdziwej** strony sklepu z fixture'a, a nie syntetycznej:
    wszystkie 6 terminow jest ``SoldOut``. Teraz program **zawsze wysyła**
    wiadomość (także przy braku biletów), więc rejestrujemy atrapę Telegrama.

    Sesja jest przekazywana do ``main()`` wprost, wiec test nie musi wiedziec,
    gdzie program szuka sekretow, i nie zapisuje niczego poza ``tmp_path``.

    ``responses.start()`` a nie ``RequestsMock()`` jako context manager:
    ``register_handshake()`` korzysta z modulowego ``responses.get()``, ktore
    rejestruje na **domyslnej** instancji. Obiekt ``RequestsMock()`` jest
    osobna atrapa i nie widzi tych rejestracji - bez tej uwagi test wyglada
    na sprawny, a w rzeczywistosci nic nie jest zarejestrowane.

    ``responses.reset()`` jest konieczny osobno: ``start()`` go **nie** robi
    (robi go dopiero wejscie w context manager, uzywane przez ``@activate``).
    Bez niego atrapa kumulowala sie miedzy testami tego pliku i pszczyla
    testy ``test_eventim.py`` - handshake zaczynal sie od hopu 5 zamiast 1.
    """
    responses.start()
    responses.reset()
    responses.mock.assert_all_requests_are_fired = False
    try:
        register_handshake(final=html_soldout)
        register_telegram_ok()
        yield FileStateStore(tmp_path / "stan.json")
    finally:
        responses.reset()
        responses.stop()


def uruchom(bez_sieci, argv=None, **overrides):
    """Wywołuje ``main()`` z wstrzykniętym magazynem i sesją."""
    return main(
        argv if argv is not None else [],
        env=env(**overrides),
        store=bez_sieci,
        session=new_session(),
    )


# --- walidacja konfiguracji -------------------------------------------------


def test_brak_sekretow_konczy_sie_czytelnym_komunikatem(capsys):
    kod = main([], env={})

    stderr = capsys.readouterr().err

    assert kod == EXIT_FAILURE
    assert "Niepoprawna konfiguracja" in stderr
    assert "EVENTIM_TARGET_URL" in stderr
    assert "TELEGRAM_BOT_TOKEN" in stderr
    assert "TELEGRAM_CHAT_ID" in stderr


def test_brak_jednego_sekretu_wymienia_tylko_ten(capsys):
    kod = main([], env=env(TELEGRAM_CHAT_ID=None))

    stderr = capsys.readouterr().err

    assert kod == EXIT_FAILURE
    assert "TELEGRAM_CHAT_ID" in stderr
    assert "TELEGRAM_BOT_TOKEN" not in stderr


def test_niepoprawny_url_nie_wypisuje_tracebacku(capsys):
    kod = main([], env=env(EVENTIM_TARGET_URL="https://eventim.de/x"))

    stderr = capsys.readouterr().err

    assert kod == EXIT_FAILURE
    assert "Traceback" not in stderr
    assert "EVENTIM_TARGET_URL" in stderr


# --- pelne sprawdzenie ------------------------------------------------------


def test_poprawna_konfiguracja_konczy_sie_sukcesem(bez_sieci, capsys):
    kod = uruchom(bez_sieci)

    err = capsys.readouterr().err

    assert kod == EXIT_OK
    assert "[start] sprawdzam" in err
    assert "[wynik]" in err


def test_testy_nie_wykonuja_zadnego_zapytania_poza_sklepem(bez_sieci):
    """Atrapa jest jedynym zrodlem odpowiedzi.

    Gdyby program zaczal chodzic w inne miejsce, ten test wykrylby to
    natychmiast - inaczej nieprzechwycony ruch poszedlby do internetu.
    """
    uruchom(bez_sieci)

    obce = [
        c.request.url
        for c in responses.calls
        if "eventim-light.com" not in c.request.url
        and "queue-it.net" not in c.request.url
        and "api.telegram.org" not in c.request.url
    ]
    assert not obce, f"zapytania poza sklepem, Queue-it i Telegram: {obce}"


def test_brak_dostepnych_terminow_nie_wysyla_alertu(bez_sieci, capsys):
    """Wszystko SoldOut — wysyła informację o braku biletów i `exit 0`."""
    from conftest import register_telegram_ok
    register_telegram_ok()

    kod = uruchom(bez_sieci)

    assert kod == EXIT_OK
    err = capsys.readouterr().err
    assert "wiadomosc wyslana" in err or "wyslano" in err
    assert [c for c in responses.calls if "api.telegram.org" in c.request.url]


def test_sciezka_stanu_z_wolnego_miejsca_nie_wywraca_programu(
    capsys, tmp_path, html_soldout
):
    """Brakujacy katalog cache to degradacja, nie awaria.

    ``save()`` nie moze rzucic - inaczej cichy brak mozliwosci zapisu
    cookies zamienilby dzialajacy monitoring w czerwony run.

    Brak mozliwosci tworzenia katalogu symulujemy **plikiem w miejscu
    katalogu**, a nie uprawnieniami: `mkdir(parents=True)` pod katalogiem
    testowym dziala, wiec katalog zrodlowy na Windowsie dalby test, ktory
    przechodzi z tego powodu z ktorego nie powinien.
    """
    przeszkoda = tmp_path / "to-jest-plik"
    przeszkoda.write_text("nie katalog", encoding="utf-8")
    store = FileStateStore(przeszkoda / "pod" / "stan.json")

    responses.reset()
    register_handshake(final=html_soldout)
    register_telegram_ok()
    kod = main([], env=env(), store=store, session=new_session())

    assert kod == EXIT_OK
    assert not store.path.exists()
    assert "nie udalo sie utworzyc katalogu" in capsys.readouterr().err


# --- logi i tokeny ----------------------------------------------------------


def test_token_nie_trafia_do_logow(bez_sieci, capsys):
    uruchom(bez_sieci)

    captured = capsys.readouterr()

    assert VALID_TOKEN not in captured.out
    assert VALID_TOKEN not in captured.err


def test_token_nie_trafia_do_logow_przy_awarii_sieci(bez_sieci, capsys):
    """Blad polaczenia echa URL API, a URL ma token w sciezce.

    Wykryte w tym pliku: ``str(exc)`` z ``requests`` zawiera pelny adres, wiec
    token ladowal do logu wbrew obietnicy z docstringu ``telegram.py``.

    Uzywamy strony z dostepnym terminem, bo inaczej program nie wysylalby
    nic i nie bylo czego redagowac.
    """
    responses.reset()
    register_handshake(final=SHOP_HTML)
    responses.post(
        "https://api.telegram.org/bot" + VALID_TOKEN + "/sendMessage",
        body=requests.ConnectionError("polaczenie zerwane"),
    )

    kod = uruchom(bez_sieci)

    captured = capsys.readouterr()
    assert kod == EXIT_FAILURE
    assert "ConnectionError" in captured.err
    assert VALID_TOKEN not in captured.out
    assert VALID_TOKEN not in captured.err


def test_info_o_domyslnym_user_agent(bez_sieci, capsys):
    uruchom(bez_sieci)

    assert "EVENTIM_USER_AGENT nieustawiony" in capsys.readouterr().err


def test_user_agent_uzywa_repo_bez_ostrzezenia(bez_sieci, capsys):
    uruchom(bez_sieci, EVENTIM_USER_AGENT="eventim-watch/2.0 (+https://github.com/user/repo)")

    err = capsys.readouterr().err
    assert "EVENTIM_USER_AGENT nieustawiony" not in err
    assert "zwroci 403" not in err


def test_ostrzezenie_gdy_user_agent_bez_github(bez_sieci, capsys):
    """UA z adresem e-mail konczy sie 403 przy kazdym sprawdzeniu (ADR-2)."""
    uruchom(bez_sieci, EVENTIM_USER_AGENT="eventim-watch/2.0 (kontakt@example.com)")

    err = capsys.readouterr().err
    assert "github.com" in err
    assert "403" in err


def test_poziom_logow_z_argumentu(bez_sieci, capsys):
    kod = uruchom(bez_sieci, ["--log-level", "debug"])

    assert kod == EXIT_OK
    assert "[debug]" not in capsys.readouterr().err


@pytest.mark.parametrize("poziom", ["DEBUG", "debug", "Warning", "error", "nonsens"])
def test_nieznany_poziom_logow_nie_wywraca_programu(bez_sieci, poziom: str):
    kod = uruchom(bez_sieci, ["--log-level", poziom])

    assert kod == EXIT_OK


# --- argparse ---------------------------------------------------------------


def test_wersja_wypisuje_numer():
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])

    assert excinfo.value.code == EXIT_OK


def test_pomoc_wypisuje_opis():
    with pytest.raises(SystemExit) as excinfo:
        main(["--help"])

    assert excinfo.value.code == EXIT_OK