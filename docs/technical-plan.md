# Plan techniczny — Eventim Ticket Watcher

Dokument 3 z 3. Zależy od `architecture.md` (uzasadnienia i wyniki badań)
oraz `roadmap.md` (kolejność prac). Tu rozbijamy **każdy aspekt implementacyjny**:
kontrakt, decyzje, pułapki i kryteria akceptacji.

---

## 1. Struktura repozytorium

```
eventim/
├── Readme.md                      # oryginalny prompt (pozostawiony)
├── pyproject.toml
├── .gitignore
├── src/
│   └── eventim_watcher/
│       ├── __init__.py
│       ├── __main__.py            # wejście: python -m eventim_watcher
│       ├── config.py              # Config + walidacja ENV
│       ├── models.py              # Term, UNAVAILABLE
│       ├── eventim.py             # EventimClient (handshake 5 hopów)
│       ├── parser.py              # JSON-LD -> Term[]
│       ├── messages.py            # formatowanie PL (czyste funkcje)
│       ├── telegram.py            # TelegramNotifier
│       ├── state.py               # StateStore + backend cache
│       └── main.py                # orkiestracja + kody wyjścia
├── tests/
│   ├── fixtures/
│   │   ├── shop_soldout.html      # prawdziwy HTML z 4.10.2026
│   │   ├── shop_available.html    # wariant InStock
│   │   ├── event_detail.html      # strona /e/ z ofertami per typ
│   │   └── interstitial.html      # strona pośrednia Queue-it
│   ├── test_parser.py
│   ├── test_eventim.py
│   ├── test_messages.py
│   ├── test_telegram.py
│   ├── test_state.py
│   └── test_live.py               # @pytest.mark.live, skipif
└── .github/
    └── workflows/
        ├── watch.yml              # cron + workflow_dispatch
        └── probe.yml              # tymczasowy, Faza 0
```

**Decyzje:**
- Layout `src/` — wymusza instalację pakietu, więc importy nie działają „przypadkiem” z katalogu.
- `tests/fixtures/` z **prawdziwym** HTML, nie wymyślonym — atrapy potrafią być
  zbyt uprzejme wobec prawdziwego formatu.
- `probe.yml` tymczasowy — usuwany po rozstrzygnięciu Fazy 0.

---

## 2. Konfiguracja

### Kontrakt

| Zmienna | Wymagana | Domyślnie | Opis |
|---|---|---|---|
| `EVENTIM_TARGET_URL` | tak | — | pełny URL strony serii |
| `TELEGRAM_BOT_TOKEN` | tak | — | token z `@BotFather` |
| `TELEGRAM_CHAT_ID` | tak | — | ID czatu/kanału (`@channel` lub liczba) |
| `EVENTIM_USER_AGENT` | nie | `eventim-watch/1.0 (+https://github.com/matus1989/eventim; ticket-availability-monitor)` | własny UA — **musi zawierać `github.com`**, inaczej Akamai zwraca 403 (architektura §2.4) |
| `EVENTIM_TIMEOUT` | nie | `30` | timeout żądania w sekundach |
| `EVENTIM_MAX_HOPS` | nie | `12` | twardy limit przekierowań |
| `EVENTIM_ERROR_COOLDOWN_HOURS` | nie | `6` | cisza między ostrzeżeniami o awarii |
| `TELEGRAM_MAX_RETRIES` | nie | `1` | ponowienia przy błędzie wysyłki |

### Decyzje

**Walidacja przy starcie, nie w trakcie użycia.** `Config.from_env()` rzuca
`ConfigError` ze **wszystkimi** brakującymi zmiennymi naraz (nie pierwszą),
bo poprawianie po jednej w runnerze to marnowanie cudu i limitu cron.

**Brak wartości domyślnej dla sekretów.** Celowa asymetria: URL da się domyślnie
ustawić na znane wydarzenie, ale brak tokenu ma być **głośny**, nie cichy.

**Walidacja typów z zakresem.** `EVENTIM_TIMEOUT=0` albo `abc` musi dać błąd
na starcie, nie `TimeoutError` w środku nocy na runnerze.

**URL ma być walidowany formatem.** Odrzucamy URL niebędący `https://*.eventim-light.com/...`
— lepszy błąd konfiguracji niż ciche pobieranie cudzej strony.

**Brak `.env` w repo.** Lokalnie sekrety z operatora powłoki lub `python-dotenv`
opcjonalnie i wyłącznie poza kontrolą wersji.

### Pułapki

| Pułapka | Skutek | Mitygacja |
|---|---|---|
| `CHAT_ID` jako `@mychannel` bez `@` w ENV | ciche niepowodzenie wysyłki | walidacja formatu |
| `TOKEN` z białym znakiem na końcu (kopiuj-wklej) | `401` u Telegrama | `.strip()` w walidacji + czytelny komunikat |
| Zmienna ustawiona jako `vars` zamiast `secrets` | pusty token w logu runa | dokumentacja + `ConfigError` |

### Kryteria akceptacji

- [ ] Brak `TELEGRAM_BOT_TOKEN` → komunikat wymienia **wszystkie** braki, `exit 1`.
- [ ] `EVENTIM_TIMEOUT=abc` → czytelny `ConfigError`, `exit 1`.
- [ ] `EVENTIM_TARGET_URL` na obcej domenie → odrzucone.
- [ ] Poprawna konfiguracja → obiekt `Config` z wartościami domyślnymi.

---

## 3. Klient HTTP i handshake Queue-it

### Kontrakt

```python
class EventimClient:
    def __init__(self, session: requests.Session, *, max_hops: int = 12,
                 timeout: float = 30.0) -> None: ...

    def fetch_html(self, url: str) -> str:
        """Zwraca HTML strony sklepu. Rzuca FetchError przy każdym niepowodzeniu."""

    def export_cookies(self) -> list[dict]: ...
    def load_cookies(self, jar: list[dict]) -> None: ...
```

### Maszyna stanów (referencyjna)

```
url = target, referer = None, hops = 0

loop:
  hops += 1;  przerwij jeśli hops > max_hops  ->  FetchError

  r = session.get(url, headers=Referer?, allow_redirects=False, timeout=timeout)

  jeśli "application/ld+json" w r.text              -> return r.text
  jeśli wygląda na stronę pośrednią (poniżej)        -> obsłuż interstitial
  jeśli r.status_code in (301,302,303,307,308):
        loc = r.headers.get("Location")
        brak loc                                    -> FetchError
        referer, url = url, urljoin(url, loc); continue
  inaczej                                           -> FetchError
```

### Wykrywanie strony pośredniej

```python
def _is_interstitial(text: str) -> bool:
    return "cookieEnabled" in text and "cookietest" in text
```

Dwa markery naraz, nie jeden — samo `cookietest` pojawia się w miejscach,
które nie są stroną pośrednią.

Wyodrębnienie URL-a przeładowania:

```python
_RELOAD_RE = re.compile(r"document\.location\.href = decodeURIComponent\('([^']+)'\)")
```

Potem **koniecznie** `unquote(matched)` — wartość jest zakodowana **podwójnie**,
co jest główną przyczyną zapętlenia się przy pierwszej implementacji.

### Nagłówki

```python
session.headers.update({
    "User-Agent": user_agent,                                  # własny, ADR-2
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
})
```

**Nie dodajemy** `Sec-Fetch-*`, `Upgrade-Insecure-Requests` ani `sec-ch-ua`.
Testowaliśmy je z IP centrum danych — nie zmieniły zachowania, a podszywanie się
pod przeglądarkę przy własnym UA tylko zwiększa ryzyko błędu 403.

### Cookies

Kluczowe rozróżnienie potwierdzone w badaniu:

| Cookie | Domena |
|---|---|
| `_abck`, `bm_sz` | `.eventim-light.com` |
| `Queue-it-token`, `Queue-it-visitorsession`, `cookietest` | `eventimlight.queue-it.net` |
| `QueueITAccepted-SDFrts345E-V3_shopde` | `www.eventim-light.com` |

`cookietest=1` ustawiamy **w domenie hosta bieżącego żądania**, nie na stałe
w `www.eventim-light.com` — inaczej handshake się nie domyka.

Serializacja do cache: `requests` ma wygodne `session.cookies.get_dict()`, ale
to gubi domenę i ścieżkę. Cache musi przechowywać listę obiektów
`domain`/`path`/`name`/`value` i odtwarzać przez `session.cookies.set(...)`
z poprawnymi `domain=` i `path=`.

### Błędy

Jeden typ wyjątku `FetchError` z komunikatem zawierającym:
liczbę hopów, ostatni URL, ostatni kod HTTP i — **obcięty do 200 znaków** — fragment
tekstu. Bez obcięcia log runa przy 152 KB HTML staje się nieczytelny.

Osobno, bo diagnostycznie istotne: wykrycie **strony marketingowej zamiast sklepu**
(czyli dokładnie objawu blokady IP z Fazy 0). Rozpoznajemy po charakterystycznym
znaczniku i **nie** podchodzimy do `FetchError` — zgłaszamy
`BlockedByIpError`, żeby czytelnik logu od razu wiedział, co się stało.

### Pułapki

| Pułapka | Skutek | Mitygacja |
|---|---|---|
| Brak `unquote` na `Location` | pętla / `404` | test na prawdziwym HTML strony pośredniej |
| Podszywanie Chrome UA | `403` Akamai | własny UA (potwierdzone) |
| Automatyczne follow redirectów | pominięcie detekcji interstitiala | `allow_redirects=False` |
| Zła domena `cookietest` | handshake się nie domyka | domena z bieżącego URL |
| Brak `Referer` | odrzucenie przez Queue-it | przekazuj referer z poprzedniego hopu |
| `3xx` bez `Location` | nieskończona pętla | `FetchError` |
| `max_redirects` requests | cichy `TooManyRedirects` zamiast `FetchError` | własny `max_hops` |

### Kryteria akceptacji

- [ ] Test na atrapach: pełna sekwencja 5 hopów → HTML z JSON-LD.
- [ ] Test: strona pośrednia bez `Location` → `FetchError`, nie pętla.
- [ ] Test: przekroczenie `max_hops` → `FetchError`.
- [ ] Test: `403` → `FetchError` z kodem w komunikacie.
- [ ] Test: strona marketingowa → `BlockedByIpError`.
- [ ] Test: cookies wyeksportowane i odtworzone → drugie pobranie w 1 hopie.

---

## 4. Parser JSON-LD i model dostępności

### Kontrakt

```python
UNAVAILABLE = frozenset({"SoldOut", "Discontinued", "OutOfStock"})

@dataclass(frozen=True)
class Term:
    name: str
    start: str
    url: str
    availability: str
    low_price: float | None
    high_price: float | None
    currency: str | None

    @property
    def available(self) -> bool:
        # UNKNOWN to brak danych, nie potwierdzenie dostępności (ADR-9).
        if self.availability == UNKNOWN:
            return False
        return self.availability not in UNAVAILABLE

    @property
    def is_unknown(self) -> bool:
        return self.availability == UNKNOWN

@dataclass(frozen=True)
class Series:
    name: str
    url: str
    terms: tuple[Term, ...]

    @property
    def available_terms(self) -> tuple[Term, ...]:
        return tuple(t for t in self.terms if t.available)

    @property
    def any_available(self) -> bool:
        return bool(self.available_terms)


def parse_series(html: str) -> Series:      # ParseError przy braku/malformacji
```

### Kroki parsowania

1. `re.search(r'<script type="application/ld\+json">(.*?)</script>', html, re.S)`
   — **wymagany `re.S`**; payload wieloliniowy, bez tego nic nie znajdzie.
2. `json.loads(...)` → słownik z kluczem `@graph`.
3. Wyszukać węzeł `n["@type"] == "EventSeries"` — **nie** `graph[0]`
   (obecnie `@graph` zaczyna się od `WebSite`).
4. Zmapować `subEvent` na `Term`.
5. `availability`: `value.rsplit("/", 1)[-1]` — wartości to pełne URI `schema.org`.
6. `url` z `subEvent["url"]`, z fallbackiem na `offers["url"]`.

### Tolerancja na braki

Wszystkie pola zagnieżdżone traktujemy jako opcjonalne — zmiana markupu nie może
wywrócić monitoringu:

| Brak | Zachowanie |
|---|---|
| `subEvent` | `Series` z pustą listą → `any_available == False`, cicho |
| `offers` | `availability = "Unknown"`, **niedostępny** (ADR-9), `Series.has_unknown` → ostrzeżenie w logu |
| `availability` | jw. |
| `name` | `"-"` |
| `lowPrice`/`highPrice` | `None` → komunikat pomija sekcję cenową |
| `url` | URL serii jako fallback |

**Brak `subEvent` to nie błąd** — znaczy „nie ma terminów”, czyli „nie ma biletów”.
Błędem jest dopiero brak parsowalnego JSON-LD albo jego brak.

### Pułapki

| Pułapka | Skutek | Mitygacja |
|---|---|---|
| `AggregateOffer.availability` bywa `absent` | fałszywy SoldOut **albo** fałszywy alarm o biletach | `Unknown` jako osobny stan (ADR-9); obecna nowa wartość schema.org → dostępna (ADR-5) |
| Założenie `graph[0]` | wyjątek KeyError | szukanie po `@type` |
| Konwersja daty do UTC | „19:00” → „17:00” w komunikacie | zachować offset z ISO 8601 |
| Wykręcanie `Event` ze strony serii | brak danych | osobna ścieżka dla `/e/` (ADR-4) |
| Normalizacja nazw | znikają informacje (`SONDERFAHRT`) | surowa nazwa w komunikacie |

### Kryteria akceptacji

- [ ] Fixture `shop_soldout.html` → 6 terminów, wszystkie `SoldOut`, `any_available == False`.
- [ ] Fixture `shop_available.html` → `any_available == True`, poprawne `url` i ceny.
- [ ] `LimitedAvailability`, `PreOrder`, `PreSale` → dostępne.
- [ ] Brak `subEvent` → pusta lista, bez wyjątku.
- [ ] Uszkodzony JSON → `ParseError` z czytelnym komunikatem.
- [ ] Brak `application/ld+json` → `ParseError`, nie `AttributeError`.

---

## 5. Notyfikacja Telegram

### Kontrakt

```python
class TelegramNotifier:
    def send(self, text: str) -> None:      # NotifyError przy niepowodzeniu
```

### Żądanie

```
POST https://api.telegram.org/bot<TOKEN>/sendMessage
{"chat_id": "...", "text": "...", "disable_web_page_preview": true}
```

- **`parse_mode` nieustawione** — decyzja „plain text”. Bez `parse_mode` Telegram
  nie interpretuje `_`, `*`, `` ` `` w nazwach wydarzeń, a mamy „SONDERFAHRT”,
  myślniki i spacje w nazwach.
- `disable_web_page_preview: true`, bo link do sklepu inaczej generuje podgląd.
- Timeout 30 s + **jedna** ponowna próba z krótkim backoffem.

### Formatowanie

Czyste funkcje w `messages.py`, bez I/O — dzięki temu testy nie potrzebują sieci.

**Alert o dostępności** — zawiera:
- nazwę serii,
- liczbę dostępnych terminów z ogólnej liczby,
- listę: data i godzina (offset), nazwa, zakres cen, link do zakupu,
- stopkę z czasem sprawdzenia w UTC.

**Ostrzeżenie o awarii** — zawiera:
- informację, że **nie udało się sprawdzić** dostępności (nie „brak biletów” —
  to rozróżnienie ma znaczenie operacyjne),
- przyczynę skróconą do ~300 znaków,
- informację, że powtórzy się za `cooldown` godzin.

### Formatowanie daty i cen

Daty `2026-10-09T19:00:00+02:00` → `piątek, 09.10.2026, 19:00 (UTC+02:00)`.
Krótkie nazwy dni po polsku **liczone ręcznie**, nie przez `strftime` —
`%a` zależy od lokalizacji procesu i na runnerze zwróci angielskie skróty.

Ceny: `40–58 EUR`; przy `low == high` tylko jedna liczba; przy braku → `cena niedostępna`.

### Pułapki

| Pułapka | Skutek | Mitygacja |
|---|---|---|
| Limit 4096 znaków | `400 Bad Request: message is too long` | dzielenie po ~3500 znaków |
| `parse_mode` ustawione | `400 can't parse entities` przy `_` w nazwie | plain text |
| Brak `disable_web_page_preview` | zaśmiecenie podglądem | ustawiane zawsze |
| `BOT_TOKEN` w logu | wyciek sekretu w logach runa | nigdy nie logować nagłówków ani odpowiedzi w całości |
| Ponowienie bez limitu | lawina przy awarii API | maks. 1 ponowienie |
| Znak `%` / `(...)` w `chat_id` | błędne URL | walidacja formatu w §2 |

### Kryteria akceptacji

- [ ] Snapshoty PL dla obu komunikatów (dostępność i awaria).
- [ ] Test na atrapach: poprawne URL, `chat_id`, `text`, `disable_web_page_preview`.
- [ ] Test: brak `parse_mode` w żądaniu.
- [ ] Test: `400`/`429`/`500` od API → `NotifyError`, nie wyjątek surowy.
- [ ] Test podziału: 200 terminów → tyle wiadomości, ile potrzeba.

---

## 6. Stan i cache

### Kontrakt

```python
class StateStore:
    def load(self) -> dict: ...
    def save(self, data: dict) -> None: ...
```

Przechowywany kształt:

```json
{
  "schema_version": 1,
  "cookies": [{"domain": "...", "path": "/", "name": "...", "value": "..."}],
  "last_check_ok": true,
  "last_check_at": "2026-10-04T12:17:33+00:00",
  "last_error_notified_at": null,
  "last_availability_state": "none_available"
}
```

### Decyzje

**Adapter backendu.** `StateStore` nie wie, gdzie są dane. Dziś: `actions/cache`
(katalog wskazany przez `GITHUB_WORKSPACE`/ENV). Rozdzielenie pozwala dołożyć
inny backend bez ruszania logiki.

**Wersjonowanie schematu.** Nieznana `schema_version` → ignorujemy cache,
a cookies odtwarzamy tylko gdy `version == 1`. Zmiana nagłówków albo struktury
Queue-it unieważnia cookies, a brak wersji oznaczałby ciche zapętlenie w nieskończoność.

**Cache jest optymalizacją, nie wymaganiem.** Poprawność nie może zależeć od cache:
przy zniknięciu cache skrypt ma działać (5 hopów zamiast 1).

**Kolejność zapisu.** Cookies zapisujemy **zawsze** po udanym fetchu, również
gdy wszystko jest wyprzedane. Martwy Telegram nie może powodować utraty cookies.

**Bez sekretów w cache.** Wyjątek: `chat_id` nie jest sekretem, ale token **tak** —
token nigdy nie trafia do cache ani do repo.

### Pułapki

| Pułapka | Skutek | Mitygacja |
|---|---|---|
| Cache trafił do repo | wyciek cookies | `.gitignore`, zapis poza drzewem git, brak `actions/upload-artifact` |
| `actions/cache` wywiektowany po 7 dniach | powrót do 5 hopów | poprawna degradacja |
| `save()` nieistniejące (brak cache) | wyjątek | `save()` nie rzuca — loguje i idzie dalej |
| Brak `schema_version` | crash po zmianie formatu | walidacja przy `load()` |
| Równoległe uruchomienia | nadpisanie cache | `concurrency` w workflow (Faza 7) |

### Kryteria akceptacji

- [ ] `save()` → `load()` odtwarza identyczny stan.
- [ ] Brak pliku cache → `load()` zwraca stan domyślny, bez wyjątku.
- [ ] Uszkodzony JSON → stan domyślny, bez wyjątku.
- [ ] Nieznana `schema_version` → cookies pominięte.
- [ ] `save()` przy niedostępnym katalogu → brak wyjątku.

---

## 7. Orkiestracja, obsługa awarii, cooldown

### Sekwencja `main.py`

```
1.  Config.from_env()                  ConfigError -> komunikat + exit 1
2.  state = store.load(); client.load_cookies(state["cookies"])
3.  try:  html = client.fetch_html(config.target_url)
    except (FetchError, BlockedByIpError) as e:
4.      store.save(błąd, cookies=bez zmian)      # cookies nie kasujemy
5.      czy minęło >= cooldown_since(state)?  tak -> Telegram ostrzeżenie + zapis ts
                                                  nie -> wycisz
6.      exit 1
7.  series = parse_series(html)         ParseError -> traktowane jak błąd fetchu (6)
8.  store.save(cookies, last_check_ok=True)     # PRZED wysyłką
9.  if series.any_available:  Telegram(format_available(...))
10. else:                     nic
11. exit 0
```

### Kody wyjścia

| Kod | Znaczenie | Dla kogo |
|---|---|---|
| `0` | Sprawdzono, biletów brak | normalne, „zielony” run |
| `0` | Sprawdzono, bilety są, wysłano alert | normalne |
| `1` | Nie udało się sprawdzić | awaria — widoczna w UI |

Rozróżnienie „nie ma biletów” vs „nie wiem” jest celowe: `0` przy braku
dostępności nie zaśmieca powiadomień, a `1` przy awarii nie udaje,
że monitoring działa.

### Cooldown

Logika porównuje `last_error_notified_at` z `last_check_ok` w stanie:

```python
should_warn = (
    state.get("last_check_ok") is not False        # nie ostrzegamy o kolejnej
    or now - parse(state["last_error_notified_at"]) >= cooldown
)
```

Dodatkowo pierwsze ostrzeżenie po awarii nie jest thumione (`last_error_notified_at is None`
→ `should_warn = True`), żeby świeży problem nie był cichy przy pierwszym uruchomieniu
po wdrożeniu.

### Degradacja przy awarce powiadomień

`TelegramNotifier.send()` rzuca `NotifyError` w `main.py`:
- przy **alertu o dostępności** → zalogować i `exit 1` (dostępność była, komunikat nie dotarł —
  to sytuacja wymagająca reakcji);
- przy **ostrzeżeniu o awarii** → złapać, zalogować, nie podnosić (pierwotny błąd
  pobrania jest ważniejszy).

### Pułapki

| Pułapka | Skutek | Mitygacja |
|---|---|---|
| Zapisanie cookies po wysyłce | martwy Telegram = utrata cookies | zapis przed wysyłką |
| Wyjątek z `send()` gubi błąd pobrania | brak diagnozy | `raise ... from e` + kolejność kroków |
| Ostrzeżenie co godzinę | 24 wiadomości/dobę o awarii | cooldown 6 h |
| Sukces przy błędzie wysyłki | fałszywie „zielony” run | `exit 1` |
| `last_check_ok` nie resetowany po awarii | wieczne wyciszenie | zapisywane w obu ścieżkach |

### Kryteria akceptacji

- [ ] Wszystkie SoldOut → brak wysyłki, `exit 0`.
- [ ] Fixture z InStock → jedna wiadomość, `exit 0`.
- [ ] Zerwany fetch → jedno ostrzeżenie, `exit 1`.
- [ ] Drugi błąd w ciągu cooldownu → **bez** wiadomości, `exit 1`.
- [ ] Po przekroczeniu cooldownu → kolejne ostrzeżenie.
- [ ] Błąd Telegram przy alercie o dostępności → `exit 1`.
- [ ] Błąd Telegram przy ostrzeżeniu o awarii → `exit 1`, pierwotny błąd w logu.

---

## 8. Workflow GitHub Actions

### Definicja

```yaml
name: Watch tickets
on:
  schedule:
    - cron: "17 * * * *"        # minuta 17 — patrz uzasadnienie
  workflow_dispatch:
permissions:
  contents: read                # minimalne uprawnienia
concurrency:
  group: ticket-watch
  cancel-in-progress: false     # nie kasujemy trwającego sprawdzenia
jobs:
  check:
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
          cache: pip
      - run: pip install -e ".[dev]"
      - run: pytest -q -m "not live"
      - run: python -m eventim_watcher
        env:
          EVENTIM_TARGET_URL: ${{ vars.EVENTIM_TARGET_URL }}
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          TELEGRAM_CHAT_ID:   ${{ secrets.TELEGRAM_CHAT_ID }}
```

### Decyzje i uzasadnienia

**`cron: "17 * * * *"`, nie `"0 * * * *"`.** Uruchomienia w szczycie minuty
koncentrują się na GitHub i bywają opóźnione o kilka–kilkanaście minut.
Minuta 17 daje ochronę przed kolejką i jednocześnie jest „na godzinie"
z punktu widzenia użytkownika. Cena: 17 min po pełnej godzinie.

**`concurrency` bez `cancel-in-progress`.** Kanapujące się sprawdzenia o nic
nie dobrego: alert o biletach i tak zostanie wysłany przez to, które skończy się
pierwsze, a drugie tylko zmarnuje limit crona.

**`timeout-minutes: 10`.** Cron liczy **minuty uruchomienia** w limicie repo —
wiszące żądanie HTTP to marnowanie budżetu. Limit 10 min to ~30× więcej niż realnie
potrzeba (zimne pobranie ~2 s).

**Sekret kontra `vars`.** `TELEGRAM_BOT_TOKEN` wyłącznie przez `secrets.*`.
`EVENTIM_TARGET_URL` nie jest sekretem, więc idzie przez `vars.*` —
i dzięki temu jest edytowalny bez rotacji sekretów.

**Testy w pipeline.** `pytest -m "not live"` przed właściwym krokiem — potwierdza,
że zmiana niczego nie zepsuła, zanim wysyłka Telegram pójdzie w świat.

**Brak `pull_request`-triggerów.** W PR-ach sekrety są niedostępne, więc test
wymagający tokenu tylko by czerwienił. Osobny `lint.yml` może działać na PR-ach.

**Job summary.** `$GITHUB_STEP_SUMMARY` z liczbą terminów i statusem —
czytelne bez otwierania logów.

### Sekrety do zdefiniowania

| Nazwa | Typ | Gdzie |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | secret | repo → Settings → Secrets |
| `TELEGRAM_CHAT_ID` | secret | repo → Settings → Secrets |
| `EVENTIM_TARGET_URL` | variable | repo → Settings → Variables |

### Pułapki

| Pułapka | Skutek | Mitygacja |
|---|---|---|
| `schedule` na publicznym repo | throttling i opóźnienia | akceptowalne, opisane w README |
| Workflow na forku | brak sekretów, run failuje | nie uruchamiać, opisać w README |
| Workflow **wyłączony** po 60 dniach nieaktywności | cichutkie wygaszenie | Faza 8: alert o ciszy |
| Limit crona w publicznym repo | ~2000 min/mies. | 720 min/mies. przy 1 min/runs — mieści się; monitorować w Faza 8 |
| Cache w `actions/cache` | wywiektowany po 7 dniach | poprawna degradacja, Faza 6 |

### Kryteria akceptacji

- [ ] `workflow_dispatch` przechodzi i generuje job summary.
- [ ] Cron `17 * * * *` pojawia się w zakładce Actions.
- [ ] Brak sekretu → `ConfigError` i `exit 1`, nie `KeyError`.
- [ ] Zablokowany fetch → `exit 1` + ostrzeżenie z cooldownem.

---

## 9. Testy

### Strategia

| Poziom | Zakres | Sieć | Kiedy |
|---|---|---|---|
| Jednostkowe | parser, formatowanie, cooldown, konfiguracja | nie | każde uruchomienie |
| Integracyjne (atrapy) | klient HTTP na pełnej sekwencji 5 hopów | nie | każde uruchomienie |
| Live | prawdziwy handshake na runnerze | **tak** | `@pytest.mark.live`, ręcznie |

Rozdzielenie jest celowe: testy jednostkowe **nie dowodzą**, że handshake działa,
a test live w domyślnym pipeline’ie to niegrzeczne obciążanie cudzego sklepu.

### Marker live

```python
LIVE = pytest.mark.skipif(
    os.getenv("EVENTIM_LIVE") != "1",
    reason="wymaga EVENTIM_LIVE=1; nie uruchamiać w domyślnym CI",
)
pytestmark = [LIVE, pytest.mark.live]
```

Podwójne zabezpieczenie (`skipif` + marker) — łatwo przez pomyłkę usunąć
jedno z nich, `pytest -m "not live"` i tak stanowi drugą linię.

### Pokrycie przypadków brzegowych dla parsera

- `availability` nieobecne → `Unknown` → **niedostępne**, `has_unknown` prawdziwe (ADR-9).
- `availability` obecne, ale nieznanej wartości (np. `PreSale`) → dostępne (ADR-5).
- `subEvent` pusta → bez wyjątku.
- `@graph` z nieznanymi typami przed `EventSeries` → znalezienie właściwego węzła.
- `availability` jako pełne URI → poprawny skrót.
- Waluta inna niż EUR → poprawny zapis.
- Nazwa wydarzenia z cudzysłowem i znakiem `<` → bez wyjątku (escaping w JSON).

### Pułapki

| Pułapka | Skutek | Mitygacja |
|---|---|---|
| Atrapa z `Location` bez auto-follow | test „przechodzi”, kod nie | rejestrować redirecty jawnie |
| `responses` nie symuluje cookies między hopami | test myli się | jawnie konfigurować `Set-Cookie` per hop |
| Test live w domyślnym CI | samoobciążanie, możliwy ban | `skipif` + `-m "not live"` |
| Test oscylujący na 5 sekundach | flaky | testy bez `sleep`, krótkie timeouty |

---

## 10. Obserwowalność

### Format logu

Sekcje z prefiksem, czytelne w składni GitHub Actions:

```
[config]    target=…  timeout=30  cooldown=6h
[cache]     wczytano 5 cookies (schema_version=1)
[fetch]     hop 1: 302 -> eventimlight.queue-it.net
[fetch]     hop 2: 200 interstitial (2378 B)
[fetch]     hop 5: 200 OK (152044 B), JSON-LD znalezione
[parse]     seria="U-Bahn-Cabriotour 2026" terminów=6 dostępnych=0
[notify]    pominięto — brak dostępnych terminów
[cache]     zapisano cookies, last_check_ok=true
[done]      exit=0
```

Poziom szczegółowości sterowany `EVENTIM_LOG_LEVEL` (domyślnie `info`).
Nie logujemy: tokenu Telegram, pełnych nagłówków żądań, całej treści HTML.

### Metryki do obserwacji w pierwszych tygodniach

| Metryka | Gdzie | Sygnał problemu |
|---|---|---|
| Liczba hopów na run | log `[fetch]` | ciągle 5 → cache przestał działać |
| Czas wykonania runa | UI Actions | > 1 min → coś się zatrzymuje |
| Wystąpienia `exit 1` | UI Actions | cokolwiek — to najważniejszy sygnał |
| Ostrzeżenia „nie udało się sprawdzić” | Telegram | blokada IP z Fazy 0 |
| Rozmiar odpowiedzi | log `[fetch]` | 16 000 B zamiast ~152 000 → strona marketingowa |

Ten ostatni wiersz to praktyczny detektor blokady IP — zanim ktokolwiek zauważy
brak alertów.

---

## 11. Ryzyka i mitigacje

| # | Ryzyko | Prawdopod. | Wpływ | Mitygacja | Właściciel |
|---|---|---|---|---|---|
| R1 | **Runner GitHub Actions zablokowany (IP centrum danych)** | wysokie | krytyczny | Faza 0 jako bramka; `BlockedByIpError`; ostrzeżenie z cooldownem; rozmiar 16 KB jako detektor; §11.1 | właściciel |
| R2 | Zmiana markupu / struktury JSON-LD | średnie | średni | parser z fallbackami + testy na prawdziwych fixture'ach; `ParseError` zamiast cichego `False` | właściciel |
| R3 | Zmiana handshake Queue-it | średnie | wysoki | test live wykrywający przed wdrożeniem; `FetchError` z liczbą hopów | właściciel |
| R4 | Telegram niedostępny przy alertach | niskie | wysoki | 1 ponowienie + `exit 1`, żeby widać było w UI Actions | właściciel |
| R5 | Wyczerpanie limitu crona | niskie | średni | 720 min/mies. vs ~2000 limit — zapas; skrypt < 1 min | właściciel |
| R6 | Workflow wyłączony po 60 dniach bez zmian | średnie | wysoki | Faza 8: osobny codzienny cron „watch w ogóle działa?” | właściciel |
| R7 | Wyciek sekretów w logach | niskie | krytyczny | nigdy nie logować nagłówków ani tokenów; `.gitignore` dla cache | właściciel |
| R8 | Powiadomienia co godzinę (24/dobę) zaakceptowane jako spam | pewne | niski | decyzja świadoma; Faza 8 oferuje limit powtórzeń | właściciel |
| R9 | Fałszywy alert na `PreOrder` przed sprzedażą | niskie | niski | świadoma decyzja „wszystko poza SoldOut”; łatwe do zawężenia w `UNAVAILABLE`/`AVAILABLE_STATES` | właściciel |
| R10 | Zmiana eventu → URL przestaje istnieć | niskie | średni | `EVENTIM_TARGET_URL` jako `vars` — zmiana bez rotacji sekretów | właściciel |

### 11.1 R1 — plan awaryjny dla blokady IP

Ustalona decyzja: **tylko GitHub Actions hosted**, bez runnera własnego.
Jeśli Faza 0 da wynik negatywny, kolejność rozważania:

1. **Ponowna weryfikacja po 24–48 h** — odrzucamy, że to anomalia sieciowa.
2. **Wariant UA** — zmiana nagłówków i User-Agent (testowaliśmy Chrome UA → 403,
   ale warto sprawdzić inne warianty, zanim odrzucimy).
3. **Pełny nagłówek przeglądarki razem z własnym UA** — mało prawdopodobne,
   bo Akamai odrzuca podszywanie.
4. **Runner spoza puli Azure** — inny dostawca chmurowy albo kontener.
   Nadal „GitHub Actions”, zmienia się tylko sam runner.
5. **Wyjątek: runner na własnym sprzęcie.** Sprzeczne z decyzją, więc wymaga
   ponownej zgody właściciela — dlatego wpisujemy to tu, a nie jako domyślną ścieżkę.

Do czasu rozstrzygnięcia monitoring **nie działa poprawnie**. Dlatego `main.py`
musi eksponować to przez `exit 1` + ostrzeżenie Telegram, a nie przez ciszę.

---

## 12. Otwarte pytania

| # | Pytanie | Wpływ |
|---|---|---|
| P1 | Czy faza 0 da wynik pozytywny na `ubuntu-latest`? | decyduje o całym planie |
| P2 | Czy `chale`/`InStock` pojawi się w JSON-LD w formie oczekiwanej przez czarną listę? | wiarygodność alertów |
| P3 | Czy queue-it zdąży zablokować częstsze sprawdzanie niż 1/h? | częstotliwość cronu |
| P4 | Czy cache `actions/cache` to właściwe miejsce na cookies, czy lepiej bez cache? | 5 hopów vs 1 hop |
| P5 | Czy utrzymywać alert co godzinę w nieskończoność przy długim okresie dostępności? | Faza 8, R8 |

P1 jest jedynym pytaniem blokującym. P2–P5 mają bezpieczne wartości domyślne
i są świadomie odłożone.