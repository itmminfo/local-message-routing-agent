# Granice lokalnego preview

Wariant jest przeznaczony dla zaufanego operatora na loopback, wyłącznie z Mailpit.
Nie obejmuje publicznego hostingu, zewnętrznego SMTP ani kont użytkowników.

## Granice zaufania

Email i treść są niezaufane. Pydantic waliduje wejście, a niezmienny oryginał
trafia do SQLite. Model ma wyłącznie interfejs propozycji, bez nadawcy SMTP.
Źródłowe cytaty muszą istnieć w oryginale, lecz ta kontrola nie dowodzi poprawności
wybranego działu. Operator musi sam sprawdzić treść i adresata.

Sesja ma losowy identyfikator, HttpOnly i SameSite=Strict. Token CSRF jest osobny.
Backend sprawdza dokładny Host, a dla mutacji także zgodny Origin i token.
Odpowiedzi z danymi są no-store, brak CORS. Zawartość wiadomości i modelu trafia
do textContent, nigdy do HTML. CSP blokuje obce skrypty, ramki i połączenia.
Lokalny proces z dostępem do przeglądarki lub bazy jest zaufany; sesja nie zapewnia
odporności na przejęty komputer ani dowodu obecności człowieka.

Podgląd należy do sesji. Obce sesje nie mogą go odczytać ani zatwierdzić.
Approve przyjmuje jedynie wersję, digest, dozwolony dział i jawne confirmed=true.
Nie przyjmuje nowej treści, nadawcy ani uzasadnienia. Serwer wiąże zgodę z zapisanym
oryginałem i sesją. Stare automatyczne endpointy są trwale zablokowane kodem 409.

## Trwała wysyłka

Krótka transakcja SQLite atomowo zapisuje zgodę i próbę sending przed SMTP.
Compare-and-swap zapobiega równoległemu approve/retry. Wywołania modelu i SMTP
odbywają się poza transakcją. Spóźniony model nie zmienia zatwierdzonej operacji.
Blokada procesu pozwala uruchomić jeden backend dla danej bazy. Po restarcie
osierocone sending staje się delivery_unknown, bez ponownej wysyłki.

Nadawca sprawdza rzeczywiste mailpit:1025 oraz allowlistę pięciu działów i CR/LF
nagłówków. Message-ID jest zapisany przed próbą. Błąd przed DATA dopuszcza ręczny
retry tej samej zgody. Utrata potwierdzenia po możliwym DATA blokuje retry.
Pozytywny ACK DATA oznacza sent nawet przy błędzie QUIT. Brak exactly-once między
SMTP i SQLite. Niepewny przypadek trzeba sprawdzić w Mailpit po Message-ID.

## Dane i eksploatacja

Trwały wolumen zawiera oryginały, zgody i dane sesji. Nie ma automatycznej retencji,
szyfrowania na poziomie aplikacji ani publicznego systemu tożsamości. Zachowaj
kontrolę dostępu do hosta i wolumenu, używaj danych syntetycznych w testach.
Nie kopiuj bazy do repo ani obrazu. Root kontenera jest read-only; /data jest
osobnym wolumenem zapisywalnym przez nieuprzywilejowanego appuser.

Ograniczenie wielkości requestu i liczby równoległych propozycji ogranicza zużycie
zasobów, ale lokalny operator nadal może zapełnić dysk. Aplikacja nie jest usługą
wielu niezaufanych użytkowników. Publiczny dostęp wymaga osobnego projektu i decyzji.

## Weryfikacja

Testy obejmują granice HTTP, izolację sesji, wersję/digest, wyścigi, późny model,
blokadę procesu, recovery i fazy SMTP. Testy z atrapą nie są realnym dostarczeniem.
Lokalny odbiór z Mailpit i odtworzeniem kontenera jest raportowany oddzielnie.
Zgłaszanie podatności opisuje [SECURITY.md](../SECURITY.md).
