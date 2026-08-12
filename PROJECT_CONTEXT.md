# SQL Conversion Engine — Full Project Context

A Django application that converts/migrates a full SQL database between **SQL Server (MSSQL)** and **PostgreSQL** in **both directions**.

It has two distinct capabilities:

1. **SQL text conversion** — paste raw SQL, get converted SQL back (one statement or a script).
2. **Live database migration** — connect two real databases, migrate **everything** end-to-end (schema, data, views, functions, procedures, triggers), then verify.

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
│   └── templates/converter/  # HTML templates (7 pages)
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
| `Index` | name, columns, unique flag, filtered-index `WHERE` |
| `Table` | name (qualified, e.g. `dbo.Employees`), columns, PK, FKs, uniques, indexes, checks |
| `View` | name + definition |
| `Routine` | name, kind (`procedure`/`function`), full CREATE source, params, returns |
| `Trigger` | name, table, timing (BEFORE/AFTER/INSTEAD OF), events, definition |
| `Sequence` | name, start_value, increment, owned_by |
| `Database` | name, dialect (`tsql`/`postgres`), lists of all the above + warnings |

Key methods on `Database`:

- `table_by_name()` — case-insensitive lookup
- `all_tables_in_dependency_order()` — **topological sort** so referenced (parent) tables come before referencing (child) tables; cycle-safe for self-referencing FKs
- `to_dict()` — serialize for the API/report storage

---

## The End-to-End Migration Pipeline (`engine/migration/orchestrator.py`)

```
extract source schema
  -> convert to target-dialect DDL
  -> apply structural DDL (schemas, CREATE TABLE, indexes)
  -> copy data table-by-table (identity preserved, sequences re-seeded)
  -> apply referential DDL (FKs/checks) + views/functions/procs/triggers
  -> verify (compare row counts source vs target)
  -> produce a per-object report
```

Deliberate ordering choices:

- **FKs applied AFTER the data copy** so parent/child insert order never matters.
- **Check constraints applied after data** so the migration doesn't fail on data that already satisfies the constraint in the source.
- **`reset_target` option** (destructive, user opts in): drops target schemas/objects before migrating so re-runs are clean.
- **PostgreSQL `search_path`** is set to the migrated schemas so views/routines that reference tables by bare name still resolve.

The report (`MigrationReport`) contains per-object results (kind, name, status, detail, rows copied/failed), a row-count verification table, warnings, and a summary. This is stored as JSON on `MigrationJob.report`.

---

## Module-by-Module Responsibilities

### 1. `engine/connectors/` — Live DB connections

- `base.py` — `DatabaseConnector` ABC + `ConnectorError`. Uniform surface: connect/test/close, `execute`, `execute_many`, `fetch`, `fetchone`, `_server_version`, `_extract_schema`, `iter_table_rows` (batched), `count_rows`, `set_identity_insert`, `max_value`, `seed_identity`, `quote_ident`. `extract_schema()` auto-connects first.
- `mssql.py` — `MSSQLConnector` on pymssql. Batched reads use `SELECT TOP n` + keyset pagination on the PK (`WHERE pk > last`). Identity insert via `SET IDENTITY_INSERT ... ON/OFF`. Reseed via `DBCC CHECKIDENT`. Identifier quoting with `[...]`.
- `postgres.py` — `PostgresConnector` on psycopg2. Batched reads use `LIMIT n` + keyset pagination. Identity insert is a no-op (converted identities are `GENERATED BY DEFAULT`, explicit values always accepted). Reseed via `pg_get_serial_sequence` + `setval`. Identifier quoting with `"..."`.
- `__init__.py` — `build_connector(dialect, host, port, database, user, password)` factory accepting `mssql`/`tsql`/`postgres`.

### 2. `engine/extractors/` — Introspection

- `mssql.py` — reads `INFORMATION_SCHEMA` for columns/PK/unique, `sys.*` catalogs for identity seed/increment, computed columns, FK actions, indexes, checks, and full view/proc/function/trigger definitions. Rebuilds type strings with lengths/precision (`NVARCHAR(50)`, `DECIMAL(19,4)`, `DATETIME2(3)`), cleans parenthesized defaults, maps FK actions, parses trigger events from the definition header.
- `postgres.py` — reads `pg_catalog` / `pg_attribute` / `pg_constraint` / `pg_index` / `pg_proc` / `pg_trigger`. Uses `format_type()` for type fidelity, addresses tables by **OID** so identifiers with spaces/uppercase never break parsing. Wraps view bodies back into `CREATE VIEW ...`, extracts identity sequences into `Sequence` objects, parses trigger timing/events from `pg_get_triggerdef`.

### 3. `engine/mappers/type_mappings.py` — Type tables

- `MSSQL_TO_POSTGRES_TYPE_OVERRIDES` — types sqlglot gets wrong or leaves unchanged (TINYINT→SMALLINT, BIT→BOOLEAN, MONEY→NUMERIC(19,4), NTEXT→TEXT, DATETIME2→TIMESTAMP, DATETIMEOFFSET→TIMESTAMPTZ, BINARY/VARBINARY/IMAGE→BYTEA, UNIQUEIDENTIFIER→UUID, ...).
- `POSTGRES_TO_MSSQL_TYPE_OVERRIDES` — reverse (BOOLEAN→BIT, UUID→UNIQUEIDENTIFIER, BYTEA→VARBINARY(MAX), SERIAL→INT IDENTITY(1,1), JSON/JSONB→NVARCHAR(MAX), TIMESTAMPTZ→DATETIMEOFFSET, ...).
- `MANUAL_REVIEW_REQUIRED` — types with **no clean equivalent** that must be flagged, never silently converted (HIERARCHYID, SQL_VARIANT, GEOGRAPHY, GEOMETRY, ROWVERSION, CURSOR; PG ARRAY, HSTORE, ranges, INET, CIDR, MACADDR).
- `BIT_DEFAULT_VALUE_MAP` — value-level fix: `1/0` <-> `true/false` for boolean defaults.

### 4. `engine/translators/` — SQL generation & translation

- **`sql_builder.py`** — the schema→DDL builder used by the migration engine. Public functions:
  - `build_database_ddl(database, target_dialect)` → `(statements, warnings)` — dependency-ordered tables, then uniques/indexes/FKs/checks, then views/functions/procedures/triggers; qualifies identifiers/table refs in bodies.
  - `build_table_ddl`, `build_unique_constraint_ddl`, `build_index_ddl`, `build_foreign_key_ddl`, `build_check_ddl`, `build_view_ddl`, `build_function_ddl`, `build_procedure_ddl`, `build_trigger_ddl`.
  - `convert_type(data_type, source_dialect, target_dialect)` → `(target_type, warning)` — regex-driven type mapping (`_MSSQL_TYPES` dict for T-SQL→PG; `_PG_TYPES` regex list for PG→T-SQL). `VARCHAR/NVARCHAR(MAX)` → `TEXT`.
  - Identifier safety: `pg_ident()` truncates to PostgreSQL's 63-byte limit with a hash suffix; PG output always double-quotes to preserve case; T-SQL output uses `[...]`.
  - `fix_boolean_predicates` — rewrites `col = 1/0` on BIT/BOOLEAN columns to `col = true/false` inside filtered-index `WHERE`, view and routine bodies.
  - `_translate_top` — rewrites T-SQL `SELECT TOP n` → trailing `LIMIT n` anywhere it appears (statement start or inside scalar subqueries), respecting quotes and parens.
  - `_translate_default` / `_translate_expr` — best-effort regex expression translation (GETDATE→CURRENT_TIMESTAMP, NEWID→gen_random_uuid, casts, N-literal stripping, bracket<->quote identifiers).
  - `_qualify_body_refs` / `_qualify_table_refs` — rewrites **bare table/column references inside view/function/procedure/trigger bodies** so case-sensitive PostgreSQL quoting resolves them; masks string literals, already-quoted identifiers and `AS alias` clauses so aliases are never rewritten.
- **`procedure_translator.py`** — T-SQL <-> PL/pgSQL routine translator.
  - `_tsql_to_plpgsql` — parses header/params/RETURNS, transforms body line-by-line while tracking BEGIN/END, IF/ELSE, WHILE, TRY/CATCH nesting. Handles DECLARE/SET vars, assignment SELECTs, PRINT/RAISERROR (→ `RAISE EXCEPTION` with `%` placeholders + args), RETURN, WAITFOR, result SELECTs→RETURN QUERY, temp tables. Infers `RETURNS TABLE(...)` column types from the source schema. Returns `(converted, warnings)` — never silently drops a statement.
  - Real-world `OBJECT_DEFINITION` quirks handled: `[schema].[name]` qualifiers, bracketed param types (`@p [int]`, `[decimal](18,2)`), `WITH SCHEMABINDING`/`WITH EXECUTE AS` clauses after `RETURNS`, and statements without terminating semicolons. `_normalize_statement_ends` inserts `;` after `RAISERROR(...)` (balanced-paren aware) and single-line `THROW`/`PRINT`; the loop additionally flushes at paren depth 0 when the next line starts a keyword that can never continue the buffered statement (`RETURN`, a second `SET`, a fresh `SELECT`, ...) while honoring continuations — `SET` after `UPDATE/INSERT/DELETE/MERGE` and `SELECT` after `WITH`/`INSERT` (INSERT...SELECT, CTEs) never split. Multi-line `UPDATE ... SET`, `WITH cte AS (`, and multi-line subqueries stay one statement.
  - Scalar return types are mapped through `convert_type` (`[int]`→INTEGER, `[decimal](18,2)`→NUMERIC(18,2), `[nvarchar](max)`→TEXT); expression-level `SELECT TOP n` (including inside scalar subqueries) becomes a trailing `LIMIT n`, and `+` is treated as string concatenation (`||`) only when a string literal is present so arithmetic like `@i + 1` keeps its plus.
  - Block engine: stack-based IF/WHILE tracking supports single-statement then/else branches (incl. `IF (cond) stmt;` on one line), `ELSE IF`, `ELSE BEGIN`, inline `BEGIN ... END` blocks on one line, `END ELSE` on one line, single-line `IF (cond) stmt; ELSE stmt;` (split on the top-level `ELSE`, CASE-ELSE excluded), and cascading `END IF;`. `BEGIN/COMMIT/ROLLBACK TRANSACTION` are **dropped with a warning** (PL/pgSQL functions can't run transaction control and already run in one implicit transaction).
  - `_plpgsql_to_tsql` — reverse: variables get `@` prefixes (skipping SQL keywords), scalar functions become `CREATE FUNCTION`, void/SETOF/TABLE functions become `CREATE PROCEDURE`.
  - `translate_routine(sql, source, target, tables)` is the public entry.
- **`trigger_translator.py`** — T-SQL <-> PL/pgSQL triggers.
  - T-SQL AFTER/INSTEAD OF triggers (statement-level, `inserted`/`deleted` pseudo-tables) → PG `FOR EACH ROW` trigger function with `NEW`/`OLD`. Rewrites `INSERT ... SELECT ... FROM inserted` → `INSERT ... VALUES (NEW.x, ...)`, `IN (... IN (SELECT x FROM inserted))` → `= NEW.x`. Warns when multi-row semantics can't be bridged.
  - Reverse direction rebuilds `CREATE TRIGGER ... ON table AFTER/INSTEAD OF ... AS BEGIN ... END` from the PG trigger function.
- **`functions.py`** — `translate_functions(text, source, target)` — best-effort mapping of built-in function calls in small expressions (GETDATE/NEWID/ISNULL/IIF/CEILING/RAND/SQUARE/DATEADD/DATEDIFF/DATEPART/YEAR/MONTH/DAY/LEN/CHARINDEX/STUFF/CONVERT → PG equivalents; reverse for the reverse direction). Uses a balanced-paren scanner and recurses into nested calls.
- **`ddl_translator.py`** — wraps **sqlglot** for ad-hoc text conversion (the `/api/convert/` path). `convert_ddl(source_sql, source_dialect, target_dialect)` → `ConversionResult(sql, warnings)`:
  1. Ensures semicolon separators between CREATE TABLE statements (sqlglot's postgres reader silently fails otherwise).
  2. Detects manual-review types from the original source.
  3. Pre-rewrites types sqlglot mangles (TINYINT→SMALLINT).
  4. Runs `sqlglot.transpile(read, write, pretty=True)`.
  5. Post-fixes types, boolean defaults (DEFAULT 1→true), BYTEA length, inline `FOREIGN KEY REFERENCES`, `DEFAULT (1=1)`, literal-cast defaults.

### 5. `engine/service.py` — Conversion facade

- `convert_sql(source_sql, direction, statement_type)` — the single entry point for the API + web UI.
  - Maps `mssql_to_postgres` / `postgres_to_mssql` to (read, write) dialect pairs.
  - Only `ddl` and `dml` are supported in text-conversion mode; `procedure`/`trigger` raise `UnsupportedStatementTypeError` (the migration engine **does** handle these — this is a text-conversion-only gap).

### 6. `engine/migration/data_mover.py` — Data copy

- Reads rows from source in ordered batches (keyset pagination on PK when available).
- Builds target INSERTs with quoted identifiers + `%s` placeholders, executed with driver parameter binding — values round-trip exactly, no string interpolation of user data.
- Identity columns populated explicitly so PK values match source; table identity/sequence re-seeded past the highest value afterward.
- If a batch insert fails, falls back row-by-row to isolate the broken row(s).

---

## Django App (`apps/converter/`)

### Models (`models.py`)

| Model | Purpose |
|---|---|
| `DatabaseConnection` | Saved live DB connection (source or target). Password stored **obfuscated** via Django `signing` (keyed on SECRET_KEY), never in plain text. `effective_port()` defaults 1433 (MSSQL) / 5432 (PG). |
| `MigrationJob` | A single end-to-end migration run between two saved connections. Status: pending/running/completed/failed/partial. Holds the full per-object `report` JSON, `warnings`, `error_message`. |
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
| Migrate | `/migrate/` | Pick source + target connections, run full migration, recent runs list. |
| Migration report | `/migrate/{pk}/` | Summary, row-count verification table, schema/data results, warnings. |
| Errors | `/errors/` | Per-object migration failures across all jobs, filterable by kind/job/keyword, with counts per object kind. |

### Bridge (`migration_service.py`)

- `connector_for(connection)` — builds a live connector from a saved `DatabaseConnection` (decrypts password).
- `test_connection(connection)` — returns server/version info.
- `run_migration(source, target, copy_data, reset_target)` — runs the `MigrationOrchestrator` and returns the serialized report.
- `record_migration_errors(job, report)` — syncs `MigrationError` rows for a job: deletes the job's prior errors, walks the report's `schema_results`/`data_results`, and records each failure (data rows failed → `data_copy`, otherwise `sql_error`).

---

## Tests

- `apps/converter/tests.py` — engine unit/integration tests: DDL both directions, manual-review flagging, computed columns, service facade, procedure translation (PG→T-SQL table function → procedure, scalar function), trigger translation both directions, API endpoint tests (200/501/422), `MigrationError` capture + errors page, and regression tests for the real-world migration fixes (MAX→TEXT, boolean filtered-index predicates, real check-constraint names, view aliases, typed params, RETURNS conversion, transaction/RAISERROR handling, balanced IF/ELSE, SELECT TOP→LIMIT, bracketed `OBJECT_DEFINITION` params/returns, `WITH SCHEMABINDING`, no-semicolon statements, nested subquery TOP, string concat vs numeric `+`, no-semicolon `SET`/assignment + `RETURN` splits, single-line `IF...ELSE`, inline `BEGIN...END ELSE BEGIN...END`, `END ELSE`, multi-line UPDATE/CTE preservation).
- `apps/converter/tests_migration.py` — end-to-end migration pipeline smoke tests using an in-memory fake connector (no live DB): full pipeline + row verification, batched keyset inserts, `reset_target` schema drops.

Run: `venv/bin/python manage.py test` (36 tests, all pass).

---

## Known Limitations / Design Notes

- `engine/dialects/` and `engine/parsers/` are **empty placeholders**.
- Text-conversion mode (`service.py`) supports only `ddl`/`dml`; `procedure`/`trigger` statement types are **disabled in the web UI** ("coming soon") and return 501 from the API — even though the migration engine translates those object types.
- Manual-review types are **flagged with warnings, never silently converted**.
- Trigger translation is best-effort: statement-level vs row-level semantics and multi-row `FROM inserted/deleted` patterns produce warnings for manual review.
- Migrations run **synchronously** in the request (large databases will block the request).
- `db.sqlite3` is the app's own storage; source/target databases are connected to at runtime by the user's saved credentials.

---

## How to Run

```bash
venv/bin/python manage.py runserver        # start Django dev server
venv/bin/python manage.py test             # run the test suite
```
