"""Testy wczytywania i walidacji konfiguracji.

Walidacja jest jedynym miejscem, ktore decyduje, czy monitoring w ogole ruszy,
wiec testy celuja w przypadki, ktore kiedys zamieniaja sie w ciche awarie:
biale znaki po kopiowaniu, zly format tokenu, liczby spoza zakresu.
"""

from __future__ import annotations

import pytest

from eventim_watcher.config import DEFAULT_USER_AGENT, Config, ConfigError

VALID_TOKEN = "1234567890:AAHkQ1exampleTOKENvalue_do_not_use_1"
VALID_URL = "https://www.eventim-light.com/de/a/65488918b266253e892eaec1/s/692425419d93a5650a708f21"


def env(**overrides: str) -> dict[str, str]:
    """Poprawne srodowisko z mozliwoscia nadpisania lub usuniecia zmiennych.

    Klucz o wartosci ``None`` oznacza zmienna nieobecna.
    """
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


# --- happy path -------------------------------------------------------------


def test_poprawna_konfiguracja():
    config = Config.from_env(env())

    assert config.target_url == VALID_URL
    assert config.telegram_bot_token == VALID_TOKEN
    assert config.telegram_chat_id == "-1001234567890"


def test_wartosci_domyslne():
    config = Config.from_env(env())

    assert config.user_agent == DEFAULT_USER_AGENT
    assert config.timeout == 30.0
    assert config.max_hops == 12
    assert config.error_cooldown_hours == 6.0
    assert config.telegram_max_retries == 1


def test_wartosci_zmiennych_nadpisuja_domyslne():
    config = Config.from_env(
        env(
            EVENTIM_USER_AGENT="eventim-watch/2.0 (kontakt@example.com)",
            EVENTIM_TIMEOUT="45",
            EVENTIM_MAX_HOPS="20",
            EVENTIM_ERROR_COOLDOWN_HOURS="2",
            TELEGRAM_MAX_RETRIES="0",
        )
    )

    assert config.user_agent == "eventim-watch/2.0 (kontakt@example.com)"
    assert config.timeout == 45.0
    assert config.max_hops == 20
    assert config.error_cooldown_hours == 2.0
    assert config.telegram_max_retries == 0


def test_podwojny_at_sign_w_chat_id_akceptowany():
    config = Config.from_env(env(TELEGRAM_CHAT_ID="@moj_kanal_ticketow"))

    assert config.telegram_chat_id == "@moj_kanal_ticketow"


def test_ujemny_chat_id_akceptowany():
    config = Config.from_env(env(TELEGRAM_CHAT_ID="-1009876543210"))

    assert config.telegram_chat_id == "-1009876543210"


# --- brakujace sekrety ------------------------------------------------------


@pytest.mark.parametrize(
    "brakujaca",
    ["EVENTIM_TARGET_URL", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"],
)
def test_brak_wymaganej_zmiennej(brakujaca: str):
    with pytest.raises(ConfigError) as excinfo:
        Config.from_env(env(**{brakujaca: None}))

    assert brakujaca in str(excinfo.value)


def test_pusta_zmienna_jest_traktowana_jak_brak():
    with pytest.raises(ConfigError) as excinfo:
        Config.from_env(env(TELEGRAM_BOT_TOKEN="   "))

    assert "TELEGRAM_BOT_TOKEN" in str(excinfo.value)


def test_wszystkie_braki_jednoczesnie():
    """Kluczowe: jeden komunikat zamiast wykrywania po jednym problemie."""
    with pytest.raises(ConfigError) as excinfo:
        Config.from_env(env(EVENTIM_TARGET_URL=None, TELEGRAM_BOT_TOKEN=None))

    message = str(excinfo.value)
    assert "EVENTIM_TARGET_URL" in message
    assert "TELEGRAM_BOT_TOKEN" in message
    # Obie zmienne w jednym komunikacie, nie w kolejnych uruchomieniach.
    assert len(excinfo.value.problems) == 1
    assert "EVENTIM_TARGET_URL" in excinfo.value.problems[0]
    assert "TELEGRAM_BOT_TOKEN" in excinfo.value.problems[0]


def test_komunikat_zawiera_podpowiedzie_gdzie_ustawic_sekrety():
    with pytest.raises(ConfigError) as excinfo:
        Config.from_env(env(TELEGRAM_BOT_TOKEN=None))

    assert "Secrets and variables" in str(excinfo.value)


# --- walidacja URL ----------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://www.eventim-light.com/de/a/x/s/y",          # nie https
        "https://eventim.de/de/a/x/s/y",                   # inna domena
        "https://eventim-light.com.evil.example/de/a/x",    # podszycie domeny
        "https://zly-host.example/de/a/x/s/y",             # obca domena
        "https://www.eventim-light.com",                    # brak sciezki
    ],
)
def test_odrzucone_url(url: str):
    with pytest.raises(ConfigError) as excinfo:
        Config.from_env(env(EVENTIM_TARGET_URL=url))

    assert "EVENTIM_TARGET_URL" in str(excinfo.value)


def test_akceptowana_domena_bez_www():
    config = Config.from_env(
        env(EVENTIM_TARGET_URL="https://eventim-light.com/de/a/x/s/y")
    )

    assert config.target_url == "https://eventim-light.com/de/a/x/s/y"


# --- walidacja sekretow -----------------------------------------------------


@pytest.mark.parametrize(
    "token",
    [
        "abc",                                             # brak separatora
        "1234567890",                                      # sam identyfikator
        "abc:jakistoken",                                  # id nie jest liczba
        f"{'1' * 10}:krótkitoken",                          # za krotki sekret
    ],
)
def test_odrzucone_formaty_tokenu(token: str):
    with pytest.raises(ConfigError) as excinfo:
        Config.from_env(env(TELEGRAM_BOT_TOKEN=token))

    assert "TELEGRAM_BOT_TOKEN" in str(excinfo.value)


@pytest.mark.parametrize(
    "chat_id",
    ["@moj kanal", "moj_kanal", "abc@def", "@abc"],
)
def test_odrzucone_formaty_chat_id(chat_id: str):
    with pytest.raises(ConfigError) as excinfo:
        Config.from_env(env(TELEGRAM_CHAT_ID=chat_id))

    assert "TELEGRAM_CHAT_ID" in str(excinfo.value)


def test_biale_znaki_obciete_we_wszystkich_zmiennych():
    """Kopiuj-wklej dokleja spacje - token z nimi konczy sie 401 u Telegrama."""
    config = Config.from_env(
        env(
            EVENTIM_TARGET_URL=f"  {VALID_URL}  ",
            TELEGRAM_BOT_TOKEN=f"\t{VALID_TOKEN}\n",
            TELEGRAM_CHAT_ID="  -1001234567890  ",
        )
    )

    assert config.target_url == VALID_URL
    assert config.telegram_bot_token == VALID_TOKEN
    assert config.telegram_chat_id == "-1001234567890"


# --- walidacja liczb --------------------------------------------------------


@pytest.mark.parametrize("wartosc", ["abc", "0", "-5", "301", "1e999"])
def test_odrzucone_wartosci_timeout(wartosc: str):
    """EVENTIM_TIMEOUT jest typu float, wiec "1.5" jest poprawne - testuje tylko niepoprawne."""
    with pytest.raises(ConfigError) as excinfo:
        Config.from_env(env(EVENTIM_TIMEOUT=wartosc))

    assert "EVENTIM_TIMEOUT" in str(excinfo.value)


def test_max_hops_musi_byc_calkowita():
    with pytest.raises(ConfigError) as excinfo:
        Config.from_env(env(EVENTIM_MAX_HOPS="12.7"))

    assert "EVENTIM_MAX_HOPS" in str(excinfo.value)


def test_ujemny_cooldown_odrzucony():
    with pytest.raises(ConfigError) as excinfo:
        Config.from_env(env(EVENTIM_ERROR_COOLDOWN_HOURS="-1"))

    assert "EVENTIM_ERROR_COOLDOWN_HOURS" in str(excinfo.value)


def test_wartosci_spoza_zakresu_nie_wplywaja_na_reszte():
    """Jeden zly numer nie powinien blokowac poprawnych pozostalych zmiennych."""
    with pytest.raises(ConfigError) as excinfo:
        Config.from_env(env(EVENTIM_TIMEOUT="0", EVENTIM_MAX_HOPS="20"))

    problems = excinfo.value.problems
    assert len(problems) == 1
    assert "EVENTIM_TIMEOUT" in problems[0]


# --- podsumowanie do logu ---------------------------------------------------


def test_summary_nie_zawiera_tokenu():
    config = Config.from_env(env())

    assert VALID_TOKEN not in config.summary()
    assert "target=" in config.summary()
    assert str(config.telegram_chat_id) in config.summary()


def test_flaga_domyslnego_user_agent():
    assert Config.from_env(env()).uses_default_user_agent is True
    assert Config.from_env(
        env(EVENTIM_USER_AGENT="eventim-watch/9.9 (+https://github.com/user/repo)")
    ).uses_default_user_agent is False


def test_domyslny_user_agent_przepuszczany_przez_akamai():
    """Wartosc domyslna musi zawierac 'github.com' - inaczej kazde sprawdzenie
    konczy sie 403 (pomiar 4.10.2026, 13 wariantow UA, docs/architecture.md 2.5).
    """
    assert "github.com" in DEFAULT_USER_AGENT


def test_niezalezny_od_srodowiska_systemowego():
    """from_env z argumentem nie czyta os.environ - dzieki temu testy sa powtarzalne."""
    with pytest.raises(ConfigError):
        Config.from_env({})


def test_konfiguracja_niezmienna():
    config = Config.from_env(env())

    with pytest.raises(AttributeError):
        config.timeout = 5.0  # type: ignore[misc]