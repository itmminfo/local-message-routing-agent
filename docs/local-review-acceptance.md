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
