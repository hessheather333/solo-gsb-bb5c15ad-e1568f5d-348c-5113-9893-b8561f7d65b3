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
- **Admin dataset catalog:** datasets are registered by an administrator with
  `POST /admin/datasets` (id, name, default ε budget) and move through a
  `draft → active → archived` lifecycle via optimistic-concurrency PATCHes.
  Releases, budget transfers and **new key grants** are only accepted against
  `active` datasets; archived datasets reject new writes but keep their
  history (releases replay, budgets stay readable). Datasets that were never
  registered keep the legacy implicit-dataset behavior.

## Authentication

All endpoints except `GET /health`, `/docs` and the OpenAPI document require
a valid `X-API-Key` header. There are two kinds of credentials:

- **Admin token** — used only to create and revoke API keys. Configure it with
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
data-plane API key was used (admin token required); `409` a listed catalogued
dataset is not `active`; `422` invalid body.

### `DELETE /auth/keys/{key_id}` (admin token only)

Revoke a key immediately and permanently (`200`, idempotent on repeated calls).
Unknown `key_id` returns `404`. The hash is retained so the same token can
never be re-minted; every later request with it returns `401`.

### Scope enforcement

- `POST /releases` — the key must cover the request's `dataset_id`.
- `GET /budgets/{dataset_id}` — the key must cover that dataset.
- `POST /budgets/transfer` — the key must cover **both** `source_dataset` and
  `target_dataset`.

Any mismatch returns `403` with `{"detail": {"error": "dataset_out_of_scope",
...}}`. Keys carry an explicit dataset id list; there is no wildcard.
Granting a key on a catalogued dataset additionally requires that dataset to
be `active` — grants on `draft`/`archived` datasets return
`409 dataset_not_active` (uncatalogued datasets remain grantable).

Security properties: only `sha256(token)` is stored (tokens contain 256 bits
of CSPRNG entropy); comparison uses `hmac.compare_digest`, including a dummy
digest comparison for unknown key ids so the unknown-key path takes the same
work as the known-key one; revoked and expired keys are rejected on every
request.

## Dataset catalog (admin token only)

Administrators manage the lifecycle of datasets; every catalog endpoint uses
the same admin token as `/auth/keys` in the `X-API-Key` header. A catalogued
dataset is in exactly one of three states:

```
 draft ──activate──▶ active ──archive──▶ archived   (terminal)
```

- **draft** — registered but not usable: releases and transfers are refused
  (`409 dataset_not_active`) and no API key can be granted on it.
- **active** — the only state on which releases, budget transfers and new key
  grants are accepted.
- **archived** — frozen: every new write is refused with `409`, but historical
  releases remain replayable (same idempotency key, no extra charge) and
  `GET /budgets/{id}` keeps reporting the used/remaining amounts.

Datasets that have no catalog row (created implicitly by older clients or by
a budget transfer) are not gated and keep working exactly as before.

### `POST /admin/datasets`

Register a dataset. It starts in `draft` with `version = 1`; its budget row is
seeded with `default_budget` unless one already exists, in which case the
existing balance is preserved.

```json
{
  "dataset_id": "sales",
  "name": "Worldwide sales pipeline",
  "default_budget": 1.0
}
```

`201` response:

```json
{
  "dataset_id": "sales",
  "name": "Worldwide sales pipeline",
  "default_budget": 1.0,
  "status": "draft",
  "version": 1,
  "created_at": "2026-09-23T10:00:00.000Z",
  "updated_at": "2026-09-23T10:00:00.000Z"
}
```

Status codes: `201` created; `401` missing/invalid admin token; `403` a
data-plane key was used; `409 dataset_already_exists` the id is already
registered; `422` invalid body (`default_budget < 0` / non-finite, bad id,
extra fields, …).

### `PATCH /admin/datasets/{dataset_id}/lifecycle`

Apply the next legal transition. The body names the desired `status` and the
catalog `version` the client last saw (optimistic concurrency):

```json
{ "status": "active", "version": 1 }
```

On success the row is updated and `version` is incremented (`draft` v1 →
`active` v2 → `archived` v3). Both checks run inside a single
`BEGIN IMMEDIATE` SQLite transaction:

- **stale or repeated version** (`version` is not the current one, including a
  retried PATCH after the transition already committed) →
  `409 version_conflict` with `expected_version`;
- **illegal transition** (e.g. `draft → archived`, anything out of
  `archived`) with a current version → `409 invalid_lifecycle_transition`;
- unknown `dataset_id` → `404`; bad body → `422`.

Because the conditional `UPDATE ... WHERE status = ? AND version = ?` itself
pins the row, concurrent PATCHes serialize: exactly one succeeds, the rest
receive `409`.

### `GET /admin/datasets`

List the whole catalog, ordered by `dataset_id`; add
`?status=draft|active|archived` to filter. Returns an array of the same
objects as the create/PATCH responses.

### How lifecycle state interacts with data-plane calls

| Call                                   | `draft`              | `active` | `archived`                     |
|----------------------------------------|----------------------|----------|--------------------------------|
| `POST /releases` (new idempotency key) | `409`                | allowed  | `409` (new writes refused)     |
| `POST /releases` (replay existing key) | replays stored row\* | replays  | replays stored row, no charge  |
| `POST /budgets/transfer`               | `409`                | allowed  | `409`                          |
| `POST /auth/keys` (grant on dataset)   | `409`                | allowed  | `409`                          |
| `GET /budgets/{id}`                    | allowed              | allowed  | allowed (history preserved)    |

\*A replay can only exist for a dataset that was previously `active`; a
`draft` row has never accepted a write. The `409` body is
`{"detail": {"error": "dataset_not_active", "dataset_id": ..., "status": ...,
"operation": "release"|"transfer"|"grant_api_key"}}`.

## Endpoints

| Method | Path                    | Auth                         | Description                                        |
|--------|-------------------------|------------------------------|----------------------------------------------------|
| POST   | `/admin/datasets`       | admin token                  | Register a dataset (`draft`, default budget)       |
| PATCH  | `/admin/datasets/{id}/lifecycle` | admin token         | `draft→active→archived` with version check         |
| GET    | `/admin/datasets`       | admin token                  | List catalog (optional `?status=` filter)          |
| POST   | `/auth/keys`            | admin token                  | Create a scoped API key (plaintext returned once)  |
| DELETE | `/auth/keys/{key_id}`   | admin token                  | Revoke an API key                                  |
| POST   | `/releases`             | API key covering `dataset_id`; dataset must be `active` | Validate budget, deduct ε, return a noised release |
| POST   | `/budgets/transfer`     | API key covering both sides; both datasets `active` | Move unused ε quota from one dataset to another    |
| GET    | `/budgets/{dataset_id}` | API key covering the dataset | Used and remaining budget (works in every state)   |
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
- `409` — idempotency key already exists with different parameters, or the
  catalogued dataset is not `active` (`draft`/`archived`)
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

Unknown datasets report `used_budget = 0` and the configured total as
remaining. After a transfer, `remaining_budget` reflects the quota moved in
or out (only the *unused* part of a budget can ever move — already-spent ε
stays with its dataset).

### `POST /budgets/transfer`

Move unused ε quota from one dataset to another. The source's total drops and
the target's total rises by `epsilon`; used amounts on both sides are
untouched.

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
- `409` — `transfer_id` already exists with different parameters
  (`source_dataset`, `target_dataset` or `epsilon` changed), or either
  catalogued dataset is not `active`
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
# 0. Register datasets in the catalog and activate them (admin token).
#    Draft datasets refuse releases/transfers/key grants until activated.
curl -s -X POST http://localhost:8000/admin/datasets \
  -H "X-API-Key: $DP_ADMIN_API_KEY" -H 'Content-Type: application/json' \
  -d '{"dataset_id":"sales","name":"Worldwide sales","default_budget":1.0}'
# -> {"status":"draft","version":1,...}

curl -s -X PATCH http://localhost:8000/admin/datasets/sales/lifecycle \
  -H "X-API-Key: $DP_ADMIN_API_KEY" -H 'Content-Type: application/json' \
  -d '{"status":"active","version":1}'
# -> {"status":"active","version":2,...}

curl -s -X POST http://localhost:8000/admin/datasets \
  -H "X-API-Key: $DP_ADMIN_API_KEY" -H 'Content-Type: application/json' \
  -d '{"dataset_id":"marketing","name":"Marketing","default_budget":1.0}'
curl -s -X PATCH http://localhost:8000/admin/datasets/marketing/lifecycle \
  -H "X-API-Key: $DP_ADMIN_API_KEY" -H 'Content-Type: application/json' \
  -d '{"status":"active","version":1}'

# inspect the catalog (optionally ?status=active)
curl -s http://localhost:8000/admin/datasets -H "X-API-Key: $DP_ADMIN_API_KEY"

# 1. Mint an API key with the admin token (save the returned api_key now;
#    the plaintext is never shown again). Grants only succeed against active
#    catalogued datasets.
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

# 4. Archive a dataset (version 2 came back from the activate call): new
#    releases/transfers/grants then fail with 409, while GET /budgets/sales
#    and idempotent replays keep working.
curl -s -X PATCH http://localhost:8000/admin/datasets/sales/lifecycle \
  -H "X-API-Key: $DP_ADMIN_API_KEY" -H 'Content-Type: application/json' \
  -d '{"status":"archived","version":2}'
# Repeating the same PATCH (or any transition out of archived) returns 409.
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
- **Dataset catalog and lifecycle:** the v3 migration adds a `datasets` table
  (`dataset_id`, `name`, `default_budget`, `status`, monotonic `version`,
  timestamps). New releases and transfers look up the catalog inside their
  existing write transaction and fail with `409 dataset_not_active` unless the
  dataset is `active` (or has no catalog row, preserving the implicit-dataset
  behavior); key creation applies the same gate before inserting scopes.
  Lifecycle PATCHes are optimistic-concurrency updates: the presented
  `version` is checked (stale/duplicate → `409 version_conflict`) and the
  state machine is checked (`draft→active→archived` only; illegal leg with a
  current version → `409 invalid_lifecycle_transition`) inside one
  `BEGIN IMMEDIATE` transaction, and the final
  `UPDATE ... WHERE status=? AND version=?` guarantees that racing PATCHes
  cannot double-apply a transition. Archiving never deletes data, so release
  replays and budget reads keep working after the fact.
