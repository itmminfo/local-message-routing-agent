# Lokalny podgląd i obowiązkowa akceptacja

Użytkownik zatwierdził 14.11.2026 wykonanie wariantu opisanego w prywatnym C-WARIANT-AKCEPTACJI.md. Wysyłka wyłącznie do Mailpit, PR #4 pozostaje osobną decyzją. Ta specyfikacja doprecyzowuje techniczne kontrakty w zatwierdzonym zakresie. Bez nowych zależności produkcyjnych.

## Granica operatora

To aplikacja lokalna na loopback. Zaufanym operatorem jest osoba mająca lokalny dostęp do niej. Sesja i CSRF chronią przed obcymi stronami oraz pomyleniem sesji; nie stanowią kryptograficznego dowodu fizycznej obecności człowieka. Nie wdrażamy kont, zewnętrznego logowania ani publicznego hostingu.

Cookie losowej sesji ma HttpOnly, SameSite=Strict, Path=/ i czas życia 8 godzin. Sesje i token CSRF są trwałe w SQLite. Odpowiedzi z danymi mają Cache-Control:no-store. Wszystkie operacje sprawdzają dokładny Host; mutacje również dokładny Origin zgodny z Host i token CSRF w X-CSRF-Token. Dopuszczone originy są jawną listą lokalnych originów z portami, domyślnie localhost:8000 i 127.0.0.1:8000. Brak CORS dla obcych stron. Błędne pochodzenie nie może wywołać modelu ani nadawcy.

Stare POST /api/v1/messages/route i /route-local-eval zwracają 409 wymagania review przed modelem i SMTP. Nie ma przełącznika przywracającego wysyłkę automatyczną. Każda nowa droga SMTP sprawdza rzeczywisty host mailpit i port 1025.

## Kontrakt HTTP dla strony i testów

Wszystkie ścieżki poniżej są pod /api/v1/review. Dla operacji mutujących wymagana jest sesja, Origin i CSRF. Sesja identyfikuje właściciela podglądu. Obca sesja nie może zobaczyć ani zatwierdzić rekordu.

- GET /session: zakłada lub odzyskuje lokalną sesję; zwraca csrf_token i departments, listę {recipient,label} z pięciu dozwolonych działów.
- POST /previews: {email,message}. Natychmiast zapisuje niezmienny oryginał i zwraca rekord pending_review. Nie wywołuje modelu ani SMTP. Nadmiarowe pola są odrzucane.
- GET /previews/{id}: zwraca zapisany rekord bieżącej sesji, także po restarcie. Wygasły podgląd nie może być zatwierdzony.
- POST /previews/{id}/suggest: {version}. Osobna, opcjonalna operacja modelu bez dostępu do nadawcy. Model działa poza transakcją i pod limitem współbieżności. Błąd lub odmowa zapisują jawny status sugestii, bez blokowania ręcznego wyboru. Wynik zapisuje się tylko do nadal oczekującej wersji; spóźniony wynik nie zmienia zatwierdzonego rekordu.
- POST /previews/{id}/approve: {version,payload_digest,recipient,confirmed:true}. Tylko jawnie wybrany dozwolony adresat. Zgoda jest zapisana na serwerze i wiąże sesję, wersję, digest oryginalnego email/message oraz adresata. Serwer nie przyjmuje treści, nadawcy ani uzasadnienia do wysłania z tego żądania. Odpowiedź to aktualny rekord operacji.
- POST /previews/{id}/retry: ponowienie wyłącznie stanu failed_before_submit, bez zmiany payloadu, wersji ani zatwierdzonego adresata. Odmawia dla delivery_unknown i sent. Replay approve dla już rozpoczętej tej samej operacji zwraca stan bez nowej próby; zmieniony adresat/digest/wersja daje konflikt.
- POST /previews/{id}/reject: {version}, tylko przed rozpoczęciem wysyłki.

Rekord JSON ma co najmniej: id, version, payload_digest, email, message, state, created_at, expires_at, recipient (zatwierdzony lub null), suggestion (status, recipient, reason, model, routing_confidence, used_fallback, issue_quotes, error), message_id, attempts, retry_allowed i error. Niezaufane teksty nigdy nie są interpretowane jako HTML. Ewentualne drobne rozstrzygnięcia schematu uzgadniają wykonawcy przez wspólny kontrakt, nie przez nowe decyzje użytkownika.

## Model jako propozycja

Nowy interfejs RoutingAgent.propose nie ma obiektu MailSender ani zdolności wysyłki. Zwraca wyłącznie propozycję i źródłowe cytaty. Brak modelu, błędny JSON, timeout oraz brak poprawnego cytatu skutkują brakiem propozycji; podgląd i wybór człowieka nadal działają. Nie dodajemy domyślnie wybranego działu w UI, także przy confidence=high.

Dotychczasowe testy parsera i routingu zachowują sprawdzane przypadki, lecz testują propozycję bez SMTP. Wszystkie pozytywne testy wysyłki przechodzą przez serwerową akceptację. Automatyczna jakość modelu pozostaje oddzielną metryką.

## Trwałość i semantyka SMTP

SQLite z biblioteki standardowej przechowuje sesje, podglądy, zatwierdzenie i próby. Baza otrzymuje własny trwały wolumen, zapis dostępny użytkownikowi kontenera mimo read_only root. Nie używamy /tmp do trwałego stanu i nie usuwamy dowodów automatycznie.

Jeden aktywny backend na bazę jest wymuszony blokadą procesu. Dzięki temu recovery nie przejmuje próby żywego drugiego procesu. SQLite z krótkimi transakcjami i CAS chroni równoległe żądania; żadna transakcja nie obejmuje LLM ani SMTP.

Zatwierdzenie i przejście do sending są utrwalane atomowo przed SMTP, z trwałym identyfikatorem próby i Message-ID. Podwójne kliknięcie lub replay HTTP nie rozpoczyna drugiej próby. Oryginał i adresat są sprawdzane ponownie przed rozpoczęciem wysyłki.

- pending_review: brak zgody i SMTP.
- approved: zgoda zapisana; zdarzenie audytowe może atomowo przejść do sending.
- sending: trwała próba; brak równoległego ponowienia.
- sent: otrzymano pozytywny ACK DATA; błąd QUIT nie zmienia tego w błąd dostarczenia.
- failed_before_submit: udowodniono, że DATA nie rozpoczęto; ręczne retry dozwolone.
- delivery_unknown: możliwe przyjęcie DATA bez końcowego potwierdzenia albo sending odzyskane po przerwaniu procesu. Brak automatycznego lub zwykłego retry; operator najpierw sprawdza Mailpit po Message-ID. Ewentualna nowa wysyłka wymaga nowego podglądu i zgody.
- rejected / expired: brak prawa wysyłki.

Nie deklarujemy exactly-once pomiędzy SQLite i SMTP. Trwały stan zapobiega świadomemu ponowieniu niepewnej operacji; nie usuwa niepewności protokołu.

## Strona

Prosta własna strona HTML/CSS/JS, serwowana w tym samym originie przez FastAPI. Bez frameworka ani bundlera. Formularz nadawcy i treści, niezmienny podgląd, opcjonalna propozycja z cytatami, pięć działów bez domyślnego wyboru i jawny przycisk akceptacji. Edycja oryginału unieważnia lokalny podgląd i wymaga utworzenia nowego. Odświeżenie odzyskuje identyfikator podglądu z sessionStorage, bez zapisywania treści w przeglądarce. UI pokazuje stany sent/failed/unknown i dopuszcza retry tylko zgodnie ze stanem serwera.

## Odbiór

Test RED obecnego obejścia bez zgody, potem testy sesji/Origin/CSRF, obcej sesji, digestu/wersji, spóźnionej sugestii, równoległego approve/retry, restartu i blokady procesu, SMTP przed DATA/utrata ACK/błąd QUIT. Pełna istniejąca bramka Linux Python 3.12 bez zmian locków. Rzeczywiste Mailpit: brak maila przed zgodą, dokładne To/Reply-To/oryginał po zgodzie, brak kopii przy ponowieniu, jawne delivery_unknown po przerwaniu i odtworzeniu kontenera. Browser smoke formularza, jeśli dostępny dopuszczony mechanizm testowy; bez przejmowania pulpitu bez wymaganej zgody.

## Podział plików

- Backend: app/review/*, routing_agent.py, issue_boundary.py jeśli potrzebne cytaty, mail_sender.py, config.py, api/routes.py, main.py i niezbędny serwis aplikacji/schematy.
- Front: app/web/* i testy tests/test_review_ui.py.
- Testy: pozostałe tests/*, odtworzenie starego obejścia i nowy odbiór stanowy.
- Koordynator: ta specyfikacja, Docker/Compose/.dockerignore, README/docs, skrypty E2E/ewaluacji i lokalne wdrożenie oraz niezależny odbiór.
