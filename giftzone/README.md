# 🎁 MONSTAL — Telegram Shop

Python 3.12+, aiogram 3, SQLAlchemy 2, Alembic, SQLite/asyncpg. MONSTAL obsługuje
**ręczną ewidencję rzeczywistych wpłat i saldo w PLN**. Przelewy realizowane są
poza botem, a administrator osobiście sprawdza wpływ i zatwierdza księgowanie.
Bot nie pobiera środków z banku ani nie przechowuje kluczy portfela.
Kwoty kryptowalut za wybraną kwotę PLN oblicza automatycznie z kursów CoinGecko.

Tryb DEMO jest oddzielony od realnych sald. W trybie rzeczywistym panel i katalog
obsługują zamówienia fizycznych produktów kupowane z ręcznie uznanego salda PLN.
Baza otrzymuje dwa nieaktywne produkty przykładowe — przed sprzedażą usuń je lub
uzupełnij prawdziwym towarem, ceną, regionem i dostępnością, a następnie aktywuj.
Po zakupie bot zbiera imię i nazwisko, telefon, e-mail i kod paczkomatu InPost (dostawa wyłącznie do paczkomatów); szyfruje te dane w bazie.
Realizacja i potwierdzenie wysyłki są ręczne, z deklarowanym terminem do 7 dni.
Nie ma jeszcze integracji z dostawcą kart ani przewoźnikiem.

## Instalacja i konfiguracja

Wymagania: Python 3.12+, pip, dostęp HTTPS do Telegrama (`api.telegram.org`) i PyPI.
Opcjonalnie Docker Compose; dla PostgreSQL potrzebny osobny serwer.
Katalog `giftzone/` zachowuje dawną nazwę techniczną dla zgodności ścieżek.
Korzystaj z istniejącego checkoutu; dodatkowy worktree nie jest potrzebny.

```bash
cd giftzone
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
python -m app.generate_customer_key
```

Na Windows: `.venv\Scripts\activate`. W przygotowanym środowisku chmurowym
możesz użyć istniejącego środowiska `/workspace/bot/.venv`.

W `.env` ustaw:

```dotenv
BOT_TOKEN=
ADMIN_IDS=
DATABASE_URL=sqlite+aiosqlite:///./monstal.db
DEMO_MODE=false
MERCHANT_TELEGRAM_USERNAME=
CRYPTO_BTC_ADDRESS=
CRYPTO_ETH_ADDRESS=
CRYPTO_USDC_ADDRESS=
CRYPTO_SOL_ADDRESS=
CRYPTO_LTC_ADDRESS=
CRYPTO_XMR_ADDRESS=
REFERRAL_BONUS_GROSZE=1000
LOG_LEVEL=INFO
```

Wszystko, co musisz podać, znajduje się w **jednym pliku `.env`**. Gotowy wzór
to [.env.example](.env.example); w przygotowanym checkoutcie utworzono również
pusty, ignorowany przez Git `.env` z uprawnieniami tylko dla właściciela.
Na początku są token, Twoje liczbowe ID, nick kontaktowy i adresy odbiorcze.
Pozostałe ustawienia mają gotowe wartości. Bot nie prosi o prywatne klucze portfeli.

- `BOT_TOKEN` uzyskaj od @BotFather. Nie publikuj go ani nie wysyłaj w czacie.
- `ADMIN_IDS`: numeryczne Telegram ID oddzielone przecinkami lub lista JSON.
  Własne ID odczytasz w profilu po `/start`, następnie zrestartuj bota po zmianie.
- `MERCHANT_TELEGRAM_USERNAME`: Twój nick, z `@` lub bez. Możesz podać go później;
  bez nicku zgłoszenia BLIK są niedostępne.
- Adresy krypto to wyłącznie publiczne adresy odbiorcze. Nigdy nie podawaj klucza
  prywatnego, seed phrase ani hasła. Nieuzupełnione metody pozostają nieaktywne.
- Po zmianie konfiguracji uruchom proces ponownie. Bot nie domyśla się danych
  kontaktowych i nie generuje adresów portfeli.

## Uruchomienie i baza

```bash
alembic upgrade head
python -m app.seed
python -m app.main
```

Migracje są addytywne i zachowują dotychczasowe dane. Seed w trybie rzeczywistym
inicjalizuje identyfikator trybu bazy, bez fikcyjnego katalogu. W DEMO dodaje
idempotentnie sześć przykładowych kategorii i fikcyjne produkty.

Aplikacja uruchamia long polling oraz ustawia nazwę wyświetlaną bota na
**MONSTAL**. Username `@...` zmienia się osobno przez @BotFather; linki poleceń
korzystają z rzeczywistego username odczytanego z Telegrama. Brak tokenu daje
czytelną instrukcję, bez rozpoczynania pollingu. Na jeden token uruchamiaj jeden
proces. Zatrzymanie: `Ctrl+C`.

Transport respektuje `HTTPS_PROXY`, `HTTP_PROXY`, `NO_PROXY`, systemowe CA oraz
`SSL_CERT_FILE`, zachowując weryfikację TLS.

### Rozdzielenie prawdziwego salda i DEMO

Baza przechowuje trwały znacznik trybu. Nie można uruchomić prawdziwych wpłat na
bazie DEMO ani odwrotnie. Istniejąca baza bez znacznika, zawierająca stare dane,
jest traktowana jako DEMO. Nigdy nie przeliczamy demonstracyjnego salda na środki
pieniężne. Domyślna nowa baza to `monstal.db`; dawny `giftzone.db` pozostaje bez
zmian i można z nim pracować po ustawieniu `DEMO_MODE=true`.

Do testów demonstracyjnych ustaw oddzielny URL i `DEMO_MODE=true`. Nie usuwaj
znacznika trybu, nie edytuj sald bezpośrednio SQL i wykonuj kopie bazy przed migracją.

PostgreSQL: URL `postgresql+asyncpg://USER:PASSWORD@HOST:5432/DB`, następnie migracje
i seed. Zmiana URL nie przenosi danych ze SQLite; eksport/import jest oddzielnym
zadaniem. Serwer PostgreSQL i konfiguracja TLS nie są dostarczane przez projekt.

## BLIK — kontakt ze sprzedawcą

1. Klient wybiera BLIK oraz kwotę w PLN i potwierdza zgłoszenie.
2. Bot pokazuje Twój skonfigurowany nick i przycisk kontaktu. Klient kontaktuje
   się z Tobą, aby ustalić szczegóły i czas wpłaty.
3. Bot nie zbiera i nie zapisuje kodów BLIK. Rozliczenie odbywa się poza botem.
4. Klient może oznaczyć zgłoszenie jako wysłane — to **nie zwiększa salda**.
5. W `/admin` sprawdzasz wpływ, wpisujesz referencję potwierdzenia z własnej
   ewidencji/rozliczenia (nie sześciocyfrowy kod BLIK) i potwierdzasz księgowanie.
   Zapisana kwota w PLN trafia na saldo dokładnie raz.

Możesz też ręcznie przyznać lub skorygować saldo użytkownika w sekcji „Salda”.
Każda korekta wymaga potwierdzenia i trafia do historii oraz audytu. Przy
rozliczaniu zgłoszeń używaj zatwierdzenia zgłoszenia zamiast dodatkowej korekty,
aby nie zaksięgować tej samej wpłaty dwiema różnymi operacjami.

## Kryptowaluty — sześć opcji

| Waluta | Jedyna obsługiwana sieć | Zmienna adresu |
| --- | --- | --- |
| Bitcoin (BTC) | Bitcoin mainnet | `CRYPTO_BTC_ADDRESS` |
| Ethereum (ETH) | Ethereum mainnet | `CRYPTO_ETH_ADDRESS` |
| USD Coin (USDC) | Ethereum mainnet / ERC-20 | `CRYPTO_USDC_ADDRESS` |
| Solana (SOL) | Solana mainnet | `CRYPTO_SOL_ADDRESS` |
| Litecoin (LTC) | Litecoin mainnet | `CRYPTO_LTC_ADDRESS` |
| Monero (XMR) | Monero mainnet | `CRYPTO_XMR_ADDRESS` |

USDC na Solanie, Base, Polygonie i innych sieciach nie jest obsługiwane w tej
konfiguracji. Przed aktywacją osobiście sprawdź zgodność adresu i sieci oraz
kontrolę nad portfelem; bot nie weryfikuje własności adresu w blockchainie.

Przebieg:

1. Klient wybiera walutę i docelową kwotę salda w PLN. Powstaje zgłoszenie
   `awaiting_quote`. Wybór jest ograniczony do skonfigurowanych adresów.
2. Bot pobiera kurs PLN z CoinGecko i **automatycznie oblicza dokładną kwotę
   krypto**. Przykładowo, przy kursie testowym 10 000 PLN/ETH wpłata 50 zł oznacza
   0,005 ETH. To przykład matematyczny, nie aktualny kurs rynkowy.
3. Klient widzi kwotę krypto, nazwę sieci, zapisany adres i termin ważności wyceny
   (30 minut). Dopiero wtedy otrzymuje instrukcję przelewu.
4. „Wysłałem wpłatę” zmienia status na `submitted`, bez uznania salda. Wygasła
   wycena nie może zostać zgłoszona; odświeżenie statusu pobierze nową wycenę,
   o ile źródło kursów jest dostępne.
5. Sprawdzasz faktyczny wpływ do właściwej sieci i portfela, kwotę oraz wymagane
   potwierdzenia. Po wpisaniu unikalnej referencji transakcji i potwierdzeniu
   bot księguje wcześniej wybraną kwotę PLN jeden raz.

Aktywnej wyceny nie można zmienić przed jej wygaśnięciem. Stary przycisk zgłoszenia
przelewu nie zatwierdzi nowszej, innej wyceny. Gdy środki wysłano po terminie,
klient powinien skontaktować się z właścicielem w celu ręcznego rozliczenia.
Jako referencję krypto podaje się hash transakcji: 64 znaki szesnastkowe dla
BTC/ETH/USDC/LTC/XMR (ETH i USDC mogą mieć prefiks `0x`), a dla SOL podpis Base58.
Sprawdzenie formatu hasha nie jest weryfikacją otrzymania pieniędzy.

### Automatyczne kursy — ustawienia w tym samym `.env`

```dotenv
CRYPTO_AUTO_RATES=true
RATE_PRICE_MODE=current
RATE_REFRESH_SECONDS=300
RATE_MAX_AGE_SECONDS=86400
COINGECKO_API_KEY=
```

Domyślnie kurs jest odświeżany **co 5 minut**, także gdy bot pracuje bez nowych
zamówień. `RATE_REFRESH_SECONDS=86400` oznacza aktualizację raz dziennie.
Tryb `current` używa kursu rynkowego w chwili aktualizacji; `low_24h` wybiera
raportowane przez CoinGecko minimum z ostatnich 24 godzin. Niższy kurs PLN za ETH
oznacza więcej ETH do przelania za te same 50 zł. Minimum nie gwarantuje ceny
wykonania transakcji na giełdzie. USDC również ma pobierany kurs PLN.

Kwota jest liczona przez `Decimal` i zaokrąglana w górę do precyzji danej waluty,
bez dodatkowej marży sklepu. Opłaty sieciowe pokrywa wpłacający. Na ekranie są
źródło, kurs i czas aktualizacji. Wycena jest zamrożona maksymalnie na 30 minut,
a jej ważność nie przekracza ważności danych kursowych.

Kursy oraz dowody wyceny są trwałe w bazie. Przy awarii API używany jest wyłącznie
cache mieszczący się w `RATE_MAX_AGE_SECONDS` (domyślnie 24 godziny, można skrócić).
Po przekroczeniu tego czasu bot nie pokazuje instrukcji nowej wpłaty. Nie ma
wymyślonego ani wklejonego na stałe kursu. Zgłoszenie czeka na odzyskanie dostępu
do API; klient może ponowić sprawdzenie statusu. Kurs aktywnej wpłaty nie zmienia
się po odświeżeniu cache. Ręczne nadpisanie wyceny przez panel jest wyłączone,
gdy `CRYPTO_AUTO_RATES=true`; tryb ręczny pozostaje opcją po ustawieniu `false`.

CoinGecko może wymagać klucza Demo API albo ograniczyć liczbę zapytań. Opcjonalny
klucz wpisz do `COINGECKO_API_KEY` w tym samym `.env`; nigdy nie trafia do logów.
W chmurze z ograniczonym Internetem dopuść `api.coingecko.com` obok
`api.telegram.org`. Aktualne środowisko odrzuciło połączenie do CoinGecko kodem
403 na proxy; domenę dodano do szkicu konfiguracji, ale pobieranie na żywo nie
zostało jeszcze zweryfikowane po zastosowaniu ustawień. Testy korzystają z
kontrolowanych odpowiedzi API, bez rzeczywistych przelewów.

Stan zgłoszenia, adres z chwili jego utworzenia, wycena i referencja pozostają
w bazie po restarcie. Zmiana adresu w `.env` dotyczy nowych zgłoszeń. Podwójne
kliknięcie i ponowne zatwierdzenie nie dodają środków drugi raz. Wspólna
transakcja, którą chcesz rozliczyć zbiorczo, nie może zasilać kilku zgłoszeń pod
tą samą referencją. Operator musi także uwzględniać wpłaty dodane przez korekty.

## Funkcje panelu i DEMO

`/admin` sprawdza liczbowe `ADMIN_IDS`, również dla callbacków. Dostępne są
wyszukiwanie użytkowników, blokowanie kont, historia, korekty salda, zgłoszenia,
wyceny krypto, zatwierdzanie/odrzucanie wpłat, statystyki, edycja pomocy i
potwierdzane powiadomienia do użytkowników. Kwoty PLN są liczbami całkowitymi
w groszach; ilości krypto to dokładne dziesiętne ciągi, bez `float`.

Panel jest dostępny wyłącznie dla ludzkiego konta o ID z `ADMIN_IDS`, w prywatnej
rozmowie z botem. Username nie przyznaje uprawnień. Ochrona obejmuje komendę,
formularze FSM i każdy callback; grupy, wiadomości przekazane, boty i konteksty
pośrednie są odrzucane. Potwierdzenia formularzy (w tym uznania prawdziwych wpłat
i korekt salda) należą do konkretnego administratora, są jednorazowe i wygasają
po 10 minutach. Warstwa usług ponownie sprawdza ID,
a operacje finansowe mają audyt i idempotencję. Lista administratorów nie jest
edytowalna przez menu bota — zmieniasz ją tylko w `.env` i restartujesz proces.

DEMO zachowuje katalog, fikcyjne zakupy/kody, atomowe rezerwacje, zwroty oraz
polecenia z bonusem 10 zł DEMO po pierwszym ukończonym zakupie. Bonus jest
niewydawalny, a zwrot kwalifikującego zamówienia cofa go raz i trwale zamyka
polecenie. W trybie rzeczywistym nie naliczamy bonusów poleceń.

## Docker

```bash
docker compose up --build -d
docker compose logs -f bot
docker compose down
```

Compose używa `/data/monstal.db` w trwałym wolumenie `giftzone_data` — nazwa
wolumenu jest zachowana, aby nie usuwać wcześniejszych danych. Stary
`/data/giftzone.db` nie jest zamieniany w prawdziwe saldo. Dla innej bazy ustaw
`DATABASE_URL_DOCKER`; `DATABASE_URL` w `.env` jest przeznaczony do uruchomienia
lokalnego. Kontener startuje jako użytkownik bez uprawnień roota i wykonuje
migracje, seed oraz polling. Procesy nie są częścią trwałego wolumenu.

`.env` i bazy nie trafiają do obrazu. Nie publikuj `docker compose config` z
sekretami. `docker compose down -v` usuwa wolumen z bazami. W środowisku z
własnym CA Dockerfile obsługuje opcjonalny sekret BuildKit `proxy_ca` zawierający
zaufany pakiet CA; TLS pozostaje włączony.

## Hosting 24/7

Darmowy, anonimowy hosting bez usypiania i z gwarancją braku przerw nie jest
realistycznie dostępny. Darmowe plany Render usypiają usługi, a Oracle Cloud
Always Free może wymagać weryfikacji konta, mieć ograniczoną dostępność zasobów
i nie daje gwarancji SLA. Przy prawdziwych saldach i danych dostawy wybierz
mały płatny VPS z trwałym dyskiem; long polling potrzebuje tylko połączeń
wychodzących HTTPS do Telegrama, bez domeny ani otwartych portów HTTP.

Przed publiczną sprzedażą skonfiguruj prawdziwe produkty, regulamin, politykę
prywatności, obsługę zwrotów i bezpieczne przechowywanie danych zamówień.
Uruchamiaj jedną instancję pollingową z trwałym wolumenem bazy.

## Testy i struktura

```bash
python -m pytest -q
```

Testy używają tymczasowych baz, lokalnego transportu Telegrama i fikcyjnych danych
adresowych. Nie wykonują prawdziwych płatności ani żądań blockchain. Sprawdzają
DEMO oraz rzeczywisty tryb ręczny, rozdzielenie trybów, dokładność kwot,
idempotencję i uprawnienia. Połączenie z prawdziwym botem wymaga własnego tokenu.

```text
giftzone/  # techniczna ścieżka projektu MONSTAL
├── app/
│   ├── main.py, config.py, seed.py
│   ├── database/, models/, repositories/
│   ├── services/, handlers/, keyboards/
│   └── middlewares/, utils/
├── assets/                    # opcjonalny start_banner.png
├── migrations/, tests/
├── .env.example, .gitignore, alembic.ini
├── requirements.txt, pyproject.toml
├── Dockerfile, docker-compose.yml
└── README.md
```

## Ograniczenia

Bot prowadzi ewidencję; nie potwierdza samodzielnie wpływu z banku ani blockchaina,
nie wykonuje wypłat i nie przechowuje aktywnych kart. Automatyczne
księgowanie płatności, podpisane webhooki i wydawanie prawdziwych produktów
pozostają niewdrożone. `PaymentProvider` jest granicą przyszłej integracji.

Formularze admina są w pamięci — po restarcie otwórz je ponownie; zatwierdzone
operacje są trwałe. Powiadomienia są dostarczane w miarę dostępności Telegrama,
bez trwałej kolejki ponowień. Statusy i saldo w bazie są źródłem prawdy.

Przed użyciem z wpłatami zapewnij kopie zapasowe, kontrolę dostępu administratorów,
procedury rozliczeń, ochronę danych i zgodność operacyjną. Implementacja ręczna
nie zastępuje potwierdzenia wpływu ani ewidencji księgowej sprzedawcy. Telegram
ustala kolory przycisków; styl MONSTAL wykorzystuje HTML, emoji i opcjonalną grafikę.
