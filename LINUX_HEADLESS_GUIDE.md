# DialectBridge -- Headless Linux Server Guide

Complete guide to running and using DialectBridge on a Linux server **without a browser**. All operations are performed via the **REST API** using `curl` (or any HTTP client).

---

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [Server Setup](#2-server-setup)
3. [Start the Server](#3-start-the-server)
4. [REST API Overview](#4-rest-api-overview)
5. [SQL Text Conversion](#5-sql-text-conversion)
6. [Manage Database Connections](#6-manage-database-connections)
7. [Run Database Migrations](#7-run-database-migrations)
8. [View Migration Reports](#8-view-migration-reports)
9. [Monitor Migration Progress](#9-monitor-migration-progress)
10. [Conversion History](#10-conversion-history)
11. [SSH Tunneling (Access Web UI Remotely)](#11-ssh-tunneling)
12. [Running Behind a Reverse Proxy (Nginx)](#12-running-behind-nginx)
13. [Production Deployment with Gunicorn](#13-production-deployment-with-gunicorn)
14. [Quick Reference Cheat Sheet](#14-quick-reference-cheat-sheet)

---

## 1. Prerequisites

```bash
# Python 3.8+
python3 --version

# pip
pip3 --version

# curl (for API calls)
curl --version
```

Optional but recommended -- install `jq` for readable JSON output:

```bash
# Debian/Ubuntu
sudo apt install jq

# RHEL/CentOS
sudo yum install jq
```

---

## 2. Server Setup

Clone or copy the project to your server, then set up the virtual environment:

```bash
# Clone or copy the project
git clone <repo-url> dialectbridge
cd dialectbridge

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install Django djangorestframework sqlglot pymssql psycopg2-binary

# Apply database migrations
python manage.py migrate
```

Verify everything works:

```bash
python manage.py test
```

---

## 3. Start the Server

### Development (single process)

```bash
# Bind to all interfaces so the API is reachable
python manage.py runserver 0.0.0.0:8000
```

The API is now available at `http://<your-server-ip>:8000`.

### Keep it running in the background

Use `nohup` or `screen`/`tmux` so the server survives disconnection:

```bash
# Option A: nohup
nohup python manage.py runserver 0.0.0.0:8000 > server.log 2>&1 &

# Option B: screen
screen -S dialectbridge
python manage.py runserver 0.0.0.0:8000
# Ctrl+A, D to detach; screen -r dialectbridge to reattach

# Option C: tmux
tmux new -s dialectbridge
python manage.py runserver 0.0.0.0:8000
# Ctrl+B, D to detach; tmux attach -t dialectbridge to reattach
```

---

## 4. REST API Overview

All endpoints accept and return **JSON**. Include this header on every request:

```
Content-Type: application/json
```

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/convert/` | POST | Convert SQL text |
| `/api/jobs/` | GET | List conversion history |
| `/api/jobs/{id}/` | GET | Single conversion detail |
| `/api/connections/` | GET / POST | List / create DB connections |
| `/api/connections/{id}/` | GET / PATCH / DELETE | Connection detail / update / remove |
| `/api/connections/{id}/test/` | POST | Test a connection |
| `/api/migrations/` | POST | Run a migration (synchronous) |
| `/api/migrations/` | GET | List past migrations |
| `/api/migrations/{id}/` | GET | Full migration report |
| `/migrate/{id}/status/` | GET | Live progress JSON (web endpoint) |

---

## 5. SQL Text Conversion

### MSSQL to PostgreSQL

```bash
curl -s -X POST http://localhost:8000/api/convert/ \
  -H "Content-Type: application/json" \
  -d '{
    "source_sql": "CREATE TABLE Employees (EmployeeID INT IDENTITY(1,1) PRIMARY KEY, IsActive BIT DEFAULT 1, HireDate DATETIME2 DEFAULT GETDATE());",
    "direction": "mssql_to_postgres",
    "statement_type": "ddl"
  }' | jq .
```

**Response:**

```json
{
  "id": 1,
  "direction": "mssql_to_postgres",
  "statement_type": "ddl",
  "source_sql": "CREATE TABLE Employees (...)",
  "converted_sql": "CREATE TABLE Employees (...)",
  "warnings": [],
  "succeeded": true,
  "error_message": "",
  "created_at": "2026-08-17T10:30:00Z"
}
```

### PostgreSQL to MSSQL

```bash
curl -s -X POST http://localhost:8000/api/convert/ \
  -H "Content-Type: application/json" \
  -d '{
    "source_sql": "CREATE TABLE employees (id SERIAL PRIMARY KEY, active BOOLEAN DEFAULT true, hired_at TIMESTAMP DEFAULT NOW());",
    "direction": "postgres_to_mssql",
    "statement_type": "ddl"
  }' | jq .
```

### DML Conversion

```bash
curl -s -X POST http://localhost:8000/api/convert/ \
  -H "Content-Type: application/json" \
  -d '{
    "source_sql": "SELECT GETDATE() AS current_time, NEWID() AS unique_id;",
    "direction": "mssql_to_postgres",
    "statement_type": "dml"
  }' | jq .
```

### Convert a SQL File

Read from a local file and send it:

```bash
# Using shell variable
SQL=$(cat migration_script.sql)

curl -s -X POST http://localhost:8000/api/convert/ \
  -H "Content-Type: application/json" \
  -d "{\"source_sql\": \"$SQL\", \"direction\": \"mssql_to_postgres\", \"statement_type\": \"ddl\"}" | jq .
```

### Save Converted SQL to a File

```bash
curl -s -X POST http://localhost:8000/api/convert/ \
  -H "Content-Type: application/json" \
  -d '{
    "source_sql": "CREATE TABLE orders (id INT IDENTITY PRIMARY KEY, total DECIMAL(10,2));",
    "direction": "mssql_to_postgres",
    "statement_type": "ddl"
  }' | jq -r '.converted_sql' > converted.sql
```

---

## 6. Manage Database Connections

### Create a Connection (MSSQL source)

```bash
curl -s -X POST http://localhost:8000/api/connections/ \
  -H "Content-Type: application/json" \
  -d '{
    "name": "My MSSQL Source",
    "engine": "mssql",
    "host": "192.168.1.100",
    "port": 1433,
    "database": "ProductionDB",
    "username": "sa",
    "password": "YourP@ssw0rd"
  }' | jq .
```

**Response:**

```json
{
  "id": 1,
  "name": "My MSSQL Source",
  "engine": "mssql",
  "host": "192.168.1.100",
  "port": 1433,
  "database": "ProductionDB",
  "username": "sa",
  "effective_port": 1433,
  "created_at": "2026-08-17T10:00:00Z"
}
```

### Create a Connection (PostgreSQL target)

```bash
curl -s -X POST http://localhost:8000/api/connections/ \
  -H "Content-Type: application/json" \
  -d '{
    "name": "My PG Target",
    "engine": "postgres",
    "host": "192.168.1.200",
    "port": 5432,
    "database": "targetdb",
    "username": "postgres",
    "password": "PgP@ssw0rd"
  }' | jq .
```

### Test a Connection

```bash
curl -s -X POST http://localhost:8000/api/connections/1/test/ | jq .
```

**Success response:**

```json
{"ok": true, "message": "Connected successfully"}
```

**Failure response:**

```json
{"ok": false, "error": "Connection refused"}
```

### List All Connections

```bash
curl -s http://localhost:8000/api/connections/ | jq .
```

### Update a Connection

```bash
curl -s -X PATCH http://localhost:8000/api/connections/1/ \
  -H "Content-Type: application/json" \
  -d '{"host": "192.168.1.101"}' | jq .
```

### Delete a Connection

```bash
curl -s -X DELETE http://localhost:8000/api/connections/1/
```

---

## 7. Run Database Migrations

### Full Migration (schema + data)

```bash
curl -s -X POST http://localhost:8000/api/migrations/ \
  -H "Content-Type: application/json" \
  -d '{
    "source": 1,
    "target": 2,
    "copy_data": true,
    "reset_target": false,
    "name": "Production migration"
  }' | jq .
```

> **Note:** The API migration endpoint is **synchronous** -- it blocks until the migration completes. For large databases this can take minutes or hours.

### Schema-Only Migration (no data copy)

```bash
curl -s -X POST http://localhost:8000/api/migrations/ \
  -H "Content-Type: application/json" \
  -d '{
    "source": 1,
    "target": 2,
    "copy_data": false,
    "reset_target": false,
    "name": "Schema only"
  }' | jq .
```

### Destructive Re-run (drop and recreate target schemas)

```bash
curl -s -X POST http://localhost:8000/api/migrations/ \
  -H "Content-Type: application/json" \
  -d '{
    "source": 1,
    "target": 2,
    "copy_data": true,
    "reset_target": true,
    "name": "Clean re-run"
  }' | jq .
```

### Run Migration in Background (using nohup + curl)

Since the API endpoint is synchronous, wrap it to run in the background:

```bash
nohup curl -s -X POST http://localhost:8000/api/migrations/ \
  -H "Content-Type: application/json" \
  -d '{
    "source": 1,
    "target": 2,
    "copy_data": true,
    "reset_target": false,
    "name": "Background migration"
  }' > migration_report.json 2>&1 &

echo "Migration running as PID $!"
```

---

## 8. View Migration Reports

### List All Migrations

```bash
curl -s http://localhost:8000/api/migrations/ | jq .
```

### Get a Specific Migration Report

```bash
curl -s http://localhost:8000/api/migrations/1/ | jq .
```

### Extract Specific Report Sections

```bash
# Migration status
curl -s http://localhost:8000/api/migrations/1/ | jq '.status'

# Schema conversion results
curl -s http://localhost:8000/api/migrations/1/ | jq '.report.schema'

# Data copy results
curl -s http://localhost:8000/api/migrations/1/ | jq '.report.data'

# Row count verification
curl -s http://localhost:8000/api/migrations/1/ | jq '.report.row_counts'

# All warnings
curl -s http://localhost:8000/api/migrations/1/ | jq '.warnings'

# Errors (from separate endpoint)
curl -s http://localhost:8000/api/migrations/1/ | jq '.report.errors'
```

### Save Report to File

```bash
curl -s http://localhost:8000/api/migrations/1/ | jq . > migration_report_1.json
```

---

## 9. Monitor Migration Progress

Web-initiated migrations run in a background thread and expose a live status endpoint:

```bash
# Poll progress every 5 seconds
while true; do
  curl -s http://localhost:8000/migrate/1/status/ | jq .
  sleep 5
done
```

**Progress response:**

```json
{
  "status": "running",
  "progress_percent": 65,
  "progress_stage": "Copying table data"
}
```

### Simple Progress Bar in Terminal

```bash
while true; do
  STATUS=$(curl -s http://localhost:8000/migrate/1/status/)
  PCT=$(echo "$STATUS" | jq -r '.progress_percent')
  STAGE=$(echo "$STATUS" | jq -r '.progress_stage')
  STATE=$(echo "$STATUS" | jq -r '.status')

  echo -ne "\r[$PCT%] $STAGE ($STATE)   "

  if [ "$STATE" = "completed" ] || [ "$STATE" = "failed" ]; then
    echo ""
    break
  fi
  sleep 3
done
```

---

## 10. Conversion History

### List All Conversions

```bash
curl -s http://localhost:8000/api/jobs/ | jq .
```

### Get a Specific Conversion

```bash
curl -s http://localhost:8000/api/jobs/1/ | jq .
```

---

## 11. SSH Tunneling

If you want to access the web UI from your local machine through an SSH tunnel:

```bash
# From your LOCAL machine:
ssh -L 8000:localhost:8000 user@your-server-ip

# Then on the server, start the dev server:
python manage.py runserver 0.0.0.0:8000

# Open http://localhost:8000 in your local browser
```

For a persistent tunnel:

```bash
ssh -fNL 8000:localhost:8000 user@your-server-ip
# -f = background, -N = no remote command, -L = local forwarding
```

---

## 12. Running Behind Nginx

For a production-like setup with HTTPS:

```nginx
# /etc/nginx/sites-available/dialectbridge

server {
    listen 80;
    server_name db.yourdomain.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    server_name db.yourdomain.com;

    ssl_certificate     /etc/ssl/certs/yourdomain.pem;
    ssl_certificate_key /etc/ssl/private/yourdomain.key;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 3600s;
    }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/dialectbridge /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

---

## 13. Production Deployment with Gunicorn

Django's dev server is not suitable for production. Use Gunicorn:

```bash
pip install gunicorn
```

### Update `settings.py` for production

Add your domain to `ALLOWED_HOSTS`:

```python
ALLOWED_HOSTS = ["db.yourdomain.com", "localhost"]
```

### Start with Gunicorn

```bash
# Bind to local port, 4 workers
nohup gunicorn config.wsgi:application \
  --bind 127.0.0.1:8000 \
  --workers 4 \
  --timeout 3600 \
  --access-logfile logs/access.log \
  --error-logfile logs/error.log \
  > gunicorn.log 2>&1 &
```

> Use `--timeout 3600` (1 hour) since the synchronous migration endpoint can run for a long time.

### Systemd Service (recommended)

```ini
# /etc/systemd/system/dialectbridge.service

[Unit]
Description=DialectBridge Database Migration Studio
After=network.target

[Service]
User=www-data
Group=www-data
WorkingDirectory=/opt/dialectbridge
ExecStart=/opt/dialectbridge/venv/bin/gunicorn config.wsgi:application \
    --bind 127.0.0.1:8000 \
    --workers 4 \
    --timeout 3600
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable dialectbridge
sudo systemctl start dialectbridge
sudo systemctl status dialectbridge
```

---

## 14. Quick Reference Cheat Sheet

```bash
# ─── Setup ───
source venv/bin/activate
python manage.py migrate
python manage.py runserver 0.0.0.0:8000

# ─── Convert SQL ───
curl -s -X POST http://localhost:8000/api/convert/ \
  -H "Content-Type: application/json" \
  -d '{"source_sql":"SELECT GETDATE()","direction":"mssql_to_postgres","statement_type":"dml"}' | jq .

# ─── Create Connection ───
curl -s -X POST http://localhost:8000/api/connections/ \
  -H "Content-Type: application/json" \
  -d '{"name":"src","engine":"mssql","host":"10.0.0.1","port":1433,"database":"mydb","username":"sa","password":"pass"}' | jq .

# ─── Test Connection ───
curl -s -X POST http://localhost:8000/api/connections/1/test/ | jq .

# ─── List Connections ───
curl -s http://localhost:8000/api/connections/ | jq .

# ─── Run Migration ───
curl -s -X POST http://localhost:8000/api/migrations/ \
  -H "Content-Type: application/json" \
  -d '{"source":1,"target":2,"copy_data":true,"reset_target":false}' | jq .

# ─── List Migrations ───
curl -s http://localhost:8000/api/migrations/ | jq .

# ─── Get Report ───
curl -s http://localhost:8000/api/migrations/1/ | jq .

# ─── Check Progress ───
curl -s http://localhost:8000/migrate/1/status/ | jq .

# ─── List Conversion History ───
curl -s http://localhost:8000/api/jobs/ | jq .

# ─── Delete Connection ───
curl -s -X DELETE http://localhost:8000/api/connections/1/

# ─── Run Tests ───
python manage.py test
```

---

## Tips

- **Use `jq`** for readable output. Without it, JSON comes back as a single unformatted line.
- **Pipe converted SQL to a file:** add `| jq -r '.converted_sql' > output.sql` to the convert command.
- **The API is synchronous for migrations.** If your migration takes hours, use the web-initiated background path (via SSH tunnel) or wrap the API call with `nohup`.
- **Log files** are at `logs/dialectbridge-YYYY-MM-DD.log` -- check the dated file when things go wrong.
- **SQLite database** (`db.sqlite3`) stores all portal data (connections, history, reports). Back it up regularly.
