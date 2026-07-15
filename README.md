# Setu — Payment Reconciliation Service

A lightweight backend that ingests payment lifecycle events, maintains transaction
and reconciliation state, and exposes operational + reconciliation APIs.

Built with **FastAPI + SQLAlchemy 2.0**, running on **SQLite** locally (zero setup)
and **PostgreSQL** in production — the same code, switched by one env var.

- **Live demo:** https://setu-reconciliation.onrender.com · Interactive docs: https://setu-reconciliation.onrender.com/docs
  *(Free tier sleeps when idle — the first request may take ~50s to wake it, then it's fast.)*
- **Postman collection:** [`postman_collection.json`](./postman_collection.json)
- **Demo walkthrough:** see [`DEMO_SCRIPT.md`](./DEMO_SCRIPT.md)

---

## Quick start (local, < 2 minutes)

No database to install — it defaults to a local SQLite file.

```bash
cd solutions-engineer
python -m venv .venv
# Windows:  .venv\Scripts\activate      |  macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt

# 1) Seed the ~10k sample events (idempotent, ~13s)
python -m scripts.load_sample_data

# 2) Run
uvicorn app.main:app --reload
```

Open <http://localhost:8000/docs> for interactive Swagger UI, or import the Postman
collection (set `base_url = http://localhost:8000`).

### Run against Postgres instead (optional, production-faithful)

```bash
docker compose up --build        # brings up Postgres + the API on :8000
docker compose exec api python -m scripts.load_sample_data
```

### Run the tests

```bash
pytest -q      # 18 tests: idempotency, lifecycle, out-of-order, discrepancies,
               # reconciliation status, money precision, error envelope, filters
```

---

## Architecture

### The core idea: append-only event log + materialized transaction projection

```
              POST /events
                   │
                   ▼
        ┌──────────────────────┐
        │  events (append-only)│  ← source of truth, event_id = PRIMARY KEY
        │  INSERT ON CONFLICT   │     (this is what makes ingestion idempotent)
        │  DO NOTHING           │
        └──────────┬───────────┘
                   │ only NEW events
                   ▼
        ┌──────────────────────┐
        │  transactions        │  ← current state, updated incrementally.
        │  (projection)        │     Boolean flags per milestone + derived status.
        └──────────┬───────────┘
                   │ single indexed SQL query per endpoint
                   ▼
     GET /transactions   GET /reconciliation/summary   /discrepancies
```

**Why two tables instead of one?**

- The **event log** preserves full history (a hard requirement) and is the
  idempotency boundary: `event_id` is the primary key, so a resubmitted event
  collides and is dropped *by the database*, not by application-level checks that
  can race.
- The **transactions projection** is what every read API queries. Because current
  status, per-milestone flags, and timestamps are already materialized and indexed,
  listing/filtering/sorting/paginating and all reconciliation aggregation happen as
  single indexed SQL queries — **no Python loops, no N+1**. A `merchant,status`
  summary over 3,800 transactions returns in ~10 ms locally.

The projection is updated **incrementally** on each new event (O(1) per event) by
flipping the relevant boolean flag and recomputing a derived `status`. This makes
updates **order-independent** (a `settled` arriving before its `payment_initiated`
still converges to the correct state) and idempotent.

### Data model

| Table | Purpose | Key columns |
|---|---|---|
| `merchants` | Merchant registry | `merchant_id` (PK), `merchant_name` |
| `events` | Append-only event history | `event_id` (PK → idempotency), `event_type`, `transaction_id` (FK), `merchant_id` (FK), `amount`, `currency`, `event_timestamp`, `ingested_at` |
| `transactions` | Materialized current state | `transaction_id` (PK), `merchant_id` (FK), `amount` (NUMERIC), `status`, `reconciliation_status`, `has_initiated/processed/failed/settled`, `*_at` timestamps, `event_count`, `duplicate_event_count` |

**Two independent state dimensions.** Rather than force events into a single linear
state machine, each transaction tracks *payment* and *settlement* independently via
boolean flags. This mirrors reality (payment success and fund settlement are separate
systems) and turns every discrepancy into a trivial, indexable SQL predicate.

**Derived lifecycle `status`** (stored + indexed for fast filtering), by precedence:
`SETTLED > FAILED > PROCESSED > INITIATED`.

**Materialized `reconciliation_status`** — `RECONCILED` (clean terminal state),
`DISCREPANCY` (payment/settlement inconsistent), or `PENDING` (still in flight).
Computed on ingest and indexed, so ops can filter directly:
`GET /transactions?reconciliation_status=DISCREPANCY`. On the sample data:
RECONCILED 3,135 · DISCREPANCY 475 · PENDING 190.

**Money is `NUMERIC(18,2)`, never `FLOAT`.** Binary floats can't represent decimal
currency exactly — unacceptable at a payments company. NUMERIC gives exact storage
and exact SQL `SUM`. (SQLite lacks a native decimal type so it approximates; prod
runs on Postgres where it's exact.)

### Indexes

| Index | Serves |
|---|---|
| `events(event_id)` PK | Idempotent ingestion |
| `events(transaction_id, event_timestamp)` | Event history on the detail endpoint |
| `transactions(merchant_id)`, `(status)`, `(merchant_id, status)` | `GET /transactions` filters |
| `transactions(reconciliation_status)` | Reconciliation-status filter |
| `transactions(last_event_at)` | Date-range filter + default sort |
| `transactions(has_processed, has_settled, has_failed)` | Discrepancy scans |

**These indexes are actually used** — `EXPLAIN QUERY PLAN` on SQLite confirms it
(Postgres picks the same indexes):

```
-- WHERE status='SETTLED' ORDER BY last_event_at DESC LIMIT 50
SEARCH transactions USING INDEX ix_txn_status (status=?)

-- WHERE merchant_id='merchant_1' AND status='SETTLED'
SEARCH transactions USING INDEX ix_txn_merchant_status (merchant_id=? AND status=?)

-- WHERE reconciliation_status='DISCREPANCY'
SEARCH transactions USING INDEX ix_txn_recon_status (reconciliation_status=?)
```

> The one non-index step is the temp-B-tree sort when a `status` filter is combined
> with `ORDER BY last_event_at`; over 3,800 rows it's negligible. A composite
> `(status, last_event_at)` index would remove even that — a deliberate deferral,
> not an oversight.

---

## API documentation

Base URL: your deployment, or `http://localhost:8000`. Full interactive schema at `/docs`.

All errors share one envelope: `{"error": {"type": "...", "message": "...", "details": [...]}}`
(`404` → `http_error`, `422` → `validation_error` with per-field `details`).

### `POST /events` — ingest an event (idempotent)

Request body:

```json
{
  "event_id": "b768e3a7-9eb3-4603-b21c-a54cc95661bc",
  "event_type": "payment_initiated",
  "transaction_id": "2f86e94c-239c-4302-9874-75f28e3474ee",
  "merchant_id": "merchant_2",
  "merchant_name": "FreshBasket",
  "amount": 15248.29,
  "currency": "INR",
  "timestamp": "2026-01-08T12:11:58.085567+00:00"
}
```

- `event_type` ∈ `payment_initiated | payment_processed | payment_failed | settled`
- **`201 Created`** for a new event, **`200 OK`** with `"status": "duplicate"` for a
  resubmission (state unchanged). **`422`** for validation errors.
- `POST /events/batch` accepts a JSON array for convenience.

### `GET /transactions` — list / filter / sort / paginate

| Query param | Description |
|---|---|
| `merchant_id` | Exact match |
| `status` | `INITIATED / PROCESSED / FAILED / SETTLED` |
| `reconciliation_status` | `RECONCILED / DISCREPANCY / PENDING` |
| `start_date`, `end_date` | ISO-8601, filter on `last_event_at` |
| `sort_by` | `last_event_at` (default), `first_seen_at`, `amount`, `created_at`, `status` |
| `sort_dir` | `asc` / `desc` |
| `limit` (1–200, default 50), `offset` | Pagination |

Returns `{ pagination: { total, limit, offset, returned }, items: [...] }`.

### `GET /transactions/{transaction_id}` — details + history

Returns transaction state, current status, merchant info, and the **full ordered
event history**. `404` if unknown.

### `GET /reconciliation/summary` — grouped summary

`?group_by=merchant,date,status` (any combination). Optional `merchant_id`,
`start_date`, `end_date` filters. Each row has `transaction_count`, `total_amount`,
and per-status counts. All computed via a single SQL `GROUP BY` with conditional
aggregates.

### `GET /reconciliation/discrepancies` — inconsistent transactions

Optional `?type=` filter. Returns per-type counts plus paginated items.

| Type | Meaning | SQL predicate |
|---|---|---|
| `PROCESSED_NOT_SETTLED` | Payment captured but never settled | `has_processed ∧ ¬has_settled ∧ ¬has_failed` |
| `SETTLED_FOR_FAILED_PAYMENT` | Settlement on a failed/unprocessed payment | `has_settled ∧ (has_failed ∨ ¬has_processed)` |
| `CONFLICTING_STATE` | Contradictory events (both processed and failed) | `has_processed ∧ has_failed` |
| `STUCK_PENDING` | Initiated, no terminal state, older than 24h | `has_initiated ∧ ¬processed ∧ ¬failed ∧ ¬settled ∧ stale` |
| `DUPLICATE_SUBMISSIONS` | Received duplicate events (idempotency engaged) | `duplicate_event_count > 0` |

> **On "duplicate events causing conflicting state transitions" (the spec's third
> example):** our idempotency (`event_id` PK) means a duplicate can *never* mutate
> state, so duplicate-induced corruption is prevented by design — we surface which
> transactions received duplicates via `DUPLICATE_SUBMISSIONS` as evidence. Should
> an upstream system emit genuinely contradictory events (a payment both processed
> *and* failed), `CONFLICTING_STATE` catches it. It is 0 on the sample data —
> that's the healthy result, and the check exists so it wouldn't stay silent if it
> weren't.

---

## Sample data

The provided `sample_events.json` is used as-is: **10,355 events → 10,165 unique +
190 duplicates**, across **5 merchants** and **3,800 transactions**, spanning
Jan–Apr 2026. It contains a realistic mix that maps directly onto the discrepancy
types above:

| Pattern | Count | Reconciliation outcome |
|---|---|---|
| initiated → processed → settled | 2,565 | clean |
| initiated → failed | 570 | clean failure |
| initiated → processed (no settle) | **380** | `PROCESSED_NOT_SETTLED` |
| initiated only | **190** | `STUCK_PENDING` |
| initiated → failed → settled | **95** | `SETTLED_FOR_FAILED_PAYMENT` |
| duplicate `event_id` submissions | **190** | `DUPLICATE_SUBMISSIONS` (all rejected) |

`scripts/load_sample_data.py` replays the file through the **same** `ingest_event`
path the API uses (batched commits for speed), so the seeded state is identical to
POSTing every event. Re-running it is safe — duplicates are ignored.

---

## Idempotency (design deep-dive)

Idempotency is enforced at the **database** layer, not in Python:

1. `event_id` is the primary key of `events`.
2. Ingestion issues `INSERT ... ON CONFLICT (event_id) DO NOTHING` (dialect-aware:
   Postgres and SQLite both support this).
3. If `rowcount == 0`, the event was a duplicate → the transaction projection is
   **not** modified (we only bump `duplicate_event_count` for reporting), so
   duplicate submissions can never corrupt state or double-count amounts.

This is race-safe under concurrency (two identical events arriving at once → the DB
constraint lets exactly one win), unlike a read-then-write "have I seen this?" check.
On Postgres, projection updates additionally take a `SELECT ... FOR UPDATE` row lock
so concurrent events for the *same* transaction serialize cleanly.

---

## Deployment (Render)

**Live:** https://setu-reconciliation.onrender.com — deployed on Render's free tier
(web service + managed Postgres).

The repo includes a [`Dockerfile`](./Dockerfile) and a [`render.yaml`](./render.yaml)
blueprint that provisions the web service **and** a managed Postgres, wiring
`DATABASE_URL` automatically.

1. Render dashboard → **New → Blueprint** → point at this repo (or paste the public
   repo URL). It reads `render.yaml` and creates both resources.
2. **Deploy Blueprint.** On first boot the service **self-seeds** the bundled
   `sample_events.json` over the internal DB connection — no shell access needed
   (see `SEED_ON_STARTUP` in `render.yaml`; it's guarded to run only when the
   events table is empty, so restarts never re-seed).
3. Health check: `GET /health`. Docs: `/docs`.

`config.py` normalizes Render's `postgres://` URL to the `postgresql+psycopg2://`
form SQLAlchemy 2.x requires, so no manual URL editing is needed.

> The same Docker image runs anywhere (Railway/Fly.io/any container host) — only the
> `DATABASE_URL` env var changes. Locally you seed explicitly with
> `python -m scripts.load_sample_data` (SEED_ON_STARTUP stays off).

---

## Assumptions & tradeoffs

**Assumptions**
- `event_id` is a client-supplied globally-unique idempotency key (true in the
  sample data). It is required by the API — without it, dedupe is impossible.
- Amounts are consistent across a transaction's events (verified: 0 transactions in
  the sample have conflicting amounts). We store the latest-seen amount.
- A transaction that is both `settled` and `failed` reports lifecycle `status =
  SETTLED` (money moved) but is surfaced as a `SETTLED_FOR_FAILED_PAYMENT`
  discrepancy. "Stuck" is defined as initiated-only for >24h (configurable).

**Tradeoffs**
- **Money as `NUMERIC`, not `float`.** Exact decimal storage + exact SQL `SUM`,
  at the cost of a little parsing/serialization ceremony (Decimal in, JSON number
  out). Non-negotiable for a payments service. SQLite approximates decimals; prod
  (Postgres) is exact.
- **Materialized projection vs. pure event-sourcing.** I keep a derived
  `transactions` table rather than recomputing state from events on every read.
  This costs a little write-time work and storage but makes all read/aggregation
  queries single, indexed, and fast — the right call for an ops-facing reporting
  service. History is never lost (it lives in `events`).
- **`reconciliation_status` materialized at ingest, staleness computed at read.**
  Structural discrepancies are deterministic and stored; the time-based
  `STUCK_PENDING` check depends on the current clock so it stays a read-time filter
  in the discrepancies endpoint.
- **`create_all` at startup vs. migrations.** For a take-home this keeps setup to
  one command. In production I'd use **Alembic** for versioned schema migrations.
- **SQLite locally / Postgres in prod.** Maximizes reviewer convenience while still
  demonstrating a real deployment. A `UTCDateTime` type decorator papers over
  SQLite's lack of timezone persistence so behavior is identical on both.
- **Offset pagination.** Simple and sufficient here. For very large, deep result
  sets I'd switch to keyset/cursor pagination.
- **Discrepancy pagination across types** is done by walking types in priority
  order; a single `UNION ALL` view would be marginally cleaner at large scale.

**With more time I would add:** Alembic migrations, structured request logging +
request IDs, rate limiting, a `/metrics` endpoint, a small React ops dashboard, a
composite `(status, last_event_at)` index, and property-based tests for the
state-derivation logic.

**Already included beyond the brief:** a consistent `{"error": {...}}` envelope for
all failures, a GitHub Actions CI workflow running the test suite, `/health` for
deployment probes, and `/` redirecting to interactive docs.

---

## AI tools disclosure

This solution was developed with **Claude Code (Anthropic)** as a pair-programming
assistant. It was used to scaffold boilerplate (FastAPI routers, Pydantic schemas,
Dockerfile, Postman collection), to help analyze the sample dataset's distribution,
and to draft this README. All architectural decisions — the append-only-log +
projection design, the two-dimension reconciliation model, the DB-level idempotency
approach, and the indexing strategy — were directed and reviewed by me, and every
endpoint and edge case was verified against the real dataset and the test suite.

## Project layout

```
solutions-engineer/
├── app/
│   ├── main.py         # FastAPI app + routes
│   ├── models.py       # SQLAlchemy tables + indexes
│   ├── schemas.py      # Pydantic request/response contracts
│   ├── services.py     # idempotent ingestion + state derivation
│   ├── queries.py      # read/reconciliation SQL (filters, GROUP BY, discrepancies)
│   ├── database.py     # engine/session
│   ├── config.py       # env-driven settings
│   └── types.py        # timezone-safe DateTime
├── scripts/load_sample_data.py
├── tests/              # pytest suite (18 tests)
├── .github/workflows/ci.yml   # runs pytest on push/PR
├── sample_events.json
├── Dockerfile · render.yaml · docker-compose.yml
├── postman_collection.json · DEMO_SCRIPT.md
└── requirements.txt
```
