"""FastAPI application: differential-privacy budget release service."""
from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from . import auth as auth_mod
from .config import settings
from .db import connect, immediate_transaction, init_db
from .privacy import derive_laplace_noise
from .schemas import (
    ApiKeyResponse,
    ApiKeyRevokedResponse,
    BudgetResponse,
    CreateApiKeyRequest,
    HealthResponse,
    ReleaseRequest,
    ReleaseResponse,
    TransferRequest,
    TransferResponse,
)
from .security import resolve_server_key

app = FastAPI(
    title="Differential Privacy Budget Release API",
    version="1.1.0",
    description="Charges epsilon budgets transactionally and releases "
    "Laplace-noised aggregates with idempotent retries. All endpoints except "
    "/health and /docs require an X-API-Key header.",
    redoc_url=None,
)

# Resolved once at startup: env-supplied keys, or persisted random keys.
_server_key: bytes = b""
_admin_key: str = ""

# Paths reachable without an API key (plus the Swagger UI assets the page needs).
_PUBLIC_PATHS = frozenset({"/health", "/docs", "/openapi.json"})

# Fields that must be identical when a release idempotency key is reused.
_REPLAY_FIELDS = (
    "dataset_id",
    "query_id",
    "epsilon",
    "sensitivity",
    "true_value",
)

# Fields that must be identical when a transfer_id is reused.
_TRANSFER_REPLAY_FIELDS = (
    "source_dataset",
    "target_dataset",
    "epsilon",
)

# Float tolerance when comparing a retry against the stored release/transfer.
_EPSILON_COMPARE_TOL = 1e-12


class _ReplayRelease(Exception):
    """Raised inside a transaction when a same-key race must be replayed."""

    def __init__(self, row: sqlite3.Row) -> None:
        self.row = row


class _ReplayTransfer(Exception):
    """Raised inside a transaction when a same-transfer_id race must be replayed."""

    def __init__(self, row: sqlite3.Row) -> None:
        self.row = row


@app.on_event("startup")
def _startup() -> None:
    global _server_key, _admin_key
    init_db()
    _server_key = resolve_server_key()
    _admin_key = auth_mod.resolve_admin_key()


def _unauthorized(detail: str) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_401_UNAUTHORIZED,
        content={"detail": detail},
        headers={"WWW-Authenticate": "ApiKey"},
    )


def _request_principal(request: Request) -> auth_mod.Principal:
    """Return the validated API-key principal attached by the middleware."""
    principal = request.state.principal
    assert isinstance(principal, auth_mod.Principal)
    return principal


def _require_dataset(principal: auth_mod.Principal, dataset_id: str) -> None:
    """Scope check: the key must explicitly cover the dataset."""
    if not principal.allows(dataset_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "dataset_out_of_scope",
                "dataset_id": dataset_id,
                "key_id": principal.key_id,
            },
        )


def _authenticate(presented: str) -> auth_mod.Principal | None:
    """Short WAL read resolving a presented key (off the event loop)."""
    conn = connect()
    try:
        return auth_mod.authenticate_key(presented, conn)
    finally:
        conn.close()


@app.middleware("http")
async def _api_key_middleware(request: Request, call_next):
    """Authenticate every request outside the public paths.

    /health and /docs (plus the OpenAPI document the UI consumes) are open.
    /auth/keys* requires the bootstrap admin token (a data-plane key there is
    403). Everything else requires a valid, unrevoked, unexpired API key;
    dataset-level scope is enforced inside the individual handlers.
    """
    path = request.url.path
    if path in _PUBLIC_PATHS or path.startswith("/docs"):
        return await call_next(request)

    presented = request.headers.get("X-API-Key", "").strip()
    if not presented:
        return _unauthorized("Missing X-API-Key header")

    is_admin_path = path == "/auth/keys" or path.startswith("/auth/keys/")
    if is_admin_path:
        if not auth_mod.authenticate_admin(presented, _admin_key):
            if presented.startswith("dpk_"):
                # A valid data-plane key is never an administrator.
                return JSONResponse(
                    status_code=status.HTTP_403_FORBIDDEN,
                    content={"detail": "Administrator key required for this endpoint"},
                )
            return _unauthorized("Invalid API key")
        request.state.principal = auth_mod.AdminPrincipal()
        return await call_next(request)

    # Data-plane endpoints: one short read transaction (WAL: never blocks
    # readers, waits up to busy_timeout on concurrent writers).
    principal = await run_in_threadpool(_authenticate, presented)
    if principal is None:
        return _unauthorized("Invalid, revoked or expired API key")
    request.state.principal = principal
    return await call_next(request)


@app.exception_handler(RequestValidationError)
def _validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    # The default handler echoes rejected values in "ctx"/"input", which can
    # contain NaN/Infinity and crash strict JSON rendering; drop ctx and
    # replace non-finite floats with their textual token.
    def _sanitize(value):  # type: ignore[no-untyped-def]
        if isinstance(value, float):
            if value != value:
                return "NaN"
            if value == float("inf"):
                return "Infinity"
            if value == float("-inf"):
                return "-Infinity"
            return value
        if isinstance(value, dict):
            return {k: _sanitize(v) for k, v in value.items()}
        if isinstance(value, list):
            return [_sanitize(v) for v in value]
        return value

    safe_errors = []
    for error in exc.errors():
        error = {k: v for k, v in error.items() if k != "ctx"}
        safe_errors.append(_sanitize(error))
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": safe_errors},
    )


@app.get("/health", response_model=HealthResponse, tags=["health"])
def health() -> HealthResponse:
    return HealthResponse(status="ok")


# --- API key management (admin token only) ---


@app.post(
    "/auth/keys",
    response_model=ApiKeyResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["auth"],
)
def create_api_key(payload: CreateApiKeyRequest) -> ApiKeyResponse:
    """Mint an API key scoped to a list of datasets.

    The plaintext token is returned exactly once in the ``api_key`` field;
    only its SHA-256 hash is persisted. Requires the admin token (env
    ``DP_ADMIN_API_KEY`` or the persisted bootstrap token) in X-API-Key.
    """
    if payload.ttl_seconds is not None:
        expires_dt: datetime | None = datetime.now(timezone.utc) + timedelta(
            seconds=payload.ttl_seconds
        )
    else:
        expires_dt = payload.expires_at
    expires_text = (
        expires_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        if expires_dt
        else None
    )

    # BEGIN IMMEDIATE serializes concurrent minting; retry on the (astronomically
    # unlikely) collision of a random key id or digest.
    for _ in range(3):
        key_id, token, key_prefix = auth_mod.generate_api_key()
        created_at = auth_mod.utcnow_iso()
        try:
            with immediate_transaction() as conn:
                conn.execute(
                    "INSERT INTO api_keys "
                    "(key_id, name, key_hash, key_prefix, expires_at, revoked, created_at) "
                    "VALUES (?, ?, ?, ?, ?, 0, ?)",
                    (
                        key_id,
                        payload.name,
                        auth_mod.hash_token(token),
                        key_prefix,
                        expires_text,
                        created_at,
                    ),
                )
                conn.executemany(
                    "INSERT INTO api_key_scopes (key_id, dataset_id) VALUES (?, ?)",
                    [(key_id, ds) for ds in payload.dataset_ids],
                )
        except sqlite3.IntegrityError:
            continue
        return ApiKeyResponse(
            key_id=key_id,
            name=payload.name,
            api_key=token,
            dataset_ids=list(payload.dataset_ids),
            expires_at=expires_dt,
            created_at=created_at,
        )
    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="could not allocate a unique API key",
    )


@app.delete(
    "/auth/keys/{key_id}",
    response_model=ApiKeyRevokedResponse,
    status_code=status.HTTP_200_OK,
    tags=["auth"],
)
def revoke_api_key(key_id: str) -> ApiKeyRevokedResponse:
    """Revoke an API key. The hash is retained (so the token can never be
    re-minted), but every subsequent authentication with it fails.

    Unknown key ids return 404.
    """
    with immediate_transaction() as conn:
        row = conn.execute(
            "SELECT 1 FROM api_keys WHERE key_id = ?", (key_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Unknown key_id"
            )
        conn.execute(
            "UPDATE api_keys SET revoked = 1 WHERE key_id = ?", (key_id,)
        )
    return ApiKeyRevokedResponse(key_id=key_id, revoked=True)


def _row_to_release(row: sqlite3.Row) -> ReleaseResponse:
    return ReleaseResponse(
        id=row["id"],
        idempotency_key=row["idempotency_key"],
        dataset_id=row["dataset_id"],
        query_id=row["query_id"],
        epsilon=row["epsilon"],
        sensitivity=row["sensitivity"],
        released_value=row["released_value"],
        noise=row["noise"],
        budget_used=row["budget_used"],
        budget_remaining=row["budget_remaining"],
    )


def _params_match(row: sqlite3.Row, req: object, fields: tuple[str, ...]) -> bool:
    for field in fields:
        stored = row[field]
        incoming = getattr(req, field)
        if isinstance(stored, float):
            if abs(stored - incoming) > _EPSILON_COMPARE_TOL * max(1.0, abs(stored), abs(incoming)):
                return False
        elif stored != incoming:
            return False
    return True


def _idempotency_key(request: Request, payload: ReleaseRequest) -> str:
    """Prefer the Idempotency-Key header, fall back to the request body."""
    header = request.headers.get("Idempotency-Key", "").strip()
    if header:
        if header != payload.idempotency_key:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Idempotency-Key header does not match idempotency_key in the body",
            )
        return header
    return payload.idempotency_key


@app.post(
    "/releases",
    response_model=ReleaseResponse,
    status_code=status.HTTP_200_OK,
    tags=["releases"],
)
def create_release(
    payload: ReleaseRequest, request: Request
) -> ReleaseResponse:
    principal = _request_principal(request)
    _require_dataset(principal, payload.dataset_id)
    idem_key = _idempotency_key(request, payload)
    total_budget = settings.budget_for(payload.dataset_id)

    try:
        with immediate_transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM releases WHERE idempotency_key = ?",
                (idem_key,),
            ).fetchone()
            if existing is not None:
                if not _params_match(existing, payload, _REPLAY_FIELDS):
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="Idempotency key was already used with different parameters",
                    )
                # Identical retry: replay the stored release, no extra charge.
                # (A read-only transaction commits harmlessly on context exit.)
                return _row_to_release(existing)

            # Ensure a budget row exists and lock/freeze it for this transaction.
            # A budget row may have been created/changed by a transfer, so on
            # conflict do nothing: the configured total only seeds a new dataset.
            conn.execute(
                "INSERT INTO budgets (dataset_id, total, used) VALUES (?, ?, 0) "
                "ON CONFLICT(dataset_id) DO NOTHING",
                (payload.dataset_id, total_budget),
            )
            budget = conn.execute(
                "SELECT total, used FROM budgets WHERE dataset_id = ?",
                (payload.dataset_id,),
            ).fetchone()
            used = float(budget["used"])
            total = float(budget["total"])
            new_used = used + payload.epsilon
            # Tolerance covers binary float rounding when the request exactly
            # exhausts the remaining budget.
            if new_used > total + 1e-9:
                raise HTTPException(
                    status_code=status.HTTP_402_PAYMENT_REQUIRED,
                    detail={
                        "error": "insufficient_privacy_budget",
                        "dataset_id": payload.dataset_id,
                        "total_budget": total,
                        "used_budget": used,
                        "remaining_budget": total - used,
                        "requested_epsilon": payload.epsilon,
                    },
                )

            noise = derive_laplace_noise(
                key=_server_key,
                dataset_id=payload.dataset_id,
                query_id=payload.query_id,
                epsilon=payload.epsilon,
                sensitivity=payload.sensitivity,
                idempotency_key=idem_key,
            )
            released_value = payload.true_value + noise
            remaining = total - new_used
            release_id = uuid.uuid4().hex

            conn.execute(
                "UPDATE budgets SET used = ? WHERE dataset_id = ?",
                (new_used, payload.dataset_id),
            )
            try:
                conn.execute(
                    """
                    INSERT INTO releases (
                        id, idempotency_key, dataset_id, query_id, epsilon,
                        sensitivity, true_value, noise, released_value,
                        budget_used, budget_remaining
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        release_id,
                        idem_key,
                        payload.dataset_id,
                        payload.query_id,
                        payload.epsilon,
                        payload.sensitivity,
                        payload.true_value,
                        noise,
                        released_value,
                        new_used,
                        remaining,
                    ),
                )
            except sqlite3.IntegrityError:
                # Lost a race against another transaction with the same key.
                winner = conn.execute(
                    "SELECT * FROM releases WHERE idempotency_key = ?",
                    (idem_key,),
                ).fetchone()
                if winner is not None and _params_match(winner, payload, _REPLAY_FIELDS):
                    # Same request won: roll back our duplicate deduction
                    # (context exit rolls back) and replay the winner.
                    raise _ReplayRelease(winner)
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Idempotency key was already used with different parameters",
                )

            stored = conn.execute(
                "SELECT * FROM releases WHERE id = ?", (release_id,)
            ).fetchone()
            return _row_to_release(stored)
    except _ReplayRelease as replay:
        return _row_to_release(replay.row)


def _row_to_transfer(row: sqlite3.Row) -> TransferResponse:
    return TransferResponse(
        transfer_id=row["id"],
        source_dataset=row["source_dataset"],
        target_dataset=row["target_dataset"],
        epsilon=row["epsilon"],
        source=BudgetResponse(
            dataset_id=row["source_dataset"],
            used_budget=row["source_used"],
            remaining_budget=row["source_remaining"],
        ),
        target=BudgetResponse(
            dataset_id=row["target_dataset"],
            used_budget=row["target_used"],
            remaining_budget=row["target_remaining"],
        ),
    )


@app.post(
    "/budgets/transfer",
    response_model=TransferResponse,
    status_code=status.HTTP_200_OK,
    tags=["budgets"],
)
def transfer_budget(payload: TransferRequest, request: Request) -> TransferResponse:
    """Move unused epsilon quota from one dataset to another.

    Both balances are read and updated inside one ``BEGIN IMMEDIATE``
    transaction; any validation failure rolls the whole transfer back.
    """
    principal = _request_principal(request)
    # A transfer writes both budgets, so the key must cover both datasets.
    _require_dataset(principal, payload.source_dataset)
    _require_dataset(principal, payload.target_dataset)

    source_id = payload.source_dataset
    target_id = payload.target_dataset
    total_for_source = settings.budget_for(source_id)
    total_for_target = settings.budget_for(target_id)

    try:
        with immediate_transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM transfers WHERE id = ?",
                (payload.transfer_id,),
            ).fetchone()
            if existing is not None:
                if not _params_match(existing, payload, _TRANSFER_REPLAY_FIELDS):
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="transfer_id was already used with different parameters",
                    )
                # Identical retry: replay the stored transfer, balances
                # are untouched.
                return _row_to_transfer(existing)

            # Seed any missing budget row with its configured total. Inserting
            # in a fixed (alphabetical) order keeps lock acquisition
            # deterministic across concurrent transfers.
            seed_ids = sorted(
                {(source_id, total_for_source), (target_id, total_for_target)}
            )
            for dataset_id, configured_total in seed_ids:
                conn.execute(
                    "INSERT INTO budgets (dataset_id, total, used) VALUES (?, ?, 0) "
                    "ON CONFLICT(dataset_id) DO NOTHING",
                    (dataset_id, configured_total),
                )

            # Read and freeze both balances within the write transaction.
            rows = conn.execute(
                "SELECT dataset_id, total, used FROM budgets "
                "WHERE dataset_id IN (?, ?)",
                (source_id, target_id),
            ).fetchall()
            balances = {row["dataset_id"]: row for row in rows}
            source = balances[source_id]
            target = balances[target_id]

            source_total = float(source["total"])
            source_used = float(source["used"])
            source_remaining = source_total - source_used
            epsilon = payload.epsilon
            # Tolerance covers binary float rounding when the transfer moves
            # exactly the remaining quota.
            if epsilon > source_remaining + 1e-9:
                raise HTTPException(
                    status_code=status.HTTP_402_PAYMENT_REQUIRED,
                    detail={
                        "error": "insufficient_transferable_budget",
                        "source_dataset": source_id,
                        "target_dataset": target_id,
                        "transfer_id": payload.transfer_id,
                        "available_remaining": source_remaining,
                        "requested_epsilon": epsilon,
                    },
                )

            target_total = float(target["total"])
            target_used = float(target["used"])

            # Moving unused quota lowers the source's total and raises the
            # target's; used amounts never change.
            conn.execute(
                "UPDATE budgets SET total = total - ? WHERE dataset_id = ?",
                (epsilon, source_id),
            )
            conn.execute(
                "UPDATE budgets SET total = total + ? WHERE dataset_id = ?",
                (epsilon, target_id),
            )

            source_remaining_after = source_remaining - epsilon
            target_remaining_after = target_total - target_used + epsilon

            try:
                conn.execute(
                    """
                    INSERT INTO transfers (
                        id, source_dataset, target_dataset, epsilon,
                        source_used, source_remaining,
                        target_used, target_remaining
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        payload.transfer_id,
                        source_id,
                        target_id,
                        epsilon,
                        source_used,
                        source_remaining_after,
                        target_used,
                        target_remaining_after,
                    ),
                )
            except sqlite3.IntegrityError:
                # Lost a race against another transaction with the same
                # transfer_id; roll back and replay the committed winner.
                winner = conn.execute(
                    "SELECT * FROM transfers WHERE id = ?",
                    (payload.transfer_id,),
                ).fetchone()
                if winner is not None and _params_match(winner, payload, _TRANSFER_REPLAY_FIELDS):
                    raise _ReplayTransfer(winner)
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="transfer_id was already used with different parameters",
                )

            stored = conn.execute(
                "SELECT * FROM transfers WHERE id = ?", (payload.transfer_id,)
            ).fetchone()
            return _row_to_transfer(stored)
    except _ReplayTransfer as replay:
        return _row_to_transfer(replay.row)


@app.get(
    "/budgets/{dataset_id}",
    response_model=BudgetResponse,
    tags=["budgets"],
)
def get_budget(dataset_id: str, request: Request) -> BudgetResponse:
    principal = _request_principal(request)
    _require_dataset(principal, dataset_id)
    configured_total = settings.budget_for(dataset_id)
    conn = connect()
    try:
        row = conn.execute(
            "SELECT total, used FROM budgets WHERE dataset_id = ?",
            (dataset_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        total = configured_total
        used = 0.0
    else:
        # The stored total reflects quota moved in/out by transfers.
        total = float(row["total"])
        used = float(row["used"])
    return BudgetResponse(
        dataset_id=dataset_id,
        used_budget=used,
        remaining_budget=total - used,
    )


def _custom_openapi() -> dict:  # type: ignore[no-untyped-def]
    """Annotate the schema with the X-API-Key header security scheme so the
    Swagger UI Authorize button sends it on protected endpoints."""
    if app.openapi_schema:
        return app.openapi_schema
    from fastapi.openapi.utils import get_openapi

    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )
    schema["components"]["securitySchemes"] = {
        "ApiKeyAuth": {
            "type": "apiKey",
            "in": "header",
            "name": "X-API-Key",
        }
    }
    protected_methods = ("get", "post", "put", "patch", "delete")
    for path, path_item in schema.get("paths", {}).items():
        if path in _PUBLIC_PATHS:
            continue
        for method, operation in path_item.items():
            if method in protected_methods:
                operation.setdefault("security", []).append({"ApiKeyAuth": []})
    app.openapi_schema = schema
    return schema


app.openapi = _custom_openapi
