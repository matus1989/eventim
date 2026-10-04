# Architektura i koncepcja — Eventim Ticket Watcher

> Dokument źródłowy. Wszystkie tezy o API i zabezpieczeniach zostały **zweryfikowane empirycznie**
> (patrz „Wyniki badań”), a nie zgadnięte.

## 1. Cel

Skrypt w Pythonie, uruchamiany co godzinę przez GitHub Actions, sprawdza dostępność biletów
na stronie:

```
https://www.eventim-light.com/de/a/65488918b266253e892eaec1/s/692425419d93a5650a708f21
```

Jeżeli **dowolny termin** serii ma dostępne bilety, wysyła wiadomość na Telegram
z listą dostępnych terminów, cenami i linkiem bezpośrednio do zakupu.

Wydarzenie: **U-Bahn-Cabriotour 2026** (organizator: Berliner Verkehrsbetriebe AöR),
seria 6 terminów (9./16./17.10.2026, godz. 19:00 i 22:30), ceny 40–58 EUR.

## 2. Wyniki badań (kluczowe)

### 2.1 Nie ma anonimowego API

Zgadywanie endpointów jest bezskuteczne — istnieje szereg JSON API, ale jest **zamknięte**:

| Endpoint | Odpowiedź |
|---|---|
| `GET /de/api/availability?shopId=…` | `401` (nginx, pusta) |
| `GET /de/api/config` | `401` |
| `GET /de/api/eventseries/{id}` | `401` |
| `GET /de/api/webshop/availability` | `401` |
| `GET /de/api/shop/{id}` | `404` JSON `404-CMN-005` |
| `GET /de/api/image/{id}/…` | `200` (to tylko CDN obrazków) |

Wniosek: **nie da się odpytać dostępności czystym API**. Wymagany jest klient HTTP
z poprawnym przebiegiem ochrony (patrz 2.2).

### 2.2 Jedyna maszynowo czytelna metoda: JSON-LD

Strona jest renderowana serwerowo i zawiera `<script type="application/ld+json">`
zgodny ze `schema.org`. To jedyne miejsce, gdzie dostępność występuje w formie
zdatowanej do maszynowego odczytu.

**Strona serii (`/s/{id}`)** → `@type: EventSeries`:

```json
{
  "@type": "EventSeries",
  "name": "U-Bahn-Cabriotour 2026",
  "subEvent": [{
    "name": "U-Bahn-Cabriotour 2026",
    "startDate": "2026-10-09T19:00:00+02:00",
    "offers": {
      "@type": "AggregateOffer",
      "availability": "https://schema.org/SoldOut",
      "lowPrice": 40, "highPrice": 58, "priceCurrency": "EUR",
      "url": "https://www.eventim-light.com/de/a/…/e/692425439d93a5650a709061"
    }
  }]
}
```

**Strona terminu (`/e/{id}`)** → `@type: Event` z zagnieżdżoną listą ofert,
czyli dostępność **per typ biletu**:

```json
{
  "@type": "Event",
  "offers": {
    "@type": "AggregateOffer",
    "lowPrice": 40, "highPrice": 58, "offerCount": 2,
    "offers": [
      {"@type": "Offer", "name": "Vollzahler", "price": 58,
       "availability": "https://schema.org/SoldOut", "validFrom": "…"},
      {"@type": "Offer", "name": "Ermäßigt",  "price": 40,
       "availability": "https://schema.org/SoldOut"}
    ]
  }
}
```

W UI odpowiada temu tekst `Freie Plätze: 0` oraz przyciski typów biletów.

> **Hierarchia dostępności (ważne):** `AggregateOffer.availability` na stronie serii jest
> wartością **zagregowaną** — `SoldOut` znaczy, że *wszystkie* typy biletów są wyprzedane.
> Granulacja per typ biletu jest dostępna **wyłącznie** na stronie terminu (`/e/`).
> Alerty oparte tylko o stronę serii są zatem wystarczające do wykrycia „są bilety",
> ale nie pokażą, *który* typ biletu.

### 2.3 Ochrona: Akamai + Queue-it

Dostęp do sklepu nie jest prostym `GET`-em. Pełny przebieg to **5 hopów**:

```
hop 1  GET  https://www.eventim-light.com/de/a/{org}/s/{shop}
            → 302 → https://eventimlight.queue-it.net/?c=eventimlight&e=shopde&t={cel}&tsr=…&tsh=…
hop 2  GET  https://eventimlight.queue-it.net/?…            (serwer: Kestrel)
            → 200, strona pośrednia "cookieEnabled" / "cookietest"
hop 3  GET  /?c=…&tsr=…&tsh=…  z Cookie: cookietest=1
            → 302 → {cel}?queueittoken=e_shopde~ts_…~ce_true~rt_safetynet~h_…
            → ustawia cookies: Queue-it-token, Queue-it-visitorsession
hop 4  GET  {cel}?queueittoken=…
            → 302 (bez Location w części przypadków), ustawia cookie
              QueueITAccepted-SDFrts345E-V3_shopde
hop 5  GET  {cel}
            → 200, 152 044 B, prawdziwa strona z JSON-LD
```

Strona pośrednia z hopu 2 to **nie** challenge anty-botowy. To zwykły test
„czy przeglądarka obsługuje cookies”: JS ustawia `cookietest=1` i przeładowuje stronę.
Odwzorowanie w Pythonie jest trywialne.

### 2.4 Zachowanie wobec User-Agenta

Nie wolno podszywać się pod Chrome — ale sama „uczciwość” nie wystarczy.
Pomiar z 4.10.2026 (13 wariantów, świeża sesja i ręczny handshake za każdym razem,
40–45 s przerwy między próbami, żeby oddzielić wpływ UA od ograniczeń tempa):

| User-Agent | Odpowiedź |
|---|---|
| `eventim-watch/1.0 (+https://github.com/; …)` | `200` — pełny przebieg działa |
| `Mozilla/5.0 (+https://github.com/; bot)` | `200` — przepuszcza nawet prefiks Chrome |
| `curl/8.5.0 (+https://github.com/)` | `200` |
| `eventim-watch/2.0 (+https://github.com/; …)` | `200` — wersja produktu nieistotna |
| `eventim-watch/1.0` | `200` — sam token produktu wystarczy |
| `eventim-watch/1.0 (+https://gitlab.com/; …)` | **`403`** |
| `eventim-watch/1.0 (+https://example.com/; …)` | **`403`** |
| `eventim-watch/1.0 (+mailto:kontakt@example.com)` | **`403`** |
| `eventim-watch/1.0 (kontakt: ktoś@example.com)` | **`403`** |
| `Mozilla/5.0 … Chrome/131 …` | **`403`** |

**Wniosek: przepustka jest zawieszona na frazie `github.com` w nagłówku**, w dowolnej
postaci i niezależnie od reszty identyfikatora. To **biała lista po stronie Akamai,
nie heurystyka** — dlatego:

* `DEFAULT_USER_AGENT` wskazuje na repozytorium (`+https://github.com/matus1989/eventim/`)
  i **nie wolno go zamieniać** na adres e-mail: zmiana wygląda na poprawę
  („dodaj kontakt"), a kończy się cichym `403` przy każdym uruchomieniu.
  Adres repozytorium jest przy okazji **realnym kontaktem** dla administracji
  Eventim — to kod, do którego mogą zajrzeć, a nie obietnica mailowa;
* program **ostrzega w logu**, gdy `EVENTIM_USER_AGENT` nie zawiera `github.com`;
* test `test_domyslny_user_agent_przepuszczany_przez_akamai` pilnuje tej własności
  na poziomie kodu, więc przypadkowa zmiana wartości domyślnej wyłapie testy;
* gdy Akamai usunie wpis, monitoring padnie — to ryzyko tej samej klasy co R1/R3.

Metodologia: kolejne warianty testowane po 40–45 s, kolejność powtarzalna
(3/3 OK vs 0/3 dla UA z e-mailem), więc efekt jest deterministyczny, a nie wynikiem
chwilowego limitu tempa.

### 2.5 Reputacja adresu IP — hipoteza i jej rozstrzygnięcie

Pierwsza wersja tej sekcji nazywała to „ryzykiem numer jeden" i była **błędna**.

**Hipoteza wyjściowa** (oparta na obserwacji z 4.10.2026): adresy centrum danych są
blokowane przez Akamai, a GitHub Actions hosted runners to właśnie adresy centrum
danych (chmura Azure) — więc projekt stoi na fundamentacie, który zawodzi.

| Źródło ruchu | Wynik |
|---|---|
| IP domowe (ISP użytkownika, AS3320 Deutsche Telekom) | **działa** — 3/3 zimne uruchomienia, 5 hopów, ~1–2 s |
| IP centrum danych, zakres nieznany | **nie działa deterministycznie** — ślepy zaułek na stronie marketingowej |
| **GitHub Actions hosted runner** (AS8075 Microsoft) | **działa** — 2/2 runy, 4.10.2026: `172.185.143.245` i `172.208.126.101`, każdy 5 hopów, `SoldOut:6` |

Z nieznanego zakresu centrum danych przebieg wygląda inaczej: hop 1 zwraca `200`
(strona pośrednia) zamiast `302` do Queue-it, a hop po ustawieniu `cookietest`
kończy się na stronie promocyjnej EVENTIM.Light (~16 000 B) zamiast na sklepie.
Powtarzalne w 4/4 próbach, niezależnie od nagłówków `Sec-Fetch`.

**Rozstrzygnięcie (Faza 0, 4.10.2026):** runner GitHub Actions pobrał prawdziwą stronę
sklepu, wykonał pełne 5-hopowe handshake i zwrócił poprawny JSON-LD. Adres pochodził
z chmury Azure — dokładnie ta kategoria, której baliśmy się. Zrobione to **dwa razy
z dwoma różnymi adresami wychodzącymi** (run `37183741624` i `37184256979`, oba AS8075), co
odróżnia wynik systematyczny od przypadkowego trafienia w dobry adres.

Wniosek: **Akamai nie blokuje klasy „adres centrum danych”**, tylko konkretne,
źle oceniane zakresy. Klasa jest zbyt szeroka, żeby z niej wyciągać wniosek
o runnera. Konkretny zakres używany przez GitHub Actions nie jest zablokowany,
a zakres, na którym to zadziałało, pozostaje nieznany — i w tym projekcie nieistotny.

Dlatego:

* bramka jakościowa z Fazy 0 **przeszła** i projekt realizuje się na założeniu
  „hosted runner działa";
* plan awaryjny §11.1 technicznego planu zostaje **zarchiwizowany, nieaktywny** —
  blokada IP jest realna dla innych adresów, więc kod (`BlockedByIpError` +
  detektor strony marketingowej) zostaje jako diagnoza;
* skrypt nadal **nie może być cichy przy awarii** — musi sam zgłosić, że przestał
  sprawdzać dostępność (patrz §5). Zmienił się tylko powód, dla którego to ważne:
  nie „pewność, że zablokują", lecz „pewność, że sytuacja się zmieni".

### 2.6 Wydajność dzięki cache cookies

Token Queue-it można serializować i przechowywać:

| Scenariusz | Hopy | Czas |
|---|---|---|
| Zimna sesja (fresh `requests.Session`) | 5 | 1 025 / 1 880 / 1 139 ms |
| Ciepła sesja (cookies z poprzedniego uruchomienia) | **1** | **59–129 ms** |

Cache cookies skraca sprawdzenie z ~1,2 s do ~0,1 s i redukuje liczbę wywołań
do Queue-it (mniej ruchu, mniejsze ryzyko zablokowania).

## 3. Koncepcja rozwiązania

### 3.1 Widok komponentów

```
┌─────────────────────────────────────────────────────────────┐
│  GitHub Actions (cron co godzinę)                           │
└────────────────────────────┬────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────┐
│  eventim_watcher / main.py                                   │
│  1. wczytaj konfigurację z ENV                              │
│  2. odtwórz cookies z cache (jeśli ważne)                    │
│  3. pobierz stronę (EventimClient)                          │
│  4. wyodrębnij JSON-LD → model Availability                 │
│  5. oceń: czy jakikolwiek termin jest dostępny?             │
│  6. wyślij Telegram (jeśli tak)                             │
│  7. zapisz cookies + stan do cache                          │
└──────┬───────────────────────────────────┬──────────────────┘
       │                                   │
       ▼                                   ▼
┌──────────────────────┐         ┌──────────────────────────┐
│  EventimClient       │         │  TelegramNotifier         │
│  - kolejka 5 hopów   │         │  - sendMessage (BOT_TOKEN)│
│  - Session + cookies │         │  - PL, plain text        │
│  - wykrywanie        │         │  - retry/timeout         │
│    strony pośredniej │         └──────────────────────────┘
└──────────┬───────────┘
           ▼
┌──────────────────────┐
│  Parser JSON-LD      │
│  - EventSeries       │
│  - schema.org        │
│  - niedostępne =      │
│    {SoldOut,         │
│     Discontinued,    │
│     OutOfStock}      │
└──────────────────────┘
```

### 3.2 Maszyna stanów pobierania

Klient realizuje pętlę z jawną kontrolą przekierowań
(`allow_redirects=False`) — `requests` nie wolno pozwalać podążać samemu,
bo przekierowania są częścią protokołu, nie zwykłym przekierowaniem HTTP:

```
        ┌──────────────┐
   ┌───►│ GET target   │
   │    └──────┬───────┘
   │           │
   │     ┌─────▼──────────────┐   nie ma JSON-LD,
   │     │ czy to strona       │──► interstitial?  ──► ustaw cookietest=1,
   │     │ pośrednia?         │                     przeładuj z Location
   │     └──┬──────────────┬───┘
   │     tak │              │ nie
   │        ▼              ▼
   │  ┌───────────┐   ┌──────────────────┐
   │  │ wyślij     │   │ jest JSON-LD?    │
   │  │ cookietest │   └─┬──────────────┬─┘
   │  │ + reload   │  tak│              │ nie
   │  └─────┬─────┘     ▼              ▼
   │        │   ┌──────────────┐  ┌──────────────┐
   └────────┘   │ PARSE JSON-LD│  │ HTTP 3xx?    │
                └──────┬───────┘  └──────┬───────┘
                       │                   │ tak → urljoin(Location), hop++
                       ▼                   │
                ┌──────────────┐            │
                │ zwróć model  │            │
                └──────────────┘            │
                                             ▼
                                   błąd → wyjątek → obsługa awarii
```

Twarde limity: `max_hops = 12`, `timeout = 30 s` na żądanie. Przekroczenie → wyjątek
(Nigdy pętla nieskończona).

### 3.3 Model danych

```python
@dataclass(frozen=True)
class Term:
    name: str
    start: str                      # ISO 8601 z offsetem, np. 2026-10-09T19:00:00+02:00
    url: str                        # link bezpośrednio do zakupu
    availability: str               # "SoldOut" | "InStock" | ...
    low_price: float | None
    high_price: float | None
    currency: str | None

    @property
    def available(self) -> bool:
        if self.availability == UNKNOWN:
            return False
        return self.availability not in UNAVAILABLE
```

Dostępność interpretowana **odwrotnie** (whitelist stanów dostępnych jest mniej
odporna niż blacklista stanów niedostępnych):

```python
UNAVAILABLE = frozenset({"SoldOut", "Discontinued", "OutOfStock"})
UNKNOWN = "Unknown"        # pole availability nieobecne w danych
```

Wszystko inne (`InStock`, `LimitedAvailability`, `PreOrder`, `PreSale`, `OnlineOnly`,
`BackOrder`) **uruchamia alert** — zgodnie z decyzją „wszystko poza SoldOut”.

**Rozróżnienie dwóch przypadków, które wyglądają podobnie (ADR-9):**

| Sytuacja | `availability` | Alert o biletach | Raportowanie |
|---|---|---|---|
| Nowa wartość schema.org, np. `PreSale` | `"PreSale"` | tak | normalne |
| Pole **nieobecne** (zmiana markupu) | `"Unknown"` | **nie** | `Series.has_unknown` → ostrzeżenie |

Brak pola to **anomalia danych, nie informacja o dostępności**. Gdyby traktować go
jak dostępność (dosłowna wersja ADR-5 sprzed Fazy 3), każda zmiana markupu
Eventim zasypywałaby użytkownika fałszywymi alarmami o biletach, których nie ma.
Gdyby traktować go jako „wyprzedane”, awaria byłaby cicha. Osobny stan `UNKNOWN`
daje trzecią drogę: brak alarmu o biletach **plus** jednoznaczny sygnał diagnostyczny
z Fazy 5.

### 3.4 Decyzje projektowe (ADR-y)

| # | Decyzja | Alternatywy odrzucone | Powód |
|---|---|---|---|
| ADR-1 | Źródło danych: JSON-LD z HTML | `/de/api/*` (401), scraping tekstu UI | jedyne źródło maszynowe; tekst UI jest locale-dependent |
| ADR-2 | Własny UA zawierający `github.com` | podszywanie Chrome UA | Chrome UA → 403; **13 pomiarów: `github.com` przepuszcza, adres e-mail nie** |
| ADR-3 | Ręczne przekierowania (`allow_redirects=False`) | `requests` auto-follow | przekierowania są częścią handshake'u |
| ADR-4 | Wartości JSON-LD **wyłącznie** na stronie serii | dodatkowo `/e/` per typ biletu | 6 dodatkowych żądań na godzinę; agregat wystarczy do alertu. Rozszerzenie opisane jako opcja |
| ADR-5 | Blacklista stanów niedostępnych | whitelist | mniej fałszywych alarmów przy nowych wartościach schema.org |
| ADR-6 | Cache cookies w `actions/cache` | świeca sesja co godzinę | 5 hopów → 1 hop, ~0,1 s zamiast ~1,2 s |
| ADR-7 | Ostrzeżenie o awarii z cooldownem 6 h | milczenie / ostrzeżenie co godzinę | przy Faza 0 realnym ryzyku milczenie = cicha utrata monitoringu |
| ADR-8 | Wersjonowany schemat cache | cache „na wiarę” | zmiana User-Agentu lub struktury unieważnia cookies |
| ADR-9 | Brak pola `availability` = `UNKNOWN`, **nie** alert | traktowanie braku danych jak dostępności | zmiana markupu zasypywałaby fałszywymi alarmami; `UNKNOWN` raportowany osobno (Faza 5) |

## 4. Przepływ decyzyjny uruchomienia

```
uruchomienie
   │
   ├─► config poprawna?  ── nie ──► ERROR → koniec (exit 1)
   │
   ├─► pobierz stronę
   │      ├─► wyjątek ──► zapisz błąd do stanu
   │      │                 ├─ ostatnie ostrzeżenie < 6 h? ── tak ──► wycisz, koniec
   │      │                 └─ nie ──► Telegram: OSTRZEŻENIE, koniec (exit 1)
   │      └─► OK
   │
   ├─► zapisz cookies do cache (nawet przy SoldOut)
   │
   ├─► czy JAKIKOLWIEK termin dostępny?
   │      ├─► nie ──► koniec (exit 0), cicho
   │      └─► tak ──► Telegram: ALERT z listą terminów + linkami
   │
   └─► exit 0
```

Zasady:
- Brak dostępności **nie** jest błędem → `exit 0` (bez alarmów w UI Actions).
- Brak możliwości sprawdzenia **jest** błędem → `exit 1` + ostrzeżenie z cooldownem.
- Powtórzenia bez limitu — przy godzinnym cronie alert o dostępności leci
  przy każdym uruchomieniu (decyzja świadoma: użytkownik woli nie przegapić).

## 5. Zakres poza MVP

Świadomie **poza** zakresem (spisane, by nie wróciły jako „dlaczego tego nie ma”):

- Automatyczny zakup / rezerwacja biletów (bot kupujący) — poza celem.
- Wiele wydarzeń — decyzja: jedno wydarzenie.
- Per-typ-biletu alerty z `/e/` — ADR-4, możliwe rozszerzenie.
- Równoległe sprawdzanie wielu stron — brak uzasadnienia przy jednym wydarzeniu.