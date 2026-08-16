# SQL Conversion Engine

Convert and migrate full databases between **SQL Server (MSSQL)** and **PostgreSQL** — in both directions.

Two capabilities in one Django app:

1. **SQL text conversion** — paste SQL, pick a direction, get converted SQL back instantly (DDL and DML).
2. **Full database migration** — connect a source and a target database and migrate **everything**: tables, data, views, indexes, constraints, stored procedures, functions and triggers — with row-count verification at the end.

---

## Features

- Bidirectional: MSSQL → PostgreSQL **and** PostgreSQL → MSSQL
- Schema conversion: tables, primary/foreign keys, unique/check constraints, indexes (incl. filtered/partial, `INCLUDE` columns), computed columns, identities, sequences, defaults, collations
- Table partitioning (range/list parents + child partitions), temporal and graph flags
- User-defined types: alias types ↔ domains, with enums/composites flagged for review
- Synonyms → PostgreSQL view wrappers
- Database roles, users, role memberships and object GRANTs both directions
- DDL triggers ↔ PostgreSQL event triggers
- Data migration: batched keyset-paginated row copy, identity preservation, sequence re-seeding
- Object conversion: views, stored procedures, user-defined functions, triggers
- Data type mapping with warning flags for types that have no clean equivalent (never silently converted)
- Manual-review warnings for anything the engine can't translate safely
- Per-object migration report with source vs target row-count verification
- Background web migrations with persisted phase/percentage progress and live polling
- Complete migration history: each migration serial links back to its saved full report
- Captured migration errors on a dedicated **Errors** page (`/errors/`), filterable by object kind / job / keyword
- Optional destructive `reset_target` mode for clean re-runs
- Web UI + REST API
- Conversion history / audit trail
- Responsive portal for desktop, tablet and mobile
- Passwords stored obfuscated (Django `signing`), never in plain text

---

## Tech Stack

| Layer | Technology |
|---|---|
| Framework | Django 6.1 |
| API | Django REST Framework |
| App DB | SQLite |
| MSSQL driver | pymssql |
| PostgreSQL driver | psycopg2 |
| SQL transpiler | sqlglot |
| Python | 3.x (virtualenv at `venv/`) |

---

## Getting Started

### 1. Create and activate a virtualenv

```bash
python3 -m venv venv
source venv/bin/activate        # or: venv\Scripts\activate on Windows
```

### 2. Install dependencies

```bash
pip install -r requirements.txt   # if present; otherwise pip install Django djangorestframework sqlglot pymssql psycopg2-binary
```

### 3. Apply migrations

```bash
python manage.py migrate
```

### 4. Run the server

```bash
python manage.py runserver
```

Open http://127.0.0.1:8000/

### 5. Run the tests

```bash
python manage.py test
```

---

## Usage

### SQL text conversion

Web: go to the home page `/`, paste SQL, pick a direction and statement type, click **Convert**.

API:

```
POST /api/convert/
{
  "source_sql": "CREATE TABLE Employees (EmployeeID INT IDENTITY(1,1) PRIMARY KEY, IsActive BIT DEFAULT 1);",
  "direction": "mssql_to_postgres",
  "statement_type": "ddl"
}
```

Supported directions: `mssql_to_postgres`, `postgres_to_mssql`.
Supported statement types (text mode): `ddl`, `dml`.

### Full database migration

1. Save your connections under **Connections** (`/connections/`) — source and target.
2. Go to **Migrate** (`/migrate/`), pick source + target, optionally copy data and reset the target.
3. Click **Run Migration**. The portal immediately opens the persisted job page and updates its phase and percentage while the migration runs in a background thread.
4. Inspect the saved report: schema results, data results, row-count verification, warnings and captured errors. You can reopen it later by clicking its migration serial number.

Only one web-started migration may run at a time. The REST create endpoint remains synchronous and returns the finished report.

API:

```
POST /api/connections/            {name, engine: "mssql"|"postgres", host, port, database, username, password}
POST /api/connections/{id}/test/  verify a connection
POST /api/migrations/             {source: <id>, target: <id>, copy_data: true, reset_target: false, name: "..."}
```

---

## Web UI Pages

| Page | Route |
|---|---|
| Convert SQL | `/` |
| Conversion history | `/history/` |
| Manage connections | `/connections/` |
| Run migration | `/migrate/` |
| Migration report | `/migrate/{id}/` |
| Migration progress JSON | `/migrate/{id}/status/` |
| Migration errors | `/errors/` |

## API Endpoints

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/convert/` | POST | Convert SQL text (saves history) |
| `/api/jobs/` | GET | List conversion history |
| `/api/jobs/{id}/` | GET | Conversion detail |
| `/api/connections/` | GET / POST | List / create connections |
| `/api/connections/{id}/` | GET / PATCH / DELETE | Connection detail / update / delete |
| `/api/connections/{id}/test/` | POST | Test a connection |
| `/api/migrations/` | POST | Run a migration |
| `/api/migrations/{id}/` | GET | Migration report |

---

## Project Structure

```
config/            Django project (settings, URLs)
engine/            Conversion + migration engine (dialect-neutral)
├── schema.py          Normalized database model
├── service.py         Text-conversion facade
├── connectors/        Live MSSQL / PostgreSQL connections
├── extractors/        Introspect a DB into the normalized model
├── translators/       Target-dialect DDL generation + SQL translation
├── mappers/           Data type mapping tables
└── migration/         End-to-end migration pipeline + data mover
apps/converter/    Django app (models, API, web UI)
```

The conversion pipeline is:

```
extract schema -> convert to target DDL -> create schemas/types/sequences/tables
  -> copy data -> apply FKs/checks/views/procs/triggers/security -> verify row counts -> report
```

Routine kinds are preserved by the live migration engine: functions remain functions and stored procedures remain stored procedures in both directions. PostgreSQL procedures cannot directly stream SQL Server-style result sets, so migrated procedures expose those result sets through `INOUT refcursor` parameters; reverse migration removes the cursor plumbing and restores ordinary SQL Server result `SELECT`s.

---

## Documentation

- `PROJECT_CONTEXT.md` — detailed, module-by-module breakdown of the entire codebase (architecture, responsibilities, data flow).

---

## Known Limitations

- Text conversion mode supports `ddl`/`dml` only; stored procedure and trigger statement types are available through the **migration engine**, not yet through the paste-SQL converter.
- Types with no clean equivalent (e.g. `GEOGRAPHY`, `SQL_VARIANT`, `HIERARCHYID`, `ROWVERSION`, `CURSOR`, PG `ARRAY`/`HSTORE`/range types/`INET`/`CIDR`/`MACADDR`) are flagged for manual review rather than converted.
- Trigger translation is best-effort — statement-level vs row-level semantics are surfaced as warnings.
- DDL trigger ↔ event trigger conversion is best-effort: `EVENTDATA()` maps to `TG_TAG`, and PG event triggers are recreated with a conservative event set that must be reviewed.
- Synonyms only become PostgreSQL views for table/view targets; procedure/function synonyms are surfaced as warnings.
- PostgreSQL enums/composites/table-types/CLR types have no SQL Server equivalent and are flagged (enums/composites map to `NVARCHAR(MAX)` with a warning when used as column types).
- Only `GRANT` permissions are ported; `DENY`/`REVOKE` are surfaced as warnings.
- Partitioned tables: PostgreSQL requires the partition key to be part of the primary key (warned when violated); SQL Server partition functions migrate as non-partitioned tables with a warning.
- Collation mapping is best-effort; unknown collations fall back to the target database collation with a warning.
- PostgreSQL routines cannot execute SQL Server transaction-control statements internally. `BEGIN/COMMIT/ROLLBACK TRANSACTION` is removed with a warning because the PostgreSQL call already runs within the caller's transaction.
- SQL Server procedure result sets are represented as PostgreSQL `refcursor` outputs. Call PostgreSQL procedures inside a transaction and fetch the returned cursors.
- Web migrations use an in-process background thread. This is suitable for the current single-process portal; production multi-worker/restart-safe deployments should move jobs to a durable queue such as Celery/RQ.
- The REST migration-create endpoint is synchronous.
- `db.sqlite3` stores portal users, saved connections, job history and reports. Deleting it deletes that portal data; run `python manage.py migrate` to recreate empty tables.
