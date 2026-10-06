# Specifiche di Progetto: Piattaforma Gestione e Ottimizzazione Orari Scolastici

Documento di riferimento **riproducibile**: descrive il sistema così com'è implementato in questo repository
(backend `app/`, frontend `frontend/`, deploy `deploy/`), con formati, formule, pesi e regole abbastanza precisi da
poterlo ricostruire. Dove una funzione è prevista ma non implementata lo si dice esplicitamente (§17).

Convenzioni: i nomi di tabelle, campi, endpoint e costanti sono quelli del codice; le date sono ISO `YYYY-MM-DD`; i
giorni nei dati sono `LUNEDI…VENERDI` (maiuscolo, senza accento) nel database e nelle API dell'orario, `lunedi…venerdi`
(minuscolo) nelle griglie di disponibilità; le ore di lezione sono le fasce `8..13` (08:00-14:00).

---

## 1. Visione, ambito e glossario

Piattaforma web per generare, rivedere e pubblicare l'**orario scolastico settimanale** di un ente di formazione
(Agenzia Formativa, istituti superiori / IeFP). Un solver a vincoli (Google OR-Tools CP-SAT) produce l'orario di una
settimana alla volta partendo da: calendario annuale, anagrafiche, assegnazioni docente-classe-materia, monte ore
residuo e disponibilità dei docenti. L'amministratore rivede il risultato, risolve i conflitti, lo approva e lo esporta
come tabellone PDF/Excel.

**Ambito v1 (implementato):** una sola scuola (single-tenant, `scuola_id = "sch_1"`), accesso con login (ruoli
ADMIN e SEGRETERIA), setup guidato da file, disponibilità docenti, generazione/modifica/approvazione dell'orario,
esportazione PDF ed Excel, apprendimento dei pesi dalle valutazioni dell'amministratore, deploy su una VM con Docker.

| Termine | Significato |
|---|---|
| Classe | Gruppo di studenti, nome `"<numero romano> <corso>"`, es. `II ELETTRICISTI`, `I I.T.C.` |
| Gruppo | Coorte per anno: `PRIME`, `SECONDE`, `TERZE`, `QUARTE`, `PRIMA4+2` (la classe `I I.T.C.`, corso "4+2") |
| Assegnazione | Terna docente + classe + materia (una riga di `assegnazione`) con il suo monte ore |
| Docente ASSUNTO | Sempre disponibile 8-14 dal lunedì al venerdì (non vincolato da griglia) |
| Docente CONTRATTO | Disponibile solo nelle fasce segnate nella griglia della settimana |
| Ora buca (docente) | Ora libera di un docente tra due sue lezioni nello stesso giorno |
| Stage / tirocinio | La classe/gruppo è fuori sede: zero lezioni, nell'orario compare "STAGE" |
| Deroga / quick action | Rilassamento esplicito, per una sola rigenerazione, di una regola quasi-rigida |
| Settimana | Lunedì-venerdì, identificata dalla data del lunedì (`week_start`) |

---

## 2. Architettura e stack

```
Browser ──HTTPS──> Caddy ──/api/*, /health, /docs, /openapi.json, /redoc──> backend (FastAPI :8000) ──> PostgreSQL
                      └────────── tutto il resto ───────────────────────> frontend (Node :3000, SSR)
```

| Componente | Tecnologia (versioni da `requirements.txt` / `package.json`) |
|---|---|
| Backend | Python 3.13, FastAPI ≥0.104, Uvicorn, SQLAlchemy 2.0 (non 2.1), Pydantic 2 + pydantic-settings, psycopg2 |
| Solver | `ortools` ≥ 9.12 (CP-SAT) |
| PDF | WeasyPrint ≥ 60 (richiede Pango/Cairo; font: Liberation/DejaVu) |
| Excel | openpyxl ≥ 3.1 |
| Lettura PDF in ingresso | pdfplumber ≥ 0.11 |
| LLM (copilota, oggi disattivato) | `anthropic` SDK, tool-use |
| Frontend | React 19, TanStack Start/Router/Query, Tailwind CSS 4, Vite, build nitro `node-server` |
| Database | PostgreSQL 15 (produzione); SQLite per sviluppo e test |
| Reverse proxy | Caddy 2 (HTTPS automatico Let's Encrypt) |

Struttura del repository:

```
app/main.py            FastAPI app, CORS, create_all + migrazione colonna admin.role
app/config.py          Settings (env): DATABASE_URL, CORS_ORIGINS, SOLVER_TIMEOUT_SECONDS, ANTHROPIC_API_KEY, TEMP_DIR…
app/database.py        engine, SessionLocal, Base, get_db, ensure_admin_role_column()
app/models.py          modelli SQLAlchemy (§3)
app/schemas.py         modelli Pydantic di richiesta/risposta
app/security.py        hash password, sessioni, guardie di ruolo (§4)
app/routes/            auth, setup, availability, schedule, chat, dati, soft_weights
app/services/          logica applicativa (schedule, setup, availability, dati, feedback, soft_weights, chat)
app/repositories/      accesso DB (schedule: contesto settimanale, celle di stage; setup; availability)
app/domain/            logica pura: solver.py, parser.py, validator.py, soft_weights.py, nlp.py
scripts/               create_admin.py, import_documenti.py, rederive_monte_ore_and_pairings.py,
                       set_contratto_availability.py, infer_availability_from_calendars.py
frontend/src/routes/   index (login), menu, setup, disponibilita, orario, dati.*, account
frontend/src/lib/api.ts client HTTP con token Bearer
deploy/                docker compose, Caddyfile, update.sh, backup, create-admin.sh
tests/                 pytest (SQLite temporaneo)
Documenti/             PDF reali di esempio (calendario annuale, orari di esempio)
```

Configurazione (`.env`, vedi `.env.example`): `DATABASE_URL`, `API_TITLE`, `API_VERSION`, `DEBUG`,
`SOLVER_TIMEOUT_SECONDS=60`, `ANTHROPIC_API_KEY`, `USE_S3`, `S3_BUCKET`, `TEMP_DIR=/tmp/calendariodocenti`,
`CORS_ORIGINS` (lista JSON; default `["*"]`, l'auth è un header Bearer, non un cookie, quindi CORS senza credenziali).

All'avvio `app/main.py` esegue `Base.metadata.create_all` e `ensure_admin_role_column()`: `create_all` non altera tabelle
esistenti, quindi aggiunge `admin.role` ai database precedenti ai ruoli (`ALTER TABLE admin ADD COLUMN role VARCHAR(20)
NOT NULL DEFAULT 'ADMIN'`). Non esiste un sistema di migrazioni (Alembic): ogni nuova colonna richiede una
funzione di migrazione analoga.

---

## 3. Modello dati

Tutti gli id sono stringhe UUID (`str(uuid4())`) tranne `scuola.id`, che in v1 vale `"sch_1"`. `created_at` è
`DateTime server_default now()`. Le enumerazioni sono `Enum` SQLAlchemy.

| Tabella | Colonne (tipo, vincoli) |
|---|---|
| `scuola` | `id`, `nome` (255, NN), `anno_formativo` (50, NN, es. `2026-2027`), `data_inizio_anno` (Date, NN), `data_fine_anno` (Date, NN) |
| `classe` | `id`, `scuola_id` FK, `nome` (50, NN), `n_studenti` (int, def 0), `gruppo` (50, nullable) |
| `docente` | `id`, `scuola_id` FK, `nome` (255, NN, formato **"COGNOME NOME"**), `email` (nullable), `tipo` Enum(`ASSUNTO`,`CONTRATTO`) NN, `active` (bool, def true), `first_time_this_year` (bool, def true) |
| `materia` | `id`, `scuola_id` FK, `nome` (255), `tipo` Enum(`TEORIA`,`PRATICA`), `peso_cognitivo` Enum(`ALTO`,`MEDIO`,`BASSO`) |
| `assegnazione` | `id`, `scuola_id`, `docente_id` FK, `classe_id` FK, `materia_id` FK |
| `monte_ore_annuale` | `id`, `scuola_id`, `classe_id`, `materia_id`, `docente_id` (FK), `ore_totali` (int NN), `ore_erogate` (int, def 0). **Residuo = `ore_totali − ore_erogate`** (vedi §8) |
| `calendario_annuale` | `id`, `scuola_id`, `data` (Date NN), `gruppo` (50, null = riga valida per tutta la scuola), `ore_max_giornata` (int, def 6; valori 4/5/6, 8 = stage), `ora_inizio_min` (int 8-13, null), `flag_chiusura` (bool), `flag_stage_classe_id` (FK classe, null), `flag_stage_gruppo` (bool) |
| `disponibilita_settimanale` | `id`, `scuola_id`, `docente_id`, `settimana_inizio` (Date), `giorni_fasce` (JSON, §6), `created_at`, `updated_at` |
| `classe_accoppiata` | `id`, `scuola_id`, `classe_a_id`, `classe_b_id`, `materia_id` (FK), `docente_id` (FK, null), `note` (Text) |
| `orario_settimanale` | `id`, `scuola_id`, `settimana_inizio` (Date), `stato` Enum(`BOZZA`,`APPROVATO`) def BOZZA, `quality_score` (Float 0-100), `quality_level` Enum(`A`,`B`,`C`), `n_conflitti_soft` (int), `approved_at` |
| `slot_lezione` | `id`, `orario_settimanale_id` FK, `classe_id`, `docente_id`, `materia_id`, `giorno` Enum(LUNEDI…VENERDI), `ora_inizio` (8-13), `ora_fine` (9-14), `accoppiata` (bool), `classe_accoppiata_id` (FK classe, null) |
| `chat_message` | `id`, `scuola_id`, `orario_settimanale_id`, `ruolo` Enum(`ADMIN`,`AI`), `messaggio` (Text), `timestamp` |
| `preferenze_ai_memory` | `id`, `scuola_id`, `regola` (Text), `peso` (Float, def 1.0) |
| `conflitto_gestito` | `id`, `orario_settimanale_id` (indice), `chiave` (300), `azione` (`APPROVATO` \| `LIBERA`). Si svuota a ogni rigenerazione della settimana |
| `orario_feedback` | `id`, `orario_settimanale_id` (indice), `admin_id`, `voto` (1-5), `motivi` (JSON lista di codici), `nota` (Text), snapshot `quality_score`/`quality_level`/`n_conflitti_soft`/`stato`. Append-only |
| `soft_weight_adjustment` | `id`, `kind` (60, indice), `old_weight`, `new_weight`, `evidenza` (JSON), `admin_id`, `created_at`. Append-only: il peso attivo di un `kind` è il `new_weight` della riga più recente |
| `audit_log` | `id`, `scuola_id`, `orario_settimanale_id` (null), `azione` (es. `GENERATE`, `MODIFY_SLOT`, `SCHEDULE_APPROVED`, `QUICK_ACTION`), `admin_id`, `dettagli` (JSON), `timestamp` |
| `admin` | `id`, `scuola_id` FK, `email` (255, **unique**, indice, salvata in minuscolo), `password_hash`, `role` String(20) NN def `ADMIN` (`ADMIN` \| `SEGRETERIA`) |
| `admin_session` | `token` (PK, opaco), `admin_id` FK, `created_at`, `expires_at` |

Note di integrità:
- Una classe accoppiata è registrata come **una riga per coppia per docente** (un gruppo di 4 classi sempre insieme =
  più righe a coppie). `docente_id` è il docente che realmente tiene la lezione congiunta: gli altri docenti delle due
  classi restano indipendenti.
- Non c'è cancellazione a cascata dichiarata: i servizi `dati` rimuovono a mano le righe dipendenti.

---

## 4. Autenticazione e ruoli

**Login:** `POST /api/auth/login {email, password}` → `{status:"ok", token, email, role}`. Errore 401 «Email o password non
corretti». Il token è opaco (`secrets.token_urlsafe(32)`), salvato in `admin_session` con scadenza **7 giorni**; il
client lo invia come `Authorization: Bearer <token>`. `POST /api/auth/logout` cancella la sessione (funziona anche se
scaduta). `GET /api/auth/me` → `{email, role}`.

**Password:** PBKDF2-HMAC-SHA256, 260 000 iterazioni, salt casuale di 16 byte; formato memorizzato
`"<salt_hex>$<digest_hex>"`; confronto con `hmac.compare_digest`. Lunghezza minima 8 caratteri.

**Cambio password:** `POST /api/auth/change-password {current_password, new_password}` (richiede login). 400 se la
password attuale è errata, se la nuova ha meno di 8 caratteri o è uguale all'attuale. Dopo il cambio **tutte le altre
sessioni dello stesso utente vengono cancellate**; quella usata resta valida.

**Creazione utenti:** non esiste registrazione. Si usa `python -m scripts.create_admin <email> <password> [scuola_id]
[ruolo]` (default `sch_1`, `ADMIN`); in produzione `deploy/create-admin.sh <email> "<password>" [ADMIN|SEGRETERIA]`.
Se l'utente esiste ne reimposta password e ruolo.

**Ruoli e permessi** (guardie in `app/security.py`; un ruolo mancante vale ADMIN):

| Area | ADMIN | SEGRETERIA |
|---|---|---|
| Login, `/auth/me`, cambio password | sì | sì |
| Lettura orario (`GET /schedule/{w}` e derivati), export PDF/Excel | sì | sì |
| Lettura docenti (`GET /teachers`) e dati scuola (`GET /classes, /subjects, /assignments, /class-pairings, /calendar`) | sì | sì |
| Disponibilità docenti: lettura, salvataggio, copia dalla settimana precedente, stato, elenco settimane | sì | **sì** |
| Generare, rigenerare, modificare slot, assegnare slot, conflitti, quick action, approvare, valutare (`POST /schedule/**`) | sì | **no (403)** |
| Setup (`/setup/**`, upload file, `/calendar/add-date-manual`) | sì | no (403) |
| Dati scuola in scrittura (POST/PUT/DELETE su classi, materie, assegnazioni, accoppiamenti, calendario) | sì | no (403) |
| CRUD docenti (`POST/PUT/DELETE /teachers…`) | sì | no (403) |
| Chat copilota, pesi del solver (`/soft-weights`) | sì | no (403) |

Implementazione: `require_admin` (solo ADMIN, sui router `setup`, `chat`, `soft_weights` e sulle scritture dei
docenti), `require_admin_for_writes` (metodi non-GET solo ADMIN, sui router `schedule` e `dati`), `get_current_admin`
(qualunque utente loggato, sul router `availability`). Risposta 403: «Permesso negato…». Nessun token/scaduto → 401.

Il frontend (§13) nasconde le funzioni vietate alla segreteria, ma l'applicazione dei permessi è sempre lato server.

---

## 5. Setup iniziale (caricamento dati)

Flusso (pagina `/setup`): per ogni tipo di file si fa upload → il file viene letto e validato → l'admin vede l'anteprima
ed eventuali avvisi → corregge → `POST /setup/save`. L'upload parcheggia i dati già letti in una cache in memoria del
`SetupService` (un'istanza di modulo); `/setup/validate` con `{file_ids:[…]}` valida la coerenza incrociata; in
alternativa accetta direttamente i contenuti CSV (`calendario, docenti, classi, materie, assegnazioni, accoppiamenti`).
La cache è per-processo: un riavvio del backend a metà setup la perde.

### 5.1 File CSV (intestazione obbligatoria, UTF-8)

| File | Endpoint upload | Colonne |
|---|---|---|
| Calendario | `POST /calendar/upload` (CSV o PDF) | `data` (YYYY-MM-DD), `ore_max_giornata` (**4/5/6**, altro = errore), `flag_chiusura` (true/1/yes), opz. `stage_classe_id`, `gruppo` |
| Docenti | `POST /teachers/upload` | `nome`, `email` (opz.), `tipo` (`ASSUNTO`/`CONTRATTO`) |
| Classi | `POST /classes/upload` | `nome`, `n_studenti` (opz.), `gruppo` (opz., es. `PRIME`) |
| Materie | `POST /subjects/upload` | `nome`, `tipo` (`TEORIA`/`PRATICA`), `peso_cognitivo` (`ALTO`/`MEDIO`/`BASSO`) |
| Assegnazioni | `POST /assignments/upload` | `docente_nome`, `classe_nome`, `materia_nome`, `ore_anno` |
| Accoppiamenti | `POST /class-pairings/upload` | `classe_a`, `classe_b`, `materia_nome`, `note` (opz.) |

### 5.2 Calendario da PDF («CALENDARIO AF»)

Il parser (`CalendarParser.parse_calendario_pdf`, pdfplumber) legge le righe della tabella principale. Geometria
(coordinate x0 in punti PDF, costante `CALENDARIO_PDF_GRUPPI_COLONNE`):

| Gruppo | x0 da | a |
|---|---|---|
| PRIME | 140 | 195 |
| SECONDE | 195 | 250 |
| TERZE | 250 | 305 |
| QUARTE | 305 | 360 |
| PRIMA4+2 | 360 | 410 |

La colonna data è tutto ciò che sta a x0 < 140; l'area letta è ritagliata a x ≤ 410 (le tabelle laterali delle attività
sono ignorate). Una riga data è del tipo «lunedì 14 settembre 2026» (mesi italiani). Per **ogni data e ogni gruppo** si
crea una riga `{data, gruppo, ore_max_giornata, flag_chiusura=false, stage_classe_id=null, flag_stage_gruppo}`:
- cella vuota → `ore_max_giornata = 0`;
- **cella con valore ≥ 8 (`STAGE_ORE_GIORNATA`) → `flag_stage_gruppo = true`**: il gruppo è in tirocinio quel giorno
  (nel PDF dell'agenzia le note a piè pagina riportano gli intervalli, es. «tirocinio classi IV dal 5/10 al 22/12»);
- i giorni lavorativi (lun-ven) compresi tra la prima e l'ultima data letta ma assenti dal PDF diventano righe
  `gruppo=null, ore_max_giornata=0, flag_chiusura=true` (chiusure/festività).
Se non si riconosce nessuna riga il parser solleva un errore («serve un parser diverso per un altro template»).
Un template diverso richiede di aggiornare le coordinate.

Il salvataggio (`bulk_create_calendar`) scrive `gruppo`, `ore_max_giornata`, `flag_chiusura`, `flag_stage_gruppo` e
`flag_stage_classe_id` (accetta anche la chiave `stage_classe_id` del parser).

### 5.3 Altri parser

- `SchoolRosterParser.parse_orario_settimanale_pdf`: legge un orario pubblicato («Orario scolastico dal X al Y…»;
  righe = ore, colonne = classi, cella = cognome docente) e restituisce `classi`, `slots {classe_nome, giorno,
  ora_inizio, docente_cognome}` e `week_label`. Serve a ricavare associazioni docente-classe e disponibilità osservate
  (script §6.3). Il gruppo si deduce dal nome classe: numero romano → `PRIME/SECONDE/TERZE/QUARTE`; `I.T.C.` → `PRIMA4+2`.
- `parse_elenco_docenti_pdf`: elenco docenti da PDF.

### 5.4 Validazione (`DataValidator.validate_all_data`)

**Errori** (bloccano `can_proceed`): assegnazione con docente/classe/materia inesistente; accoppiamento con classe o
materia inesistente, oppure senza una assegnazione per quella classe+materia su entrambe le classi; `stage_classe_id`
che non è una classe nota.
**Avvisi** (non bloccanti, correggibili): docente senza assegnazioni; classe senza assegnazioni; materia non usata.
Correzioni in tempo reale: `POST /setup/validate-field {file_type, entity, field, new_value}` e
`POST /setup/apply-correction {file_id, correction}`; data mancante: `POST /calendar/add-date-manual {file_id, date,
ore_max_giornata (4/5/6), flag_chiusura, flag_stage_classe_id}`.

### 5.5 Salvataggio

`POST /setup/save {file_ids, corrections_applied, school_name, school_year, data_inizio?, data_fine?}`. Se le date non
sono fornite si usano min/max del calendario. Crea `scuola` (id fisso `sch_1`), classi, docenti, materie, calendario,
assegnazioni (con relativa riga `monte_ore_annuale`) e accoppiamenti in un'unica transazione; in caso di errore rollback.
Risposta: `{status:"setup_complete", school_id, next_page}`.

Dopo il setup si può correggere tutto dalle pagine «Dati scuola» (§13), che offrono CRUD completo.

---

## 6. Disponibilità dei docenti

**Formato `giorni_fasce`** (obbligatorio, validato: tutti e 5 i giorni, ogni giorno una lista non vuota di fasce con
`ora_inizio`, `ora_fine`, `disponibile`):

```json
{"lunedi": [{"ora_inizio": "08:00", "ora_fine": "09:00", "disponibile": true}, …6 fasce 08-14…], "martedi": [...], …}
```

**Lettura (`GET /availability/{week}/{teacher_id}`)**, in ordine: record della settimana; se non c'è e il docente è al
primo anno → griglia di default (tutto disponibile 8-14, `is_default=true`); altrimenti eredita la settimana
precedente più recente; altrimenti default. `POST /availability/{week}/{teacher_id} {giorni_fasce}` salva;
`POST /availability/{week}/copy-from/{prev_week}` copia le griglie saltando i docenti già compilati;
`GET /availability/{week}/status` → `{week_complete, total_teachers, teachers_with_availability, teachers_missing,
ready_to_generate, completion_percentage}`; `GET /availability/weeks` → `[{start, end (=venerdì), week_num}]` per ogni
lunedì dell'anno formativo a partire dal primo lunedì ≥ `data_inizio_anno`.

**Uso nel solver** (`ScheduleRepository.get_week_context`): per ogni docente senza record la settimana si applica la
stessa eredità (settimana precedente, poi default), in sola lettura, senza persistere nulla. Gli **ASSUNTI** sono sempre
disponibili 8-14 (la griglia non li vincola); per i **CONTRATTI** vale la griglia (§9, vincolo H9). Un contrattista senza
nessuna disponibilità registrata viene trattato come indisponibile tutta la settimana.

### 6.3 Script di inizializzazione dati (una tantum, non parte dell'app)

- `scripts/import_documenti.py`: costruisce scuola, calendario, classi, docenti e associazioni leggendo i PDF in `Documenti/`.
- `scripts/rederive_monte_ore_and_pairings.py`: ricalcola `ore_totali` (= ore settimanali osservate × settimane rimaste)
  e gli accoppiamenti per docente a partire dall'orario di esempio, **azzerando `ore_erogate`**. Serve solo per l'inizializzazione:
  da quando l'approvazione scala le ore (§8) **non va più rieseguito**, altrimenti si perdono le ore già erogate e quelle da recuperare.
- `scripts/set_contratto_availability.py`, `scripts/infer_availability_from_calendars.py`: ricavano la disponibilità dei
  contrattisti dagli orari di esempio (giorni = ore/6 arrotondato, scelta con seme fisso; oppure unione delle ore osservate in due settimane).

---

## 7. Calendario: come si risolve una classe in un giorno

Per ogni classe e giorno il solver (e le celle di stage) usano questa precedenza (`_ore_max_for_classe_giorno_raw`):

1. riga con `flag_stage_classe_id == classe` → **0 ore** (stage);
2. riga con `gruppo == gruppo della classe` e `flag_stage_gruppo` → **0 ore** (stage di gruppo);
3. riga con `gruppo == gruppo della classe` → `0` se `flag_chiusura`, altrimenti `ore_max_giornata`;
4. riga con `gruppo = NULL` (tutta la scuola) → idem;
5. nessun dato → **6**.

`ore_max_giornata` è poi limitato a **6** (le variabili coprono solo 8-13). `ora_inizio_min` (8-13) vieta lezioni
prima di quell'ora per quella classe/gruppo (ingressi scaglionati), con la stessa precedenza gruppo → scuola.
I giorni di stage di una classe compaiono nelle celle di stage (`stage_cells`, §10) come «STAGE», senza docente.

---

## 8. Monte ore e target settimanale

- Residuo di un'assegnazione: `ore_residue = ore_totali − ore_erogate` (0 se manca la riga `monte_ore_annuale`). `ore_totali`
  è il monte ore da svolgere dall'inizio del periodo gestito; `ore_erogate` cresce a ogni approvazione.
- **Scalare le ore (decisione admin 2026-10-05):** all'**approvazione** della settimana (`POST /schedule/{w}/approve`)
  ogni lezione dell'orario aggiunge **1 ora a `ore_erogate`** dell'assegnazione (docente + classe + materia) della lezione,
  nella stessa transazione che porta lo stato a `APPROVATO`. In una lezione accoppiata ogni classe riceve la lezione, quindi
  vengono scalate entrambe le assegnazioni. Le ore non svolte (assenze, lezioni saltate) non vengono scalate: restano nel
  residuo e le settimane successive le recuperano, perché **il target di ogni settimana si basa sul residuo**, non sul totale.
  L'operazione è idempotente: riapprovare una settimana già approvata non scala di nuovo (`ore_scalate: 0`). Le lezioni
  senza riga `monte_ore_annuale` non vengono scalate e sono contate in `senza_monte_ore`. Le ore si possono sempre
  correggere a mano dalla pagina «Assegnazioni» (campo `ore_erogate`). Non esiste (ancora) l'annullamento di un'approvazione.
- **Target settimanale** per assegnazione: `settimane_rimaste = max(1, (data_fine_anno − week_start).days // 7)`,
  `ore_target = max(1, ore_residue // settimane_rimaste)`. Nessun target se `ore_residue ≤ 0` o se la classe non ha ore
  di lezione in tutta la settimana (es. tutta la settimana in stage). Deroga `reduce_contract_hours` (solo contrattisti):
  `ore_target = max(1, min(target, max_hours))`, oppure `max(1, target // 2)` se `max_hours` non è dato.
- Limite rigido: un docente **ASSUNTO** non supera mai `ore_residue` per assegnazione (H8); un docente **CONTRATTO** può
  superarlo (admin, 2026-10): è limitato solo dalla disponibilità.

---

## 9. Motore di ottimizzazione (CP-SAT)

### 9.1 Modello

Variabili booleane `x[(assegnazione_id, giorno 0-4, ora 0-5)]` (ora 0..5 = 8..13). Una lezione = una variabile a 1.
Funzione obiettivo: **minimizzare** `Σ peso_i · violazione_i` su tutte le penalità (`SoftPenalty`). Tempo massimo
`SOLVER_TIMEOUT_SECONDS` (60 s). Stati: `OPTIMAL`, `FEASIBLE`, `INFEASIBLE`, `UNKNOWN`.

Contesto di input (`ScheduleContext`, costruito da `get_week_context`): assegnazioni con residuo, mappa disponibilità,
calendario dei 5 giorni (mancante → 6 ore), classi accoppiate `(classe_a, classe_b, materia, docente)`, mappa
docente→tipo, mappa classe→gruppo, `anno_fine`.

### 9.2 Vincoli rigidi (sempre rispettati)

| Id | Regola |
|---|---|
| H1 | Un docente non è in due classi nella stessa ora (eccezione: lezione accoppiata) |
| H1b | Una classe ha al più una lezione per ora |
| H2 | **Capienza esatta:** ore di lezione di una classe in un giorno = ore massime del giorno (§7). Nessuna ora libera: se impossibile, la settimana è INFEASIBLE |
| H2b | Le ore di lezione di una classe in un giorno formano **un unico blocco contiguo** (nessuna ora buca per la classe). In un giorno corto la/e ora/e libere stanno all'inizio o in fondo |
| H2c | Nessuna lezione prima di `ora_inizio_min` della classe |
| H3 | **Accoppiate:** per ogni coppia, il docente indicato ha la lezione nello stesso giorno/ora in entrambe le classi e il monte ore scala **una volta sola** |
| H5 | Classi in stage: zero lezioni (§7) |
| H6 | Docente ASSUNTO: ore settimanali per assegnazione ≤ residuo (vedi §8) |
| H7/H8 | Esclusioni e tetti richiesti dalla chat per la sola esecuzione (giorno escluso per un docente; massimo di ore settimanali) |

### 9.3 Vincoli «quasi rigidi» (rilassabili solo a peso 1000, sempre segnalati)

Ognuno ha una variabile `override` con peso **`RELAX_WEIGHT = 1000`**: il solver li viola solo se non esiste
alternativa e ogni violazione compare tra i conflitti con la deroga corrispondente (nessuna violazione silenziosa).

| Id | Regola | Deroga che la autorizza |
|---|---|---|
| H4 | **Disponibilità contrattisti**: nessuna lezione dove `disponibile=false`. In generazione è **rigido** (non si forza mai); la penalità 1000 esiste solo per modifiche manuali dell'admin (modalità *forced*, §10) | `override_availability` |
| H9 | Venerdì: il blocco di lezioni di una classe **inizia alle 8:00** (decisione admin 2026-09-23); non applicato se la classe non ha lezioni o ha un `ora_inizio_min` | `authorize_friday_late_start` |
| H10 | **Pratica**: le ore di una materia PRATICA formano **un blocco di almeno `min(3, capienza)` ore** nel giorno, oppure nessuna (decisione admin 2026-09-23) | `authorize_short_pratica_block` |
| H11 | Un docente tiene al massimo **4 ore** (`MAX_HOURS_PER_CLASSE_DAY`) con la stessa classe nello stesso giorno; si applica ai docenti con ≥ 2 classi; le lezioni accoppiate contano una volta | `authorize_single_classe_day` |

### 9.4 Preferenze (soft) e pesi

| Id | Regola | Peso default | Note |
|---|---|---|---|
| S1 `classe_late_start` | Una classe inizia alle 8:00 | **100** | peso massimo tra le preferenze; inattivo se il giorno è pieno |
| S2 `teoria_consecutive` | Max 2 ore consecutive di teoria con lo stesso docente nella stessa classe | **10** | deroga `force_3_hours_theory` alza il tetto (3 o `max_hours`) |
| S3 `ore_target_deviation` | Ore settimanali dell'assegnazione = target (§8) | **15 + 500** per ora di scarto | 500 = `EXACT_TARGET_WEIGHT` (decisione admin 2026-10: target «esatto», cede solo per riempire un'aula). Per i contrattisti vedi 9.5 |
| S4 `contractor_gap` | Nessuna **ora buca vera** per un contrattista (ora libera con lezione prima e dopo nello stesso giorno) | **150** per ora buca | Solo CONTRATTO: i buchi cadono quindi sugli assunti (nessuna penalità). Per i contrattisti vedi 9.5. Deroga `authorize_early_exit` la spegne |
| S5 `single_classe_day` | Un docente con ≥2 classi non passa un'intera giornata (≥3 ore) con una sola classe | **10** | deroga `authorize_single_classe_day` |

Pesi regolabili (`DEFAULT_SOFT_WEIGHTS`, tetto `MAX_SOFT_WEIGHT = 200`): `classe_late_start 100`,
`teoria_consecutive 10`, `ore_target_deviation 15`, `contractor_gap 150`, `single_classe_day 10`. Pesi fissi:
`RELAX_WEIGHT 1000` (vincoli quasi rigidi), **1500** per ora non coperta (`classe_unfilled`), ora oltre capienza
(`classe_over_capacity`) e monte ore superato in modalità *forced* (`monte_ore_exceeded`): una classe vuota non viene
mai scambiata con una preferenza. Ordine di priorità: **ore non coperte (1500) > vincoli quasi rigidi (1000) > target
esatto (500+) > preferenze (≤ 200)**.

### 9.5 Contrattisti: scarsità di disponibilità

Il peso di S3 e S4 per un contrattista è moltiplicato per `m = 1.5 − (ore_disponibili / 30)` (da 0.5 a 1.5):
chi ha pochissima disponibilità ha penalità più alte (si rispetta di più il suo target e si evitano i suoi buchi,
viene «piazzato per primo»); chi è molto disponibile ha più flessibilità. Nessuna griglia registrata → `m = 1.5`;
deroga `override_availability` attiva per quel docente → `m = 1.0`; non-contrattisti → `m = 1.0`. Il peso risultante è
`max(1, round(peso · m))`. Valori: gap 75…225, scarto dal target ≈ 258…772 (se non si ritocca il peso S3).

### 9.6 Punteggio di qualità

`score = (penalità a valore 0 / numero di penalità) × 100`; livello **A** se > 95, **B** se > 80, altrimenti **C**.
Se il solver non trova soluzione: score 0, livello C. `n_soft_conflicts = numero penalità violate`.

### 9.7 Fallback e deroghe

`_solve_with_fallback`: (1) modello rigido (H2 uguaglianza); se `INFEASIBLE` (2) **modalità best-effort**: le ore non
coperte diventano penalità a 1500 (`classe_unfilled`) e si restituisce il miglior orario possibile con messaggio «Nessun
orario completo possibile…»; (3) per un orario forzato a mano (`solve_fixed` con assegnazione pinnata) ulteriore modalità
**forced** in cui capienza, doppia prenotazione, monte ore, inizio e stage diventano conflitti segnalati invece di errori.

**Quick action / deroga** (`POST /schedule/{w}/apply-quick-action {action_type, class_id?, teacher_id?, giorno?,
max_hours?}`), valida per **una sola rigenerazione** (non persistita), con ambito classe/docente/giorno opzionale:

| `action_type` | Effetto |
|---|---|
| `force_3_hours_theory` | Tetto di ore teoria consecutive 3 (o `max_hours`) |
| `reduce_contract_hours` | Target del contrattista ridotto (§8) |
| `authorize_early_exit` | Niente penalità sui buchi del contrattista |
| `override_availability` | Ignora la disponibilità registrata del docente |
| `authorize_single_classe_day` | Disattiva H11/S5 per docente/classe/giorno |
| `authorize_friday_late_start` | Disattiva H9 |
| `authorize_short_pratica_block` | Disattiva H10 |

### 9.8 Conflitti

`get_conflicts()` elenca le penalità violate della soluzione, deduplicate per `(kind, classe, docente, giorno, ora)`:
`{conflict_id, chiave, description, suggested_action {action_type, label}, classe_id, docente_id, giorno, ore?, ore_slot?}`.
`chiave = "<kind>|<classe_id>|<docente_id>|<GIORNO>[|<ora>]"` è stabile tra ricalcoli (a differenza di `conflict_id`).
Per `ore_target_deviation` la descrizione riporta ore assegnate/target/scarto e come è stato derivato il target; la
deroga «Riduci ore» è suggerita solo per un contrattista **sopra** target.

---

## 10. Ciclo di vita dell'orario di una settimana

Stati: assente → `BOZZA` → `APPROVATO` (bloccato).

| Operazione | Endpoint | Comportamento |
|---|---|---|
| Generare | `POST /schedule/generate {week_start, include_preferences=true}` | Rifiuta se la settimana è APPROVATA. Errore se non ci sono assegnazioni. Risolve (§9.7). Risposta `status`: `generated` (con `schedule_id, quality_score, quality_level, n_soft_conflicts, slots, conflicts, stage_cells, message?`), `infeasible` (con `conflicting_constraints`, `suggested_deroghe`), `timeout`, `error`. I pesi usati sono quelli attivi (default + adeguamenti appresi, §11) |
| Rigenerare | `POST /schedule/{w}/regenerate` | Sostituisce gli slot della settimana (stesso `schedule_id`), svuota `conflitto_gestito` |
| Leggere | `GET /schedule/{w}` | Slot, qualità, conflitti (ricalcolati a ogni lettura e filtrati dalle decisioni dell'admin), `stato`, `stage_cells`; 404 se assente |
| Qualità | `GET /schedule/{w}/quality-score` | Score, livello, n. conflitti |
| Spostare una lezione | `POST …/modify-slot {slot_id, changes{docente_id?, giorno?, ora_inizio?}}` | Rifiuta se APPROVATO; verifica `ora_inizio` 8-13 e giorno valido; rivaluta con `solve_fixed` (variabili pinnate) e ricalcola qualità/conflitti |
| Assegnare un'ora libera | `GET …/assignable/{classe_id}` elenca coppie docente/materia; `POST …/assign-slot {classe_id, giorno, ora_inizio, docente_id, materia_id}` | Forzatura valutata come modifica manuale (le violazioni diventano conflitti); rifiutato solo se supera le ore massime giornaliere della classe. Un docente in lezione accoppiata viene assegnato a tutte le classi del gruppo |
| Conflitti | `POST …/conflicts/approve {chiavi}` (il conflitto non viene più segnalato); `POST …/conflicts/reject {classe_id, giorno, ora_inizio}` (toglie la lezione e lascia l'ora libera, `azione=LIBERA`) | Decisioni in `conflitto_gestito` |
| Deroga | `POST …/apply-quick-action` | §9.7 |
| Approvare | `POST …/approve` | `stato=APPROVATO`, `approved_at`, **scala le ore delle lezioni dal monte ore (§8)**, voce in `audit_log` (`SCHEDULE_APPROVED` con `ore_scalate`, `assegnazioni_aggiornate`, `senza_monte_ore`); risposta `{status, schedule_id, stato, ore_scalate, assegnazioni_aggiornate, senza_monte_ore}`; da quel momento nessuna modifica/rigenerazione; idempotente |
| Esportare | §12 | PDF ed Excel |
| Valutare | `GET/POST …/feedback` | §11 |

Ogni slot restituito ha: `slot_id, classe_id, classe_nome, docente_id, docente_nome, materia_id, materia_nome,
materia_tipo, giorno, ora_inizio, ora_fine, accoppiata, classe_accoppiata_id, conflitto, conflitto_chiavi, indisponibile`.
`stage_cells`: `[{classe_id, classe_nome, giorno}]` per ogni classe in stage (di classe o di gruppo) nei 5 giorni;
il `classe_id` in un record di stage può essere anche il **nome** della classe (viene risolto, case-insensitive, sul
nome se non coincide con un id).

Le modifiche manuali e le deroghe scrivono in `audit_log`.

---

## 11. Valutazioni e apprendimento dei pesi

- `POST /schedule/{w}/feedback {voto 1-5, motivi[], nota ≤ 2000}`: salva una riga append-only con lo snapshot di qualità.
  Codici `motivi` (`MOTIVI`): `classe_late_start`, `friday_late_start`, `teoria_consecutive`, `pratica_block`,
  `contractor_gap`, `single_classe_day`, `ore_target_deviation`, `classe_unfilled`, `altro`. Ognuno è collegato a una
  penalità del solver (`MOTIVO_CONSTRAINTS`); solo cinque hanno un peso regolabile.
- **Proposte** (`GET /soft-weights`): per un motivo con ≥ **2** valutazioni basse (voto ≤ 3) si propone di **alzare** il
  peso: passo `+10% × gravità` (1 stella = 3, 2 = 2, 3 = 1), al massimo **+50%** per passo e mai oltre 200. Ogni
  valutazione conta una volta (dopo un adeguamento o un reset contano solo quelle successive).
- `POST /soft-weights/apply {kinds?}` accetta le proposte (tutte o alcune); `POST /soft-weights/reset {kind?}` ripristina il
  default di un peso o di tutti. Ogni cambiamento è una riga in `soft_weight_adjustment` (storico). Nulla cambia da solo.
  I pesi attivi valgono dalle generazioni successive.

---

## 12. Esportazione

`GET /schedule/{w}/export-pdf` → `{status:"generated", filename, pdf_url:"/api/schedule/{w}/export-pdf/file"}`;
`…/export-pdf/file` risponde `application/pdf` (inline, `orario_{w}.pdf`). Analogo per `export-excel`
(`xlsx_url`, `…/export-excel/file`, `.xlsx`). I file richiedono il token Bearer (il frontend li scarica con `fetch` e li
apre/salva come blob; `window.open` diretto darebbe 401). Si può esportare un orario in qualunque stato.

**Layout (identico per PDF ed Excel), modellato su `Documenti/es_di_calendario.pdf`:**

- **PDF:** una sola pagina **A4 orizzontale**, margini 8 mm, font Calibri/Carlito/Helvetica.
  - Banda azzurra `#95b3d7`, testo nero grassetto 14 pt centrato: riga 1 «Agenzia Formativa don Angelo Tedoldi», riga 2
    «Orario scolastico dal 5 al 9 Ottobre» (mesi in italiano con iniziale maiuscola; se la settimana attraversa due mesi:
    «dal 28 Settembre al 2 Ottobre»).
  - Intestazione tabella su fondo rosso `#c00000`, testo bianco grassetto: `DATA | Orario | <classi>`.
  - **Ordine classi:** per corso `OP. INFORM.` → `ELETTRICISTI` → `ESTETISTE` → `PAN. E PAST` → `I.T.C.`, dentro il corso
    per anno I, II, III, IV; i nomi non riconosciuti in coda, in ordine alfabetico.
  - Cinque blocchi (lunedì…venerdì), ciascuno con la colonna `DATA` unita verticalmente e l'etichetta del giorno in
    minuscolo ruotata di 90°, bianca corsiva su arancione `#e46c0a` (giorni 1,3,5) o blu `#0070c0` (giorni 2,4); righe
    `8-9, 9-10, … 13-14`; tra un giorno e l'altro una striscia grigia `#d9d9d9`. Le righe finali senza alcuna lezione in
    quel giorno (es. un venerdì corto) vengono omesse.
  - Cella: **solo il cognome del docente** (prima parola di `docente.nome`), grassetto 5.8 pt centrato, bordo nero 0.5 pt;
    sfondo per (corso, anno): OP. INFORM. I `#ddd9c4`, II `#f2dcdb`, III `#92d050`; ELETTRICISTI I `#ddd9c4`, II `#f2dcdb`,
    III `#ff99cc`; ESTETISTE I `#c5d9f1`, II `#fde9d9`, III `#ff99cc`; PAN. E PAST I `#c5d9f1`, II `#fde9d9`,
    III `#92d050`; I.T.C. I `#ffff99`; IV anno e non riconosciute: bianco.
  - Cella senza lezione: grigio `#d9d9d9`, vuota. **Cella di stage: testo «STAGE», sfondo giallo `#ffff00`, nessun docente.**
  - Le lezioni accoppiate non si fondono: il cognome è ripetuto in ciascuna classe.
- **Excel:** un solo foglio «Orario» con la stessa struttura (titolo unito su tutta la larghezza, intestazione rossa,
  colonna giorno unita con testo ruotato, colori identici, colonne 15 caratteri, riquadri `freeze` su intestazione e prime
  due colonne, pagina A4 orizzontale adattata a una pagina).

---

## 13. Frontend

SPA React (TanStack Router, file-based routing in `frontend/src/routes/`) servita in SSR da Node; chiamate a `/api/*`
con `fetch` e token Bearer in `sessionStorage` (`authToken`, `authRole`). Un 401 (tranne sul login) cancella il token e
riporta al login. Pagine:

| Route | Contenuto |
|---|---|
| `/` | Login (email, password); salva token e ruolo |
| `/menu` | Panoramica con le schede (Setup, Disponibilità, Orario, Dati scuola) |
| `/setup` | Upload dei file, anteprima, avvisi con correzione in tempo reale, aggiunta date, salvataggio |
| `/disponibilita` | Selettore settimana, griglia ore × giorni per docente, «copia dalla settimana precedente», stato di completamento |
| `/orario` | Selettore settimana; punteggio e livello; tabella classi × ore per giorno (schede lunedì-venerdì), colori teoria/pratica, conflitti evidenziati, clic su docente per evidenziarne le presenze, **Genera nuovo orario / Genera da zero / Approva settimana**, azioni rapide (deroghe), pannello conflitti/modifica a destra, valutazione a stelle con motivi e pannello «Apprendimento dai voti», **Esporta PDF / Esporta Excel** |
| `/dati/*` | CRUD di calendario, docenti, classi, assegnazioni, accoppiamenti (il campo «Singola classe in stage» è un menu di classi) |
| `/account` | Cambio password (attuale, nuova ≥ 8 caratteri, conferma) |

Interfaccia **SEGRETERIA** (`isSegreteria()`): nel menu laterale restano Panoramica, Disponibilità, Orario; la
schermata Orario è in sola lettura (niente Genera/Approva/azioni rapide/pannello laterale, celle non cliccabili);
etichetta utente «Segreteria / Sola lettura». In basso nel menu: «Cambia password» ed «Esci».

Colonna di stage nella tabella orario: cella gialla (`bg-yellow-300`, bordo `border-yellow-500`) con «STAGE».

---

## 14. Copilota AI (chat) — funzione presente ma disattivata

`POST /chat/send` (solo ADMIN, risposta SSE: `thinking`, `clarification`, `suggestion`, `ready`, `error`) e
`GET /chat/history/{w}`. Il parsing usa l'API Claude con tool-use: una funzione per intento — `move_lesson`,
`reduce_workload`, `exclude_day`, `set_soft_constraint`; chiede chiarimenti se mancano parametri. Gli intenti sono
applicati davvero (rigenerazione in sul posto con vincoli aggiuntivi H7/H8, spostamento di lezione, tetto di ore
consecutive teoria). Nel frontend è nascosto dal flag `COPILOT_ENABLED = false` in `orario.tsx`. Richiede
`ANTHROPIC_API_KEY`.

---

## 15. Deploy e operazioni

- **Stack:** `docker-compose.yml` con `postgres:15-alpine` (volume dati, healthcheck), `backend` (Dockerfile Python 3.13
  con Pango/Cairo/fonts), `frontend` (Node 22, build multi-stage, `node .output/server/index.mjs`, porta 3000) e `caddy:2`
  (80/443, `DOMAIN` da env). Solo Caddy espone porte. Il `Caddyfile` instrada `/api/*`, `/health`, `/docs`, `/openapi.json`,
  `/redoc` al backend `:8000` e il resto al frontend.
- **Configurazione:** `.env.production` (non versionato; da `deploy/generate-secrets.sh`): `DOMAIN`, `POSTGRES_USER`,
  `POSTGRES_PASSWORD`, `POSTGRES_DB`, `DATABASE_URL`, `ANTHROPIC_API_KEY`, ecc.
- **Primo avvio:** `deploy/up.sh`; poi creare gli utenti con `deploy/create-admin.sh` (§4); su VM con 1 GB di RAM
  `deploy/setup-swap.sh` (4 GB di swap).
- **Aggiornamento:** sulla VM `deploy/update.sh` (`git pull --ff-only`, `docker compose up -d --build`, `image prune`).
  Su una VM da 1 GB la build è lenta (decine di minuti, `pip install` incluso); in alternativa costruire le immagini
  altrove. All'avvio del backend la colonna `admin.role` viene aggiunta se manca.
- **Backup:** `deploy/backup-db.sh` (dump locale, cron notturno), `deploy/backup-offsite.sh` (Object Storage, settimanale,
  da configurare), `deploy/restore-db.sh`, `deploy/dump-local-db.sh`.
- **Produzione attuale:** VM Oracle Always Free, `https://130-110-14-231.sslip.io`. `sslip.io` risolve un nome solo se
  contiene l'IP; per un nome pulito serve un dominio proprio (record A verso l'IP) e aggiornare `DOMAIN`.
- **Sviluppo locale:** `python -m venv .venv && .venv/bin/pip install -r requirements.txt`;
  `DATABASE_URL=sqlite:///./dev.db .venv/bin/python -m uvicorn app.main:app --port 8000`; frontend
  `cd frontend && VITE_API_BASE_URL=http://localhost:8000 npm run dev` (porta 8080). Con Anaconda l'import di `ortools`
  può andare in segfault: usare un venv di sistema.

---

## 16. Test e criteri di accettazione

`pytest` (`tests/`, SQLite temporaneo, `tests/conftest.py` esclude l'autenticazione dall'API client e ricrea lo schema a
ogni test). Copertura: solver (vincoli rigidi e soft, accoppiate, chiusure, stage, contrattisti: monte ore superabile,
disponibilità mai violata, moltiplicatore di scarsità), parser (CSV e PDF sintetico), validazione, endpoint `/api/*`
end-to-end, ruoli (`tests/test_roles.py`), cambio password (`tests/test_auth_password.py`), esportazione, valutazioni e
pesi. **Stato noto:** 6 test falliscono già su un checkout pulito (accoppiate con docente condiviso, blocchi pratica,
conflitti dettagliati, quick action `override_availability`, modifica di un solo lato di una coppia); non sono
regressioni delle funzioni descritte qui.

Criteri di accettazione di un orario generato (verificabili): (1) ogni classe ha esattamente le ore del giorno (o il
conflitto `classe_unfilled` lo dice); (2) nessun docente in due classi nella stessa ora (salvo accoppiata); (3) nessuna
lezione per classi in stage, che nell'export mostrano «STAGE» in giallo; (4) nessuna lezione di un contrattista fuori
disponibilità; (5) le ore dell'assegnazione sono vicine al target e comunque non oltre il residuo per gli assunti;
(6) il PDF ha la stessa struttura del modello §12.

---

## 17. Limiti noti e non implementato

- **Multi-tenant e Super-Admin**: non implementati (single-tenant, `scuola_id` fisso `sch_1`).
- **Portale docenti** (login docente, inserimento disponibilità in autonomia, «conferma orario settimana precedente»,
  scadenza settimanale con eredità automatica lato docente): non implementato; le disponibilità le inserisce
  l'amministratore o la segreteria. L'eredità dalla settimana precedente esiste già in lettura (§6).
- **Sostituzioni in tempo reale per malattia**: non implementate.
- **Memoria permanente delle preferenze** (`preferenze_ai_memory`): tabella presente ma non usata dal solver.
- **Annullamento di un'approvazione**: non esiste; per correggere ore scalate per errore si modifica `ore_erogate` dalla pagina «Assegnazioni».
- **Nomi docente**: il cognome nel tabellone è la prima parola di «COGNOME NOME»; i cognomi composti vengono troncati.
- **Cache del setup** in memoria del processo (si perde al riavvio).
- **Template PDF del calendario**: il parser dipende da coordinate fisse (§5.2).
- **Materie reali**: i dati importati usano spesso una materia segnaposto («Materia da definire»); i vincoli su teoria/pratica
  hanno senso pieno solo con le materie reali configurate.
- **Integrazione registri elettronici** (Spaggiari, ClasseViva, Axios): roadmap v2.
- La generazione su VM con 1 GB di RAM è lenta sotto carico; il solver ha un timeout di 60 s.
