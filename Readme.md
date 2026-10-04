# eventim — monitor dostępności biletów

Sprawdza co godzinę, czy **U-Bahn-Cabriotour 2026** na Eventim Light ma
wolne terminy, i wysyła wiadomość na Telegram, gdy którykolwiek się pojawi.

Sprawdzana seria:
<https://www.eventim-light.com/de/a/65488918b266253e892eaec1/s/692425419d93a5650a708f21>

Jedna seria, jeden kanał, zero interfejsu do obsługi. Program uruchamia się
sam na GitHub Actions i milczy, dopóki nie ma po co mówić.

---

## Jak to działa

Nie ma tu API do odpytywania — `/de/api/*` zwraca 401. Jedynym
maszynowo czytelnym źródłem jest **JSON-LD `schema.org`** osadzony w
stronie renderowanej przez serwer. Program:

1. pobiera serię przez pięciohopowy handshake Queue-it,
2. wyciąga JSON-LD i czyta `EventSeries.subEvent[].offers.availability`,
3. porównuje z poprzednim sprawdzeniem,
4. jeśli **cokolwiek innego niż `SoldOut`** — wysyła alert z linkiem zakupowym,
   **przy każdym uruchomieniu**, bez limitu powtórzeń.

Szczegóły decyzji (dlaczego nie wolno podszywać się pod Chrome, skąd wziąć
User-Agent, co to zmienia w strukturze JSON-LD) są w `docs/architecture.md`
— decyzje tam są ponumerowane i mają uzasadnienia.

---

## Wdrożenie

### 1. Ustaw sekret w repozytorium

**Settings → Secrets and variables → Actions → Secrets → New repository secret**

| Nazwa | Co wpisać |
|---|---|
| `TELEGRAM_BOT_TOKEN` | token od [@BotFather](https://t.me/BotFather), np. `1234567890:AAH...` |
| `TELEGRAM_CHAT_ID` | ID czatu, np. `-1001234567890` (supergrupa) lub `@twojkanal` |

**…oraz zmienną** (ta sama strona, zakładka **Variables**):

| Nazwa | Co wpisać |
|---|---|
| `EVENTIM_TARGET_URL` | adres serii z sekcji „Jak to działa" |

Sekret i zmienna celowo są rozdzielone. Adres nie jest tajemnicą — ma dać się
zmienić bez dotykania sekretów. Token **musi** być sekretem: `vars.*` nie jest
maskowane i GitHub wypisuje wartość w logu przy każdym uruchomieniu.
`tests/test_workflow.py` pilnuje tego regresji.

### 2. Uruchom ręcznie raz

Po zapisaniu sekretów: zakładka **Actions → Watch tickets → Run workflow**.

Ręczne uruchomienie jest tu obowiązkowe, nie wycieczka. Dopóki nie sprawdzisz
tablicy **Summary** w tym runie, nie wiesz, czy token w ogóle działa — a przy
wszystkich terminach wyprzedanych program milczy zgodnie z założeniem, więc
nie ma żadnego innego sygnału.

### 3. Zostaw na cron

`watch.yml` ma już `cron: "17 * * * *"`. Nie trzeba nic włączać.

---

## Konfiguracja

Wymagane — brak którejkolwiek to błąd konfiguracji, nie ciche pominięcie:

| Zmienna | Opis |
|---|---|
| `EVENTIM_TARGET_URL` | adres serii; domena musi być `eventim-light.com` |
| `TELEGRAM_BOT_TOKEN` | token bota |
| `TELEGRAM_CHAT_ID` | ID czatu lub `@nazwa` |

Opcjonalne — sensowne wartości domyślne:

| Zmienna | Domyślnie | Uwagi |
|---|---|---|
| `EVENTIM_USER_AGENT` | zawiera adres tego repozytorium | **Nie zmieniaj bez powodu.** Pomiar 4.10.2026: każdy User-Agent bez frazy `github.com` kończy się 403 od Akamai (ADR-2) |
| `EVENTIM_TIMEOUT` | `30` s | 1–300 |
| `EVENTIM_MAX_HOPS` | `12` | 1–50; handshake ma 5 |
| `EVENTIM_ERROR_COOLDOWN_HOURS` | `6` h | 0–168; tyle cisza po awarii pobierania |
| `TELEGRAM_MAX_RETRIES` | `1` | 0–5 |
| `EVENTIM_STATE_FILE` | `.cache/eventim-state.json` | tu trzymane są cookies sesji |
| `EVENTIM_LOG_LEVEL` | `info` | `debug` pokaże każdy hop handshake'u |

### Kody wyjścia

| Kod | Znaczenie |
|---|---|
| `0` | sprawdzenie wykonane — biletów nie ma **albo** alert poszedł |
| `1` | **nie udało się sprawdzić**: błąd pobrania, błąd parsowania, brak pola `availability`, niewysłany alert, zła konfiguracja |

`0` przy „brak dostępnych terminów" i `1` przy „nie wiem" to celowy podział.
Gdyby oba przypadki dawały `0`, cicha awaria monitoringu wyglądałaby w
zakładce Actions jak zwykły udany run.

Kod `1` przy anomalii (brak pola `availability`) jest **najbardziej
kontrowersyjnym** wyborem w tym projekcie i został świadomie podjęty —
uzasadnienie w `docs/technical-plan.md`, sekcja 7.

---

## Uruchomienie lokalne

```bash
python -m venv .venv && . .venv/Scripts/activate   # Windows
pip install -e ".[dev]"

export EVENTIM_TARGET_URL="https://www.eventim-light.com/de/a/.../s/..."
export TELEGRAM_BOT_TOKEN="1234567890:AAH..."
export TELEGRAM_CHAT_ID="-1001234567890"

python -m eventim_watcher
```

Bez sekretów program **nie zgłosi się po cichu** — wypisze wszystkie
brakujące zmienne naraz i wyjdzie z kodem `1`:

```console
$ python -m eventim_watcher
Niepoprawna konfiguracja - problemy:
  - brak wymaganych zmiennych srodowiskowych: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
Sekrety ustaw w repozytorium: Settings -> Secrets and variables -> Actions.
Zmienna EVENTIM_TARGET_URL moze byc zmienna (Settings -> Variables).
[ERROR] koniec: konfiguracja niepoprawna
```

To celowe. Poprawianie konfiguracji po jednej zmiennej na runnerze marnuje
i limit crona, i czas.

### Testy

```bash
pytest -q -m "not live"      # 433 testów, bez sieci, ~3 s
```

Testy na prawdziwym sklepie wymagają zgody w dwóch kształtach — pliku
`EVENTIM_LIVE=1` i markera `live` — żeby zwykłe CI nigdy nie obciążało cudzego
serwera:

```bash
EVENTIM_LIVE=1 pytest -q -m live
```

Nie wysyłają Telegrama. Testy live kończą się na parsowaniu: wysyłka jest
jedyną nieodwracalną operacją w tym repozytorium, więc pilnuje tego osobny
test, który sprawdza własny kod przez `ast`, a nie tekst pliku.

---

## Gdy przestanie działać

| Objaw | Przyczyna | Co zrobić |
|---|---|---|
| brak runów w zakładce Actions | GitHub wyłącza `schedule` po **60 dniach bez aktywności** w repozytorium | skomentować dowolny commit; cron wraca |
| run jest czerwony bez treści | brak sekretu | **Settings → Secrets** — patrz krok 1 |
| `Akamai odrzucił adres IP` | zmiana białej listy po stronie sklepu | `Actions → Diagnostyka pobierania strony → Run workflow`; potem `docs/technical-plan.md`, sekcja 11.1 |
| `nie znaleziono JSON-LD` | zmiana układu strony | j.w. — to jedyna rzecz, którą potrafi rozpoznać `probe.yml` |
| run startuje z 10–20 min opóźnieniem | throttling crona GitHuba | normalne; cron jest na minucie 17 właśnie dlatego |
| praca na forku | brak sekretów po stronie forku | nie uruchamiać; monitoring ma działać w oryginale |

`probe.yml` (`Diagnostyka pobierania strony`) to narzędzie diagnostyczne:
odpala się **tylko ręcznie**, nie ma sekretów i nic nie wysyła. Bez niego
awaria monitoringu wyglądałaby po prostu jak cisza na Telegramie.

---

## Dokumentacja

| Plik | Zawartość |
|---|---|
| [`docs/architecture.md`](docs/architecture.md) | decyzje projektowe (ADR) i ich uzasadnienia |
| [`docs/technical-plan.md`](docs/technical-plan.md) | plan faz, konfiguracja, sekwencje, pułapki |
| [`docs/roadmap.md`](docs/roadmap.md) | stan prac i czego świadomie **nie** zrobiono |

---

## Treść oryginalnego polecenia

> jestes programista python, potrzebujesz stworzyc skrypt ktory bedzie
> sprwdzal strone https://www.eventim-light.com/de/a/65488918b266253e892eaec1/s/692425419d93a5650a708f21
>
> use api check availibility
> use telegram to send message
> it will be run on github as workflow action (every hour)

Jedno odejście od literalnego polecenia: **nie ma API do sprawdzania.**
`/de/api/*` zwraca 401 dla zapytania anonimowego (ADR-1), więc program czyta
JSON-LD z wyrenderowanej strony. Efekt jest taki sam — godzinne sprawdzanie
i alert na Telegramie — bez udawania, że korzystamy z interfejsu, którego nie ma.
