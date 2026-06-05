# Local Message Routing Agent

Konteneryzowany prototyp wewnętrznego routera wiadomości wykorzystującego lokalny
model AI.

## Cel

Usługa przyjmuje adres e-mail nadawcy i treść wiadomości, a następnie przekazuje je
do lokalnego modelu uruchomionego w Ollamie. Model wybiera właściwy dział wewnętrzny
przez wywołanie narzędzia `send_email`. Backend waliduje to wywołanie i wysyła
utworzoną wiadomość e-mail do Mailpita.

Ścieżka podstawowa:

```text
POST request
-> MessageRoutingService
-> RoutingAgent
-> Ollama chat with tools
-> send_email tool call
-> MailSender
-> Mailpit
```

Mechanizm awaryjny (`fallback`) w backendzie służy wyłącznie jako zabezpieczenie.
Ścieżka podstawowa nie może być deterministycznym routingiem po słowach kluczowych
w kodzie API ani serwisu.

## Architektura

- `app/api`: trasy FastAPI i wstrzykiwanie zależności.
- `app/application`: serwis aplikacyjny mapujący schematy API na agenta.
- `app/agent`: prompty, schemat narzędzia i orkiestracja wywołań narzędzia.
- `app/domain`: dozwoleni odbiorcy-działy oraz tekst polityki routingu.
- `app/infrastructure`: klient HTTP Ollamy i nadawca SMTP do Mailpita.
- `app/schemas`: schematy żądania i odpowiedzi.
- `scripts`: inicjalizacja Ollamy oraz opcjonalne narzędzia diagnostyczne (E2E,
  trafność routingu).

Usługi uruchamiane przez Compose:

- `api`: usługa FastAPI na Pythonie 3.12.
- `ollama`: lokalny serwer Ollama z trwałym wolumenem na modele.
- `ollama-init`: czeka na Ollamę i pobiera skonfigurowany model przez HTTP.
- `mailpit`: lokalne przechwytywanie SMTP, interfejs WWW i API HTTP.

Zadanie wymieniało MailHog jako przykład, ale dopuszczało "dowolne inne podobne
narzędzie", więc użyłem **Mailpita zamiast MailHog**. MailHog jest od lat
nierozwijany (zarchiwizowany), a Mailpit to jego aktywnie rozwijany następca o tym
samym przeznaczeniu (przechwytywanie SMTP i interfejs WWW). Zdecydowało API HTTP
Mailpita: dzięki niemu test E2E weryfikuje dostarczenie wiadomości i nagłówki
programowo, a nie tylko wzrokowo w interfejsie.

## Uruchomienie

```bash
docker compose up -d
```

Pierwsze uruchomienie może potrwać kilka minut: Compose czeka na pobranie i
rozgrzanie modelu, zanim wystartuje API (zobacz
[Pierwsze uruchomienie](#pierwsze-uruchomienie)). To oczekiwane zachowanie, a nie
zawieszenie. Postęp można śledzić poleceniem `docker compose logs -f ollama-init`.

Wartości domyślne są wbudowane, więc projekt działa bez tworzenia `.env`. Skopiuj
`.env.example` do `.env` tylko wtedy, gdy chcesz nadpisać ustawienia takie jak
`OLLAMA_MODEL`.

Jeśli zmieniłeś zależności lub chcesz przebudować obraz:

```bash
docker compose up -d --build
```

Środowisko jest gotowe, gdy:

```bash
docker compose ps
curl http://localhost:8000/health
```

Docker Compose publikuje porty API, Mailpit i Ollamy wyłącznie lokalnie
(`127.0.0.1`), więc są dostępne z tej maszyny, ale nie wystawione do sieci LAN.

Usługi działające stale (`api`, `ollama`, `mailpit`) mają politykę
`restart: unless-stopped`, więc Docker podnosi je ponownie po awarii kontenera.
Jednorazowy `ollama-init` nie jest restartowany, ponieważ kończy pracę po
przygotowaniu modelu.

## Pierwsze uruchomienie

Pierwsze uruchomienie może potrwać kilka minut. Docker musi pobrać obraz Ollamy,
a Ollama musi pobrać wagi modelu.

Model jest inicjalizowany automatycznie przez `ollama-init`:

1. czeka na `http://ollama:11434/api/tags` (maksymalny czas oczekiwania kontroluje
   `OLLAMA_WAIT_TIMEOUT_SECONDS`, domyślnie 120 s)
2. sprawdza, czy `OLLAMA_MODEL` istnieje
3. wywołuje `/api/pull`, jeśli modelu brakuje (z ponawianiem i narastającym
   odstępem przy błędach pobierania; liczbę prób i odstęp kontrolują
   `OLLAMA_PULL_MAX_ATTEMPTS` oraz `OLLAMA_PULL_RETRY_DELAY_SECONDS`)
4. opcjonalnie rozgrzewa model niestrumieniowym żądaniem `/api/chat`
5. kończy się kodem `0` dopiero, gdy model jest gotowy

Rozgrzewka jest domyślnie włączona:

```bash
OLLAMA_WARMUP_ENABLED=true
```

Ustaw `OLLAMA_WARMUP_ENABLED=false`, aby pominąć rozgrzewkę. Rozgrzewka jedynie
wczytuje do pamięci już pobrany model, więc jej niepowodzenie lub przekroczenie
czasu nie jest krytyczne: `ollama-init` zapisuje ostrzeżenie i nadal kończy się
kodem `0`, a pierwsze żądanie wczytuje model. Dzięki temu `docker compose up -d`
nie jest blokowane przez powolną pierwszą inferencję na słabym sprzęcie. Dostosuj
`OLLAMA_WARMUP_TIMEOUT_SECONDS`, jeśli żądanie rozgrzewki `/api/chat` potrzebuje
więcej czasu; pobierania modelu nie są ograniczone tym ustawieniem.

Nie uruchamiaj ręcznego pobierania w stylu:

```bash
docker compose exec ollama ollama pull ...
```

Model domyślny:

```bash
OLLAMA_MODEL=qwen2.5:3b
```

Możesz go zmienić w `.env`.

Domyślny model `qwen2.5:3b` potrzebuje około 4 GB wolnej pamięci RAM. Bez GPU
Ollama uruchamia inferencję na CPU, co jest **znacznie wolniejsze**: pełny prompt
routingu na samym CPU potrafi trwać ponad minutę (zob. sekcję "Wybór obrazu Ollamy"
oraz `REQUEST_TIMEOUT_SECONDS`). Kolejne żądania są szybsze, gdy model jest już
wczytany do pamięci.

## Akceleracja GPU (opcjonalnie)

Bazowy `docker-compose.yml` uruchamia inferencję na CPU, więc projekt działa na
każdej maszynie. Na hoście z kartą NVIDIA i NVIDIA Container Toolkit dołączony plik
override udostępnia kontenerowi Ollamy GPU:

```bash
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d
```

W obu przypadkach używany jest ten sam model. Ollama automatycznie umieszcza
warstwy modelu na GPU, jeśli kontener ma do niego dostęp. Pomiar na RTX 3080:
opóźnienie pojedynczego żądania spadło z dziesiątek sekund na CPU do około 3 s na
GPU. Domyślna ścieżka pozostaje uniwersalna, więc maszyny bez GPU działają bez
zmian.

## Wybór obrazu Ollamy (rozmiar a uniwersalność)

Domyślny obraz `ollama/ollama` działa zarówno na CPU, jak i na GPU, ponieważ
zawiera biblioteki CUDA/ROCm. Dzięki temu zwykłe `docker compose up -d` jest
poprawne na każdej maszynie, ale obraz jest duży (~3,7 GB do pobrania, ~10,6 GB
rozpakowany).

Obraz można nadpisać zmienną `OLLAMA_IMAGE`. Na hoście CPU-only lub z wolnym łączem
można użyć znacznie lżejszego, CPU-only `alpine/ollama` (ta sama wersja Ollamy,
~30 MB do pobrania):

> **⚠️ Uwaga:** tryb CPU-only jest **znacznie wolniejszy**: pojedyncze żądanie
> routingu potrafi przekroczyć minutę. Dlatego poniżej podniesiono
> `REQUEST_TIMEOUT_SECONDS` do 300; bez tego API zwróci błąd przekroczenia czasu
> (HTTP 503). Używaj tego wariantu głównie dla oszczędności miejsca i pasma.

```bash
OLLAMA_IMAGE=alpine/ollama:latest REQUEST_TIMEOUT_SECONDS=300 docker compose up -d
```

Uwagi dla trybu CPU-only:

- `alpine/ollama` nie obsługuje GPU, więc nie łącz go z `docker-compose.gpu.yml`,
  który i tak wymusza użycie `ollama/ollama` z CUDA,
- domyślnie pozostaje `ollama/ollama`, więc bez ustawiania `OLLAMA_IMAGE` nic się
  nie zmienia.

## Endpointy

- Baza API: `http://localhost:8000`
- Health (liveness): `GET http://localhost:8000/health`
- Readiness (Ollama API + obecność modelu + SMTP): `GET http://localhost:8000/ready`
- Routing wiadomości: `POST http://localhost:8000/api/v1/messages/route`
- Swagger UI: `http://localhost:8000/api/v1/docs`
- ReDoc: `http://localhost:8000/api/v1/redoc`
- OpenAPI JSON: `http://localhost:8000/api/v1/openapi.json`
- Mailpit UI: `http://localhost:8025`
- API Ollamy: `http://localhost:11434`

## Przykłady cURL

Health:

```bash
curl http://localhost:8000/health
```

Routing wiadomości:

```bash
curl -X POST http://localhost:8000/api/v1/messages/route \
  -H "Content-Type: application/json" \
  -d '{"email":"jan.nowak@example.com","message":"Nie działa mi komputer"}'
```

## Automatyczny test E2E

Opcjonalny automatyczny test E2E zakłada, że środowisko Docker Compose jest już
uruchomione:

```bash
docker compose run --rm tools scripts/e2e_mail_check.py
```

Dzięki profilowi `tools` weryfikacja nie wymaga Pythona na hoście: usługa ponownie
wykorzystuje obraz API i łączy się z pozostałymi usługami przez sieć Compose. Jeśli
masz Pythona 3 na hoście, możesz uruchomić skrypt bezpośrednio z katalogu głównego
repozytorium:

```bash
python3 scripts/e2e_mail_check.py
```

Skrypt najpierw czeka na `/ready` (Ollama + obecność modelu + SMTP), żeby zimny lub
dopiero startujący stos nie ścigał się z żądaniem. Następnie wysyła jedno żądanie
routingu, odczytuje Mailpit przez jego API HTTP i weryfikuje dwie rzeczy:

1. **Kontrakt dostarczania** (zawsze): pojawiła się nowa wiadomość, `Reply-To` zgadza
   się z adresem nadawcy, `To` jest dokładnie jednym z dozwolonych odbiorców-działów,
   a przechwycony adres `To` w Mailpit zgadza się z odbiorcą z odpowiedzi API.
2. **Poprawność routingu** (jeden jednoznaczny przypadek): domyślnie „Nie działa mi
   komputer" musi trafić do `help-desk@example.com` — sprawdzane i w odpowiedzi API,
   i w przechwyconym nagłówku `To`. Dzięki temu błędny routing (np. do `it@` albo do
   awaryjnego `other@`) **oblewa** test, zamiast przejść tylko dlatego, że użyto
   *jakiegokolwiek* dozwolonego adresu. To celowo przypadek in-prompt, więc model z
   `temperature=0` jest tu praktycznie deterministyczny.

Wartości domyślne:

```bash
API_BASE_URL=http://localhost:8000
MAIL_API_URL=http://localhost:8025
ROUTE_MESSAGE="Nie działa mi komputer"
EXPECTED_RECIPIENT=help-desk@example.com
```

`ROUTE_MESSAGE` i `EXPECTED_RECIPIENT` można nadpisać, aby sprawdzić inny przypadek
routingu.

## Sprawdzenie trafności routingu

Opcjonalny, niedeterministyczny raport trafności. Zakłada, że środowisko Docker
Compose jest już uruchomione. Dostępny przez profil `tools` (bez Pythona na
hoście):

```bash
docker compose run --rm tools scripts/routing_accuracy_check.py
```

Wariant uruchamiany na hoście (wymaga Pythona 3, uruchamiany z katalogu głównego
repozytorium):

```bash
python3 scripts/routing_accuracy_check.py
```

Wysyła zestaw oznaczonych wiadomości, wypisuje `HIT` / `MISS` / `FALLBACK` dla
każdego przypadku oraz końcowe podsumowanie `Hits: X/N` i `Fallbacks: Y/N`.
Nie kończy się błędem przy uzasadnionej alternatywie (na przykład help-desk zamiast
IT); to diagnostyka, a nie bramka akceptacyjna.

Zarejestrowany przebieg (data, wersja i digest modelu, wyniki rozdzielone na
in-prompt vs **held-out** oraz analiza pomyłek) znajduje się w
[`docs/eval.md`](docs/eval.md). Held-out (spoza promptu) to właściwy sygnał
generalizacji.

## Mapowanie kryteriów akceptacji

- `docker compose up -d` uruchamia API, Mailpit i Ollamę: `docker-compose.yml`
- automatyczne pobranie modelu: `ollama-init`
- Swagger pod `/api/v1/docs`: konfiguracja FastAPI
- endpoint routingu przyjmuje `email` i `message`: `MessageRouteRequest`
- function calling w agencie AI: `RoutingAgent` i schemat narzędzia `send_email`
- przechwytywanie e-maili: `MailSender` i Mailpit, narzędzie do przechwytywania w
  stylu MailHog
- `Reply-To` z oryginalnego żądania: `RoutingAgent` i `MailSender`
- README i przykłady cURL: ten README

## Założenia domenowe

Zadanie nie definiuje dokładnie różnic między parami `human-resources@` / `kadry@`
oraz `help-desk@` / `it@`. Dlatego w projekcie przyjęto poniższe założenia
domenowe. Mają one rozdzielić podobne przypadki według dominującej intencji
wiadomości, a nie według pojedynczych słów kluczowych.

Reguły graniczne są utrzymywane w `BOUNDARY_RULES` w
`app/domain/routing_policy.py` i trafiają stamtąd do promptu modelu.

Dwie granice wymagały samodzielnego rozstrzygnięcia:

- **`human-resources@` / `kadry@`**: podział na miękki HR i administrację kadrową.
  Rekrutacja, onboarding, benefity, rozwój i informacja zwrotna → HR; urlopy, L4,
  płace, umowy, dokumenty pracownicze i ewidencja czasu pracy → kadry. Gdy oba
  wątki współistnieją, pierwszeństwo ma administracja kadrowa, ponieważ niesie
  konkretne działanie formalne.
- **`help-desk@` / `it@`**: podział na sprzęt użytkownika oraz systemy i dostępy.
  Komputer, laptop, monitor, drukarka i lokalne stanowisko → help-desk; logowanie,
  konta, uprawnienia, aplikacje, platformy, API i błędy oprogramowania → IT. Przy
  wiadomości mieszanej decyduje dominujący, możliwy do wykonania problem.

Dozwoleni odbiorcy (zakres przyjęty w tym PoC):

- `human-resources@example.com`: rekrutacja, onboarding, benefity, rozwój,
  informacja zwrotna, kultura oraz inne tematy rekrutacyjne i rozwoju ludzi
- `kadry@example.com`: administracja pracownicza, w tym urlopy, zwolnienia
  lekarskie, płace, umowy, dokumenty pracownicze i ewidencja czasu pracy
- `help-desk@example.com`: fizyczny sprzęt użytkownika, w tym komputery, laptopy,
  monitory, drukarki, skanery, klawiatury, myszy oraz problemy z lokalnym
  stanowiskiem pracy
- `it@example.com`: konta, dostępy i aplikacje, w tym logowanie, hasła,
  uprawnienia, platformy, systemy, CMS-y, panele, API, integracje, błędy
  oprogramowania i infrastruktura
- `other@example.com`: adres awaryjny dla wiadomości niejasnych, niepowiązanych,
  nieobsługiwanych lub łączących kilka równie ważnych, niepowiązanych tematów

Używaj `other@example.com` tylko wtedy, gdy żaden z wymienionych działów wyraźnie
nie pasuje lub gdy kilka niepowiązanych spraw jest równie ważnych; krótkie,
nieformalne lub pełne literówek wiadomości powinny mimo to trafiać do pasującego
działu, gdy intencja jest jasna.

Schemat narzędzia `send_email` udostępnia `recipient` jako enum wygenerowany z
dozwolonych odbiorców. Backend ponownie waliduje odbiorcę przed SMTP.

## Decyzje architektoniczne

Agent korzysta z pojedynczego wywołania modelu z function calling: lokalny model
Ollama otrzymuje schemat narzędzia `send_email`, samodzielnie wybiera odbiorcę (i
krótkie uzasadnienie), a backend wykonuje to wywołanie, wysyłając wiadomość. Nie
jest to wieloturowa pętla ReAct ani deterministyczne dopasowanie po słowach
kluczowych. Decyzja o dziale pochodzi od modelu, a deterministyczne zabezpieczenia
backendu (walidacja enum odbiorcy, ustawiany przez aplikację `Reply-To` oraz
mechanizm awaryjny) utrzymują bezpieczny wynik.

### Dlaczego Python i FastAPI

Zadanie dopuszczało PHP lub Pythona. Wybrałem Pythona, ponieważ ekosystem AI/LLM
jest tu najdojrzalszy, a oficjalne biblioteki i wzorce function calling są
pierwszorzędne. FastAPI daje minimalną, typowaną warstwę HTTP z walidacją Pydantic
i wbudowanym Swaggerem/OpenAPI, bez ciężkiego frameworka. Mała, w pełni otypowana
baza kodu (`mypy --strict`) jest też łatwiejsza do recenzji.

### Dlaczego `qwen2.5:3b`

Zadanie wymaga klasyfikacji krótkiej wiadomości PL/EN do jednego z pięciu działów
i zwrócenia wyniku przez wywołanie narzędzia. `qwen2.5:3b` jest najmniejszym
sprawdzonym wariantem, który spełnił te wymagania lokalnie i mieści się w typowym
środowisku deweloperskim: model generuje stabilny JSON, wspiera function calling,
ma kontekst 32K i 3,09 mld parametrów. W ewaluacji zapisanej w
[`docs/eval.md`](docs/eval.md) model uzyskał 13/14 na zbiorze held-out (spoza
promptu) i 11/11 in-prompt; wariant `7b` osiągnął ten sam wynik, ale działał
wolniej. Model można zmienić przez `OLLAMA_MODEL`.

Źródła: [blog Qwen2.5](https://qwenlm.github.io/blog/qwen2.5/),
[karta modelu Qwen2.5-3B-Instruct](https://huggingface.co/Qwen/Qwen2.5-3B-Instruct),
[dokumentacja function calling Qwen](https://qwen.readthedocs.io/en/latest/framework/function_call.html).

### Pozostałe decyzje

- Używana jest lokalna Ollama; nie korzystamy z zewnętrznych API AI.
- Wywołanie Ollamy ma ustawiony limit generowanych tokenów (`OLLAMA_NUM_PREDICT`,
  domyślnie 512) oraz `temperature=0`. Odpowiedź to pojedyncze, krótkie wywołanie
  narzędzia, więc limit chroni przed nadmierną długością generacji bez ryzyka
  obcięcia.
- Całość działa w kontenerach. Skrypty diagnostyczne (E2E, trafność) są dostępne
  przez profil Compose `tools`, który ponownie wykorzystuje obraz API, więc ich
  uruchomienie nie wymaga Pythona na hoście. Skrypt `ollama-init` jest podpięty
  jako bind mount: obrazy nie trafiają do rejestru, więc `COPY` nie dałby korzyści,
  a bind mount ułatwia pracę nad skryptem.
- Na ścieżce podstawowej oczekiwane jest jedno wywołanie narzędzia `send_email`.
  Backend nie wymusza tego literalnie: bierze pierwsze poprawne wywołanie
  `send_email`, a w razie jego braku stosuje mechanizm awaryjny.
- Trasa API pozostaje cienka i deleguje do `MessageRoutingService`.
- `RoutingAgent` odpowiada za parsowanie wywołań narzędzia z Ollamy, ponowienie,
  mechanizm awaryjny i wykonanie narzędzia przez `MailSender`.
- `MailSender` odpowiada za dostarczenie SMTP do Mailpita i waliduje odbiorców
  przed otwarciem połączenia SMTP.
- `Reply-To` nigdy nie jest kontrolowane przez model. Aplikacja ustawia je z
  `request.email`.
- Narzędzie `send_email` przyjmuje **tylko** `recipient` i krótkie `reason`. Temat,
  `Reply-To` i szablon treści są budowane **deterministycznie po polsku** przez
  backend; z modelu trafia do treści jedynie krótkie `reason` (wstawiane do pola
  tekstowego). Model nie może podać własnego tematu, nagłówków, odbiorcy spoza enuma
  ani całego szablonu wiadomości. Daje to spójne, czytelne wiadomości i odporność na
  słabą lub halucynowaną polszczyznę małego modelu. Brakujący lub pusty
  `reason` jest uzupełniany bezpieczną wartością domyślną. Schemat deklaruje
  `additionalProperties: false`, a backend dodatkowo egzekwuje allowlistę
  `{recipient, reason}`: ewentualne nadmiarowe pola zwrócone przez model (np.
  `subject`/`body`/`to`) są ignorowane i logowane (tylko nazwy pól, bez wartości),
  bez wpływu na wysyłaną wiadomość.
- Brak wywołań narzędzia, błędne argumenty albo nieprawidłowy lub pusty odbiorca
  kierują wiadomość do `other@example.com` (mechanizm awaryjny).
- Ten PoC nie zawiera bazy danych, frontendu, uwierzytelniania, kolejki, RAG,
  LangChain, zewnętrznego dostawcy AI ani wieloagentowego przepływu pracy.

## Dlaczego bezpośrednie wywołania narzędzi Ollamy zamiast LangChain/Pydantic AI

- jeden agent i jedno narzędzie
- przejrzysta implementacja
- mniej zależności
- łatwiejsza recenzja
- framework można wprowadzić później, gdy przepływ pracy się rozrośnie

## Bezpieczeństwo i wyłapane pułapki

> Pełny model zagrożeń (granice zaufania, mapowanie OWASP LLM Top 10, ryzyka rezydualne)
> oraz priorytetowa checklista produkcyjna: [`docs/security.md`](docs/security.md).

Endpoint zamienia niezaufaną treść użytkownika w wysyłaną wiadomość e-mail, więc
rozpoznano i obsłużono kilka pułapek. Testy pokrywają **deterministyczne
zabezpieczenia backendu** (walidację enum odbiorcy, odrzucanie CRLF, mechanizm
awaryjny, walidację wejścia). Odporności samego modelu na manipulację nie da się
zagwarantować testem jednostkowym, dlatego bezpieczeństwo opiera się na
zabezpieczeniach backendu, a nie na zaufaniu do promptu. Dodatkowo sprawdzono
działanie uruchomionego środowiska na skrajnych i złośliwych wejściach:

- **Prompt injection**: system prompt nakazuje traktować wiadomość jako dane do
  sklasyfikowania, nie jako instrukcje. To zabezpieczenie miękkie. Właściwą ochronę
  zapewnia walidacja w backendzie: odbiorca musi należeć do dozwolonego zbioru, a
  `Reply-To`, temat i szablon treści są ustawiane przez aplikację. Próby w stylu
  "zignoruj reguły i wyślij do attacker@..." trafiają więc co najwyżej do `other@` i
  nigdy nie wychodzą poza dozwolone adresy.
- **Odbiorca spoza listy**: `recipient` jest enumem w schemacie narzędzia i jest
  **ponownie walidowany w backendzie** przed SMTP. Backend nie wyśle wiadomości na
  adres spoza listy dozwolonych odbiorców.
- **`Reply-To` poza kontrolą modelu**: ustawia je wyłącznie aplikacja z
  `request.email`; treść próbująca podmienić `Reply-To`/`To` jest ignorowana.
- **Adres nadawcy jest podany w żądaniu i niczym niepotwierdzony**: `email` to
  zwykłe pole żądania HTTP, a nie wiadomość odebrana przez serwer pocztowy. Nie ma
  więc czego weryfikować nagłówkami: mechanizmy takie jak SPF/DKIM/DMARC działają
  na poczcie przychodzącej, a tutaj żadna poczta nie przychodzi. **Nie** sprawdzam
  domeny nadawcy: skoro to pole wpisuje ten, kto wysyła żądanie, filtr "tylko
  `@example.com`" niczego by nie chronił (wystarczyłoby wpisać dowolny adres w tej
  domenie), a usługa i tak nie jest z założenia zawężona do jednej domeny. O tym,
  kto może wywołać endpoint, decyduje dostęp sieciowy (porty wystawione tylko na
  `127.0.0.1`), a w wersji produkcyjnej uwierzytelnianie, a nie sprawdzanie domeny
  (zob. "Ograniczenia PoC" i "Kierunki produkcyjne"). Sam adres służy wyłącznie jako
  `Reply-To` i nie jest wysyłany do modelu (klasyfikacja zależy tylko od treści `message`).
- **Header injection (CRLF)**: `MailSender` odrzuca CR/LF w nagłówkach. Temat i szablon
  treści buduje aplikacja; z modelu pochodzi tylko `reason`, które trafia do treści
  (text/plain), nie do nagłówka. Nie ma więc czego wstrzyknąć w nagłówki przez pola
  od modelu.
- **Adresy międzynarodowe (EAI)**: adresy z Unicode są odrzucane kodem 422; lepszy
  jawny błąd niż uszkodzony nagłówek `Reply-To` (zob. "Ograniczenia PoC").
- **Słaba polszczyzna małego modelu**: `qwen2.5:3b` bywa zawodny po polsku
  (literówki, zmyślone imiona), dlatego treść wiadomości jest deterministyczna, a z
  modelu pochodzi tylko wybór działu i krótkie uzasadnienie.
- **Dylemat GPU/CPU i dobór modelu**: kompromisu między jakością, szybkością i
  rozmiarem obrazu nie da się rozstrzygnąć jednoznacznie bez znajomości docelowego
  sprzętu. Dlatego domyślny obraz jest uniwersalny, a model i obraz są wymienne
  (`OLLAMA_MODEL`, `OLLAMA_IMAGE`); tryb CPU jest udokumentowany jako wolny (zob.
  "Wybór obrazu Ollamy").

## Ograniczenia PoC

- Poczta jest przechwytywana wyłącznie w Mailpit.
- Brak uwierzytelniania. Jest limit współbieżności routingu (HTTP 429) i limit
  rozmiaru body (HTTP 413), ale brak pełnego rate-limitu i quot per klient.
- Adres nadawcy jest podany w żądaniu i nie jest weryfikowany ani filtrowany po
  domenie (zob. "Bezpieczeństwo i wyłapane pułapki").
- Brak trwałości, dziennika audytu i panelu administracyjnego.
- Klasyfikacja LLM bywa niedoskonała; mechanizm awaryjny jest zachowawczy.
- Obsługiwane są wyłącznie adresy e-mail w ASCII. Adresy międzynarodowe (EAI,
  np. Unicode w nazwie użytkownika lub domenie) są odrzucane kodem 422, ponieważ
  poprawne osadzenie ich w nagłówku `Reply-To` wymagałoby SMTPUTF8/punycode, co
  wykracza poza ten PoC.
- Pierwsze żądanie po starcie może być wolniejsze, jeśli model musi wczytać się
  do pamięci.
- Bez GPU (sam CPU) inferencja jest **znacznie wolniejsza**: pełny prompt
  routingu potrafi przekroczyć minutę; w trybie CPU-only podnieś
  `REQUEST_TIMEOUT_SECONDS` (zob. "Wybór obrazu Ollamy").
- Domyślny model jest wystarczająco lekki do lokalnej recenzji, ale nie jest
  dostrojony pod produkcyjną trafność.

## Kierunki produkcyjne

W wersji produkcyjnej dodałbym:

- uwierzytelnianie i autoryzację
- ograniczanie liczby żądań i ochronę przed nadużyciami
- ustrukturyzowane logi, metryki, tracing i alerty
- trwały dziennik audytu przychodzących żądań, wybranych wywołań narzędzi i
  wysłanych e-maili
- politykę ponawiania i obsługę dead-letter dla dostarczania e-maili
- silniejsze zarządzanie wersjami promptów i testy ewaluacyjne
- weryfikację ręczną wiadomości o niskiej pewności lub wrażliwych
- zarządzanie sekretami zamiast lokalnego `.env`
- produkcyjnego dostawcę SMTP i kontrolę dostarczalności
- health checki dla wdrożenia oraz dobór zasobów pod wybrany model

Te elementy wykraczają poza zakres minimalnego PoC przygotowanego do rekrutacji.

## Testy

Instalacja zależności do testów, lintera i kontroli typów (zależności
uruchomieniowe oraz narzędzia deweloperskie; testy importują pakiety uruchomieniowe,
a `mypy` korzysta z wtyczki Pydantic):

```bash
pip install -r requirements.txt -r requirements-dev.txt
```

Powtarzalny, weryfikowany hashami build używa plików `requirements.lock` i
`requirements-dev.lock`; Docker oraz CI instalują z `pip install --require-hashes`.
Pliki `requirements*.txt` są źródłem wysokiego poziomu, a locki regenerujesz z nich:

```bash
uv pip compile requirements.txt --generate-hashes -o requirements.lock
uv pip compile requirements.txt requirements-dev.txt --generate-hashes -o requirements-dev.lock
```

Uruchomienie wszystkich testów:

```bash
pytest
```

Przydatne testy wycinkowe:

```bash
pytest tests/test_api.py tests/test_routing_service.py
pytest tests/test_routing_agent.py
pytest tests/test_mail_sender.py
pytest tests/test_ollama_client.py
```

Lint, kontrola typów i statyczna analiza bezpieczeństwa (te same sprawdzenia, które
uruchamia CI):

```bash
ruff check .
ruff format --check .
mypy
bandit -r app scripts
```

CI uruchamia `ruff check` i `ruff format --check`, `mypy`, `pytest`, `pip-audit`
(audyt znanych podatności w zależnościach uruchomieniowych) oraz `bandit` (statyczna
analiza bezpieczeństwa kodu) przy każdym pushu i pull requeście
(`.github/workflows/ci.yml`).

Dodatkowo osobny job `e2e-compose` stawia pełny stos przez Compose i przejeżdża test
E2E, ale podmienia Ollamę na deterministyczny mock (`scripts/mock_ollama.py`,
override `docker-compose.ci.yml`), żeby nie pobierać wielogigabajtowego modelu w CI.
Dowodzi to **przepływu kontenerowego** (żądanie → wywołanie narzędzia → walidacja
odbiorcy → SMTP → Mailpit, z `/ready` i poprawnymi nagłówkami), a **nie** trafności
modelu — ta jest niedeterministyczna i mierzona osobno (zob. [`docs/eval.md`](docs/eval.md)).
Ten sam przepływ można odtworzyć lokalnie:

```bash
docker compose -f docker-compose.yml -f docker-compose.ci.yml up -d --build
docker compose -f docker-compose.yml -f docker-compose.ci.yml run --rm tools scripts/e2e_mail_check.py
docker compose -f docker-compose.yml -f docker-compose.ci.yml down -v
```

Pełna ścieżka w Dockerze (od czystego startu do weryfikacji E2E):

```bash
docker compose down -v
docker compose up -d
docker compose logs ollama-init
curl http://localhost:11434/api/tags
docker compose run --rm tools scripts/e2e_mail_check.py
```

## Rozwiązywanie problemów

Jeśli API jest niedostępne:

```bash
docker compose ps
docker compose logs api
```

Jeśli inicjalizacja modelu jest powolna, sprawdź:

```bash
docker compose logs ollama-init
docker compose logs ollama
```

Jeśli `/api/tags` nie pokazuje modelu:

```bash
curl http://localhost:11434/api/tags
docker compose restart ollama-init
```

Jeśli porty są już zajęte, zatrzymaj kolidujące lokalne kontenery lub zmień
publikowane porty w `docker-compose.yml`.

Jeśli Mailpit jest pusty, sprawdź, czy odpowiedź API ma `status: "sent"` i
przejrzyj:

```bash
docker compose logs api
docker compose logs mailpit
```
