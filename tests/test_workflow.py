"""Testy plikow workflow (.github/workflows/).

Workflow jest jedynym miejscem w repozytorium, gdzie sekret dotyka
konfiguracji. Blad tutaj jest cichy i pozornie niegroźny: przesunięcie
`secrets.*` do `vars.*` nie wywala nic, nie psuje testów i nie widać tego
w logu - GitHub po prostu wypisze token jawnym tekstem. Dlatego kontrakt
jest pilnowany testem, a nie komentarzem.

**Zadanie tych testow jest w duzej czesci negatywne** - sprawdzaja czego
plik **nie** zawiera. Samo potwierdzenie, ze `cron` jest wczesniejszy niz
`0 * * * *`, nie chroni przed wywolaniem na `push`.

`pyyaml` jest w dodatkowych zaleznosciach dev **celowo**: bez parsera
plikow workflow nie da sie przetestowac, a wersja bez tego testu nie ma
jak zglosic regresji.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"

#: Krok, ktory odpala wlasciwy monitoring.
KROK_CHECK = "python -m eventim_watcher"

#: Zmienne, ktore **maja** byc sekretami. Numery sa z definicji
#: niesprawdzalne - chodzi o to, skad GitHub je czyta.
SEKRETY = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")


def wczytaj(nazwa: str) -> dict:
    """Wczytuje workflow i odzyskuje sekcje `on`.

    YAML 1.1 traktuje niequoted ``on:`` jako boola ``True``, wiec klucz jest
    naprzemiennie ``True`` albo ciągiem znaków, zależnie od wersji parsera.
    Bez tej obsługi test wykryłby "brak triggerów" i przeszedłby na ślepo.
    """
    surowe = yaml.safe_load((WORKFLOWS / nazwa).read_text(encoding="utf-8"))
    assert isinstance(surowe, dict), nazwa
    triggery = surowe.get("on", surowe.get(True))
    assert triggery is not None, f"{nazwa}: brak sekcji `on`"
    surowe["on"] = triggery
    return surowe


def kroki(wf: dict) -> list[dict]:
    """Kroki jedynego zadania w pliku.

    Nazwa zadania jest różna w obu workflow (`check` vs `bramka`) i nie ma
    znaczenia dla kontraktu, więc nie pinujemy jej tutaj — pilnujemy tylko,
    że zadanie jest jedno.
    """
    zadania = list(wf["jobs"])
    assert len(zadania) == 1, f"oczekiwano jednego zadania, jest {zadania}"
    return list(wf["jobs"][zadania[0]]["steps"])


def env_krokow(wf: dict) -> list[dict]:
    """Kroki, ktore deklaruja `env`."""
    return [s for s in kroki(wf) if s.get("env")]


def wszystkie_env(wf: dict) -> dict[str, str]:
    """Scalone `env` wszystkich kroków — kolejny krok nadpisuje poprzedni."""
    wynik: dict[str, str] = {}
    for krok in env_krokow(wf):
        wynik.update(krok["env"])
    return wynik


# --- watch.yml: monitoring ------------------------------------------------


@pytest.fixture(scope="module")
def watch() -> dict:
    return wczytaj("watch.yml")


def test_watch_odpala_sie_co_godzine(watch: dict) -> None:
    assert watch["on"]["schedule"] == [{"cron": "17 * * * *"}]


def test_watch_da_sie_odpalic_recznie(watch: dict) -> None:
    """Bez tego po wygaśnięciu crona (60 dni bez aktywności) nie da się
    sprawdzić, czy monitoring w ogóle działa — a po wygaśnięciu jest już
    za późno, żeby zobaczyć to w historii uruchomień."""
    assert "workflow_dispatch" in watch["on"]


@pytest.mark.parametrize("trigger", ["push", "pull_request", "pull_request_target"])
def test_watch_nie_odpala_sie_w_czwy_stylu(watch: dict, trigger: str) -> None:
    """Każdy push to ~40 s limitu crona na nic.

    W `pull_request` sekretów dodatkowo nie ma, więc taki trigger czerwieniłby
    się na konfiguracji, której z definicji nie dostanie.
    """
    assert trigger not in watch["on"]


def test_watch_ma_minimalne_uprawnienia(watch: dict) -> None:
    assert watch["permissions"] == {"contents": "read"}


def test_watch_nie_kasuje_trwajacego_sprawdzenia(watch: dict) -> None:
    assert watch["concurrency"]["group"] == "ticket-watch"
    assert watch["concurrency"]["cancel-in-progress"] is False


def test_watch_ma_krotki_limit_czasu(watch: dict) -> None:
    """Wiszące żądanie HTTP to marnowanie budżetu crona, nie ochrona."""
    limit = watch["jobs"]["check"]["timeout-minutes"]
    assert 0 < limit <= 15, limit


@pytest.mark.parametrize("sekret", SEKRETY)
def test_sekrety_pochodza_z_secrets_a_nie_z_vars(watch: dict, sekret: str) -> None:
    """Najważniejszy test tego pliku.

    `vars.*` nie maskuje wartości — GitHub wypisuje ją w logu przy
    każdym uruchomieniu. Przesunięcie jednej nazwy z `secrets` na `vars`
    nie psuje żadnego innego testu w repozytorium.
    """
    env = wszystkie_env(watch)
    assert sekret in env, f"{sekret} nie jest w zadnym kroku watch.yml"
    wartosc = env[sekret]
    assert wartosc == f"${{{{ secrets.{sekret} }}}}", (
        f"{sekret} czytane z {wartosc!r} - wariant secrets zamiast vars"
    )


def test_adres_sklepu_idzie_przez_vars(watch: dict) -> None:
    """Adres nie jest sekretem — ma byc edytowalny bez rotacji sekretów."""
    assert wszystkie_env(watch)["EVENTIM_TARGET_URL"] == "${{ vars.EVENTIM_TARGET_URL }}"


def test_zaden_krok_nie_zawiera_literalu_w_looku_sekretu(watch: dict) -> None:
    """Nawet `vars` zapisane na sztywno to wyciek w pliku, który czytają
    wszyscy z dostępem do repozytorium, także forkujący."""
    for krok in env_krokow(watch):
        for nazwa, wartosc in krok["env"].items():
            if nazwa in SEKRETY:
                continue
            assert wartosc == "" or wartosc.startswith("${{"), (
                f"{nazwa} = {wartosc!r} - literał zamiast odwołania"
            )


def test_watch_odpala_wlasciwe_check(watch: dict) -> None:
    runy = [s["run"] for s in kroki(watch) if "run" in s]
    assert KROK_CHECK in runy


def test_watch_wylacza_testy_live(watch: dict) -> None:
    """Bez `-m "not live"` godzinne sprawdzenie odpalałoby 14 testów na
    prawdziwym sklepie. To samoobciążanie cudzego serwera i ciche
    pogorszenie czasu każdego runa."""
    runy = " ".join(s["run"] for s in kroki(watch) if "run" in s)
    assert 'pytest -q -m "not live"' in runy


def test_watch_testuje_przed_wysylka(watch: dict) -> None:
    """Testy muszą być **przed** sprawdzeniem, inaczej pierwszy run po złym
    pushu zdąży wysłać do świata komunikat z wadliwego kodu."""
    nazwy = [s.get("name") or s.get("uses") for s in kroki(watch)]
    indeks_testow = next(i for i, n in enumerate(nazwy) if "Testy" in n)
    indeks_checku = next(i for i, n in enumerate(nazwy) if n == "Sprawdzenie dostepnosci")
    assert indeks_testow < indeks_checku


def test_cache_stanu_ma_klucz_unikalny_na_przebieg(watch: dict) -> None:
    """Stały klucz oznacza, że cache zapisuje się raz i już nigdy więcej,
    choć cookies zmieniają się po każdym ruchu. Wywiektowany cache wraca do
    pięciohopowego handshake'u przy każdym sprawdzeniu."""
    cache = next(s for s in kroki(watch) if "actions/cache" in str(s.get("uses", "")))
    assert "${{ github.run_id }}" in cache["with"]["key"]
    assert cache["with"]["restore-keys"], "bez restore-keys pierwszy run zawsze zimny"
    assert cache["with"]["path"] == ".cache"


# --- probe.yml: diagnostyka -----------------------------------------------


def test_probe_nie_pali_limitu_crona() -> None:
    """`probe.yml` nie wysyla nic i nie ma sekretow — odpalanie co godzine
    marnowaloby minuta limitu crona na nic.

    Porownujemy **zbiory kluczy**, nie wartosci: `workflow_dispatch:` bez
    wartosci parsuje sie jako `None`, a nie `True`, i test na wartosci
    wywalilby sie na pliku, ktory jest poprawny.
    """
    triggery = wczytaj("probe.yml")["on"]
    assert "schedule" not in triggery
    assert set(triggery) == {"workflow_dispatch"}, triggery


def test_probe_nie_ma_sekretow() -> None:
    """Diagnostyka nie moze zaleziec od konfiguracji Telegrama — inaczej
    jej awaria wyglada jak awaria monitoringu.

    `env` w tym pliku **jest** i to jest w porzadku: `EVENTIM_TARGET_URL`
    idzie przez `vars`, bo to nie jest sekret. Liczy sie wylacznie brak
    `secrets.*` — sekret w diagnostyce oznaczalby, ze jej awaria nie
    odróżnia sie od awarii monitoringu.
    """
    sekrety = [
        nazwa
        for nazwa, wartosc in wszystkie_env(wczytaj("probe.yml")).items()
        if "secrets." in str(wartosc)
    ]
    assert not sekrety, sekrety
