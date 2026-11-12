# Audyt aplikacji Adult Hub

**Data:** 12 listopada 2026  
**Wersja:** 0.4.15  
**Przeanalizowany commit:** `45ac75e` (`main`, `origin/main` w chwili rozpoczęcia audytu)  
**Zakres:** poprawność funkcjonalna, niezawodność, wydajność, architektura, jakość kodu, testy, konfiguracja i dokumentacja.  
**Poza zakresem:** kwestie bezpieczeństwa — zgodnie z poleceniem nie analizowano ich i nie oceniano ich.

## 1. Podsumowanie wykonawcze

Adult Hub jest rozbudowaną, spójną architektonicznie aplikacją Telegram–PostgreSQL–Jellyfin z wyraźnym podziałem na resolver, synchronizację i kod współdzielony. Najsilniejszymi stronami są obszerna automatyczna walidacja, trwała kolejka zadań, mechanizmy odzyskiwania po restartach oraz ostrożne reguły usuwania i publikowania danych.

Nie stwierdzono krytycznej awarii głównego przepływu danych w analizie statycznej ani w uruchomionych testach. Zidentyfikowano natomiast **2 problemy o wysokim priorytecie, 5 średnich i 3 niskie**. Najważniejsze z nich dotyczą niezawodności odczytu stanu aplikacji oraz ograniczenia czasu pracy żądań sieciowych. Dodatkowo główne lokalne polecenie walidacyjne `make test` obecnie kończy się błędem mimo poprawnych wyników właściwych testów.

| Obszar | Ocena | Najważniejsza uwaga |
|---|---|---|
| Poprawność i spójność danych | Dobra, z lokalnymi usterkami | Błąd zapytania do bazy może zostać przedstawiony jako poprawny, pusty wynik |
| Niezawodność strumieniowania | Dobra struktura, niepełne ograniczenia czasu | Retry jest ograniczone liczbą prób, ale nie kończy zawieszonego bloku |
| Wydajność | Wymaga pomiarów na większym katalogu | Status wykonuje zbędne odczyty; wyszukiwanie i historia zadań mają koszty rosnące z danymi |
| Jakość testów | Bardzo dobra liczba testów, słabsza bramka uruchomieniowa | 2264 testy Python przechodzą, lecz `make test` kończy się błędem |
| Utrzymanie | Dobry podział modułów, wysoki koszt fasad i dokumentacji | Dokumentacja zawiera nieaktualne wartości domyślne |
| Obserwowalność | Dobre podstawy | Brakuje metryk limitów czasu, wyniku odświeżania statusu i planów zapytań na danych docelowych |

**Rekomendowana kolejność:** A-01 → A-02 → A-03/A-04 → pomiary A-05/A-06 → A-07/A-08/A-09/A-10.

## 2. Metoda, zakres i ograniczenia

Audyt obejmował:

- statyczną analizę ścieżek resolvera, Admin v2, pobierania Telegramu, kolejek, synchronizacji, migracji, publikacji NFO/STRM i retencji;
- kontrolę konfiguracji, zależności, CI, dokumentacji i skryptów pomocniczych;
- uruchomienie testów jednostkowych/integracyjnych na skonfigurowanej bazie testowej PostgreSQL;
- uruchomienie testów kontraktowych JavaScript, składni JS, Ruff i ESLint;
- małe, lokalne reprodukcje zachowania kodu bez modyfikacji aplikacji i bez kontaktu z produkcyjnymi usługami Telegramu/Jellyfin.

Nie wykonano:

- testu na żywym koncie Telegramu, rzeczywistym kliencie Jellyfin ani w pełnej przeglądarce;
- testu obciążeniowego ani `EXPLAIN ANALYZE` na reprezentatywnym dużym katalogu produkcyjnym;
- budowy obrazów Docker i testu wdrożenia podczas tego audytu (CI zawiera taki smoke test, ale nie był tu powtarzany);
- pomiarów na produkcyjnych danych, ponieważ nie były one potrzebne do potwierdzenia wskazanych usterek.

Z tego powodu usterki potwierdzone kodem lub reprodukcją są oznaczone jako **potwierdzone**, a wpływ zależny od skali danych lub realnej sieci — jako **ryzyko wymagające pomiaru**.

## 3. Wyniki automatycznych kontroli

| Kontrola | Wynik | Interpretacja |
|---|---|---|
| `python -m unittest discover -s tests` w profilu pełnym | **2264 testy, OK**, 258,924 s | Bardzo dobre zachowanie zestawu na konfiguracji audytowej |
| `make coverage` | **79%**, próg 72% spełniony | 35 384 analizowane instrukcje, 7388 niepokrytych |
| Ruff (`ruff check .`) | **OK** | Brak zgłoszeń |
| ESLint (`resolver/static/admin-v2`) | **OK** | Brak zgłoszeń |
| `node --check` dla plików Admin v2 | **24 pliki, OK** | Składnia poprawna |
| 27 pliki `tests/**/*.cjs` uruchomionych bezpośrednio | **27 zakończonych powodzeniem** | Sama zawartość kontraktów przechodzi |
| `make ui-contracts` | **Błąd wyjścia 2** | Fałszywy negatyw w regule Makefile; szczegóły A-03 |
| `make proxy-origin` | **Błąd importu** | Problem uruchomienia skryptu; szczegóły A-04 |
| `python -m tests.proxy_origin_contract` | **3 testy, OK** | Sam kontrakt jest poprawny |

Wynik 79% jest miarą pokrycia wykonania, nie dowodem poprawności wszystkich scenariuszy. Przykładowo niskie pokrycie występuje w `resolver/video_duplicate_merge_history.py` (11%), `resolver/providers/telegram_history.py` (18%), `sync/scene_scraper_text_sources.py` (11%), `sync/video_visual.py` (17%) i `resolver/catalog_api.py` (40%). Nie oznacza to samo w sobie błędu, ale wskazuje miejsca na testy zachowawcze, a nie tylko podnoszenie procentu.

## 4. Ustalenia priorytetowe

### A-01 — Wysoki: błąd odczytu bazy jest przedstawiany jako prawidłowy brak niedostępnych filmów

**Dowód:** `resolver/admin_data.py:331-346,378-379`, `resolver/admin.py:85-113`, `resolver/admin_v2_issue_database.py:106-117`.

`unavailable_videos()` inicjalizuje pusty wynik z `available=True`. Po połączeniu z bazą każdy `db.OperationalError` — również podczas introspekcji schematu, zliczania lub pobierania rekordów — wraca do tej wartości. Lokalna reprodukcja z `table_columns()` zgłaszającym `OperationalError` zwróciła:

```text
{'videos': [], 'total': 0, 'truncated': False, 'offset': 0, 'available': True}
```

Nie chodzi o zamierzoną zgodność z brakującymi starszymi tabelami/kolumnami, lecz o utratę rozróżnienia między „brak problemów” a „nie udało się sprawdzić”.

**Wpływ:** Home może pokazać zero niedostępnych filmów i `unavailable_available=true`. W tym przypadku centrum problemów pomija komunikat o niedostępnych pozycjach, chociaż nie udało się go obliczyć. Ten sam wzorzec występuje w `quarantine_report()` (`resolver/admin_data.py:388-429`), gdzie błąd zapytania może wyglądać jak pusta kwarantanna.

**Rekomendacja:** wszystkie błędy operacyjne po otwarciu połączenia zwracać jako `available=False`, z pustymi polami danych i logiem diagnostycznym; zachować osobny, jawny rezultat tylko dla rzeczywiście nieobsługiwanego schematu. Dodać testy błędu introspekcji i błędu zapytania głównego dla obu funkcji oraz test propagacji do statusu Home.

### A-02 — Wysoki: pojedyncze pobranie Telegramu nie ma aplikacyjnego deadline’u

**Dowód:** `resolver/telegram_proxy.py:215-248`, `resolver/providers/telegram_download.py:110-157,207-216`, `resolver/telegram_stream.py:288-309,489-496`, `resolver/providers/telegram_retry.py:97-120`, `resolver/providers/telegram_router.py:271`.

Żądania strumienia nie są objęte ogólnym limitem czasu. Sekwencyjne `iter_download` i równoległe `_download_block` oczekują na zakończenie pobierania. `retry_call()` ogranicza liczbę prób i czas oczekiwania **między** nimi, lecz nie czas jednej zawieszonej próby. W trybie równoległym czytelnik czeka na `job.future` również bez własnego deadline’u. Klienci Telethon są tworzeni bez jawnych `connection_retries`, `request_retries` i `flood_sleep_threshold`.

**Wpływ:** przy zerwanym połączeniu, długim oczekiwaniu upstream lub nieskończonej sekwencji zdarzeń pojedyncze żądanie może pozostawać aktywne bardzo długo. Może to utrzymać zasoby klienta i workerów. Dotyczy to również ścieżki historii (`resolver/providers/telegram_history.py:43-73`), a następnie czasu synchronizacji kanału.

**Rekomendacja:**

1. wprowadzić jawne, konfigurowalne i ograniczone `connection_retries`, `request_retries` oraz `flood_sleep_threshold` dla wszystkich klientów Telethon;
2. dodać limit czasu pobrania pierwszych metadanych i osobny limit dla pojedynczego bloku, również w `manager._execute_job()`;
3. po timeoutcie zamknąć iterator i odwzorować błąd na 504/502 przed wysłaniem bajtów; po rozpoczęciu odpowiedzi poprawnie zakończyć strumień;
4. pokryć testem, którego `fetch()` nigdy się nie kończy, sprawdzając powrót błędu w skończonym czasie i brak osieroconych zadań/rezerwacji pamięci.

Deadline nie powinien przerywać legalnego długiego odtwarzania jako całości — powinien dotyczyć nawiązania połączenia, braku postępu i pojedynczej operacji.

### A-03 — Średni: `make test` i `make ui-contracts` odrzucają przechodzący test

**Dowód:** `Makefile:63-78`, `tests/admin_v2_telegram_session_replace.cjs:24`.

Reguła Make akceptuje tylko wyjście zawierające `PASS` lub `passed`. Test kończy się poprawnym komunikatem `admin_v2_telegram_session_replace.cjs: ok`. Bezpośredni `node` zwraca kod 0, ale `make ui-contracts` zwraca 2 i opisuje możliwe zawieszenie Promise. Przez to `make test` nigdy nie dociera do kolejnych kontroli.

**Wpływ:** główna instrukcja z README (`make test`) daje mylący wynik i może zniechęcić do uruchamiania pozostałych walidacji. Obecny CI unika problemu przez wywoływanie osobno `make coverage`, `make proxy-origin`, `make ui-contracts` i `make js-check`, lecz nadal zawiera ten błędny krok kontraktowy.

**Rekomendacja:** ujednolicić marker końcowy testu do `PASS: ...` albo — lepiej dla całego runnera — uznać kod wyjścia 0 za podstawę i wymagać jednoznacznego, jednolitego `PASS` dla każdego skryptu. Dodać mały test samego runnera dla wyniku OK, błędu i zawieszenia. Nie usuwać zabezpieczenia przed brakiem markera.

### A-04 — Średni: `make proxy-origin` nie działa zgodnie z dokumentowanym lokalnym wywołaniem

**Dowód:** `Makefile:8,91-92`, `tests/proxy_origin_contract.py:1-8`, `tests/__init__.py`, `resolver/__init__.py`, `docker-compose.dev.yml:56,62`.

Skrypt uruchamiany ścieżką `tests/proxy_origin_contract.py` ma `sys.path[0]` wskazujący `tests/`, a nie katalog główny repozytorium. Pakiety `resolver` i `common` nie są instalowane przez `make install-dev` (instalowany jest tylko `common`), więc lokalne `make proxy-origin` kończy się `ModuleNotFoundError: No module named 'resolver'`. Uruchomienie jako `python -m tests.proxy_origin_contract` przechodzi wszystkie 3 testy.

**Wpływ:** kolejny krok `make test` jest wadliwy nawet po poprawieniu A-03. Test działa w kontenerze dzięki `WORKDIR` i `PYTHONPATH`, co ukrywa problem lokalny.

**Rekomendacja:** uruchamiać `$(PYTHON) -m tests.proxy_origin_contract` z katalogu głównego. Dodać ten sam sposób uruchomienia do CI, aby testować rzeczywistą lokalną bramkę. Nie wymaga to zmiany kodu aplikacji ani instalowania pakietu resolvera.

### A-05 — Średni: status wykonuje niepotrzebne odczyty bazy co 15 sekund, a zależność od historii zadań rośnie z czasem

**Dowód:** `resolver/static/admin-v2/admin-v2-status.js:57-73`, `resolver/admin_services.py:161-201`, `resolver/admin_v2_status.py:316-341`, `resolver/admin.py:85-113`, `resolver/admin_v2_issue_database.py:65-117`, `resolver/admin_services.py:260-321`.

`loadHeaderStatus()` potrzebuje jedynie `active_total` z odpowiedzi zadań, ale `job_listing()` zawsze wykonuje introspekcję tabel, pobiera wszystkie identyfikatory aktywnych decyzji, analizuje wszystkie oczekujące operacje undo i wykonuje dwa zliczenia — także przy `limit=1`. Po stronie `/api/v2/status` `_attention_summary()` ponawia podobne odczyty sekwencyjnie.

Dodatkowo `/api/v2/issues` używa `unresolved_failed_task_listing()`, które analizuje pełną historię zakończonych zadań i grupuje/przetwarza payloady JSON. Retencja ogranicza je co prawda do miesiąca (`common/history_retention.py:107-145`), lecz nie ustala stałej, małej liczby rekordów. Przy dużej liczbie operacji zapytania będą rosły wraz z aktywnością, a nie tylko z bieżącą liczbą problemów.

**Wpływ:** na większej bazie panel może generować zbędny, cykliczny koszt i obciążenie bazy. Jest to **ryzyko wydajnościowe potwierdzone strukturą zapytań**, nie wynik benchmarku produkcyjnego.

**Rekomendacja:**

- dodać lekki endpoint/zapytanie tylko do liczników aktywnych zadań i użyć go w nagłówku;
- uruchamiać niezależne części `_attention_summary()` równolegle i unikać powtarzania odczytów dostępnych w pozostałych częściach `/status`;
- utrzymywać indeks/tabelę najnowszego wyniku na kanoniczny identyfikator zadania zamiast grupować pełną historię przy każdym odświeżeniu;
- rozdzielić szybki snapshot statusu od pełnego, mniej częstego raportu problemów;
- przed indeksami wykonać `EXPLAIN (ANALYZE, BUFFERS)` na docelowej liczbie zadań i filmów.

### A-06 — Średni: częste zapytania katalogu i jego obrazków mają słabą skalowalność

**Dowód:** `resolver/admin_catalog_queries.py:37-66,199-249,319-331,348-371`, `resolver/admin_data.py:114-147,200-203`, `tools/benchmark_catalog_search.py:1-8,76-111`.

Wyszukiwanie katalogu wykonuje `ILIKE '%term%'` na kilku polach tekstowych i JSONB, z dodatkowymi `COALESCE` i warunkami nadpisań. Takie predykaty nie wykorzystają zwykłych indeksów B-tree dla typowego wyszukiwania z prefiksem `%`. Zliczenie i pobranie strony powtarzają filtrowanie.

Przy filtrze brakującego obrazka `_missing_local_artwork_ids()` pobiera wszystkie zarejestrowane pliki i wykonuje `lstat()` dla każdego. `db_stats()` skanuje katalog obrazków, po czym pobiera wszystkie powiązania `synced_files` z `videos`, aby policzyć pokrycie w Pythonie.

**Wpływ:** czas odpowiedzi może rosnąć liniowo lub gorzej wraz z liczbą filmów i plików. Dokładny priorytet optymalizacji zależy od liczby rekordów i systemu plików. Projekt ma już odpowiednie narzędzie pomiarowe `tools/benchmark_catalog_search.py`; nie uruchamiano go na danych produkcyjnych podczas audytu.

**Rekomendacja:** zebrać medianę/p95 i plany zapytań dla reprezentatywnych zapytań; dopiero potem dobrać `pg_trgm`/indeksy lub osobną projekcję wyszukiwania. Dla artworku utrzymywać stan publikacji w bazie lub wykonywać kontrolę partiami/asynchronicznie i buforować wynik z jawną świeżością. Nie optymalizować schematu „na ślepo”.

### A-07 — Średni: dokumentacja podaje błędne wartości domyślne i historyczne ustalenia jako aktualne

**Dowód:**

- `docs/advanced-configuration.md:67` podaje `TELEGRAM_OSHASH_BACKFILL_PER_RUN=200`, natomiast kod używa 500 (`sync/scene_scraper_fingerprint_backfill.py:14-18`) i 500 dokumentuje również `CONFIGURATION.md:69`;
- `docs/advanced-configuration.md:91` podaje 12 nieudanych prób watchdoga i 2 minuty, natomiast `common/config.py:64` i `resolver/supervisor.py:35-37` używają 3, co przy odstępie 10 s daje około 30 s; poprawna wartość jest w `CONFIGURATION.md:93`;
- `common/config_loader.py:80` ma `jellyfin_people_per_run=50`, a `config.example.yaml:27` rekomenduje 25; użytkownik bez pliku lub bez tego pola otrzyma inną wartość niż sugerowana;
- `docs/configuration-audit.md:7-27,139-168` opisuje stan „przed refaktoryzacją” i zalecenia już zrealizowane (m.in. opcjonalny `config.yaml`, inne domyślne watchdoga), lecz zwykła nazwa pliku sugeruje aktualny audyt.

**Wpływ:** operator może źle oszacować obciążenie synchronizacji i czas reakcji watchdoga albo wprowadzić niewłaściwą zmianę konfiguracji na podstawie dokumentu.

**Rekomendacja:** synchronizować tabelę domyślnych wartości z kodem w teście, oznaczyć stary dokument jako archiwalny z datą stanu, a różnicę 50/25 albo ujednolicić, albo jasno opisać jako świadomą różnicę „wartość wbudowana” vs „wartość rekomendowana w przykładzie”.

## 5. Ustalenia niższego priorytetu

### A-08 — Niski: konfiguracja `shutdown_grace_seconds` nie jest sprzężona z Compose

**Dowód:** `common/config_loader.py:80-84,231-243`, `docker-compose.yml:109-111`, `config.example.yaml:19-20`.

Parametr YAML przyjmuje każdą liczbę całkowitą ≥1. Compose ma stałe `stop_grace_period: 4m`. Wymaganie, aby druga wartość była większa, jest opisane w komentarzu i `AGENT.md`, ale testy sprawdzają działanie samego runnera, a nie parę wartości z efektywnego YAML i Compose.

**Ryzyko:** legalna zmiana `shutdown_grace_seconds` powyżej 240 sekund może spowodować wymuszone zakończenie przed osiągnięciem bezpiecznego punktu synchronizacji. Nie jest to błąd przy bieżącej konfiguracji 180/240 s.

**Rekomendacja:** dodać walidację konfiguracji wdrożeniowej przy starcie lub też test kontraktu odczytujący obie wartości i wymagający marginesu. Dokumentować przykłady zmian obu parametrów razem.

### A-09 — Niski: testy UI są w dużej części kontraktami tekstowymi/sandboxowymi, nie pełnymi scenariuszami przeglądarkowymi

**Dowód:** `tests/helpers/admin_v2_scripts.cjs:1-57`, `tests/admin_v2_telegram_session_replace.cjs:1-24`, `.github/workflows/tests.yml:89-92`.

Pomocnicze skrypty czytają JavaScript z dysku i wykonują go w `vm`. Część testów sprawdza wyrażenia regularne w źródle. To daje szybkie i użyteczne kontrakty, ale nie wykrywa pełnych problemów z renderowaniem, prawdziwymi zdarzeniami przeglądarki, układem strony czy cyklem życia żądań HTTP w produkcyjnym stacku.

**Rekomendacja:** zachować obecne testy jako szybką warstwę i dodać mały zestaw E2E dla krytycznych scenariuszy: Home z błędem bazy, dodanie kanału, kolejka i retry zadania, niedostępny film, wygasanie odpowiedzi podczas przełączania widoków. Nie zastępować 27 istniejących kontraktów kosztownym, niestabilnym zbiorem testów.

### A-10 — Niski: duże fasady i warstwa kompatybilności podnoszą koszt zmian

**Dowód:** `sync/sync.py:1-1122` (76 definicji funkcji i wiele reeksportów), `resolver/providers/telegram_login.py:1-1103`, `common/db.py:106-274,320-468`, `pyproject.toml:5-9`.

Moduły domenowe są już dobrze rozdzielone, ale centralne fasady zachowują historyczne punkty patchowania i dynamiczny `_core`. Adapter bazy dopasowuje zapytania tekstowe przez wyrażenia regularne, aby zachować stare konwencje czasu i JSON. Są testy kompletności fasad (`tests/test_sync_facade_names.py`), lecz każda zmiana nadal wymaga analizy niejawnych zależności i zachowania kompatybilności.

**Rekomendacja:** nie wykonywać dużego refaktoringu jednorazowo. Dla nowych funkcji stosować jawne serwisy i typed interfaces; stopniowo przenosić wywołania z `_core` i zapytań wymagających adaptacji na nowe helpery. Zachować testy fasad do czasu usunięcia publicznych punktów kompatybilności.

## 6. Mocne strony potwierdzone w kodzie i testach

1. **Bezpieczne reguły retencji i porządkowania.** Upkeep jest ograniczony do zarejestrowanych, włączonych i poprawnie zsynchronizowanych źródeł (`sync/sync_orchestration.py:113-137`), z dodatkowym limitem skali usuwania. Testy `tests/test_retention_guard.py` obejmują brak źródła, wyłączenie, błąd synchronizacji i kanały Telegramu.
2. **Publikacja katalogu ma ścieżkę odzyskiwania.** Zapis bazy poprzedza publikację plików, a brak rejestracji `synced_files` pozwala ponowić publikację po awarii (`sync/video_flush.py:96-174,230-267`). Testy obejmują m.in. odmowę nadpisania oraz błąd zapisu systemu plików.
3. **Migracje są wersjonowane i ostrożne.** `common/migrations/runner.py:373-440` stosuje advisory lock, transakcje i historię; odrzuca niejednoznaczny stan starego schematu (`:306-357`). `tests/test_migrations.py` porównuje ścieżkę aktualizacji z baseline.
4. **Kolejki zadań mają mechanizmy własności i odzyskiwania.** Wspólna kolejka używa tokenów dzierżawy i `SKIP LOCKED` (`common/job_queue.py:242-281,303-378`); pHash ma limit prób i odzyskiwanie wygasłych dzierżaw (`common/phash_jobs.py:239-315`).
5. **Kod radzi sobie z restartem połączenia PostgreSQL.** Warstwa `common/db.py:369-450` ponawia tylko bezpieczne przypadki poza transakcją; scenariusze są sprawdzone w `tests/test_db_reconnect.py`.
6. **Retry nie psuje strumienia po rozpoczęciu odpowiedzi.** `resolver/providers/telegram_retry.py:67-95` ponawia tylko przed pierwszym przekazanym bajtem; równoległe pobieranie ponawia cały blok przed dostarczeniem.
7. **Cache strumienia ma sprawdzony limit pamięci.** Nie potwierdzono przekroczenia limitu w lokalnej kontroli `_evict_unpinned_locked()`; istniejące testy obejmują również przypadek `max_cache_bytes == block_size` i wiele wolnych strumieni.
8. **Synchronizacja ma współpracujące zatrzymanie.** Orkiestracja przerywa na granicach etapów i kanałów (`sync/sync_shutdown.py`, `sync/sync_orchestration.py:15-40,237-255`).
9. **Konfiguracja źródeł jest wersjonowana i poprzedzana podglądem.** `resolver/admin_v2_sources.py` używa `expected_revision`, obsługuje konflikty oraz kompensację przy nieudanym zakolejkowaniu usunięcia kanału.
10. **Testy są szerokie i obejmują wyścigi oraz błędy.** Oprócz testów jednostkowych istnieją kontrakty na przestarzałe odpowiedzi UI, paginację, anulowanie i blokady; zarówno Ruff, jak i ESLint przechodzą czysto.

## 7. Plan działań

### Natychmiast — 1–2 dni

- **A-01:** poprawić semantykę `available` w odczytach niedostępnych filmów i kwarantanny; dodać testy błędów.
- **A-03, A-04:** naprawić uruchamianie obu poleceń i dodać ich wynik jako jeden wspólny gate lokalny.
- **A-07:** poprawić wartości dokumentacji i oznaczyć historyczny audyt konfiguracji jako archiwum.

### Krótki termin — kolejny sprint

- **A-02:** wdrożyć timeouty i jawne limity Telethon wraz z testami zawieszonego bloku; obserwować timeouty w metrykach.
- **A-05:** wprowadzić lekkie zapytania liczników i usunąć pełne odczyty pomocnicze z cyklicznego nagłówka.
- **A-06:** wykonać benchmark katalogu i statusu na rzeczywistej skali; zapisać medianę/p95 przed i po zmianach.

### Średni termin

- **A-08:** dodać test zgodności czasu zatrzymania YAML/Compose.
- **A-09:** dodać 4–6 krytycznych scenariuszy E2E bez zastępowania obecnych szybkich kontraktów.
- **A-10:** ograniczać wzrost fasad, wprowadzając nowe funkcje w modułach domenowych z jawnymi zależnościami.

## 8. Kryteria odbioru rekomendowanych zmian

- Błąd PostgreSQL w odczycie niedostępności nigdy nie daje `available=true` ani pozornego zera problemów.
- Zawieszone pobieranie metadanych/bloku kończy się błędem w konfigurowanym czasie, zwalnia zasoby i pozostawia poprawny stan managera.
- `make test` zwraca 0 po poprawnych testach i niezera przy rzeczywistym błędzie lub braku markera.
- `make proxy-origin` przechodzi w świeżym lokalnym środowisku przygotowanym zgodnie z README.
- Nagłówek nie uruchamia odczytów decyzji/undo potrzebnych tylko do pełnej listy zadań.
- Raport benchmarku przed/po zawiera liczbę rekordów, medianę i p95 zapytań katalogu oraz centrum problemów.
- Tabela domyślnych wartości jest zgodna z kodem i nie zawiera dwóch sprzecznych odpowiedzi dla tej samej konfiguracji.

## 9. Ocena końcowa

**Architektura i baza testowa są dobre.** Projekt nie wymaga gruntownej przebudowy. Największy zwrot przyniosą małe, ukierunkowane poprawki wiarygodności stanu, ograniczenia czasu pracy sieci oraz naprawa lokalnej bramki walidacji. Optymalizacje zapytań i plików powinny być prowadzone po pomiarach na realnej skali katalogu, z wykorzystaniem istniejącego narzędzia benchmarkowego.
