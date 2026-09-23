# Differential Privacy Budget Release API

A FastAPI + SQLite service that releases **Laplace-mechanism** noised query
answers while charging a per-dataset ε (epsilon) privacy budget.

- Budget is checked **and deducted inside a single SQLite transaction**
  (`BEGIN IMMEDIATE`), so concurrent requests racing for the last slice of a
  budget serialize — exactly one succeeds, the rest get `402`.
- Noise is **deterministic and reproducible**: derived from an
  HMAC-SHA256 keyed by a server-side secret over
  `(dataset, query, epsilon, sensitivity, idempotency key)`.
- **Idempotent** releases: retrying with the same idempotency key returns the
  *exact same* release (same `id`, same noise, same value) and never charges
  the budget twice. Reusing a key with different parameters returns `409`.
- **Budget transfers:** unused ε quota can be moved from one dataset to another
  with `POST /budgets/transfer`. Both balances are locked and updated in one
  SQLite transaction; transfers are idempotent on `transfer_id`.
- **API-key authentication:** every endpoint except `GET /health` and the
  `/docs` Swagger UI requires an `X-API-Key` header. Keys are scoped to an
  explicit list of datasets; out-of-scope access returns `403`. Only the
  SHA-256 hash of a key is stored, and the plaintext is shown once at
  creation.
- **Admin-managed dataset catalog:** datasets are registered with
  `POST /admin/datasets` (admin token) carrying a `dataset_id`, a name and a
  default ε budget, then moved through a `draft → active → archived`
  lifecycle. Releases, budget transfers and new key grants are only accepted
  against **active** datasets; archiving rejects new writes while preserving
  historical queries and idempotent replays. Lifecycle changes use an
  optimistic version counter checked inside a SQLite transaction — stale or
  duplicate versions return `409`.

## Authentication

All endpoints except `GET /health`, `/docs` and the OpenAPI document require
a valid `X-API-Key` header. There are two kinds of credentials:

- **Admin token** — used to create and revoke API keys and to manage the
  dataset catalog (`/auth/keys*`, `/admin/datasets*`). Configure it with
  `DP_ADMIN_API_KEY`. If unset, a random token is generated once and persisted
  at `/data/admin.key` (mode `0600`) in the data volume; the startup log tells
  you where to find it. Set it explicitly for production.
- **API key** — a scoped data-plane token of the shape
  `dpk_<key_id>.<secret>`. Presented in `X-API-Key`, validated against its
  stored SHA-256 hash with a constant-time comparison, and rejected if revoked
  or expired.

### `POST /auth/keys` (admin token only)

Create a key scoped to a list of datasets. The plaintext token is returned
**exactly once** in the `api_key` field.

Request body:

```json
{
  "name": "reporting service",
  "dataset_ids": ["sales", "marketing"],
  "ttl_seconds": 7776000
}
```

Provide `ttl_seconds` (lifetime from now) or `expires_at` (UTC ISO-8601, e.g.
`"2026-12-31T23:59:59Z"`), or neither for a non-expiring key; not both. The
dataset list is required (1–100 ids; duplicates are ignored).

`201` response:

```json
{
  "key_id": "3db822ac890983674d22e77aede4a13a",
  "name": "reporting service",
  "api_key": "dpk_3db822ac890983674d22e77aede4a13a.djIeFd5_p8dcgP3Atx7H9XhC4ohH5UxORBo7i4rj1CU",
  "dataset_ids": ["sales", "marketing"],
  "expires_at": "2026-12-21T10:00:00Z",
  "created_at": "2026-09-22T10:00:00Z"
}
```

Status codes: `201` created; `401` missing/invalid admin token; `403` a
data-plane API key was used (admin token required); `422` invalid body.

### `DELETE /auth/keys/{key_id}` (admin token only)

Revoke a key immediately and permanently (`200`, idempotent on repeated calls).
Unknown `key_id` returns `404`. The hash is retained so the same token can
never be re-minted; every later request with it returns `401`.

## Dataset catalog and lifecycle

Every data-plane operation addresses a dataset that an administrator has
registered in the catalog. Entries move through a one-way state machine:

```
draft  ──activate──▶  active  ──archive──▶  archived
```

- **draft** — registered but not usable: releases, transfers and new key
  grants are refused (`409 dataset_not_active`).
- **active** — the only state that accepts releases, budget transfers and new
  API-key grants.
- **archived** — frozen. New releases/transfers are refused and no new key may
  be scoped to it, but historical queries (`GET /budgets/{id}`) and
  idempotent replays of past releases keep working.

Operations on an unknown `dataset_id` return `404 dataset_not_found`.

### `POST /admin/datasets` (admin token only)

Register a dataset. The entry starts in `draft` at version `0`.

Request body:

```json
{
  "dataset_id": "sales",
  "name": "Sales pipeline",
  "default_budget": 1.0
}
```

`default_budget` is the total ε seeded when the dataset's budget row is first
used by a release or transfer (must be a finite number `>= 0`; `0` is legal).
A pre-existing budget row (e.g. carried over by migration from a pre-catalog
deployment) is never overwritten.

`201` response:

```json
{
  "dataset_id": "sales",
  "name": "Sales pipeline",
  "status": "draft",
  "version": 0,
  "default_budget": 1.0,
  "created_at": "2026-09-23T10:00:00.000Z",
  "updated_at": "2026-09-23T10:00:00.000Z"
}
```

Status codes: `201` created; `401` missing/invalid admin token; `403` a
data-plane key was used; `409 dataset_already_exists` — the id is taken;
`422` invalid body (empty id/name, negative or NaN budget, extra fields).

### `PATCH /admin/datasets/{dataset_id}/lifecycle` (admin token only)

Apply one state-machine transition with optimistic concurrency control:

```json
{ "version": 0, "action": "activate" }
```

`action` is `activate` (`draft → active`) or `archive`
(`active → archived`). `version` must equal the row's **current** version;
every successful transition increments it by one and stamps `updated_at`.
The whole check-and-update runs inside a single `BEGIN IMMEDIATE` transaction.

`200` returns the updated entry (new `status` and `version`). Conflict cases
all return `409`:

- `version_conflict` — the supplied version is stale (another transition
  committed first) or a duplicate of an already-applied request; the body
  includes `current_version` and `provided_version`.
- `invalid_lifecycle_transition` — the action is not legal from the current
  status (e.g. archiving a draft, re-activating an archived dataset).

Unknown `dataset_id` returns `404`; an invalid body returns `422`.

Typical flow: `POST` (draft, v0) → `PATCH activate` with `version: 0`
(active, v1) → `PATCH archive` with `version: 1` (archived, v2). Re-sending
an old request body (e.g. activate with `version: 0` again) fails with
`409 version_conflict` — clients must re-read the current version first.

### `GET /admin/datasets` (admin token only)

List all catalog entries ordered by `dataset_id`. Add `?status=draft|active|
archived` to filter by state (an unknown value is a `422`).

```json
{
  "datasets": [
    {
      "dataset_id": "sales",
      "name": "Sales pipeline",
      "status": "active",
      "version": 1,
      "default_budget": 1.0,
      "created_at": "2026-09-23T10:00:00.000Z",
      "updated_at": "2026-09-23T10:00:05.000Z"
    }
  ]
}
```

### Scope enforcement

- `POST /releases` — the key must cover the request's `dataset_id`.
- `GET /budgets/{dataset_id}` — the key must cover that dataset.
- `POST /budgets/transfer` — the key must cover **both** `source_dataset` and
  `target_dataset`.

Any mismatch returns `403` with `{"detail": {"error": "dataset_out_of_scope",
...}}`. Keys carry an explicit dataset id list; there is no wildcard.

New keys may only be scoped to **active** datasets: `POST /auth/keys` returns
`404 dataset_not_found` for an unregistered id and `409 dataset_not_active`
for a draft/archived one. Existing scopes are not revoked by archiving — an
archived dataset simply refuses new writes for every key that covers it.

Security properties: only `sha256(token)` is stored (tokens contain 256 bits
of CSPRNG entropy); comparison uses `hmac.compare_digest`, including a dummy
digest comparison for unknown key ids so the unknown-key path takes the same
work as the known-key one; revoked and expired keys are rejected on every
request.

## Endpoints

| Method | Path                    | Auth                         | Description                                        |
|--------|-------------------------|------------------------------|----------------------------------------------------|
| POST   | `/auth/keys`            | admin token                  | Create a scoped API key (plaintext returned once)  |
| DELETE | `/auth/keys/{key_id}`   | admin token                  | Revoke an API key                                  |
| POST   | `/admin/datasets`       | admin token                  | Register a draft dataset with a default budget     |
| PATCH  | `/admin/datasets/{id}/lifecycle` | admin token          | activate/archive with optimistic version check     |
| GET    | `/admin/datasets`       | admin token                  | List catalog entries (`?status=` filter)           |
| POST   | `/releases`             | API key covering `dataset_id`| Validate budget, deduct ε, return a noised release |
| POST   | `/budgets/transfer`     | API key covering both sides  | Move unused ε quota between two active datasets    |
| GET    | `/budgets/{dataset_id}` | API key covering the dataset | Used and remaining budget only                     |
| GET    | `/health`               | none                         | Health check                                       |
| GET    | `/docs`                 | none                         | Interactive Swagger UI                             |

### `POST /releases`

Request body:

```json
{
  "dataset_id": "sales",
  "query_id": "daily-revenue-2026-09-22",
  "epsilon": 0.1,
  "sensitivity": 1.0,
  "true_value": 12345.67,
  "idempotency_key": "client-uuid-7f3a..."
}
```

You may also pass the key via the `Idempotency-Key` header; if both are sent
they must match (otherwise `409`).

`200` response:

```json
{
  "id": "d6e2db1798ca4ff78c293ae2e00ded34",
  "idempotency_key": "client-uuid-7f3a...",
  "dataset_id": "sales",
  "query_id": "daily-revenue-2026-09-22",
  "epsilon": 0.1,
  "sensitivity": 1.0,
  "released_value": 12340.2175,
  "noise": -5.4525,
  "budget_used": 0.3,
  "budget_remaining": 0.7
}
```

Status codes:

- `200` — release created, or identical idempotent retry replayed
- `401` — missing, invalid, revoked or expired `X-API-Key`
- `403` — the key's scope does not include `dataset_id`
- `404` — `dataset_id` is not registered in the catalog
- `409` — idempotency key already exists with different parameters; or
  `dataset_not_active` (the dataset is `draft`/`archived`, so new writes are
  refused). Identical retries of an earlier release stay replayable after
  archival.
- `402` — remaining budget is smaller than the requested ε
- `422` — invalid request (ε/sensitivity ≤ 0, NaN/Infinity, bad id, …)

The released value is `true_value + noise` with
`noise ~ Laplace(0, sensitivity / epsilon)`.

### `GET /budgets/{dataset_id}`

Returns only used and remaining amounts (never released values):

```json
{
  "dataset_id": "sales",
  "used_budget": 0.3,
  "remaining_budget": 0.7
}
```

Unknown (unregistered) datasets return `404 dataset_not_found`. For a
registered dataset that has never spent or received quota, the response shows
`used_budget = 0` and the catalog default as remaining (archived datasets are
still readable here). After a transfer, `remaining_budget` reflects the quota
moved in or out (only the *unused* part of a budget can ever move —
already-spent ε stays with its dataset).

### `POST /budgets/transfer`

Move unused ε quota from one dataset to another. The source's total drops and
the target's total rises by `epsilon`; used amounts on both sides are
untouched. Both datasets must be **active**.

Request body:

```json
{
  "transfer_id": "client-uuid-9c1b...",
  "source_dataset": "sales",
  "target_dataset": "marketing",
  "epsilon": 0.2
}
```

`200` response:

```json
{
  "transfer_id": "client-uuid-9c1b...",
  "source_dataset": "sales",
  "target_dataset": "marketing",
  "epsilon": 0.2,
  "source": {
    "dataset_id": "sales",
    "used_budget": 0.3,
    "remaining_budget": 0.5
  },
  "target": {
    "dataset_id": "marketing",
    "used_budget": 0.0,
    "remaining_budget": 1.2
  }
}
```

Status codes:

- `200` — transfer applied, or identical idempotent retry replayed
- `401` — missing, invalid, revoked or expired `X-API-Key`
- `403` — the key does not cover both the source and target datasets
- `404` — either side is not registered in the catalog
- `409` — `transfer_id` already exists with different parameters
  (`source_dataset`, `target_dataset` or `epsilon` changed); or
  `dataset_not_active` — a side is `draft`/`archived` (identical replays of a
  transfer recorded before archival still succeed)
- `402` — source has less than `epsilon` unused quota remaining; the body
  identifies the source, the available remainder and the requested amount
- `422` — invalid request: same source and target, `epsilon ≤ 0`,
  NaN/Infinity, bad identifier, extra fields, …

**Idempotency.** Retrying with the same `transfer_id` *and the exact same
parameters* returns the original transfer result (balances frozen at the time
of the first transfer are echoed back) without moving any additional quota.
Reusing a `transfer_id` with changed parameters returns `409` and moves
nothing.

**Atomicity.** Both budget rows are seeded (for unknown datasets) and then
read and updated inside a single `BEGIN IMMEDIATE` transaction. If the source
cannot cover the transfer, the whole request rolls back; concurrent
transfers serialize on SQLite's write lock, so quota is never double-spent.

## Quick start with Docker Compose

Requirements: Docker with the Compose plugin.

```bash
# optional: set persistent secrets (otherwise random ones are generated
# automatically and stored in the data volume)
export DP_SERVER_KEY=$(openssl rand -hex 32)
export DP_ADMIN_API_KEY=$(openssl rand -hex 32)

docker compose up --build -d
```

The API is then served at:

- **Base URL:** `http://localhost:8000`
- **Health check:** `http://localhost:8000/health`
- **Swagger UI:** `http://localhost:8000/docs` (use the **Authorize** button to
  set the `X-API-Key` header)
- **Port:** `8000` (host and container)

Logs and lifecycle:

```bash
docker compose logs -f
docker compose down            # keep data
docker compose down -v         # also delete the database volume
```

The SQLite database (`dp.db`), the auto-generated noise key (`server.key`) and
the auto-generated admin token (`admin.key`, both mode `0600`) live in the
named volume `dp-data` mounted at `/data`. If you boot without
`DP_ADMIN_API_KEY`, copy the token out of the volume (or read the startup log
for its path) to create and revoke API keys:

```bash
docker compose exec dp-api cat /data/admin.key
```

## Configuration

All configuration is via environment variables:

| Variable             | Default      | Description                                                            |
|----------------------|--------------|------------------------------------------------------------------------|
| `DP_SERVER_KEY`      | *(generated)*| HMAC key for deterministic noise. Set it for stable noise across rebuilds. Generate: `openssl rand -hex 32`. |
| `DP_ADMIN_API_KEY`   | *(generated)*| Admin token for `/auth/keys` create/revoke. If unset, generated once and persisted at `/data/admin.key` (mode `0600`). Generate: `openssl rand -hex 32`. |
| `DP_TOTAL_BUDGET`    | `1.0`        | Total ε budget every dataset receives by default.                      |
| `DP_DATASET_BUDGETS`| *(empty)*    | Per-dataset overrides, e.g. `sales:0.5,users:2.0`; others fall back to `DP_TOTAL_BUDGET`. |
| `DATABASE_PATH`      | `/data/dp.db`| SQLite database file path.                                             |
| `APP_HOST`           | `0.0.0.0`    | Bind address.                                                          |
| `APP_PORT`           | `8000`       | Listen port.                                                           |

Copy `.env.example` to `.env` and adjust — Compose picks it up automatically.

> **Key management note:** if `DP_SERVER_KEY` is unset, a random 32-byte key
> is generated once and persisted next to the database. Deleting the volume
> (or running without one) rotates the key and changes future noise, so set
> `DP_SERVER_KEY` explicitly for production.

## Local development (without Docker)

Python 3.11+:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
DATABASE_PATH=./data/dp.db DP_TOTAL_BUDGET=1.0 DP_SERVER_KEY=dev-key \
DP_ADMIN_API_KEY=dev-admin-token \
  .venv/bin/uvicorn app.main:app --reload --port 8000
```

Run the test suite (starts a real uvicorn server on port 8765 against a
temporary database, including concurrency races):

```bash
.venv/bin/python -m unittest tests.test_api -v
```

## Try it

```bash
# 0. Register datasets and bring them live (admin token). New entries start
#    as draft; activate them with the version returned by the create call.
curl -s -X POST http://localhost:8000/admin/datasets \
  -H "X-API-Key: $DP_ADMIN_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"dataset_id":"sales","name":"Sales pipeline","default_budget":1.0}'
# -> {"status":"draft","version":0, ...}

curl -s -X PATCH http://localhost:8000/admin/datasets/sales/lifecycle \
  -H "X-API-Key: $DP_ADMIN_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"version":0,"action":"activate"}'
# -> {"status":"active","version":1, ...}

curl -s -X POST http://localhost:8000/admin/datasets \
  -H "X-API-Key: $DP_ADMIN_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"dataset_id":"marketing","name":"Marketing","default_budget":1.0}'
curl -s -X PATCH http://localhost:8000/admin/datasets/marketing/lifecycle \
  -H "X-API-Key: $DP_ADMIN_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"version":0,"action":"activate"}'

curl -s "http://localhost:8000/admin/datasets?status=active" \
  -H "X-API-Key: $DP_ADMIN_API_KEY"

# 1. Mint an API key with the admin token (save the returned api_key now;
#    the plaintext is never shown again). Grants only succeed for active
#    datasets.
curl -s -X POST http://localhost:8000/auth/keys \
  -H "X-API-Key: $DP_ADMIN_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"name":"local-dev","dataset_ids":["sales","marketing"]}'

KEY='dpk_....'   # paste the api_key from the response

# 2. Health needs no key; everything else does
curl -s http://localhost:8000/health

curl -s -X POST http://localhost:8000/releases \
  -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"dataset_id":"sales","query_id":"q1","epsilon":0.3,
       "sensitivity":1.0,"true_value":42.0,"idempotency_key":"abc-123"}'

# repeat the exact request -> byte-identical response, budget charged once

curl -s http://localhost:8000/budgets/sales -H "X-API-Key: $KEY"

# datasets outside the key scope are 403
curl -s http://localhost:8000/budgets/hr -H "X-API-Key: $KEY"

# move 0.2 of sales' unused quota to marketing (both must be in scope)
curl -s -X POST http://localhost:8000/budgets/transfer \
  -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"transfer_id":"tr-001","source_dataset":"sales",
       "target_dataset":"marketing","epsilon":0.2}'

# retrying with the same transfer_id returns the original result, moves nothing

curl -s http://localhost:8000/budgets/marketing -H "X-API-Key: $KEY"

# 3. Revoke the key (admin token); it stops authenticating immediately
curl -s -X DELETE http://localhost:8000/auth/keys/<key_id> \
  -H "X-API-Key: $DP_ADMIN_API_KEY"

# 4. Freeze a dataset (use the current version — here 1 after activation):
#    new releases/transfers/grants get 409, but past releases still replay.
curl -s -X PATCH http://localhost:8000/admin/datasets/sales/lifecycle \
  -H "X-API-Key: $DP_ADMIN_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"version":1,"action":"archive"}'
```

## Design notes

- **Concurrency:** each release runs in `BEGIN IMMEDIATE`, which takes
  SQLite's reserved lock at transaction start. Concurrent writers queue on
  `busy_timeout` and each reads the committed balance, so the total deducted
  never exceeds the configured budget.
- **Deterministic noise:** `u ∈ (0,1)` is derived from
  `HMAC-SHA256(key, "dp-release-v1" ␟ dataset ␟ query ␟ ε ␟ sensitivity ␟ key)`
  (56 bits), then inverted through the Laplace CDF. Same inputs ⇒ identical
  float noise, independent of the database or request timing.
- **Idempotency:** `idempotency_key` has a `UNIQUE` constraint. Retries replay
  the stored row; the lost side of a same-key insert race rolls back and
  replays the winner instead of double-charging. Transfers follow the same
  pattern on `transfers.id` (`transfer_id`): identical retries replay the
  stored balances, changed parameters get `409`.
- **Budget transfers:** unused quota is moved by adjusting the two rows'
  `total` columns inside one `BEGIN IMMEDIATE` transaction (`used` never
  changes, so ε already spent cannot be moved out). Budget rows are seeded in
  a fixed alphabetical order so crossing transfers always acquire locks in the
  same order and cannot deadlock. A release never overwrites a budget row
  created by a transfer — the configured total only seeds a brand-new dataset.
- **Privacy:** budget accounting is basic ε-sum bookkeeping with no
  composition niceties; release values are returned to the caller (this is a
  release service), while the budget endpoint exposes amounts only.
- **Authentication:** an HTTP middleware authenticates every non-public path.
  Tokens (`dpk_<key_id>.<secret>`, 256-bit CSPRNG secret) are stored only as
  SHA-256 hashes and verified with a constant-time `hmac.compare_digest`; the
  unknown-`key_id` path compares against a dummy digest to avoid leaking id
  existence via timing. Revocation sets a flag checked on every request, and
  expiry is compared against current UTC. Key rows and the scope child table
  (`api_key_scopes`) are created and revoked inside `BEGIN IMMEDIATE`
  transactions, so concurrent minting and revoking serialize cleanly; request
  authentication uses a short WAL read that never blocks readers. Schema
  upgrades run through `PRAGMA user_version`-stamped migrations, so existing
  databases gain the auth tables without losing budget/release data.
- **Dataset catalog:** the `datasets` table holds `status`
  (`draft`/`active`/`archived`, CHECK-constrained) and a monotonic `version`
  counter. A lifecycle PATCH runs in `BEGIN IMMEDIATE`, reads the current
  row, rejects a mismatched version or an illegal transition with `409`, and
  otherwise updates `status`, bumps `version` and stamps `updated_at`
  atomically — concurrent transitions serialize and exactly one wins.
  Releases and transfers check catalog state *after* their idempotency replay
  lookup, so archival never hides history: retries of recorded operations
  replay, while genuinely new writes get `409 dataset_not_active`. The v3
  migration backfills catalog entries (as `active`, preserving existing
  budget totals) for every dataset referenced by pre-existing budgets,
  releases, transfers or key scopes, so upgrading an old database changes no
  behavior.
