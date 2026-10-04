"""Testy punktu wejscia CLI.

Kryterium ukonczenia Fazy 1: ``python -m eventim_watcher`` konczy sie czytelnym
komunikatem o brakujacym sekrecie - a nie ``KeyError`` z tracebackiem.
"""

from __future__ import annotations

import pytest

from eventim_watcher.main import EXIT_FAILURE, EXIT_OK, main

VALID_TOKEN = "1234567890:AAHkQ1exampleTOKENvalue_do_not_use_1"
VALID_URL = "https://www.eventim-light.com/de/a/65488918b266253e892eaec1/s/692425419d93a5650a708f21"


def env(**overrides: str) -> dict[str, str]:
    base = {
        "EVENTIM_TARGET_URL": VALID_URL,
        "TELEGRAM_BOT_TOKEN": VALID_TOKEN,
        "TELEGRAM_CHAT_ID": "-1001234567890",
    }
    for key, value in overrides.items():
        if value is None:
            base.pop(key, None)
        else:
            base[key] = value
    return {k: v for k, v in base.items() if v is not None}


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


def test_poprawna_konfiguracja_konczy_sie_sukcesem(capsys):
    kod = main([], env=env())

    captured = capsys.readouterr()

    assert kod == EXIT_OK
    assert "konfiguracja poprawna" in captured.err


def test_token_nie_trafia_do_logow(capsys):
    main([], env=env())

    captured = capsys.readouterr()

    assert VALID_TOKEN not in captured.out
    assert VALID_TOKEN not in captured.err


def test_info_o_domyslnym_user_agent(capsys):
    main([], env=env())

    assert "EVENTIM_USER_AGENT nieustawiony" in capsys.readouterr().err


def test_user_agent_uzywa_repo_bez_ostrzezenia(capsys):
    main([], env=env(EVENTIM_USER_AGENT="eventim-watch/2.0 (+https://github.com/user/repo)"))

    err = capsys.readouterr().err
    assert "EVENTIM_USER_AGENT nieustawiony" not in err
    assert "zwroci 403" not in err


def test_ostrzezenie_gdy_user_agent_bez_github(capsys):
    """UA z adresem e-mail konczy sie 403 przy kazdym sprawdzeniu (ADR-2)."""
    main([], env=env(EVENTIM_USER_AGENT="eventim-watch/2.0 (kontakt@example.com)"))

    err = capsys.readouterr().err
    assert "github.com" in err
    assert "403" in err


def test_poziom_logow_z_argumentu(capsys):
    kod = main(["--log-level", "debug"], env=env())

    assert kod == EXIT_OK
    assert "[debug]" not in capsys.readouterr().err


@pytest.mark.parametrize("poziom", ["DEBUG", "debug", "Warning", "error", "nonsens"])
def test_nieznany_poziom_logow_nie_wywraca_programu(capsys, poziom: str):
    kod = main(["--log-level", poziom], env=env())

    assert kod == EXIT_OK


def test_wersja_wypisuje_numer():
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])

    assert excinfo.value.code == EXIT_OK


def test_pomoc_wypisuje_opis():
    with pytest.raises(SystemExit) as excinfo:
        main(["--help"])

    assert excinfo.value.code == EXIT_OK