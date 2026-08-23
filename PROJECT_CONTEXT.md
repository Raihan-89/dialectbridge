# DialectBridge — Full Project Context

A Django application that converts/migrates a full SQL database between **SQL Server (MSSQL)** and **PostgreSQL** in **both directions**.

It has two distinct capabilities:

1. **SQL text conversion** — paste raw SQL, get converted SQL back (one statement or a script).
2. **Live database migration** — connect two real databases, migrate **everything** end-to-end (schema, data, views, functions, procedures, triggers, synonyms, partitions, user-defined types, roles/grants), then verify.

---

## Tech Stack

| Layer | Choice |
|---|---|
| Web framework | Django 6.1 |
| API | Django REST Framework 3.18 |
| DB (app storage) | SQLite (`db.sqlite3`) |
| MSSQL driver | pymssql 2.3 |
| PostgreSQL driver | psycopg2-binary 2.9 |
| SQL transpiler | sqlglot 30.16 |
| Runtime | Python 3 (venv at `venv/`) |

---

## Directory Map

```
sql-conversion-engine/
├── manage.py                 # Django entry point
├── config/                   # Django project settings/URLs
│   ├── settings.py           # INSTALLED_APPS, DATABASES (sqlite), DRF
│   └── urls.py               # routes '/' -> apps.converter.urls, /admin/
├── engine/                   # The conversion/migration engine (pure Python)
│   ├── schema.py             # normalized, dialect-independent data model
│   ├── service.py            # facade for ad-hoc SQL text conversion
│   ├── connectors/           # live DB connections (MSSQL / PostgreSQL)
│   ├── extractors/           # introspect a live DB -> normalized Database
│   ├── translators/          # turn schema/SQL into target-dialect SQL
│   ├── mappers/              # data-type mapping tables
│   ├── migration/            # end-to-end data migration pipeline
│   ├── dialects/             # (empty placeholder)
│   ├── parsers/              # (empty placeholder)
│   └── poc_test.py           # sqlglot experiment script
├── apps/converter/           # Django app: models, API, web UI
│   ├── models.py             # DatabaseConnection, MigrationJob, ConversionJob, MigrationError
│   ├── serializers.py        # DRF serializers
│   ├── views.py              # REST API views
│   ├── web_views.py          # HTML page handlers
│   ├── migration_service.py  # bridge: Django models <-> migration engine
│   ├── urls.py               # API + web routes
│   ├── tests.py              # unit/integration tests for the engine + API
│   ├── tests_migration.py    # migration pipeline smoke tests (fake connector)
│   ├── admin.py              # admin for ConversionJob
│   └── templates/converter/  # shared layout + 8 portal/report pages
└── venv/                     # virtualenv
```

---

## Core Design: The Normalized Schema Model (`engine/schema.py`)

Both extractors produce the **same** neutral structure; the conversion layer reads
this structure and emits target-dialect DDL. The pipeline never needs to know
which database a table came from.

| Dataclass | Holds |
|---|---|
| `Column` | name, native type string, nullable, default, identity seed/increment, computed flag + expression, collation, comment |
| `Constraint` | name + column list (used for PK and UNIQUE) |
| `CheckConstraint` | name + definition (check constraints now keep their real name when present, otherwise get a stable `{table}_chk_{hash}` name) |
| `ForeignKey` | name, columns, ref_table, ref_columns, ON UPDATE/DELETE actions |
| `Index` | name, columns, unique flag, filtered-index `WHERE`, `INCLUDE (...)` columns, clustered flag, index type (columnstore/spatial/xml/fulltext) |
| `PartitionChild` | a concrete child partition: name + bounds (`FOR VALUES FROM ... TO ...` / `FOR VALUES IN (...)`) |
| `Table` | name (qualified, e.g. `dbo.Employees`), columns, PK, FKs, uniques, indexes, checks, partition metadata, temporal (history table) and graph (node/edge) flags |
| `View` | name + definition, `is_materialized` flag + indexes (indexed/mat views) |
| `Routine` | name, kind (`procedure`/`function`), full CREATE source, params, returns |
| `Trigger` | name, table, timing (BEFORE/AFTER/INSTEAD OF), events, definition, `is_ddl` + `ddl_scope` (object/database/server) |
| `Sequence` | name, start_value, increment, owned_by, current_value, native data type, cycling flag |
| `UserType` | user-defined type: kind (`domain`/`alias`/`enum`/`composite`/`table_type`/`clr`), base type, default, nullable, CHECK constraints, enum values |
| `Synonym` | name, target_object, target_kind (table/view/procedure/function/...) |
| `Principal` | a database-level user or role: name, kind, system flag, role memberships |
| `Permission` | a GRANT/DENY/REVOKE: principal, securable, object, action, grant type, `WITH GRANT OPTION` |
| `Database` | name, dialect (`tsql`/`postgres`), lists of all the above + warnings |

Key methods on `Database`:

- `table_by_name()` — case-insensitive lookup
- `type_by_name()` — find a user-defined type by qualified name or bare last segment
- `all_tables_in_dependency_order()` — **topological sort** so referenced (parent) tables come before referencing (child) tables; cycle-safe for self-referencing FKs
- `to_dict()` — serialize for the API/report storage

---

## The End-to-End Migration Pipeline (`engine/migration/orchestrator.py`)

```
extract source schema
  -> convert to target-dialect DDL
  -> create target schemas
  -> apply types/sequences (CREATE DOMAIN / TYPE / SEQUENCE) + tables + indexes
  -> copy data table-by-table (identity preserved, sequences re-seeded)
  -> apply referential DDL (FKs/checks) + views/functions/procs/triggers + security
  -> verify (compare row counts source vs target)
  -> reconcile (ask the target which objects it actually holds)
  -> produce a per-object report
```

Deliberate ordering choices:

- **Target schemas are created for every migrated object**, not only for the
  ones that own tables — a view, routine, trigger, sequence, type or synonym in
  a schema without tables used to fail with `schema ... does not exist` after
  the whole data copy had already run.
- **Reconciliation is the last phase.** Row counts only prove the data landed;
  the reconciliation pass queries the target catalog (`pg_class`/`pg_proc`/
  `pg_trigger`, or `sys.objects`) and records an explicit `failed` result for
  every source table/view/function/procedure/sequence/trigger/synonym the
  target does not hold. An object that disappeared without an error — a CREATE
  that reported success but left nothing behind — can no longer pass unnoticed.
  Severity follows intent: an object the engine **declined on purpose** (an
  unconvertible construct it refuses to guess at) already carries a warning
  explaining why, so it is recorded as `skipped` and the run still completes;
  only an object that vanished with no explanation is recorded as `failed`. When the target cannot be
  inventoried (unknown dialect, refused catalog query, a connector that returns
  nothing) the pass is skipped rather than reporting phantom losses.
- **Per-table copy times are stored, not just streamed.** Each `data_results`
  entry carries `duration_seconds` plus a formatted `duration_display`, mirrored
  onto the matching row-count verification row, and the summary carries the
  copy phase's wall-clock `data_seconds`/`data_duration` (a sum of the per-table
  times would overcount by the worker count when tables copy in parallel). The
  live progress payload only exists while a job runs, so a finished report used
  to show no timings at all.
- **Object identity is truncation-aware.** PostgreSQL caps identifiers at 63
  bytes, so a longer SQL Server name is created there in the shortened,
  hash-suffixed form `pg_ident()` produces. Reconciliation and the Verify
  page's pairing both fold names through the same truncation, so a long-named
  routine or view is recognised as migrated instead of appearing missing on one
  side and unexpected on the other.
- **A missing relation is confirmed against the live source** before the report
  blames it. `_missing_source_dependency` first checks the extracted inventory,
  then probes the source itself (`OBJECT_ID()` / `to_regclass()`, cached per
  reference). Only an object that is absent from both is called stale; anything
  the source still holds is reported as a genuine migration failure with the
  original error, instead of being quietly replaced by a placeholder.

- **FKs applied AFTER the data copy** so parent/child insert order never matters.
- **Check constraints applied after data** so the migration doesn't fail on data that already satisfies the constraint in the source.
- **User-defined types and standalone sequences are created before any table** that references them (the DDL builder already emits them first).
- **`reset_target` option** (destructive, user opts in): PostgreSQL drops the target schemas `CASCADE`; SQL Server instead drops FKs, then tables, then views/procedures/functions/triggers inside the affected schemas (owner schemas like `dbo` cannot themselves be dropped). Re-runs are then clean.
- **PostgreSQL `search_path`** is set to the migrated schemas so views/routines that reference tables by bare name still resolve.

The report (`MigrationReport`) contains per-object results (kind, name, status, detail, rows copied/failed, copy duration), a row-count verification table, warnings, and a summary. This is stored as JSON on `MigrationJob.report`. An optional progress callback reports the major phases (extract, convert, structural DDL, data copy, objects, verification and report persistence) to the web job.

---

## Module-by-Module Responsibilities

### 1. `engine/connectors/` — Live DB connections

- `base.py` — `DatabaseConnector` ABC + `ConnectorError`. Uniform surface: connect/test/close, `execute`, `execute_many`, `fetch`, `fetchone`, `_server_version`, `_extract_schema`, `iter_table_rows` (batched), `count_rows`, `set_identity_insert`, `max_value`, `seed_identity`, `quote_ident`. `extract_schema()` auto-connects first. `to_int()` coerces DB-API values to int, tolerating bigint values some TDS drivers return as raw little-endian bytes.
- `mssql.py` — `MSSQLConnector` on pymssql. PK-backed reads use `SELECT TOP n` with correct lexicographic keyset predicates for simple and composite keys; keyless tables stream the complete result through one cursor. Integer columns are normalized through `to_int()` when the driver returns raw bytes. Connections enable `use_datetime2=True` so PostgreSQL microseconds survive parameter binding. Identity insert uses `SET IDENTITY_INSERT ... ON/OFF`; reseeding uses `DBCC CHECKIDENT`; identifiers use `[...]`.
- `postgres.py` — `PostgresConnector` on psycopg2. PK-backed reads use `LIMIT n` and PostgreSQL tuple comparison for simple/composite keyset pagination; keyless tables stream the complete result through one cursor. Identity insert is a no-op (converted identities are `GENERATED BY DEFAULT`, explicit values always accepted). Reseeding uses `pg_get_serial_sequence` + `setval`; identifiers use `"..."`.
- `__init__.py` — `build_connector(dialect, host, port, database, user, password)` factory accepting `mssql`/`tsql`/`postgres`.

### 2. `engine/extractors/` — Introspection

- `mssql.py` — reads `INFORMATION_SCHEMA` for columns/PK/unique, `sys.*` catalogs for identity seed/increment, computed columns, FK actions, indexes, checks, sequences, user-defined alias types, synonyms, DDL triggers, partition functions/boundaries, temporal/graph flags, database collation, and full view/proc/function/trigger definitions. Rebuilds type strings with lengths/precision (`NVARCHAR(50)`, `DECIMAL(19,4)`, `DATETIME2(3)`), cleans parenthesized defaults, maps FK actions, parses trigger events from the definition header. Also extracts a basic security model (users, roles, role memberships, GRANT permissions). Everything with no clean PostgreSQL equivalent (columnstore / spatial / XML / full-text indexes, CDC, Service Broker, Query Store, TDE, certificates/keys, DENY permissions) is surfaced as a warning.
- `postgres.py` — reads `pg_catalog` / `pg_attribute` / `pg_constraint` / `pg_index` / `pg_proc` / `pg_trigger`. Uses `format_type()` for type fidelity, addresses tables by **OID** so identifiers with spaces/uppercase never break parsing. Wraps view bodies back into `CREATE VIEW ...`, extracts identity sequences into `Sequence` objects, parses trigger timing/events from `pg_get_triggerdef`. Beyond the relational surface it also extracts domains/enums/composite types, standalone sequences (excluding identity/serial-owned ones), materialized views, **event triggers**, partitioned tables (parents + child bounds), and a security model (roles, memberships, object grants).

### 3. `engine/mappers/type_mappings.py` — Type tables

- `MSSQL_TO_POSTGRES_TYPE_OVERRIDES` — types sqlglot gets wrong or leaves unchanged (TINYINT→SMALLINT, BIT→BOOLEAN, MONEY→NUMERIC(19,4), NTEXT→TEXT, DATETIME2→TIMESTAMP, DATETIMEOFFSET→TIMESTAMPTZ, BINARY/VARBINARY/IMAGE→BYTEA, UNIQUEIDENTIFIER→UUID, ...).
- `POSTGRES_TO_MSSQL_TYPE_OVERRIDES` — reverse (BOOLEAN→BIT, UUID→UNIQUEIDENTIFIER, BYTEA→VARBINARY(MAX), SERIAL→INT IDENTITY(1,1), JSON/JSONB→NVARCHAR(MAX), TIMESTAMPTZ→DATETIMEOFFSET, ...).
- `MANUAL_REVIEW_REQUIRED` — types with **no clean equivalent** that must be flagged, never silently converted. MSSQL side: HIERARCHYID, SQL_VARIANT, GEOGRAPHY, GEOMETRY, ROWVERSION, CURSOR. PG side: ARRAY, HSTORE, range types (INT4RANGE, INT8RANGE, NUMRANGE, TSRANGE, TSTZRANGE, DATERANGE), INET, CIDR, MACADDR.
- `BIT_DEFAULT_VALUE_MAP` — value-level fix: `1/0` <-> `true/false` for boolean defaults.
- `MSSQL_TO_PG_COLLATIONS` / `PG_TO_MSSQL_COLLATIONS` — best-effort locale-based collation mapping (e.g. `SQL_Latin1_General_CP1_CI_AS` → `en_US`, `C` → `Latin1_General_CS_AS`). Unknown collations are warned and omitted (target database collation applies).
- `MSSQL_DDL_EVENT_TO_PG_TAG` / `PG_TAG_TO_MSSQL_DDL_EVENT` — MSSQL DDL-trigger event names <-> PostgreSQL event-trigger tags (e.g. `CREATE_TABLE` ↔ `'CREATE TABLE'`).

### 4. `engine/translators/` — SQL generation & translation

- **`sql_builder.py`** — the schema→DDL builder used by the migration engine. Public functions:
  - `build_database_ddl(database, target_dialect)` → `(statements, warnings)` — user-defined types, then sequences, then dependency-ordered tables, then uniques/indexes/FKs/checks, then views/functions/procedures/triggers, then synonyms (table/view synonyms become PG views), then security DDL (roles, users, memberships, grants); finally qualifies identifiers/table refs in bodies.
  - `build_table_ddl` — partitioned tables emit `PARTITION BY RANGE/LIST` plus `CREATE TABLE ... PARTITION OF ... FOR VALUES ...` child statements; warns when the PG partition key is not part of the PK. Computed columns become `GENERATED ALWAYS AS (...) STORED` (PG) or `AS (...) PERSISTED` (T-SQL); identities become `GENERATED BY DEFAULT AS IDENTITY` / `IDENTITY(seed,inc)`; character columns get mapped `COLLATE` clauses. Uses `build_type_ddl` for user-defined types and `build_sequence_ddl` for standalone sequences.
  - `build_unique_constraint_ddl`, `build_index_ddl` (partial/filtered, `INCLUDE`, clustered — clustered is warned as not enforced on PG and skipped for specialized index types), `build_foreign_key_ddl`, `build_check_ddl`, `build_view_ddl` (materialized/indexed views: PG matview + indexes, or T-SQL `WITH SCHEMABINDING` + unique clustered index), `build_function_ddl`, `build_procedure_ddl`, `build_trigger_ddl` (routes DDL triggers to the DDL-trigger translator), `build_type_ddl`, `build_sequence_ddl`, `build_synonym_ddl`, `build_security_ddl` / `_build_grant`.
  - `convert_type(data_type, source_dialect, target_dialect)` → `(target_type, warning)` — regex-driven type mapping (`_MSSQL_TYPES` dict for T-SQL→PG; `_PG_TYPES` regex list for PG→T-SQL). `VARCHAR/NVARCHAR(MAX)` → `TEXT`. Column types are resolved by `_resolve_column_type`, which also handles user-defined types: MSSQL alias types reference the recreated DOMAIN, and PG domains/enums/composites map back to the alias type (or `NVARCHAR(MAX)` with a warning).
  - Identifier safety: `pg_ident()` truncates to PostgreSQL's 63-byte limit with a hash suffix; PG output always double-quotes to preserve case; T-SQL output uses `[...]`.
  - `fix_boolean_predicates` — rewrites `col = 1/0` on BIT/BOOLEAN columns to `col = true/false` inside filtered-index `WHERE`, view and routine bodies.
  - `_translate_top` — rewrites T-SQL `SELECT TOP n` → trailing `LIMIT n` anywhere it appears (statement start or inside scalar subqueries), respecting quotes and parens.
  - `_translate_default` / `_translate_expr` — run the full builtin-function translator (GETDATE→CURRENT_TIMESTAMP, NEWID→gen_random_uuid, `NEXT VALUE FOR seq`→`nextval(...)`, casts, N-literal stripping, `AT TIME ZONE` grouping, bracket<->quote identifiers).
  - `_views_in_dependency_order` — emits views after the views they select from (cycle-safe, otherwise stable), so a view built on another view no longer fails with "relation does not exist".
  - `_qualify_body_refs` / `_qualify_table_refs` — rewrites **bare table/view/synonym/column references inside view/function/procedure/trigger bodies** so case-sensitive PostgreSQL quoting resolves them; masks string literals, already-quoted identifiers, `AS alias` clauses and routine parameters so they are never rewritten.
- **`procedure_translator.py`** — T-SQL <-> PL/pgSQL routine translator.
  - `_tsql_to_plpgsql` — parses header/params/RETURNS, transforms body line-by-line while tracking BEGIN/END, IF/ELSE, WHILE, TRY/CATCH nesting. Handles DECLARE/SET vars, assignment SELECTs, PRINT/RAISERROR (→ `RAISE EXCEPTION` with `%` placeholders + args), RETURN, WAITFOR and temp tables. Genuine SQL Server functions remain PostgreSQL functions. Genuine procedures remain `CREATE PROCEDURE`; each result-returning SELECT becomes an `OPEN ... FOR SELECT` backed by an `INOUT refcursor` parameter because PostgreSQL procedures cannot directly stream result sets.
  - Real-world `OBJECT_DEFINITION` quirks handled: `[schema].[name]` qualifiers, bracketed param types (`@p [int]`, `[decimal](18,2)`), `WITH SCHEMABINDING`/`WITH EXECUTE AS` clauses after `RETURNS`, and statements without terminating semicolons. `_normalize_statement_ends` inserts `;` after `RAISERROR(...)` (balanced-paren aware) and single-line `THROW`/`PRINT`; the loop additionally flushes at paren depth 0 when the next line starts a keyword that can never continue the buffered statement (`RETURN`, a second `SET`, a fresh `SELECT`, ...) while honoring continuations — `SET` after `UPDATE/INSERT/DELETE/MERGE` and `SELECT` after `WITH`/`INSERT` (INSERT...SELECT, CTEs) never split. Multi-line `UPDATE ... SET`, `WITH cte AS (`, and multi-line subqueries stay one statement. Single-statement `IF (cond) stmt;` bodies (with or without `BEGIN`) are handled.
  - Scalar return types are mapped through `convert_type` (`[int]`→INTEGER, `[decimal](18,2)`→NUMERIC(18,2), `[nvarchar](max)`→TEXT); expression-level `SELECT TOP n` (including inside scalar subqueries) becomes a trailing `LIMIT n`, and `+` is treated as string concatenation (`||`) only when a string literal is present so arithmetic like `@i + 1` keeps its plus.
  - Block engine: stack-based IF/WHILE tracking supports single-statement then/else branches, `ELSE IF`, `ELSE BEGIN`, inline `BEGIN ... END` blocks on one line, `END ELSE` on one line, single-line `IF (cond) stmt; ELSE stmt;` (split on the top-level `ELSE`, CASE-ELSE excluded), and cascading `END IF;`. `BEGIN/COMMIT/ROLLBACK TRANSACTION` are **dropped with a warning** (PL/pgSQL functions can't run transaction control and already run in one implicit transaction).
  - `_plpgsql_to_tsql` — reverse: variables get `@` prefixes (skipping SQL keywords), explicit `IN`/`OUT`/`INOUT` modes and defaults are parsed correctly, functions remain functions where appropriate, and PostgreSQL procedures become `CREATE PROCEDURE`. Converter-owned result cursors disappear on round-trip and `OPEN cursor FOR SELECT` becomes an ordinary SQL Server result SELECT. Same-name parameter/column predicates are repaired (`[ProductID] = @ProductID`) without corrupting qualified columns.
  - `translate_routine(sql, source, target, tables)` is the public entry.
- **`trigger_translator.py`** — T-SQL <-> PL/pgSQL triggers.
  - T-SQL AFTER/INSTEAD OF triggers (statement-level, `inserted`/`deleted` pseudo-tables) → PG `FOR EACH ROW` trigger function with `NEW`/`OLD`. Rewrites `INSERT ... SELECT ... FROM inserted` → `INSERT ... VALUES (NEW.x, ...)`, `IN (... IN (SELECT x FROM inserted))` → `= NEW.x`. INSTEAD OF DELETE becomes BEFORE DELETE. The header parser tolerates `WITH EXECUTE AS CALLER` / `NOT FOR REPLICATION` / `WITH APPEND` clauses between the table and the `AS`. Warns when multi-row semantics can't be bridged.
  - Reverse direction rebuilds `CREATE TRIGGER ... ON table AFTER/INSTEAD OF ... AS BEGIN ... END` from the PG trigger function.
- **`ddl_trigger_translator.py`** — T-SQL **database DDL triggers** <-> PostgreSQL **event triggers** (added with the partition/synonym/security work):
  - T-SQL `ON DATABASE FOR CREATE_TABLE, ...` triggers → PG event-trigger function (`RETURNS event_trigger`) + `CREATE EVENT TRIGGER ... WHEN TAG IN (...)` using the DDL event maps. `EVENTDATA()` → `TG_TAG` (with a warning), `ROLLBACK`/`RAISERROR` abort patterns → `RAISE EXCEPTION`.
  - Reverse direction rebuilds a database DDL trigger from the PG event trigger. The extractor only captures the firing point (`ddl_command_start`/`ddl_command_end`), not the exact tag list, so the FOR clause is reconstructed from a **conservative event set** and the user is warned to review it.
- **`functions.py`** — `translate_functions(text, source, target)` — best-effort mapping of built-in function calls in small expressions (GETDATE/NEWID/ISNULL/IIF/CEILING/RAND/SQUARE/DATEADD/DATEDIFF/DATEPART/YEAR/MONTH/DAY/LEN/CHARINDEX/STUFF/CONVERT/PATINDEX → PG equivalents; reverse for the reverse direction). Uses a balanced-paren scanner and recurses into nested calls.
- **`ddl_translator.py`** — wraps **sqlglot** for ad-hoc text conversion (the `/api/convert/` path). `convert_ddl(source_sql, source_dialect, target_dialect)` → `ConversionResult(sql, warnings)`:
  1. Ensures semicolon separators between CREATE TABLE statements (sqlglot's postgres reader silently fails otherwise).
  2. Detects manual-review types from the original source.
  3. Pre-rewrites types sqlglot mangles (TINYINT→SMALLINT).
  4. Runs `sqlglot.transpile(read, write, pretty=True)`.
  5. Post-fixes types, boolean defaults (DEFAULT 1→true), BYTEA length, inline `FOREIGN KEY REFERENCES`, `DEFAULT (1=1)` (→ plain 1/0 on the reverse), literal-cast defaults.

### 5. `engine/service.py` — Conversion facade

- `convert_sql(source_sql, direction, statement_type)` — the single entry point for the API + web UI.
  - Maps `mssql_to_postgres` / `postgres_to_mssql` to (read, write) dialect pairs.
  - Only `ddl` and `dml` are supported in text-conversion mode; `procedure`/`trigger` raise `UnsupportedStatementTypeError` (the migration engine **does** handle these — this is a text-conversion-only gap).

### 6. `engine/migration/data_mover.py` — Data copy

- Reads every source row in 5,000-row batches. Tables with a PK use lexicographic keyset pagination (including composite keys and keys that are not the leading selected columns); keyless tables use a single streaming cursor rather than stopping after the first batch. `int_columns` identifies integer values that some TDS drivers return as raw bytes.
- Builds target INSERTs with quoted identifiers + `%s` placeholders, executed with driver parameter binding — values round-trip exactly, no string interpolation of user data.
- Identity columns populated explicitly so PK values match source; table identity/sequence re-seeded past the highest value afterward.
- If a batch insert fails, falls back row-by-row to isolate the broken row(s).

---

## Django App (`apps/converter/`)

### Models (`models.py`)

| Model | Purpose |
|---|---|
| `DatabaseConnection` | Saved live DB connection (source or target). Password stored **obfuscated** via Django `signing` (keyed on SECRET_KEY), never in plain text. `effective_port()` defaults 1433 (MSSQL) / 5432 (PG). Unique per user+name. |
| `MigrationJob` | A single end-to-end migration run between two saved connections. Status: pending/running/completed/failed/partial. Holds the full per-object `report` JSON, `warnings`, `error_message`, plus persisted `progress_percent` and `progress_stage`. |
| `ConversionJob` | Audit trail of one text-conversion request: direction, statement type, source SQL, converted SQL, warnings, succeeded flag, error message. |
| `MigrationError` | Per-object failure captured from a migration's report for analysis: FK to `MigrationJob`, `object_kind` (table/index/constraint/view/function/procedure/trigger/data/object), `object_name`, `error_type` (`sql_error`/`data_copy`), `message`, `detail`, timestamp. |

### REST API (`views.py`, `urls.py`)

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/convert/` | POST | Convert SQL text. Body: `{source_sql, direction, statement_type}`. Returns converted SQL + warnings, saves a `ConversionJob`. Status 200 / 501 (unsupported type) / 422 (error). |
| `/api/jobs/` | GET | List past conversions. |
| `/api/jobs/{id}/` | GET | Conversion detail. |
| `/api/connections/` | GET/POST | List / create saved connections. |
| `/api/connections/{id}/` | GET/PATCH/DELETE | Detail / update / delete. |
| `/api/connections/{id}/test/` | POST | Verify the connection, returns server version. |
| `/api/migrations/` | POST | Run a migration: `{source, target, copy_data, reset_target, name}`. Synchronous; saves full report. |
| `/api/migrations/{id}/` | GET | Migration report. |

### Web UI (`web_views.py` + templates)

| Page | Route | Purpose |
|---|---|---|
| Convert form | `/` | Paste SQL, pick direction + statement type, see result + warnings. Sample DDL/DML loaders. |
| History | `/history/` | List of past conversion jobs. |
| Connections | `/connections/` | Create/test/delete saved connections. |
| Migrate | `/migrate/` | Pick source + target connections, start a background web migration, and browse complete history by serial number. Prevents a second web migration while one is running. |
| Migration report | `/migrate/{pk}/` | Persisted summary, live progress while running, row-count verification, schema/data results, warnings and captured errors. |
| Migration status | `/migrate/{pk}/status/` | Lightweight JSON polled by the report page (`status`, stage, percentage, finished flag). |
| Errors | `/errors/` | Per-object migration failures across all jobs, filterable by kind/job/keyword, with counts per object kind. |
| Verify | `/verify/` | Live read-only source/target comparison for overview, tables, columns, views, functions, procedures, triggers, indexes, constraints, sequences, types, synonyms, security and row counts. Definitions are expandable in place. |
| Data | `/data/` | Live side-by-side table-row browser. Aligns by a shared primary key where available, highlights differing values, paginates at 50 rows, and offers exhaustive whole-table fingerprint verification with downloadable JSON evidence. |

Supporting read-only JSON routes are `/verify/{pk}/{section}/`, `/data/{pk}/tables/`, `/data/{pk}/rows/` and `/data/{pk}/checksum/`.

### Bridge (`migration_service.py`)

- `connector_for(connection)` — builds a live connector from a saved `DatabaseConnection` and loads its Django-signed credential.
- `test_connection(connection)` — returns server/version info.
- `run_migration(source, target, copy_data, reset_target)` — runs the `MigrationOrchestrator` and returns the serialized report.
- `assess_migration(source, target)` — performs read-only source extraction and target connectivity checks, generates all target DDL, classifies blockers/warnings and returns an assessment shown by **Assess & preview first**.
- `run_migration(...)` accepts an optional progress callback used by the background web runner. The API path continues to run synchronously.
- `record_migration_errors(job, report)` — syncs `MigrationError` rows for a job: deletes the job's prior errors, walks the report's `schema_results`/`data_results`, and records each failure (data rows failed → `data_copy`, otherwise `sql_error`).

---

## Tests

- `apps/converter/tests.py` — engine unit/integration tests: DDL both directions, manual-review flagging, computed columns, service facade, procedure translation (PG→T-SQL table function → procedure, scalar function, explicit IN params, parameter/column collisions), trigger translation both directions (including INSTEAD OF DELETE → BEFORE DELETE, dollar-quoted function bodies), API endpoint tests (200/501/422), `MigrationError` capture + errors page, responsive layout, and regression tests for the real-world migration fixes:
  - Routine body parsing: no-semicolon `SET`/assignment + `RETURN` splits, single-line `IF...ELSE`, inline `BEGIN...END ELSE BEGIN...END`, `END ELSE`, multi-line UPDATE/CTE preservation, `SELECT TOP`→`LIMIT` (incl. scalar subqueries), string concat vs numeric `+`, bracketed `OBJECT_DEFINITION` params/returns, `WITH SCHEMABINDING`, unparenthesized `IF` with single-statement body.
  - Types/defaults: MAX→TEXT, `CONVERT([nvarchar],...)`→`CAST(... AS TEXT)`, `SYSUTCDATETIME()` → `(CURRENT_TIMESTAMP AT TIME ZONE 'UTC')`, `NEXT VALUE FOR` → `nextval(...)`, boolean filtered-index predicates.
  - Schema features: alias type→DOMAIN and reverse, domains with CHECK (warning on reverse), enums warned not created, sequences (both directions, current-value re-seeding), partitioned tables (PG emits `PARTITION BY` + children; T-SQL warns), indexed/mat views both directions, `INCLUDE` columns, clustered indexes (emitted to T-SQL, warned on PG), specialized index skip, collations (mapped / reverse / unknown warned), synonyms (table→view, procedure warned, bracketed + 4-part targets normalized), security (roles/users/memberships/grants both directions, reserved `public` role skipped, routine grants resolved via `pg_proc`).
  - DDL triggers: T-SQL DDL trigger → event trigger with `WHEN TAG IN (...)`; event trigger → T-SQL DDL trigger with conservative event set warning.
  - Connector robustness: legacy SQL Server skips optional catalogs, index probes tolerate missing columns, raw-bigint bytes coerced via `to_int()`.
  - Trigger header tolerance: `WITH EXECUTE AS`/`NOT FOR REPLICATION`; compact trigger bodies with `RAISERROR` → `RAISE EXCEPTION`.
  - Reported real-migration routine failures (`ReportedMigrationFailureTests`): `THROW n, 'msg, with commas', s` keeps the whole literal; a body opening directly with `AS BEGIN TRY` stays block-balanced; `INSERT @tablevar VALUES` gains its `INTO`; multi-line `DECLARE @a t, @b t` declares every variable; `IF (expr) IS NOT NULL` stays one condition; a multi-line `UPDATE ... SET` on an `ELSE` line stays one statement; `MERGE ... WHEN MATCHED THEN UPDATE` is never split; unbalanced bodies are auto-closed with a warning; routine parameters/locals are never rewritten into quoted column references.
  - Second reported batch (`SecondRoundMigrationFailureTests`): `END;`/`BEGIN TRY;` written with a semicolon still close/open their block; a misplaced `ELSE` gets its missing `END;` from the block-repair pass; a `(` inside a string literal no longer desynchronises the call scanner (which silently deleted SQL); `CHAR(n)` becomes `chr(n)` in value position but stays a type after `AS`/in DDL; `STUFF((SELECT sep + col ... FOR XML PATH('')), 1, n, '')` becomes `string_agg`; other `FOR XML` shapes and SQL Server metadata scripting (`QUOTENAME`/`PARSENAME`/...) are reported for manual rewrite instead of guessed.
  - View dependencies (`ViewDependencyTests`): a view selecting from another view is schema-qualified and quoted, and is created after the view it depends on (cycles still emit every view).
- `apps/converter/tests_migration.py` — end-to-end migration pipeline smoke tests using an in-memory fake connector (no live DB): full pipeline + row verification, batched inserts, `reset_target` schema drops, complete keyless streaming, composite-key pagination, non-leading key columns and SQL Server `DATETIME2` binding.
- `apps/converter/tests_verification.py` — live-comparison service tests: schema/name pairing, table discovery, primary-key row alignment, changed-value detection and order-independent exhaustive fingerprints.

Current verification: `python manage.py test` runs **260 tests**; `python manage.py check` reports no issues.

---

## Known Limitations / Design Notes

- `engine/dialects/` and `engine/parsers/` are **empty placeholders**.
- Text-conversion mode (`service.py`) supports only `ddl`/`dml`; `procedure`/`trigger` statement types are **disabled in the web UI** ("coming soon") and return 501 from the API — even though the migration engine translates those object types.
- Manual-review types are **flagged with warnings, never silently converted**.
- Trigger translation is best-effort: statement-level vs row-level semantics and multi-row `FROM inserted/deleted` patterns produce warnings for manual review.
- DDL trigger translation is best-effort: PostgreSQL event triggers cannot reconstruct the rich `EVENTDATA()` XML (approximated by `TG_TAG`), and the reverse direction only captures the firing point, so the tag set is reconstructed conservatively with a warning.
- Views whose base tables no longer exist in SQL Server (real databases keep
  them long after the table was dropped) cannot be recreated as real views on
  PostgreSQL. They are preserved as **dependency-free compatibility views** with
  the source's exact column names and types, so the target's object inventory
  still matches. Column names come from `sys.columns`; when SQL Server can no
  longer describe the view, they are read from its SELECT list, and failing that
  a single-column placeholder view is created — a view is never lost.
- Synonyms map to PostgreSQL views for table/view targets; procedure/function synonyms are surfaced as warnings. On the reverse leg, the wrapper views created this way are recognised (single-table, all-columns view) and skipped with a warning — the SQL Server target is expected to already hold the original synonym, so they are never recreated as views.
- SQL Server table types are represented as PostgreSQL composite types; TVP routine parameters become arrays expanded with `unnest()`. Reverse migration restores the composite definition as `CREATE TYPE ... AS TABLE`, the array as a `READONLY` TVP, and `unnest()` row sources as table-parameter reads. General enums, scalar composite uses and CLR types still require review.
- Only `GRANT` permissions are ported; `DENY`/`REVOKE` are surfaced as warnings (SQL Server's deny-everything and per-object permission model has no clean PostgreSQL equivalent).
- Partitioned tables: PostgreSQL requires the partition key to be part of the primary key — the DDL is emitted but flagged when that requirement is violated. SQL Server partition functions with boundary-value semantics (`[lower, upper)`) are migrated as non-partitioned tables with a warning because the range interpretations differ.
- Collation mapping is best-effort locale approximation; unknown collations are omitted (target database collation applies) with a warning.
- Web-started migrations run in an in-process daemon thread, redirect immediately to the job report, persist coarse phase/percentage updates, and are polled by the browser. One running web job is allowed at a time. This is not a durable distributed task queue: a process restart can interrupt an active job. The REST create endpoint remains synchronous.
- The pre-migration assessment is read-only and previews generated DDL, counts objects and classifies conversion findings. It does not yet estimate runtime/storage or reserve capacity.
- The Data portal's exhaustive mode streams every shared value and compares canonical order-independent SHA-256 fingerprints. It can be expensive on large databases. The ordinary browser compares only its current 50-row page; without a shared PK that page is aligned by position and clearly warns the user.
- Incremental/CDC synchronization, restart-safe pause/resume, automated cutover and rollback are not implemented.
- Saved passwords use Django signing/obfuscation rather than external envelope encryption; production deployments should integrate a secrets manager.
- Stored procedures remain stored procedures in both directions. SQL Server result sets use PostgreSQL `INOUT refcursor` outputs and are restored to normal SELECT result sets on reverse migration.
- PostgreSQL routines cannot contain SQL Server transaction control. BEGIN/COMMIT/ROLLBACK TRANSACTION is removed with an explicit warning; trigger self-delete behavior used by an INSTEAD OF DELETE conversion may also be removed when PostgreSQL performs it implicitly.
- The shared template provides a responsive desktop/tablet/mobile UI, direction-specific DDL/DML samples, collapsible mobile navigation, scroll-safe tables/code, progress display and accessible reduced-motion behavior.
- `db.sqlite3` is the portal's own storage for users, saved connections, conversion history, migration jobs/reports and captured errors; deleting it loses those records. Run `python manage.py migrate` after creating/recreating it. Source/target databases remain external and are connected at runtime using saved credentials.

---

## How to Run

```bash
venv/bin/python manage.py runserver        # start Django dev server
venv/bin/python manage.py test             # run the test suite
```

After a fresh clone or if `db.sqlite3` was removed, run `python manage.py migrate` before starting the server. Migration `0005_migrationjob_progress` adds the persisted progress fields; `0006_migrationjob_pending_deletion` adds the recoverable deletion window metadata.
