# Code review — Adult Hub

**Data przeglądu:** 27 listopada 2026  
**Wersja:** 0.6.12  
**Przeanalizowany commit:** `bb0894f5` (`main`). Wybrane ustalenia doprecyzowano na podstawie statycznego sprawdzenia kodu w `60c5976c`.  
**Zakres przeglądu:** `common/`, `resolver/`, `sync/`, testy, Compose, skrypty i narzędzia.

## Zadanie dla agenta kodującego

Rozlicz **wszystkie 37 pozycji** poniżej, także dotyczące bezpieczeństwa, dokumentacji i higieny repozytorium. Kolejność jest dowolna. Identyfikatory K i W zachowano z pierwotnego raportu. Sprawdź każde ustalenie na bieżącym kodzie, popraw potwierdzone usterki i zweryfikuj poprawkę. Gdy ustalenie okaże się nieaktualne lub błędne, podaj dowód z kodu albo wynik testu. Dla pozycji zbiorczych rozlicz każdy podpunkt.

Bieżąca instancja jest używana przez jedną osobę przez LAN i Tailscale. Ustalenia o publicznej domenie dotyczą konfiguracji z publicznym proxy. Dostępność działającej instancji z internetu nie była sprawdzana. Zachowaj prywatny dostęp do panelu, działanie klientów i wywołania między usługami. Udokumentuj wymagane zmiany konfiguracji dla istniejących instalacji. Weryfikację migracji, usuwania i scenariuszy awarii przeprowadź na danych testowych z jednorazowym PostgreSQL.

Pierwotny przegląd raportował przejście `ruff check` i testów dymnych bez bazy. Pełnego zestawu testów nie uruchomiono z powodu braku jednorazowego PostgreSQL (`TEST_DATABASE_URL`). Szacunki pokrycia pochodzą z artefaktu z 24 listopada 2026. Po zmianach podaj rzeczywiście uruchomione sprawdzenia i ich wyniki. Raport nie zleca wdrożenia.

## Wdrożenie, dostęp, katalog i streaming

### K-1. Import migracji w obu wariantach standalone

`compose.standalone.yml:39`, `release/standalone/docker-compose.yml:34`, `sync/Dockerfile`

Polecenie migracji importuje `db_schema` z poziomu `/app`, podczas gdy obraz umieszcza moduł w `/app/sync`. Resolver i sync czekają na pomyślne zakończenie migracji. Popraw oba pliki, korzystając z działającego wzorca w `docker-compose.dev.yml`. Zweryfikuj składnię obu konfiguracji oraz rzeczywiste wykonanie komendy migracji w odpowiednim obrazie. Samo `docker compose config` nie wykryje błędu importu.

### K-2. Domena publiczna w głównym Compose

`docker-compose.yml`, `resolver/admin.py:61`, `resolver/admin_security.py`, `common/config.py`

Główny Compose nie przekazuje resolverowi `PUBLIC_DOMAIN`. W środowisku z publicznym proxy resolver może użyć domeny przykładowej i nie rozpoznać domeny skonfigurowanej na proxy. Przekaż ustawienie do kontenera i sprawdź panel, API admina oraz `/metrics`. Zweryfikuj blokadę na skonfigurowanej domenie przy `ADMIN_PUBLIC=false`, dostęp prywatny bez domeny oraz zachowanie po świadomym włączeniu `ADMIN_PUBLIC`. Walidacja konfiguracji publicznej powinna uwzględniać działające instalacje prywatne bez publicznej domeny.

### W-1. Zapisy administratora przy pustym tokenie

`resolver/admin_security.py:46`

Pusty `ADMIN_WRITE_TOKEN` pozwala zapisywać po sprawdzeniu zgodności `Origin`. Wymagaj prawidłowego, niepustego tokena i zachowaj kontrolę `Origin`. Zaktualizuj stan możliwości panelu i instrukcje konfiguracji. Sprawdź zapis z poprawnym tokenem oraz odmowę przy braku tokena, błędnym tokenie lub niedozwolonym `Origin`.

### W-2. Próg 25% czyszczenia osieroconych wpisów Telegrama

`sync/library_cleanup.py:101`, `sync/provider_registry.py:82`, `AGENT.md`

Próg jest liczony dla całego Telegrama. Jeżeli mały kanał zaliczy synchronizację jako udaną, a jego pliki znikną, plan usunięcia całego kanału może zmieścić się w limicie całego providera. Grupuj populację i plan usunięć według tożsamości źródła. Uwzględnij historyczne identyfikatory kanałów i istniejące bramki czyszczenia. Sprawdź mały kanał bez plików, zwykłe czyszczenie poniżej progu oraz ochronę kanału bez udanej synchronizacji.

### W-3. Filtrowanie i wygaszanie denylisty po zapisach partii

`sync/dead_streams.py:233`, `sync/sync_orchestration.py:351`, `sync/telegram_pipeline.py`, `sync/video_flush.py:170`, `resolver/client_catalog.py:909`

Końcowy filtr dostaje `sp.collected` po zapisach stron i kanałów, a udany flush opróżnia ten zbiór. Filtrowanie przed publikacją i wygaszanie wpisów mogą więc zostać pominięte. Katalog klienta nadal osobno wyklucza rekordy obecne w `dead_streams`. Sprawdź ponowny zapis i publikację wpisu z denylisty w zwykłym syncu i przy ponawianiu publikacji. Oprzyj wygaszanie na wiarygodnych danych o sprawdzonym źródle. Przetestuj zachowanie przy niepełnej synchronizacji, wygaśnięcie według ustalonej polityki oraz powrót dostępnego filmu. Brak ID na jednej stronie nie dowodzi zniknięcia filmu.

### W-4. Kontrola dostępu do wewnętrznych tras Telegrama

`resolver/admin_v2_telegram_auth.py:455`

Trasy `/admin/internal/telegram/auth/status` i `/admin/internal/telegram/auth/any` zwracają stan kont bez własnej bramki dostępu. Główny Compose publikuje port resolvera na hoście. Ustal wszystkich klientów tych tras, dodaj kontrolę dostępu zgodną z granicą usług i sprawdź zarówno autoryzowane wywołania między usługami, jak i odmowę dla pozostałych klientów.

### W-5. Synchroniczne odczyty PostgreSQL podczas obsługi streamingu

`resolver/providers/telegram_account_store.py:151/218/644`, `resolver/providers/telegram_router.py:407`

Wybrane operacje doboru konta i dostępu wykonują synchroniczne odczyty na pętli zdarzeń i otwierają zwykłe połączenia z bazą. Częstotliwości wywołań i opóźnień przewijania nie zmierzono. Prześledź ścieżki odtwarzania i żądań Range, przenieś blokujące operacje poza pętlę i użyj istniejącej puli tam, gdzie służy temu przepływowi. Przy symulowanym opóźnieniu bazy sprawdź responsywność health checka, poprawność odtwarzania i przewijania oraz aktualność decyzji o dostępie do kanału.

### W-6. Koszt operacji panelu na dużym katalogu

- **Duplikaty.** Walidacja `dismiss` uruchamia pełne wyliczenie kandydatów w osobnym wątku. Limit zatrzymuje skan po znalezieniu określonej liczby par i nie ogranicza liczby porównań przy rzadkich dopasowaniach. `Undismiss` sprawdza rekord operacji. Usuń pełny skan ze ścieżki pojedynczej pary, zachowując wszystkie typy sugestii, w tym te bez zarejestrowanego porównania wizualnego. `resolver/admin_v2_duplicate_routes.py:238`, `resolver/video_duplicates.py:172/480/549`.
- **Brak artworku.** Filtr sprawdza ścieżki z rejestru plików przed liczeniem wyników i paginacją. Zmierz koszt i ogranicz liczbę operacji dyskowych. Zachowaj poprawną liczbę i kolejność wyników oraz pełne strony tam, gdzie jest wystarczająco dużo dopasowań. `resolver/admin_catalog_queries.py:42/209`.
- **Wyszukiwanie i sortowanie.** Panel wyszukuje po efektywnych metadanych i korzysta z innego wyrażenia sortowania niż indeksowana kolumna `title_sort`. Sprawdź plany zapytań przed i po zmianie. Zachowaj uwzględnianie nadpisań, kolejność i stabilną paginację. Pomiary w `docs/catalog-performance.md` dotyczą przede wszystkim ścieżki klienta. `resolver/admin_catalog_queries.py:360`, `resolver/admin_catalog_pagination.py:12`.

## Pozostałe ustalenia

| ID | Obszar i lokalizacja | Zadanie i weryfikacja |
|---|---|---|
| M-01 | Integralność usuwania · `sync/library_cleanup.py:127/179`, `sync/source_removal.py:279` | Porównaj trzy ścieżki usuwania wideo i zastosuj spójną obsługę danych zależnych. Rozróżnij sieroty w `video_sources`, `dead_streams` i `video_duplicate_*` od rekordów zachowywanych celowo do audytu, blokowania lub undo. Sprawdź usunięcie w każdej ścieżce oraz zgodność ze stanem istniejącej bazy. |
| M-02 | Przejmowanie zadań · `sync/duplicate_job_worker.py:39/206` | Sprawdź brak identyfikatora dzierżawy, ponowne kolejkowanie wszystkich wpisów `processing` i brak odpowiedniego CHECK. Uporządkuj własność zadania i odzyskiwanie po awarii. Przetestuj restart, wygaśnięcie i próbę zakończenia zadania przez poprzedniego właściciela. |
| M-03 | Blokada cache · `resolver/stream_cache.py:125` | Przenieś I/O z `get`, `invalidate` i `__len__` poza globalną blokadę, zachowując spójność. Sprawdź operacje współbieżne i opóźniony odczyt. |
| M-04 | Range bez znanego rozmiaru · `resolver/telegram_proxy.py:70/91/258` | Usuń niepoprawną odpowiedź 206 z `Content-Range: bytes X-/*`. Zweryfikuj odpowiedzi dla zakresu otwartego i zamkniętego, rozmiaru znanego i nieznanego oraz zakresu poza plikiem. |
| M-05 | Zakończenie syncu · `sync/sync_orchestration.py:494/662` | Sprawdź ścieżkę wyjątku w retencji i obsługę przerwania przed `library-maintenance`. Zachowaj historię przebiegu i zapis stanu `interrupted` przy zatrzymaniu. |
| M-06 | Kwarantanna a zapis do bazy · `sync/duplicate_job_external_handlers.py:726` | Przeniesienie plików wyprzedza zapis rekordu kwarantanny. Kompensacja błędu podczas samego przenoszenia nie obejmuje późniejszej awarii zapisu w bazie. Zapewnij odzyskiwalność po błędzie zapisu i przerwaniu procesu. Sprawdź ponowne uruchomienie oraz przywrócenie plików po częściowym wykonaniu. |
| M-07 | Bezczynne odpytywanie kolejki · `sync/runner.py:425` | Zmierz tempo otwierania połączeń przy pustej kolejce. Ogranicz liczbę nowych połączeń, zachowując czas podjęcia nowego zadania, reakcję na awarię bazy i szybkość zatrzymania. |
| M-08 | Rate limit za proxy · `common/client_auth/service.py:222`, `resolver/supervisor.py:29` | Sprawdź efektywne zachowanie przypiętej wersji Uvicorn i konfigurację `FORWARDED_ALLOW_IPS`. Brak jawnej flagi w komendzie startu nie przesądza o zachowaniu. Zweryfikuj rozróżnianie klientów za zaufanym proxy i ignorowanie nagłówków z niezaufanego połączenia. |
| M-09 | Docker i konfiguracja · `.dockerignore`, `docker-compose.yml:73/136`, `compose.standalone.yml` | Ogranicz kontekst buildu do potrzebnych plików i sprawdź oba obrazy. Zweryfikuj zachowanie przy braku `config.yaml`, żeby przypadkowy katalog nie prowadził do cichego użycia wartości domyślnych. Sprawdź tagi 0.6.0 używane jako fallback względem wersji wydania i sposobu pakowania. |
| M-10 | Pokrycie testami · `Makefile:61` | Uruchom pełny pomiar i sprawdź próg 72%. Historyczne odczyty wynosiły około 71% łącznie, 49% dla `client_catalog.py`, 39% dla `catalog_api.py` i 11% dla `sync/media_maintenance.py`. Uzupełnij potrzebne testy zachowania i podaj aktualny wynik. |
| M-11 | Cache schematu i zgodność typów · `common/db.py:172/206` | Sprawdź klucz cache kolumn INSERT i izolację między bazami lub schematami. Sprawdź miejsca wymagające przepisywania SQL przy konwersji epoki na `timestamptz`. Utwardź lub zastąp wadliwe ścieżki, zachowując zgodność istniejących zapisów. |
| M-12 | Instrukcje wdrożenia · `INSTALL.md:348`, `AGENT.md:15` | Uzgodnij opis migracji i blokowania startu z zachowaniem runnera oraz zweryfikuj wzmiankę o usłudze `backup` względem aktualnego Compose. |

## Dodatkowe ustalenia

| ID | Lokalizacja | Zadanie i weryfikacja |
|---|---|---|
| L-01 | `sync/duplicate_job_undo.py:302/310` | Sprawdź klucze JSON używane jako identyfikatory SQL podczas undo. Ogranicz je do dozwolonych kolumn i zachowaj obsługę istniejących snapshotów. |
| L-02 | `sync/telegram_unavailable.py:44` | Zastosuj uzgodnioną z resztą aplikacji politykę ścieżek w kwarantannie. Sprawdź symlinki wewnątrz i poza katalogiem mediów. |
| L-03 | `resolver/app.py:273` | Zweryfikuj spójność docelowego adresu między walidacją URL i połączeniem. Test polityki powinien objąć zmianę wyniku resolvera DNS i przekierowania z użyciem atrap. |
| L-04 | `common/backup.py:64/291` | Usuń hasło z argumentów procesu backupu. Sprawdź, że backup nadal działa i że argumenty oraz logi nie zawierają hasła. |
| L-05 | `resolver/admin_v2_phash.py:256` | Ustal, czy `PHASH_UPLOAD_ENABLED` ma obejmować odczyt miniatur pHash. Dostosuj zachowanie do przyjętej polityki i pokryj oba stany flagi. |
| L-06 | `resolver/catalog_api.py:186` | Zwracaj typ MIME zgodny z formatem miniatury. Sprawdź JPEG, PNG i WebP. |
| L-07 | `resolver/app.py:250–274` | Zweryfikuj osiągalność fallbacku Telegram w `parse_composite_id`. Uzgodnij implementację i docstring z kontraktem historycznych identyfikatorów. |
| L-08 | `resolver/app.py:795` | Sprawdź, kiedy `upstream_url_alive` uznaje odpowiedzi 301 i 302 za dowód dostępności, i doprecyzuj kontrakt. Sprawdź przekierowanie do żywego zasobu, do niedostępnego zasobu i pętlę przekierowań. |
| L-09 | `common/db.py:394–467` | `commit()` może po wykryciu zamkniętej sesji ponownie połączyć się z bazą i zwrócić sukces mimo utraty pierwotnej transakcji. Wykaż błąd po utracie sesji między zapisem a zatwierdzeniem, także w menedżerze kontekstu. Zachowaj odzyskiwanie połączenia dla kolejnej niezależnej operacji. |
| L-10 | `common/migrations/runner.py:354` | Sprawdź zakres blokady przy `recreate_database`. Zapewnij serializację od pierwszej operacji niszczącej przez inicjalizację schematu. Sprawdź współbieżność na jednorazowej bazie. |
| L-11 | `sync/data_migrations.py:200` | Zmierz liczbę zapytań migracji kategorii i ogranicz N+1. Zachowaj wynik migracji i możliwość jej ponownego uruchomienia. |
| L-12 | `common/job_queue.py:97` | Ogranicz skanowanie i ponowne parsowanie wszystkich payloadów przy dodawaniu zadania. Zachowaj reguły deduplikacji i działanie przy współbieżnym dodawaniu zadań. |
| L-13 | `uv.lock`, `sync/VERSION`, `resolver/VERSION` | Sprawdź odwołania do pustych plików. Usuń nieużywane i potwierdź prawidłowe źródło wersji w buildzie i wydaniu. |
| L-14 | `Makefile:11` | Usuń zależność fallbacku Ruff od prywatnej ścieżki `$HOME/develop/bin/ruff`. Sprawdź standardowy przebieg lokalny i CI. |
| L-15 | `backup_db.py` | Uzgodnij znaczenie `--keep 0`, zweryfikuj wejście z `BACKUP_KEEP_DAYS` i opisz przyjętą semantykę. Sprawdź retencję na plikach testowych, w tym dla zera i wartości błędnych. |
| L-16 | `common/fuzzy_performers.py:27` | Usuń niejawne sprzężenie przez `_core=globals()` lub zawęź przekazywane zależności. Zachowaj kontrakt fasady, wyniki fuzzy-merge i zgodność testów. |
| L-17 | `sync/telegram_pipeline.py:263`, `sync/video_flush.py:170` | Sprawdź, czy resztki po nieudanym zapisie kanału mogą zostać opublikowane w przebiegu następnego kanału. Zasymuluj awarię kanału A i udany przebieg kanału B. Zachowaj tożsamość partii i możliwość odzyskania wpisów kanału A. |

## Zachowanie, które należy zachować

Przy zmianach uwzględnij istniejące kontrakty uwierzytelniania klientów i przechowywania tokenów, migracji i zgodności schematu, ponawiania zadań, limitów pamięci streamingu, sprzątania po rozłączeniu oraz łagodnego zatrzymania. Wykorzystaj istniejące testy tych kontraktów.

## Rozliczenie prac

Dla każdego ID podaj status, istotne pliki i dowód weryfikacji. Przy ustaleniu niepotwierdzonym podaj ścieżkę lub przypadek, który rozstrzyga wątpliwość. Przy braku środowiska wskaż konkretną blokadę i pozostający test. Oddziel historyczne wyniki przeglądu od wyników uzyskanych po zmianach.
