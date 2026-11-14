# Local Message Routing Agent

Lokalny formularz do podglądu wiadomości i ręcznego zatwierdzania działu. Model w
Ollamie może podpowiedzieć adresata, ale nie ma dostępu do wysyłki. Poczta trafia
wyłącznie do Mailpit, bez zewnętrznego SMTP.

## Uruchomienie

```sh
cp .env.example .env
docker compose up -d --build
```

Otwórz http://127.0.0.1:8000. Mailpit jest pod http://127.0.0.1:8025.
API, Ollama i Mailpit publikują porty wyłącznie na loopback. GPU jest opcjonalne:

```sh
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d --build
```

Przygotowanie modelu przez `ollama-init` może potrwać kilka minut. API nie czeka
na zakończenie pobierania: formularz i ręczna akceptacja działają bez modelu.
`GET /health` sprawdza sam backend. `GET /ready` osobno raportuje dostępność
Ollamy, modelu i Mailpit; status 503 nie blokuje formularza.

## Korzystanie

1. Wpisz adres nadawcy i treść, następnie utwórz podgląd. To niczego nie wysyła.
2. Sprawdź zapisany oryginał. Opcjonalnie poproś o propozycję AI z cytatami.
3. Sam wybierz dział, zaznacz potwierdzenie i kliknij akceptację wysyłki do Mailpit.
4. Sprawdź stan i Message-ID. W Mailpit znajdziesz To, Reply-To i dokładną treść.

Żaden dział nie jest wybrany domyślnie. Zmiana treści wymaga nowego podglądu.
Model może się mylić, także wskazując adres z dozwolonej listy. Wysoki confidence
nie jest miarą poprawności. Akceptacja dotyczy zapisanego oryginału i wybranego
działu; propozycja nie zmienia ich po zatwierdzeniu.

Ekstrakcja przyjmuje najwyżej osiem unikalnych cytatów. Backend znajduje ich
dosłowne, rozłączne wystąpienia, także przy powtarzających się frazach, i układa je
w kolejności źródła. Brak poprawnego przypisania daje brak propozycji, bez SMTP.
Ten limit ogranicza koszt walidacji; nie stanowi dowodu trafności wyboru modelu.

Dozwolone działy: `human-resources@example.com`, `kadry@example.com`,
`help-desk@example.com`, `it@example.com`, `other@example.com`.
`Reply-To` pochodzi wyłącznie ze zwalidowanego oryginalnego adresu nadawcy.
Model i żądanie approve nie mogą zmienić treści ani nagłówków wysyłanej wiadomości.

## Trwałość i niepewna wysyłka

Wolumen `review-data` przechowuje SQLite w `/data/review.sqlite3`, również sesje,
oryginały, zgody i próby. Przeglądarka zapisuje w sessionStorage identyfikatory
i rodzaj nierozstrzygniętego żądania, bez treści wiadomości i danych sesji.
Odświeżenie tej samej karty odzyskuje zapisany podgląd. Sesja trwa 8 godzin,
podgląd można zatwierdzić przez 30 minut. Dane nie są automatycznie kasowane.

- `pending_review`: brak zgody, brak wysyłki.
- `sending`: trwała zgoda i rozpoczęta próba; bez równoległego retry.
- `sent`: Mailpit potwierdził przyjęcie DATA.
- `failed_before_submit`: udowodniony błąd przed DATA; można ręcznie ponowić tę samą zgodę.
- `delivery_unknown`: możliwe przyjęcie DATA bez potwierdzenia lub przerwany proces.
  Sprawdź Mailpit po Message-ID. Przycisk retry jest zablokowany.
- `rejected` / `expired`: nie można wysłać tego podglądu.

Podwójne kliknięcie lub replay approve nie rozpoczyna drugiej próby. Nie ma
atomowej transakcji SQLite + SMTP ani gwarancji exactly-once. Po przerwaniu
procesu stan sending przechodzi konserwatywnie do delivery_unknown. Blokada
procesu dopuszcza tylko jeden backend dla danej bazy; nie uruchamiaj wielu workerów.

Retry wymaga `expected_attempt_id`, czyli ID nieudanej próby widzianej przez
operatora. Ta sama próba może mieć tylko jednego następcę. Powtórzenie starego
żądania nie uruchomi kolejnej wysyłki, także po szybkim błędzie przed DATA i
restarcie. Kolejny ręczny retry wskazuje ID nowej nieudanej próby.

Przed wysyłką formularz utrwala znacznik żądania. Utrata odpowiedzi lub przejściowy
błąd odczytu zachowuje kontekst także po odświeżeniu. Stary stan bez nowej próby
nie dowodzi, że opóźnione żądanie nie zostanie wykonane. Operator może wycofać
niewysłaną operację; atomowe odrzucenie blokuje późną zgodę lub retry.
Brak dostępu po wygaśnięciu sesji
nie oznacza braku wysyłki. Formularz wymaga osobnej świadomej decyzji, zanim pozwoli
odłożyć taką sprawę i rozpocząć nowe zgłoszenie.

Zatrzymuj przez `docker compose down`. Nie używaj `down -v`, jeśli chcesz zachować
sesje, zgody i modele. Backup wykonuj przy zatrzymanym API i zachowaj cały wolumen.
Nie dodawaj oryginalnych wiadomości ani bazy do Git.

## API

Pełny kontrakt jest w `/api/v1/docs`. Formularz korzysta z `/api/v1/review`:

| Metoda i ścieżka | Działanie |
| --- | --- |
| GET `/session` | Cookie sesji, token CSRF i lista działów |
| POST `/previews` | Niezmienny podgląd, bez modelu i SMTP |
| GET `/previews/{id}` | Stan należący do bieżącej sesji |
| POST `/previews/{id}/suggest` | Opcjonalna propozycja, bez SMTP |
| POST `/previews/{id}/approve` | Jawna akceptacja wersji, digestu i działu |
| POST `/previews/{id}/retry` | Tylko udowodniony błąd przed DATA |
| POST `/previews/{id}/reject` | Odrzucenie przed wysyłką |

Mutacje wymagają Cookie, dokładnego Host/Origin i X-CSRF-Token. Odczyt cudzej
sesji jest zabroniony. Stare `/api/v1/messages/route` i `/route-local-eval`
zwracają 409 przed modelem i SMTP; nie ma przełącznika przywracającego automat.
Nowe pola treści/nadawcy w approve są odrzucane, a wersja/digest są sprawdzane
na serwerze. Sesja to granica lokalnego operatora, nie dowód fizycznej obecności
człowieka. Zaufany lokalny program też może używać API.

## Konfiguracja

Nie zmieniaj SMTP_HOST=mailpit ani SMTP_PORT=1025. Nadawca odrzuca inne wartości.
Domyślny model to `qwen2.5:3b`; model 7B także nie zapewnia poprawnych decyzji.
Zmiana modelu nie zmienia wymogu akceptacji. Nie dodano zależności produkcyjnych.

Jeśli zmieniasz port UI, ustaw też `REVIEW_ALLOWED_ORIGINS` (tablica JSON pełnych
lokalnych originów) i `REVIEW_ORIGIN` dla diagnostyki Compose. Przykład portu 18000:

```dotenv
REVIEW_ALLOWED_ORIGINS=["http://localhost:18000","http://127.0.0.1:18000","http://localhost:8000"]
REVIEW_ORIGIN=http://127.0.0.1:18000
```

Wpis localhost:8000 zachowuje wewnętrzny healthcheck kontenera.
Mapowanie portu zmień osobnym override Compose. Zmienna origin nie zmienia mapowania.
Nie stosuj wildcardów, obcego hosta ani publicznego reverse proxy. Szczegóły granic
zaufania opisuje [docs/security.md](docs/security.md).

## Testy i diagnostyka

```sh
pip install --require-hashes -r requirements-dev.lock
ruff check .
ruff format --check .
mypy
pytest
node --test tests/review_ui.test.cjs
bandit -r app scripts
pip-audit -r requirements.lock -r requirements-dev.lock
docker compose config -q
```

Node jest potrzebny wyłącznie do testów zachowania rzeczywistego JavaScriptu
formularza. Testy korzystają z biblioteki standardowej, bez pakietów npm.

E2E używa danych syntetycznych i sam pełni rolę testowego operatora. Sprawdza brak
maila przed zgodą, dokładne To/Reply-To/Subject, całą zatwierdzoną treść, poprawny
Message-ID i jedną kopię po replay.
Nie jest dowodem, że zatwierdzał człowiek, ani pomiarem jakości modelu.

```sh
docker compose run --rm tools scripts/e2e_mail_check.py
# Diagnostyka propozycji: brak approve i brak SMTP.
docker compose run --rm tools scripts/routing_accuracy_check.py
docker compose run --rm tools scripts/adversarial_eval.py
```

`REQUIRE_SUGGESTION=true` dodaje do E2E sprawdzenie opcjonalnej propozycji. CI
korzysta z deterministycznej atrapy Ollamy i testuje tę ścieżkę. Rzeczywiste
historyczne pomiary jakości znajdują się w [docs/eval.md](docs/eval.md); nie są
wynikiem odbioru ręcznego preview. Zbiory oryginalny i holdout pozostają bez zmian.

Normalna odmowa lub błąd propozycji nie przerywa diagnostyki. Raport zapisuje
`unavailable` bez adresata i osobno liczy `clean_unavailable` oraz
`injected_unavailable`. Taki wynik nie jest trafieniem, fallbackiem do other ani
błędnym dozwolonym przełączeniem. Mianowniki nadal obejmują cały zbiór. Błąd
transportu lub konfiguracji może przerwać uruchomienie i nie jest wynikiem modelu.

## Licencja

[MIT](LICENSE).
