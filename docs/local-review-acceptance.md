# Odbiór lokalnego review, 14.11.2026

Wariant wymaga jawnej akceptacji oryginału i wybranego działu. Wdrożenie wyłącznie
lokalne, Mailpit. PR #4 pozostaje osobną decyzją. Bez nowych zależności produkcyjnych
oraz bez zmian zamrożonych zbiorów oryginalnego i holdout.

## Odtworzenie przed poprawką

Na odizolowanej kopii bazowego 22218dc oba stare endpointy wykonały serwis
wysyłający (HTTP200, wywołanie1), zamiast odmówić bez efektu (409, wywołanie0).
`test_review_required.py` miał dwa niepowodzenia. Nowa wersja odmawia przed
wywołaniem modelu i SMTP, także w legacy serwisie Python.

## Bramka

Linux, Python3.12, istniejący requirements-dev.lock instalowany z hashami.
Ruff check i format, mypy, pytest (285 testów), Bandit, pip-audit obu locków, walidacja zwykłego
oraz CI Compose i build obrazu przeszły. Rzeczywisty obraz zawiera własny front
oraz backend review, nie zawiera tests, README, SECURITY ani .env.

Testy obejmują izolację sesji, dokładny Host/Origin, token CSRF, strict confirmed,
wersję/digest, nadmiarowe pola, spóźniony model, równoległe approve/retry,
wygaśnięcie, trwałość i blokadę drugiego procesu oraz fazy SMTP. Oddzielna kontrola 45
mutacji sprawdza, że usunięcie tych zabezpieczeń faktycznie powoduje niepowodzenia.

## Realne kontenery i Mailpit

Przy użyciu danych syntetycznych i jawnej testowej akceptacji:

| Przypadek | Obserwacja |
| --- | --- |
| Podgląd i opcjonalna sugestia | Brak poczty przed approve |
| Model niedostępny | Podgląd natychmiast, błąd propozycji, ręczna zgoda działa |
| Zgoda i replay | Dokładnie jedna próba i jedna wiadomość |
| Sześć równoległych approve | Jedna próba SMTP i jedna wiadomość |
| SMTP odmawia przed DATA | failed_before_submit, brak wiadomości, jawny retry działa |
| Mailpit przyjął DATA, proxy urwało ACK | delivery_unknown, jedna wiadomość, retry odrzucone |
| Pozytywny ACK DATA, zerwanie QUIT | sent, jedna wiadomość |
| SIGKILL API po przyjęciu DATA, odtworzenie kontenera | Ta sama próba i Message-ID, delivery_unknown, bez kopii |
| Odtworzenie kontenera z oczekującym podglądem | Ten sam oryginał i sesja, nadal bez zgody |

Dla dostarczonych wiadomości odczytano API Mailpit i potwierdzono To, Reply-To,
Message-ID, oryginalną treść i dział zatwierdzony przez operatora. Proxy awarii
łączyło się wyłącznie z istniejącym lokalnym sinkiem Mailpit.

Deterministyczny mock E2E osobno sprawdził preview, propozycję, testową zgodę,
SMTP i replay. Nie jest to pomiar rzeczywistej trafności modelu.

## Przeglądarka

Headless Chromium, wyłącznie własny origin loopback. Rzeczywisty formularz:
niezaznaczony dział i checkbox, HTML pokazany jako tekst, akceptacja podczas
wiszącej propozycji, sent z Message-ID, brak błędów konsoli, restore po ponownym
utworzeniu kontenera. Widoki1280px i390px, bez poziomego overflow. Dodatkowy
harness rzeczywistego review.js sprawdził edycję unieważniającą podgląd i późne
odpowiedzi oraz ekran delivery_unknown bez retry.

## Granice dowodu

Testy symulują operatora; API nie dowodzi fizycznej obecności człowieka. To lokalna
aplikacja z zaufanym hostem. Mailpit nie jest zewnętrznym dostarczeniem. Nie ma
gwarancji exactly-once SQLite+SMTP. Propozycje AI mogą nadal wskazywać błędny dział;
nie przedstawiamy ręcznego wyboru jako poprawy automatycznej klasyfikacji.

## Dodatkowy review PR #6, 14.11.2026

Review odtworzył trzy grupy usterek:

- Identyczny retry po szybkim błędzie przed DATA i restarcie mógł rozpocząć kolejną
  próbę bez nowego gestu. Obowiązkowy expected_attempt_id wiąże retry z jedną próbą
  i jej jedynym trwałym następcą. Działa też dla istniejących rekordów bez migracji.
- UI tracił ID po awarii odczytu i odblokowywał nową operację po niepewnym POST.
  Znacznik zapisany przed żądaniem przetrwa reload; sam stary GET nie rozstrzyga
  opóźnionego żądania. Jawne wycofanie pod blokadą zapisu blokuje późny approve/retry.
- GET mógł mieszać stan podglądu z próbami zatwierdzonymi później. Oba SELECT są
  teraz w jednym snapshotcie SQLite. Testy odtwarzają wyścig w trybach DELETE i WAL.

314 testów Pythona i 32 testy rzeczywistego review.js przeszły, razem z Ruff,
formatem, mypy, Bandit, pip-audit obu locków i buildem. Node testy są w CI i nie
wymagają zależności npm. Testy obejmują czerwone odtworzenia przed poprawką,
spóźnione żądania, wycofanie po TTL, zachowanie historii, problemy sessionStorage
oraz odrzucenie bezpośredniej próby obejścia sesji/Origin/CSRF i niezmiennej zgody.

Realny Mailpit potwierdził: 16 szybkich równoległych retry tworzy jednego następcę;
po odtworzeniu kontenera stary retry nie wysyła mimo przywrócenia SMTP; nowa ręczna
akcja działa. Opóźniona zgoda i retry po wycofaniu nie wysyłają. Nadal przechodzą
utrata ACK, błąd QUIT, równoległe approve i SIGKILL z delivery_unknown bez kopii.

W rzeczywistym headless Chromium odtworzono utratę odpowiedzi po przyjęciu do
Mailpit oraz opóźnioną zgodę przy pozornie starym pending_review. Po poprawce
blokada i ID przetrwały reload, a odzyskanie po odtworzeniu kontenera pokazało
jedną próbę sent. Wycofanie przed zwolnieniem opóźnionej zgody dało potem HTTP409,
bez wiadomości. Widoki 1280 i 390 px sprawdzono osobno.

Nie odtworzono bezpośredniego obejścia serwerowej zgody w sprawdzonym zakresie.
Nie deklarujemy dowodu fizycznej obecności człowieka, exactly-once ani odporności
na dowolne awarie dysku/zasilania. Nadal tylko lokalny Mailpit. PR #4 pozostaje
osobną decyzją; review i poprawki nie oznaczają zgody na merge.

## Review całego PR #5 po dołączeniu lokalnego preview

Przegląd względem gałęzi adversarial-eval obejmował również pierwotną ekstrakcję
cytatów i diagnostykę. Potwierdzone i naprawione:

- Walidator zawsze brał pierwsze wystąpienie frazy, więc legalne powtórzenie mogło
  dawać fałszywe nakładanie. Dobór rozłącznych wystąpień ma teraz ograniczony DP
  i kanoniczną kolejność. Maksymalnie osiem cytatów, 256 stanów i 1024 wyszukania.
- Zwykły status błędu propozycji przerywał cały raport. Teraz unavailable ma własne
  liczniki i pełne mianowniki, bez udawania odbiorcy lub fallbacku.
- E2E mógł przepuścić dopisany tekst lub puste Message-ID. Teraz porównuje całą
  niezależnie opisaną treść, Subject i prawidłowe stabilne identyfikatory.

Regresje najpierw były czerwone. 348 testów Python i 32 testy JavaScriptu przeszły,
wraz z Ruff, mypy, Bandit, pip-audit obu locków, buildem i Compose. Własny oracle
cytatów sprawdził 3906 kombinacji, dodatkowa kontrola 51492; gate diagnostyki i E2E
ma 13 zabitych mutacji. Zamrożone zestawy i etykiety pozostały bez zmian.

Odizolowany realny Mailpit potwierdził dwa E2E z pełnym body i jedną kopią po replay.
Diagnostyka ukończyła 40/40 niedostępnych propozycji przy wyłączonym modelu oraz
40/40 propozycji deterministycznej atrapy, bez SMTP. Krótkie wywołanie rzeczywistego
Qwen 7B po rozgrzaniu dało poprawną propozycję zwykłego przypadku; zimny start
przekroczył 90-sekundowy limit testowy i został jawnie oznaczony jako niedostępny.
Nie zmienia to historycznych wyników jakości, nie jest nowym odbiorem 20/20.

Fingerprint ekstrakcji tej wersji: 5744c07e0e20. Historyczne raporty wcześniejszego
fingerprintu nie opisują bieżącej jakości. Gotowość dotyczy lokalnego przepływu
z obowiązkową zgodą i Mailpit. Automatyczny wybór działu nadal nie jest odebrany.
PR #4 pozostaje do osobnej decyzji. Przygotowanie PR #5 nie oznacza jego scalenia.

## Review PR #4 względem main po scaleniu PR #5 i #6

Zakres obejmuje całą zmianę od automatycznego routingu do lokalnego preview,
sesje i zgodę, trwałe próby, formularz, diagnostykę i konfigurację Compose.
Tytuł oraz opis PR wymagają tego samego zakresu; historyczna ewaluacja nie jest
aktualną ścieżką wysyłki ani dowodem jakości obecnego modelu.

Potwierdzony błąd P3: odrzucenie podglądu podczas pracy modelu zostawiało sugestię
w stanie pending. Wygaśnięcie TTL miało analogiczny skutek. Formularz dodatkowo
nadawał lokalnej oczekującej odpowiedzi pierwszeństwo przed zamkniętym stanem,
więc mógł nadal pokazywać pobieranie lub zachęcać do akceptacji.

Odrzucenie zamyka wyłącznie oczekującą sugestię; gotowa propozycja i wcześniejszy
błąd pozostają bez zmian. Odczyt wygasłego podglądu pokazuje zakończone oczekiwanie
bez mutacji z GET. Pasująca spóźniona odpowiedź po TTL zapisuje błąd zamiast wyniku
i usuwa token. Nie może zmienić zgody ani wysłać wiadomości. Formularz pokazuje
gotową propozycję jako historię i nie zachęca do akceptacji zamkniętego podglądu.
Nie jest to anulowanie trwającego połączenia z modelem.

Końcowy review odtworzył drugi błąd P3: starszy GET mógł cofnąć formularz z
wygasłego albo innego zamkniętego stanu do pending_review. Backend odmawiał
wysyłki po TTL, ale UI ponownie oferował akceptację. Guard tego samego podglądu
odrzuca teraz regresję do pending_review. Wirtualne expired nadal może przejść
do sending/sent, gdy zgoda weszła wcześniej; nie dodano go do nieodwracalnych
stanów terminalnych ani nie osłabiono odzyskiwania nierozstrzygniętej zgody.

Regresje backendu i rzeczywistego JavaScriptu miały RED przed poprawką, następnie
GREEN. Obejmują odrzucenie, granicę TTL, spóźniony model, restart i zachowanie
gotowych wyników. Dodatkowe próby granic HTTP/SMTP, SQL rollback, blokady procesu,
osieroconej wysyłki i replay po wielokrotnych restartach nie ujawniły nowego
materialnego obejścia zgody ani duplikatu. Próby z atrapą są oddzielone od
rzeczywistego odbioru Mailpit. Brak gwarancji exactly-once i dowodu fizycznej
obecności człowieka pozostaje jawnym ograniczeniem. Nadal wyłącznie lokalny Mailpit.

Po poprawce przeszło 357 testów Python i 52 testy rzeczywistego JavaScriptu oraz
Ruff, mypy, Bandit, pip-audit obu locków, build i walidacja Compose. W odizolowanym
stosie rzeczywisty Mailpit odebrał zgodę bez dostępnego modelu. Po restarcie API
replay nie dodał maila. Oddzielny przypadek błędu przed DATA przetrwał restart;
16 równoległych retry utworzyło jednego następcę i jedną wiadomość. Replay tego
retry po kolejnym restarcie nie wysłał kopii. Cała treść, Subject, To, Reply-To
i stabilny Message-ID obu syntetycznych wiadomości były zgodne z zatwierdzeniem.
Nie uruchamiano zewnętrznego SMTP ani nie zmieniano zamrożonych danych i etykiet.
