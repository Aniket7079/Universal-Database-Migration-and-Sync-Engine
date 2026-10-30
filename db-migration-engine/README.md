# 🔄 Database Migration Engine

A FastAPI + Celery service that copies data between databases (verified: **MySQL → PostgreSQL**, SQLite → SQLite)
with configurable column transforms, PII masking, automatic MySQL→PostgreSQL type coercion, a **dry-run preview**,
conflict strategies, chunked batching, and live progress tracking.

> **Scope note:** this engine copies *data*. It does **not** create tables — the target schema must exist first
> (a ready-made demo schema is in `docker/pg-target-schema.sql`).

---

## ✨ Features

| Feature | Details |
|---|---|
| 🔍 **Dry-run** | Reads the source, applies every transform, returns before/after sample rows. Writes nothing. |
| 🎭 **PII masking** | `mask_email` (`a****@example.com`), `mask_phone` (`******3210`), `mask_full` (`[REDACTED]`) |
| 🔄 **Type coercion** | MySQL `TINYINT(1)` → PostgreSQL `BOOLEAN` automatically, even with no transform configured |
| 📦 **Chunked batches** | Reads/writes in batches (default 1000) — never loads a whole table into memory |
| ⚔️ **Conflict strategies** | `skip` (`ON CONFLICT DO NOTHING`), `overwrite` (upsert), `error` |
| 🛑 **Fails loudly** | Pre-flight check verifies every table exists *before* writing anything; any write error fails the run |
| 📊 **Live progress** | Counters are committed per batch; poll `/runs/{id}/progress` |
| ⚙️ **Async via Celery** | Long migrations run in workers; the API returns `202` immediately |

**Idempotent by design:** with the default `skip` strategy, re-running a config never duplicates rows
(verified: second run → `written 0, skipped N`).

**Not implemented (roadmap):** rollback of a finished run, schema/DDL creation, encrypted connection-string
storage (URLs are currently stored as-is — don't point this at production credentials without adding encryption).

---

## 🛠️ Tech stack
FastAPI · SQLAlchemy 2.0 (Core for data movement) · Celery · Redis · PostgreSQL · MySQL (PyMySQL) · Pydantic v2 · Pytest · Docker Compose

---

## ⚡ Quick start (Docker — everything in one command)

```bash
git clone https://github.com/YOUR_USERNAME/db-migration-engine
cd db-migration-engine
cp .env.example .env
docker compose up -d --build
```

Wait ~30 s (MySQL initialises and seeds itself), then:

```bash
curl http://localhost:8000/health        # {"status":"ok","database":true}
```

Services: API `:8000` (docs at `/docs`) · Flower `:5555` · demo MySQL `:3306` · demo target PostgreSQL `:5433`.

---

## 📡 Walkthrough (demo MySQL → PostgreSQL)

> Inside Docker the worker reaches databases **by service name** (`mysql_source`, `postgres_target`), not `localhost`.

**1. Register the source and target**
```bash
curl -s -X POST localhost:8000/api/v1/connections -H "Content-Type: application/json" -d '{
  "name":"mysql-source","db_type":"mysql",
  "url":"mysql+pymysql://sourceuser:source123@mysql_source:3306/source_db"}'

curl -s -X POST localhost:8000/api/v1/connections -H "Content-Type: application/json" -d '{
  "name":"pg-target","db_type":"postgresql",
  "url":"postgresql://targetuser:target123@postgres_target:5432/target_db"}'
```
Copy the two returned `id` values → `SOURCE_ID`, `TARGET_ID`. Verify them with
`curl -X POST localhost:8000/api/v1/connections/<id>/test`.

**2. Create the migration config**
```bash
curl -s -X POST localhost:8000/api/v1/configs -H "Content-Type: application/json" -d '{
  "name":"users-migration",
  "source_conn_id":"SOURCE_ID","target_conn_id":"TARGET_ID",
  "conflict_strategy":"skip","batch_size":1000,
  "table_mappings":[
    {"source_table":"users","target_table":"members","columns":[
      {"source":"username","target":"username","transform":"lowercase"},
      {"source":"email","target":"email","transform":"mask_email"},
      {"source":"phone","target":"phone","transform":"mask_phone"},
      {"source":"is_active","target":"is_active","transform":"tinyint_to_bool"}]},
    {"source_table":"products","target_table":"products","columns":[]}]}'
```

**3. Dry-run first (writes nothing)**
```bash
curl -s -X POST localhost:8000/api/v1/configs/CONFIG_ID/dry-run | python -m json.tool
```

**4. Run it, then poll**
```bash
curl -s -X POST localhost:8000/api/v1/configs/CONFIG_ID/run -H "Content-Type: application/json" -d '{}'
curl -s localhost:8000/api/v1/runs/RUN_ID/progress
curl -s localhost:8000/api/v1/runs/RUN_ID            # full per-table stats
```

**Expected result in the target DB:** 5 `members` with masked emails/phones and real `boolean` values,
plus 5 `products`.

### Running without Docker for the app (databases still in Docker)
Start only the databases: `docker compose up -d postgres_internal mysql_source postgres_target redis`, then use
`localhost` in the connection URLs (MySQL `:3306`, target PostgreSQL `:5433`) and run:
```bash
python -m venv venv && source venv/bin/activate && pip install -r requirements.txt
uvicorn app.main:app --reload                                   # terminal 1
celery -A app.tasks.celery_app worker --loglevel=info -Q migrations   # terminal 2
```

---

## 🎭 Transform reference

| Transform | Example |
|---|---|
| `none` | unchanged |
| `mask_email` | `alice@example.com` → `a****@example.com` |
| `mask_phone` | `9876543210` → `******3210` |
| `mask_full` | anything → `[REDACTED]` |
| `tinyint_to_bool` / `cast_bool` | `1`/`0` → `true`/`false` |
| `cast_int` / `cast_str` | `"42"` ↔ `42` |
| `uppercase` / `lowercase` / `strip` | string cleanup |

Per column you can also set `"skip": true` (exclude the column) or give a different `target` name (rename).

---

## 🧪 Tests

```bash
pytest tests/ -v        # 39 tests, ~1 s, needs NO external services (uses SQLite)
```
Includes end-to-end tests (`tests/test_e2e.py`) that migrate real rows between two databases, plus regression tests
for the failure modes below.

---

## 🐛 Real bugs found and fixed while building this

1. **Silent success on failed writes.** A target with missing columns made the run report `completed, 0 written`.
   Fix: write errors now carry their reason and fail the table and the run.
2. **A "skip" that hid real errors.** The generic writer treated *any* DB error as a duplicate-key skip.
   Fix: only `IntegrityError` counts as a skip; everything else propagates.
3. **Cryptic mid-run crash on a missing target table** (`NoSuchTableError: products`) after half the data was copied.
   Fix: a pre-flight check lists every missing table up front, before anything is written.
4. **Primary-key reflection crash.** `get_pk_constraint()` returns column *names* (strings); the code indexed them like dicts.
   Found by the end-to-end test, invisible to the API-level tests.
5. **Invalid `docker-compose.yml`** (inline `interval: 5s; timeout: 5s`) and `localhost` URLs that can't work between containers.

## ⚠️ Known limitations
- `OFFSET/LIMIT` pagination slows down on very large tables; keyset pagination on the primary key is the next step.
- MySQL as a *target* (`_mysql_write`) and large-scale (millions of rows) runs are not covered by the tests.
- Connection URLs are stored unencrypted.

MIT License
