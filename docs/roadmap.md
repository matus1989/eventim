# Roadmap — Eventim Ticket Watcher

## Zasady

1. **Faza 0 jest bramką.** Dopóki nie potwierdzimy na prawdziwym runnerze GitHub Actions,
   że pobieranie danych działa, żadna kolejna faza nie jest „gotowa do wdrożenia".
2. **Każda faza kończy się uruchomialnym artefaktem**, nie tylko kodem.
3. **Brak skrótów w testach fetchu.** Testy jednostkowe na atrapach nie dowodzą,
   że handshake działa — dowodzi to jedynie test integracyjny (§Faza 6).
4. Fazy 1–4 to **blok budowania fazy 0**: potrzebujemy działającego klienta,
   żeby cokolwiek zweryfikować na runnerze.

## Status

Fazy 0–4 mają kod i testy. **Faza 0 przeszła na prawdziwym runnerze**
(run `37183741624`, 4.10.2026) — fundament planu jest potwierdzony, więc fazy
5–8 można realizować bez zmiany założenia o środowisku wykonania.

Jedyna rzecz weryfikowana „na żywo" to pobieranie strony. Dostarczanie wiadomości
do Telegramu nadal nie zostało sprawdzone na prawdziwym tokenie — Faza 7 musi
to domknąć, zanim uznamy monitoring za działający.

| Faza | Nazwa | Stan |
|---|---|---|
| 0 | Walidacja na runnerze GitHub Actions | ✅ **przeszła** — `hops=5`, 6 terminow, run `37183741624` |
| 1 | Szkielet projektu i konfiguracja | ✅ |
| 2 | Klient Eventim + handshake Queue-it | ✅ |
| 3 | Parser JSON-LD i model dostępności | ✅ |
| 4 | Notyfikacja Telegram | ✅ kod + testy, **nie sprawdzone na Telegramie** |
| 5 | Orkiestracja, awarie, cooldown | ✅ |
| 6 | Testy i walidacja end-to-end | ✅ 14 testów live, 13 przeszło na żywo |
| 7 | Workflow i dokumentacja | ✅ kod gotowy — ⏳ do wdrożenia (sekrety) |
| 8 | Hardening i obserwowalność | 🔲 |

---

## Faza 0 — Walidacja na runnerze GitHub Actions ✅

**Cel:** dowiedzieć się, czy plan w ogóle jest wykonalny, zanim napiszemy resztę.

**Powód, dla którego to była bramka:** wynik §2.5 dokumentacji architektury
wskazywał, że z IP centrum danych pobieranie deterministycznie kończy się stroną
marketingową zamiast sklepu, a hosted runners to IP chmury Azure. Okazało się
jednak, że hipoteza była zbyt szeroka — patrz wynik poniżej.

**Zakres:** workflow `probe.yml` (110 linii) plus `scripts/probe.py` (613 linii),
który na `ubuntu-latest` uruchamia dokładnie algorytm 5-hopowy z §3.2, wypisuje
liczbę hopów, próbuje sparsować JSON-LD i zapisuje 6 możliwych werdyktów do
artefaktu.

**Metryka sukcesu:** `hops == 5` **i** znaleziony `EventSeries` z 6 `subEvent`.

**Warianty wyniku:**

| Wynik | Działanie | Trafiło? |
|---|---|---|
| Działa | Faza 0 ✓ → kontynuować zgodnie z planem | **tak** |
| Nie działa (strona marketingowa / 403) | Podjąć decyzję z §„Ścieżki eskalacji” | nie |

**Uwaga o kolejności:** fazy 1–3 warto zrobić **równolegle** — klient fetchu jest
potrzebny do Fazy 0, więc nie ma sensu pisać go dwa razy.

**Ścieżki eskalacji — zarchiwizowane, niepotrzebne** (gdyby Faza 0 była negatywna):
- ~~A. Ponowna weryfikacja po 24 h (chwilowa anomalia sieciowa).~~
- ~~B. Zmiana nagłówków / User-Agent na UA przeglądarki — testowane, Chrome UA → 403.~~
- ~~C. Runner spoza puli Azure (inny provider chmurowy, kontener, VPS).~~
- ~~D. Akceptacja gorszego wariantu: runner na maszynie z dostępem do internetu poza chmurą.~~

Zachowane w planie technicznym §11.1 jako instrukcja na wypadek, gdyby kiedyś
monitoring przestał pobierać stronę. Ścieżka D wymagałaby nowej zgody właściciela —
stanowi świadome odejście od ustalenia „tylko hosted runner".

**Zależności:** brak.
**Kryterium zakończenia:** znany wynik z API runnera, udokumentowany w repo.

**WYNIK: ✅ bramka przeszła (run `37183741624`, 4.10.2026, 26 s)**

```
runner:   Linux 6.17.0-1022-azure, x86_64, Python 3.12.14
IP:       172.185.143.245 — AS8075 Microsoft Corporation (Azure)
handshake: 302 -> 200 -> 302 -> 302 -> 200 (152 034 B)
JSON-LD:  U-Bahn-Cabriotour 2026, 6 terminow, SoldOut:6, nieznanych: 0
WERDYKT=OK   hops=5   czas=3.7 s
```

**Potwierdzenie niezależne:** run `37184256979` (4.10.2026, po naprawie opisu runnera)
wyszedł z **innym adresem wychodzącym** — `172.208.126.101`, ten sam AS8075 — i
tez `WERDYKT=OK`, `hops=5`, `SoldOut:6`. To ważniejsze od pierwszego runu: jeden
działający adres mógłby być szczęśliwym trafieniem w obrębie zablokowanego zakresu,
dwa różne adresy z tego samego zakresu **są** dowodem, że blokada go nie obejmuje.

**Najważniejszy wniosek: hipoteza blokady IP centrum danych była błędna.**

Cały plan opierał się na obserwacji, że z adresów centrum danych handshake
kończy się stroną marketingową zamiast sklepu (§2.5 architektury). Runner
GitHub Actions to **też** adres centrum danych — AS8075 Microsoft, dokładnie
ten typ puli, którego baliśmy się — a sklep pobrał się normalnie.

Wniosek operacyjny: **Akamai nie blokuje całych klas adresów**, tylko
konkretne, źle oceniane zakresy. Nasza maszyna lokalna (AS3320 Deutsche
Telekom, adres prywatny) też działa. Zakres, na którym to nie działa, jest
nieznany — i nie jest to runner GitHub Actions, więc nie dotyczy tego projektu.

Konsekwencje:

- **Ryzyko R1 spada z „krytyczne” na „niskie”** — dla tego runnera zostało
  rozstrzygnięte pozytywnie.
- **Pytanie P1 ma odpowiedź: TAK.**
- Ścieżki eskalacji A–D z tej sekcji **nie są potrzebne**. Bezpiecznie je
  zarchiwizować: R1 jest realnym ryzykiem dla *innych* adresów IP, więc kod
  (`BlockedByIpError` + detektor strony marketingowej) zostaje — tylko jako
  diagnoza, nie jako ścieżka awaryjna projektu.
- **`probe.yml` zostaje, ale zmienia rolę** — patrz „Dalszy los diagnostyki” niżej.

**Dalszy los diagnostyki — decyzja właściciela: zostaje, tylko na `workflow_dispatch`.**

Usunięcie workflowu było pierwotnym planem („tymczasowy, kasowany po Fazie 0").
Odrzucone, bo kasowanie usuwa **jedyny** mechanizm w repo, który potrafi odróżnić
te trzy awarie — wszystkie wyglądają identycznie jako „cisza na Telegramie":

| Awaria | Objaw dla użytkownika |
|---|---|
| Akamai usunęło wpis w białej liście User-Agentów (ADR-2, R3) | brak alertów |
| Zmienił się JSON-LD, parser przestał widzieć terminy (R2) | brak alertów |
| Adres runnera w zablokowanym zakresie (R1) | brak alertów |

`watch.yml` z Fazy 7 powie *że* coś się zepsuło. `probe.yml` powie *co*.

Trigger `push` usunięty — 26 s CI na każdy push niczego nie dokładało, a `watch.yml`
i tak zgłosi awarię wcześniej i częściej. Workflow nadal bez sekretów, więc nie da się
go pomylić z brakiem konfiguracji Telegrama. Nazwa zmieniona z „Faza 0 - bramka
pobierania" na „Diagnostyka pobierania strony", bo Faza 0 jest zamknięta i nazwa
kłamałaby.

**Weryfikacja fixture'a:** JSON-LD pobrany na runnerze jest **bajt w bajt
identyczny** z `tests/fixtures/shop_soldout.html`. Parser został zwalidowany
na prawdziwej stronie produkcyjnej, a fixture nadal jest aktualny.

**Błąd znaleziony dzięki artefaktowi:** pierwsza wersja proby gubiła pole
`srodowisko` w JSON-ie — `report, ... = fetch_shop(...)` zastępowało obiekt,
a środowisko było przypisane do poprzedniego. W logu było, w artefakcie już
nie, i nic o tym nie mówiło. Naprawione, z testem `test_raport_json_zawiera_opis_runnera`.
Lekcja: **to, co widać tylko w logu, nie istnieje dla porównania runów**.

---

## Faza 1 — Szkielet projektu i konfiguracja

**Cel:** działający szkielet uruchamiany na maszynie, wczytujący ENV.

**Zakres:**
- `pyproject.toml` — `requests`, `pytest`, `responses`; `requires-python = ">=3.11"`.
- `.gitignore` — `__pycache__/`, `.pytest_cache/`, `.venv/`, `*.egg-info/`, `.env`.
- `src/eventim_watcher/config.py` — dataclass `Config`, walidacja i czytelne komunikaty błędów.
- `src/eventim_watcher/__init__.py`, `__main__.py`.
- Test: poprawna konfiguracja się wczytuje; brak sekretu → czytelny błąd, nie `KeyError`.

**Pułapki:**
- `.env` **nigdy** nie trafia do repo. Sekrety wyłącznie przez GitHub Secrets.
- Nazwy zmiennych ustalone raz: `EVENTIM_TARGET_URL`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.

**Kryterium zakończenia:** `python -m eventim_watcher` kończy się czytelnym
komunikatem o brakującym sekrecie; `pytest` przechodzi.

**Zależności:** brak.

---

## Faza 2 — Klient Eventim + handshake Queue-it 🔏 krytyczna

**Cel:** pobrać prawdziwą stronę sklepu. To najtrudniejszy element całego projektu.

**Zakres:**
- `src/eventim_watcher/eventim.py` — `EventimClient`:
  - `requests.Session` z nagłówkami z ADR-2 (UA zawierający `github.com` — patrz
    architektura §2.4, pomiar 13 wariantów).
  - `allow_redirects=False` i maszyna stanów z ADR-3.
  - Detekcja strony pośredniej: `"cookieEnabled" in text and "cookietest" in text`.
  - Ustawienie `cookietest=1` w domenie bieżącego hosta — **domena musi odpowiadać
    `eventimlight.queue-it.net`, nie `www.eventim-light.com`** (potwierdzone: przy
    złej domenie handshake nie domyka się).
  - Wyodrębnienie URL-a przeładowania z `document.location.href = decodeURIComponent('…')`
    — **wymagane `unquote()`**, parametr jest zakodowany podwójnie.
  - `max_hops = 12`, `timeout = 30`; przekroczenie → `FetchError`.
  - Wyjątki: `FetchError` (obejmuje nietypowe stany, błędy sieciowe i limity hopów).
- Testy jednostkowe na atrapach (`responses`): poprawna sekwencja 5 hopów,
  wykrycie strony pośredniej, limit hopów, timeout, 403.

**Pułapki (potwierdzone empirycznie):**
- `dokument.location.href` jest zakodowane **podwójnie** — bez `unquote` pętla się nie kończy.
- `Location` może być **względne** — zawsze `urljoin`.
- Follow-up do Queue-it musi nosić `Referer` z poprzedniego hopu.
- `responses` nie symuluje automatycznie `Location` — trzeba rejestrować redirecty ręcznie.
- Pusty `Location` przy `3xx` → natychmiastowy błąd, nie ponowienie w nieskończoność.

**Kryterium zakończenia:** testy jednostkowe na atrapach przechodzą;
zadziała też test integracyjny z Fazy 6.

**Zależności:** Faza 1.

---

## Faza 3 — Parser JSON-LD i model dostępności

**Cel:** z HTML wyciągnąć listę terminów ze statusem dostępności.

**Zakres:**
- `src/eventim_watcher/models.py` — `Term` (frozen dataclass) + `UNAVAILABLE`.
- `src/eventim_watcher/parser.py`:
  - Wyciągnięcie `<script type="application/ld+json">` (z flagą `re.S`).
  - `json.loads` → znalezienie węzła `@type == "EventSeries"` w `@graph`
    (nie zakładać, że jest pierwszy).
  - `offers.availability` → `rsplit("/", 1)[-1]` (wartość to pełny URI `schema.org`).
- Testy jednostkowe: JSON-LD z **realnym** payloadem zapisanym w `tests/fixtures/`
  (dane z 4.10.2026), warianty `SoldOut` / `InStock` / `LimitedAvailability` /
  `PreOrder`, brak `subEvent`, uszkodzony JSON, `AggregateOffer` bez `availability`.

**Pułapki:**
- Nazwy wydarzeń zawierają nietypowe znaki i spacje — nie normalizować bez potrzeby
  (potrzebne w komunikacie Telegram).
- Daty mają offset `+02:00` — nie konwertować na UTC przy wyświetlaniu,
  bo „19:00” w Berlinie to „19:00”, a nie „17:00”.
- `AggregateOffer.availability` bywa **absentne** — traktować jako `Unknown`,
  czyli **niedostępne do czasu potwierdzenia** (ADR-9), i zaznaczyć w logu jako
  anomalię markupu. Świadoma korekta wobec pierwotnej wersji planu: brak pola
  to nie informacja o dostępności, lecz brak danych.

**Kryterium zakończenia:** parser zwraca 6 terminów z zapisanego fixture;
test wariantów dostępności przechodzi.

**Zależności:** brak (parser operuje na tekście HTML, nie na sieci).

---

## Faza 4 — Notyfikacja Telegram

**Cel:** wysyłka wiadomości w języku polskim, zwykłym tekstem.

**Zakres:**
- `src/eventim_watcher/telegram.py` — `TelegramNotifier`:
  - `POST {TELEGRAM_BOT_URL}/sendMessage` z `chat_id`, `text`,
    `disable_web_page_preview = true`.
  - `parse_mode` **ustawione na `None`** — świadoma decyzja (ADR: plain text).
  - Obsługa błędów: `raise_for_status()`, mapowanie na `NotifyError`.
  - Timeout i **jedna** ponowna próba — bez niej alert o biletach ginie.
- Formatowanie `src/eventim_watcher/messages.py` — czyste funkcje, łatwe do przetestowania:
  - `format_available(series, terms)` → alert z listą dostępnych terminów, cenami, linkiem.
  - `format_fetch_error(target, error)` → ostrzeżenie o awarii.
- Testy: formatowanie (snapshoty tekstu), wysyłka na atrapze, błąd API.

**Pułapki:**
- Telegram ma limit 4096 znaków na wiadomość → przy wielu terminach potrzebny podział
  na chunki po ~3500 znaków. Uwzględnić od początku.
- `disable_web_page_preview = true` inaczej Telegram generuje podgląd dla linku do sklepu.
- Nie wstawiać `parse_mode` — przy znakach `_`, `*`, `` ` `` w nazwach wydarzeń
  Markdown by się zepsuł. Nazwy wydarzeń tutaj mają `-`, ale „SONDERFAHRT” pokazuje,
  że spacje i myślniki się zdarzają.
- Nie logować `BOT_TOKEN` — odpowiedzi API mogą go echoować w błędach.

**Kryterium zakończenia:** testy formatowania i wysyłki przechodzą;
na Telegramie widać poprawny komunikat.

**Zależności:** Faza 3 (formatowanie używa modelu `Term`).

**Stan (po implementacji):**

- Zrealizowana — `messages.py` + `telegram.py`, 82 testy
  (`tests/test_messages.py`, `tests/test_telegram.py`).
- Doszło trzeciego komunikatu: `format_unknown_availability(series)` — ostrzeżenie
  o anomalii `UNKNOWN` (ADR-9). Bez niego brak danych byłby cichy i wyglądałby
  jak zwykłe `wyprzedane`.
- Odstępstwo: zamiast `raise_for_status()` budujemy opis z pola `description`.
  Telegram echuje token w błędach, a `raise_for_status()` dokładałby do wyjątku
  adres URL z tokenem w ścieżce.
- Odstępstwo: `400` nie jest ponawiane — zły token, nieistniejący czat i za długi
  tekst nie ustąpią przed próbą. Ponawiamy `429`, `5xx` i wyjątki sieciowe, maks. raz.
- Podział na części dzieli po **liniach**, nie po znakach: ucięcie w połowie URL-a
  daje link, którego nie da się kliknąć. Pojedyncza linia za długa idzie w całości.

Pułapki z listy powyżej potwierdziły się w praktyce — każda dostała własny test:

| Pułapka | Test |
|---|---|
| token w logu lub wyjątku | `test_komunikat_bledu_nie_zawiera_tokenu`, `test_komunikat_bledu_nie_wkleja_cala_odpowiedzi` |
| ucięcie URL-a | `test_podzial_nie_lamie_url` |
| `%a` zależny od lokalizacji | `test_dzien_tygodnia_nie_zalezy_od_lokalizacji_procesu` |
| znaki markdown w nazwie | `test_alert_nie_escapuje_znacznikow_markdown` |
| `400` bez sensu ponawiania | `test_tekst_za_dlugi_nie_powoduje_wyslania_od_nowa` |

---

## Faza 5 — Orkiestracja, awarie, cooldown

**Cel:** powiązać komponenty w `main.py` i obsłużyć scenariusze awarii.

**Zakres:**
- `src/eventim_watcher/state.py` — `StateStore` z cache GitHub (adapter `StateBackend`,
  dziś `actions/cache`, jutro dowolny inny):
  - `load_state() / save_state()` — cookies + `last_error_notified_at` + `last_check_ok`.
- `src/eventim_watcher/main.py` — przepływ z §4 dokumentacji architektury:
  1. konfiguracja → 2. cookies → 3. pobranie → 4. parsowanie → 5. ocena → 6. Telegram → 7. zapis.
- Kody wyjścia: `0` = sukces (także „brak dostępności”), `1` = nie udało się sprawdzić.
- Cooldown 6 h na ostrzeżenie o awarii.
- Logowanie strukturalne (sekcje, czytelne w logach Actions).

**Pułapki:**
- `actions/cache` ma twardy limit 10 GB/repozytorium i jest **ewiektowany** po 7 dniach
  bez trafienia — dlatego klient musi działać poprawnie także bez cache (5 hopów).
- Zapis cache **przed** wysyłką Telegram: martwy Telegram nie może powodować utraty cookies.
- Wyjątek w fazie „wyślij Telegram o awarii” nie może maskować pierwotnego błędu —
  złapać, zalogować, nie podnosić dalej.
- Nie kasować cookies przy błędzie — mogą być wciąż ważne i przyspieszyć następny raz.

**Kryterium zakończenia:** przy celowo zerwanym fetchu skrypt wysyła jedno
ostrzeżenie, a kolejne niepowodzenia są wyciszone; exit code poprawny.

**Zależności:** Fazy 2, 3, 4.

**Stan (po implementacji):**

- Zrealizowana — `state.py` + `main.py`, 85 testów
  (`tests/test_state.py` 49, `tests/test_main.py` 36). Cały zestaw: 375 testów w 3 s,
  bez sieci.
- Dwa odstępstwa od planu, oba zapisane w `technical-plan.md` §2 i §6:
  1. **`EVENTIM_STATE_FILE`** — zmienna środowiskowa zamiast zakodowanej ścieżki.
     Bez niej `actions/cache` musiałoby zgadywać katalog; teraz ścieżka jest jawna.
  2. **`last_anomaly_notified_at`** — osobny kanał ostrzegania dla anomalii
     `UNKNOWN`. Przy jednym wspólnym znaczniku czasu świeża anomalia zgłoszona
     godzinę po ostrzeżeniu o awarię zniknęłaby bez powodu.
- Kody wyjścia rozszerzone o trzeci przypadek: anomalia markupu też daje `1`.
  Uzasadnienie i argumenty przeciw — `technical-plan.md` §7.
- `last_availability_state` jest **wyłącznie informacyjne**. Wymaganie właściciela
  brzmi: alert o biletach leci przy każdym uruchomieniu, bez limitu i bez
  porównywania z poprzednim runem (`test_alert_leci_przy_kazdym_uruchomieniu_bez_limitu`).
- Bezpieczeństwo: `str(exc)` z `requests` zawiera pełny adres API wraz z tokenem
  Telegrama. `telegram.py` ma `_redact()` i `_describe_exception()` — bez nich
  token lądowałby w logach i w tekście wyjątku.
- Kolejność jest egzekwowana testami, nie komentarzem: cookies widoczne w pliku
  stanu **w momencie** próby wysyłki (`test_cookies_sa_zapisane_przed_proba_wysylki`).

---

## Faza 6 — Testy i walidacja end-to-end

**Cel:** udowodnić, że handshake działa na żywo — tego nie da się zasymulować.

**Zakres:**
- Testy jednostkowe (atrapy `responses`) — dla faz 2–4, bez sieci.
- Fixture z prawdziwym HTML zapisanym offline → test parsera bez sieci.
- `tests/test_live.py::test_live_fetch` — **wymuszany wyłącznik**, np. `EVENTIM_LIVE=1`,
  domyślnie `skip`. Nie uruchamia się w zwykłym CI.
- Uruchomienie raz na runnerze GitHub Actions: `pytest -m live`.
- Smok: na razie wszystkie terminy `SoldOut` → oczekiwany **brak** alertu, kod `0`.

**Pułapki:**
- Test live w głównym pipeline’ie = samoobciążanie cudzej strony. Tylko na wyraźne żądanie.
- `skipif` musi być oparty na zmiennej środowiskowej, żeby nie dało się przypadkiem włączyć.
- Testy jednostkowe nie mogą udawać testów live — świadomie je rozdzielić i tak nazywać.

**Kryterium zakończenia:** `pytest` zielony bez sieci; `pytest -m live` potwierdza
dostępność na runnerze.

**Zależności:** Fazy 2–5.

**Stan (po implementacji):**

- `tests/test_live.py` — 14 testów, `skipif EVENTIM_LIVE != "1"` + `@pytest.mark.live`.
  W zwykłym przebiegu `pytest` są pomijane (`14 deselected`), więc zwykły CI
  nie dotyka cudzego serwera.
- **Przebieg na żywo 4.10.2026: 13 przeszło, 1 pominięte, 3,5 s.** Jedyny
  pominięty to `test_dostepne_terminy_daja_poprawny_alert` — wszystkie terminy
  są `SoldOut`, więc nie ma realnego alertu do sformatowania. To poprawne
  zachowanie, nie luka.
- Potwierdzone na prawdziwych danych:
  - handshake przechodzi, JSON-LD w jednym bloku, 6 terminow jak w zrzucie;
  - **ADR-6** — ciepły cache daje mniej niż 5 hopów,
  - **ADR-9** — żaden termin nie ma `UNKNOWN`, czyli pole `availability`
    nadal jest zawsze obecne. To potwierdza założenie ADR-9 **tylko dla stanu**
    **`SoldOut`** — pełne zastrzeżenie niżej.

Pułapki z listy powyżej potwierdziły się — każda dostała zabezpieczenie:

| Pułapka | Zabezpieczenie |
|---|---|
| test live w zwykłym CI | `skipif` na zmiennej środowiskowej **oraz** osobny marker — `pytest.ini` z `--strict-markers` odmawia nieznanych markerów |
| obciążanie cudzego serwera | jeden fixture `scope="session"` — cały plik robi **jedno** pobranie, reszta testów czyta jego wynik |
| testy udające testy live | osobny plik, osobny marker, nazwy mówią wprost że są na żywo |

### Czego testy live **nie** sprawdzają

- **Dostarczania Telegrama.** Testy live kończą się na parsowaniu i nigdy
  nie wysyłają wiadomości. Wysyłka jest jedyną nieodwracalną operacją w tym
  repozytorium, a jedyny test, który ją wykonuje, to test jednostkowy na
  atrapie. Realna dostarczalność pozostaje **niesprawdzona** — to zadanie Fazy 7.
- **Stanu innego niż `SoldOut`.** Wszystkie 6 terminów jest wyprzedanych od
  4.10.2026, więc ścieżka „bilet jest” została przetestowana wyłącznie na
  fixture. To świadoma luka, nie pominięcie — nie da się jej zamknąć inaczej niż
  czekając na prawdziwą dostępność.

### Znalezione i naprawione błędy w samych testach

Warto odnotować, bo oba były groźniejsze od braku testu:

| Błąd | Skutek |
|---|---|
| licznik hopów podłączony do loggera bez `setLevel` | `pytest` dziedziczy po rocie `WARNING`, więc `logger.info()` nigdzie nie docierało i licznik widział zero rekordów. Test `0 < 5` przechodził **w próżni** — nie bledzie, tylko kłamie |
| test higieny skanujący własne źródło | znajdował własną linię asercji, więc nie mógł przejść nigdy. Test, który z definicji pada, uczy czytelnika ignorować plik |

---

## Faza 7 — Workflow i dokumentacja

**Cel:** wdrożyć na GitHub Actions.

**Zakres:**
- `.github/workflows/watch.yml`:
  - `on: schedule: - cron: "17 * * * *"` — **celowo minuta 17**, nie `0`:
    cron co godzinę w szczycie minuty jest najbardziej obciążony na GitHub,
    przez co uruchomienia bywają opóźnione o kilka–kilkanaście minut.
  - `workflow_dispatch` do ręcznego uruchomienia i diagnostyki.
  - `permissions: contents: read` — minimalne uprawnienia.
  - `concurrency: group: ticket-watch, cancel-in-progress: false` — brak nakładania
    się uruchomień.
  - `timeout-minutes: 10` — twardy limit, żeby wiszący fetch nie ciągnął minuty.
  - `actions/checkout@v4`, `actions/setup-python@v5` (cache `pip`), `actions/cache@v4`.
  - Sekrety przez `${{ secrets.* }}` — **nigdy** przez `vars`.
  - Job summary (`$GITHUB_STEP_SUMMARY`) — czytelny raport w UI.
- `Readme.md` — opis, instrukcja konfiguracji sekretów, lokalne uruchomienie,
  tabela „co oznacza status runa”. Nazwa z małym `m` **celowo**: to ją podaje
  `pyproject.toml` w polu `readme`. Na systemie plików z rozróżnianiem
  wielkości liter literówka rozbiłaby `pip install -e .`.
- Sekrety do zdefiniowania: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.

**Pułapki:**
- `schedule` na publicznym repo jest throttlowany i **opóźniony o kilka minut** — to norma.
- Workflow **nie działa** na forkach; sekrety są niedostępne w PR-ach.
- Cron nie gwarantuje wykonania co godzinę przy dużym obciążeniu platformy.
- Brak `workflow_dispatch` = brak możliwości szybkiej diagnostyki.

**Kryterium zakończenia:** ręczne uruchomienie `workflow_dispatch` przechodzi
i widać raport; cron zaświeci się w zakładce Actions.

**Zależności:** Fazy 5, 6.

**Stan (po implementacji):**

| Plik | Co wnosi |
|---|---|
| `.github/workflows/watch.yml` | monitoring: cron 17 min po pełnej godzinie + `workflow_dispatch` |
| `src/eventim_watcher/main.py` | `Raport`, `render_step_summary()`, `_zapisz_podsumowanie()` |
| `tests/test_summary.py` | 40 testów podsumowania |
| `tests/test_workflow.py` | 18 testów kontraktu plików workflow |
| `Readme.md` | pełna dokumentacja dla użytkownika |
| `pyproject.toml` | `pyyaml` w zależnościach `dev` |

### Odstępstwo od planu: `pyyaml` w zależnościach `dev`

Plan nie przewidywał parsera YAML, a `tests/test_workflow.py` bez niego nie da się
napisać. Jedna zależność dev, która zamienia regresję cichą w widoczną.

### Podsumowanie runu

`$GITHUB_STEP_SUMMARY` powstaje z obiektu `Raport`, **nie** z parsowania logów
(ADR-12). Liczby wypełniane są tam, gdzie są już policzone, w każdej gałęzi
`run()` — również na ścieżce anomalii, która nie przechodzi przez główny blok.

`_zapisz_podsumowanie()` nigdy nie rzuca (ADR-13): brak prawa do zapisu nie może
zamienić zielonego runu w czerwony. Podsumowanie powstaje też przy błędzie
konfiguracji, bo to najczęstszy błąd wdrożenia i właśnie wtedy zakładka Actions
pokazuje pustkę.

### Testy workflow jako zabezpieczenie cichej regresji

Najgroźniejszy błąd w tym pliku to przesunięcie `secrets.*` do `vars.*`: nic się
nie wywala, `pytest` jest zielony, a GitHub wypisuje token jawnym tekstem przy
każdym uruchomieniu. `tests/test_workflow.py` pilnuje tego pozytywnie
(sekrety czytane z `secrets.*`) i **negatywnie** (brak `push`, brak
`pull_request`, `probe.yml` bez crona, testy przed wysyłką).

### Drobna poprawka w `config.py`

Nagłówek `ConfigError` zgłaszał „znaleziono 1 problemów”, wymieniając dwie
brakujące zmienne — sugerowało to, że wystarczy poprawić jedną. Licznik
usunięty, lista bez zmian. Komunikat jest pierwszą rzeczą, którą wdrażający
widzi po źle ustawionych sekretach, więc „1 problemów" przy dwóch brakach
kosztowało realny czas.

### Czego ta faza **nie** robi

**Nie ustawia sekretów — bo nie ma ich wartości.** Token bota i ID czatu zna
tylko właściciel. `gh secret set` umiałby je wgrać, ale nie ma czego wgrać,
więc kroki są opisane w `Readme.md`, sekcja „Wdrożenie”. Do czasu ustawienia:

- żaden run `watch.yml` nie był wykonany,
- **dostarczalność Telegrama pozostaje niesprawdzona** — jedyna rzecz w całym
  projekcie potwierdzona tylko na atrapach,
- kryterium zakończenia fazy („ręczne uruchomienie przechodzi i widać raport”)
  nie jest spełnione i nie może być spełnione bez właściciela repozytorium.

Kolejny krok dla właściciela: skopiować dwie nazwy z `Readme.md`, sekcja
„Wdrożenie”, do **Settings → Secrets and variables → Actions**, potem
**Run workflow** i spojrzeć na Summary. Przy wszystkich terminach wyprzedanych
nie będzie alertu — i to jest wynik poprawny, nie awaria.

### Wydatek, który warto znać

`pytest -m "not live"` wewnątrz pętli godzinowej to ok. 25 s na godzinę, czyli
~300 min/mies. z limitu ~2000 na repo publicznym. Kupuje jedno: pewność, że
checkout i instalacja są sprawne, zanim program napisze do świata. Jeśli limit
zacznie przeszkadzać, testy przenoszą się do `ci.yml` na `push` — nie usuwają.

---

## Faza 8 — Hardening i obserwowalność

**Cel:** ograniczyć ryzyko po tym, jak monitoring już działa.

**Zakres:**
- **Odzyskanie po wyczerpaniu limitu cron** — sprawdzić, czy planowane 720 uruchomień
  miesięcznie mieści się w limicie publicznych repozytoriów (~2000 min/mies.).
  Zmierzyć realny czas wykonania i zaplanować ewentualne rzadsze sprawdzanie.
- Rozważyć alert po **ciszy**: osobny tani cron (np. co 24 h) sprawdzający,
  czy `watch` w ogóle ostatnio się powiódł — chroni przed sytuacją
  „watch padł i nikt nie wie” przez całą dobę.
- Opcjonalnie ADR-4: rozszerzenie o stronę terminu `/e/` dla granulacji per typ biletu.
- Opcjonalnie: `--dry-run` do podglądu wiadomości bez wysyłki.

**Pułapka:** limit crona liczy **minuty uruchomienia**, nie liczbę uruchomień —
krótki skrypt jest tu zaletą i powodem decyzji o cache cookies.

**Kryterium zakończenia:** limit crona zweryfikowany; alert o ciszy działa.

**Zależności:** Faza 7.

---

## Kolejność i równoległość

```
Faza 1 ──┬──► Faza 2 ──┐
         │             ├──► Faza 5 ──┬──► Faza 6 ──┬──► Faza 7 ──► Faza 8
         └──► Faza 3 ──┤             │             │
                    ───┴──► Faza 4 ──┘             │
                                                    │
Faza 2 (+3) ────────────────────────────────────────┴──► Faza 0 (bramka)
```

Faza 0 korzysta z klienta powstałego w Fazie 2 — dlatego nie jest pierwsza w kodzie,
tylko pierwsza w wartości: **jej wynik może unieważnić cały plan**, więc należy ją
przepuścić zaraz po Fazie 2, przed pisaniem reszty.
