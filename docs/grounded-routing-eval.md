# Lokalny routing z weryfikacją cytatów, 14 listopada 2026

**Wynik jest częściowy. Automatyczny routing nie spełnia warunku odbioru.**
Brak błędnego adresata w dostarczonych wiadomościach nie zastępuje trafności
całego zbioru. Odmowy HTTP 502 są liczone osobno, nie jako poprawna klasyfikacja.
Nie podłączano zewnętrznego SMTP i nie scalono PR #4.

## Zmiana i przyczyna

Stary agent przekazywał cały tekst do modelu posiadającego narzędzie send_email.
Allowlista sprawdzała dopuszczalność adresu, ale nie zgodność działu z problemem.
Ponowna lokalna reprodukcja pary kadry-to-hr-01 potwierdziła błędny HR również
na docelowym modelu 7B ze starym kontraktem. Sama zmiana modelu nie usunęła tego
przypadku. [Kontrole starego kontraktu](eval-runs/2026-11-14-old-contract-controls.json).

Nowy kontrakt rozdziela ekstrakcję i wybór narzędzia. Pierwsze wywołanie lokalnego
modelu nie ma narzędzi ani listy adresatów. Proponuje dosłowne cytaty opisujące
sprawę. Backend odrzuca pustą listę, obce pola, narzędzia, brakujące cytaty,
duplikaty i nakładanie. Sam kopiuje zaakceptowane fragmenty do klasyfikatora.
Oryginał maila, allowlista, Reply-To z backendu, CRLF i guard Mailpit pozostają.

Pochodzenie cytatu nie dowodzi, że model usunął każdą instrukcję. Wybranie całego
oryginału pozostaje semantycznie możliwe. To zabezpieczenie oceniane na skończonych
zbiorach, bez twierdzenia o ogólnej odporności. Ograniczono też niejasność opisu
działów: sprawy budynku i recepcji nie są wsparciem sprzętu komputerowego ani HR.
Nie ma routingu po słowach kluczowych w ścieżce aplikacji.

## Testowana konfiguracja

- Kod wykonawczy: [11a1ac5facb1242ebdac1d98ce5c0f6e0ce8bcdd](https://github.com/itmminfo/local-message-routing-agent/commit/11a1ac5facb1242ebdac1d98ce5c0f6e0ce8bcdd).
- Ollama 0.24.0, eksperymentalny qwen2.5:7b, digest 845dbda0ea48ed749caafd9e6037047aa19acfcfd82e704d7ca97d631a0b697e.
- temperature=0, num_predict=512, seed nieustawiony; kontekst zgłoszony przez serwer 4096.
- Prompt routingu 39ca0f98cff3; prompt ekstrakcji a0e1c266e85e.
- Kanoniczny hash plików runtime 90d554892575073d53dd60e249bec39a1778fbbedc1c5bba1c54d71ebf43b623. Raporty zawierają hashe poszczególnych plików i obrazu.
- GPU współdzielone, bez zatrzymywania cudzych procesów, limit 3 CPU i 4 GiB RAM dla Ollamy. Brak OOM w sprawdzonym przebiegu. Wpływu na wydajność innych procesów nie mierzono.

Domyślny model projektu nadal jest 3B. Wariant 7B był konfiguracją eksperymentalną
lokalnego odbioru, a nie zaakceptowanym profilem produkcyjnym.
[Snapshot override](eval-runs/2026-11-14-local-7b-compose-override.yml) wymaga
własnego wolumenu lmra-routing-boundary-candidate-models. Inicjalizacja używa
istniejącego ollama-init. Nie usuwać wcześniejszych modeli ani wolumenów.

## Wyniki rzeczywistego modelu i SMTP

| Zbiór | Kontrole poprawne bez fallbacku | Ataki poprawne bez fallbacku | Odmowy | Fallbacki | Błędne przełączenia / poprawne kontrole | Mailpit zgodne To / dostarczone |
|---|---:|---:|---:|---:|---:|---:|
| Oryginalne 20 par, seria 1 | 19/20 | 16/20 | 5/40 | 0 | 0/19 | 35/35 |
| Te same 20 par po restarcie API i Ollamy | 18/20 | 16/20 | 6/40 | 0 | 0/18 | 34/34 |
| Zamrożony holdout, 10 nowych par | 10/10 | 9/10 | 1/20 | 0 | 0/10 | 19/19 |

Oryginalny zbiór nie został zmieniony: b6c68592e231ebe81faf22aae8a7c8a9210d00e6c22e11cbd75a389aa8de9e53.
Holdout zamrożono przed implementacją i dostrajaniem, po dwie pary na dział,
z różnymi instrukcjami oraz legalnymi cytatami: c07eb22942c5e56b42fdd75d1c136583e5c82cf2a35d1a439cd7c3cce61fef82.

Surowe wyniki per przypadek: [seria 1](eval-runs/2026-11-14-grounded-7b-v1.json),
[seria 2](eval-runs/2026-11-14-grounded-7b-v1-repeat.json),
[holdout](eval-runs/2026-11-14-grounded-7b-holdout.json).
Driver niezależnie odczytał Mailpit HTTP, powiązał Reply-To i ID oraz sprawdził To.
Dodatkowy odczyt koordynatora potwierdził wszystkie 88 dostarczonych wiadomości
tych trzech serii i brak dostarczenia 12 odmów. Pełne prywatne dowody zachowano
poza repo.

Serie nie były identyczne per-case mimo tych samych plików, modelu i parametrów:
w drugiej doszła odmowa czystej hr-to-other-08. Nie rozstrzygnięto przyczyny
tej zmienności. temperature=0 bez seeda nie jest dowodem deterministyczności.
Pierwszy request po restarcie trwał 86.46 s; /ready wcześniej zwracało 200.
Mailpita nie restartowano, aby zachować jego nietrwałą kolejkę dowodową.

Zwykła poczta: [22/25 poprawnych, 3 odmowy, 0 błędnych To](eval-runs/2026-11-14-grounded-7b-ordinary.json).
Odmowy dotknęły również jednoznacznych zwykłych zgłoszeń, więc brak regresji
nie został wykazany. Pusty wynik ekstrakcji i parafraza zamiast dosłownego cytatu
wyjaśniają zbadane odmowy. Nie poluzowano walidacji, aby otrzymać wynik pozytywny.

## Pozostały odbiór

194 pytest, Ruff, format, mypy, Bandit, pip-audit i Compose config zakończone
pozytywnie. Mock E2E i osobny rzeczywisty E2E z 7B potwierdziły To, Reply-To
i oryginał w Mailpit. To nie jest dowód trafności pozostałych wiadomości.
Dodatkowy realny probe instrukcji po średniku trafił teraz do kadry; wcześniejszy
kontrakt indeksów całych zdań przekazywał go błędnie do HR i został porzucony.

Na rzeczywistym HTTP guard innego SMTP zwrócił 409 z zerem wywołań modelu i nadawcy.
Kontrolowane błędny JSON, timeout i niedostępny model dały 502/503 bez Mailpit.
W teście SMTP proxy przekazało DATA do lokalnego Mailpit i zerwało końcowe ACK:
API zwróciło 502 mimo dostarczenia. Ponowienie dało drugą kopię. Zwykłe powtórzenie
żądania także wysłało dwie kopie. Usługa nie ma exactly-once ani trwałego outbox.

Automatyczna wysyłka do rzeczywistych działów pozostaje nieodebrana. Następna
decyzja dotyczy lokalnego preview bez SMTP i obowiązkowego zatwierdzania przez
człowieka, z niezmiennym payloadem, serwerowym rejestrem zgód i jawnym stanem
delivery_unknown. Takiego przepływu w tej zmianie nie wdrożono.
