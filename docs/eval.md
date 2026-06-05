# Wyniki ewaluacji routingu

Ten dokument zawiera realny, datowany przebieg `scripts/routing_accuracy_check.py`
wraz ze środowiskiem i wersją/digestem modelu. To **diagnostyka jakości**, a nie
bramka CI (uzasadnienie w sekcji "Ograniczenia").

## Środowisko

| Element | Wartość |
|---|---|
| Data | 2026-06-04 |
| Host | Windows + Docker Desktop (backend WSL2) |
| GPU | NVIDIA GeForce RTX 3080 (10 GB) |
| Obraz / serwer Ollama | `ollama/ollama:0.24.0` |
| Model | `qwen2.5:3b` |
| Digest modelu | `sha256:357c53fb659c5076de1d65ccb0b397446227b71a42be9d1603d46168015c9e4b` |
| Rodzina / parametry / kwantyzacja | qwen2 / 3.1B / Q4_K_M |
| Dekodowanie | `temperature=0`, `OLLAMA_NUM_PREDICT=512` |

## Metodologia

`scripts/routing_accuracy_check.py` wysyła oznaczone wiadomości do działającego API
i raportuje, do którego działu trafiła każda z nich. Trzy części:

- **In-prompt (11)** - wiadomości pokrywające się z few-shotami w promkcie. Wysoki
  wynik sprawdza tu głównie memoryzację, nie generalizację. Zostawione jako sanity-check.
- **Held-out (14)** - wiadomości **nieobecne w promkcie** (inne sformułowania, żargon,
  literówki, nowe intencje). To **właściwy sygnał generalizacji**. Rozdzielność
  held-out od promptu pilnuje test
  `tests/test_routing_accuracy_script.py::test_held_out_cases_do_not_overlap_prompt`
  (nawet modulo polskie znaki).
- **Boundary (5)** - celowo niejednoznaczne styki (HR vs kadry, help-desk vs IT);
  akceptowany jest każdy odbiorca ze zbioru, nigdy nie liczy się jako porażka.

Uruchomiono **3 przebiegi pod rząd**. Wiadomości są ASCII (czysty wydruk na każdej
konsoli); model routuje po intencji, nie po dosłownych słowach.

## Wyniki

| Przebieg | In-prompt (memoryzacja) | **Held-out (generalizacja)** | Boundary |
|---|---|---|---|
| 1 | 11/11 | **13/14** | 5/5 |
| 2 | 11/11 | **13/14** | 5/5 |
| 3 | 11/11 | **13/14** | 5/5 |

**Headline: 13/14 (≈93%) na zbiorze held-out**, 0 fallbacków, identycznie w 3
przebiegach (spójne z `temperature=0`; bez ustawionego seeda).

### Jedyny powtarzalny błąd (held-out)

```
[MISS] expected=other@example.com  got=help-desk@example.com
       :: "Gdzie moge zaparkowac rower na terenie biura"
```

To **broniona pomyłka**: parking rowerowy to sprawa administracyjno-biurowa
(`other@`), ale model potraktował "na terenie biura" jako problem lokalnego
stanowiska i wybrał `help-desk@`. Prompt jawnie zniechęca do nadużywania `other@`
("Do not route to other@ merely because the message is short/informal"), więc
model woli konkretny dział - to spójny bias, nie losowy błąd. Ilustruje granicę
małego modelu: rozróżnienie "sprawy biurowe" vs "stanowisko pracy" bywa płynne.

### Transkrypt held-out (identyczny w 3 przebiegach)

```
[HIT]  kadry@        :: Od kiedy nalicza sie ekwiwalent za niewykorzystany urlop
[HIT]  kadry@        :: Prosze o korekte liczby nadgodzin w grafiku za maj
[HIT]  kadry@        :: Czy moj aneks do umowy jest juz podpisany
[HIT]  human-res@    :: Chcialbym polecic znajomego na otwarty wakat
[HIT]  human-res@    :: Jak zapisac sie na szkolenie z przywodztwa
[HIT]  human-res@    :: Czy karta sportowa obejmuje osobe towarzyszaca
[HIT]  help-desk@    :: Mysz bezprzewodowa nie reaguje po wymianie baterii
[HIT]  help-desk@    :: Zaciecie papieru w drukarce na trzecim pietrze
[HIT]  help-desk@    :: Drugi monitor pokazuje tylko czarny ekran
[HIT]  it@           :: Po resecie hasla nadal dostaje access denied w CRM
[HIT]  it@           :: Potrzebuje uprawnien do repozytorium git zespolu
[HIT]  it@           :: Integracja z API platnosci zwraca blad 401
[MISS] other@ -> help-desk@ :: Gdzie moge zaparkowac rower na terenie biura
[HIT]  other@        :: Mam pytanie o parking dla gosci
```

Boundary: 5/5 rozstrzygnięte w akceptowalnym zbiorze (model konsekwentnie wybiera
"miękką" stronę styku: HR, help-desk).

## Jak odtworzyć

Przy działającym stacku (`docker compose up -d`):

```bash
docker compose run --rm tools scripts/routing_accuracy_check.py
# lub z Pythonem 3 na hoście, z katalogu repo:
python3 scripts/routing_accuracy_check.py
```

Provenance środowiska:

```bash
curl http://localhost:11434/api/version
curl http://localhost:11434/api/tags   # name, digest, details
```

## Ograniczenia (świadome)

- **Mały, ręcznie dobrany zbiór** (14 held-out + 11 in-prompt + 5 boundary). To
  smoke-test jakości, nie benchmark statystyczny - bez przedziałów ufności.
- **Pojedynczy host i model.** Dotyczy `qwen2.5:3b @ Ollama 0.24.0` na RTX 3080;
  inny `OLLAMA_MODEL` wymaga osobnego przebiegu i własnego wpisu.
- **`temperature=0`, bez seeda.** Zmienność niska (3/3 identyczne), ale pełnej
  determinacji dekodowania nie gwarantuję.
- **To nie jest bramka CI.** Trafność LLM jest niedeterministyczna; CI pilnuje
  deterministycznych zabezpieczeń (enum, CRLF, fallback, walidacja). Kierunek
  produkcyjny: nightly eval z większym, wersjonowanym datasetem i seedami.
- **Model nie jest dostrojony pod produkcyjną trafność** - to lekki model lokalny
  dobrany pod wąskie zadanie tool-call.

Powiązane: `README.md` ("Sprawdzenie trafności routingu", "Decyzje
architektoniczne") oraz `docs/security.md`.
