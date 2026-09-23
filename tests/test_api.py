"""End-to-end tests using a temporary SQLite database."""
from __future__ import annotations

import importlib
import os
import tempfile
import threading
import unittest
import urllib.request
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# Configure environment BEFORE importing the app.
_TMP = tempfile.mkdtemp(prefix="dp-api-tests-")
os.environ["DATABASE_PATH"] = str(Path(_TMP) / "test.db")
os.environ["DP_TOTAL_BUDGET"] = "1.0"
os.environ["DP_DATASET_BUDGETS"] = "tiny:0.5"
os.environ["DP_SERVER_KEY"] = "unit-test-server-key"
os.environ["DP_ADMIN_API_KEY"] = "admin-secret-test-token"

ADMIN_KEY = os.environ["DP_ADMIN_API_KEY"]

import uvicorn  # noqa: E402

from app import main as main_module  # noqa: E402
from app.db import connect, init_db  # noqa: E402

PORT = 8765

# Every dataset touched by the legacy test suite; the main data key is scoped
# to all of them. They must be registered and activated through the admin
# catalog before any data-plane traffic (new grants/releases/transfers only
# target active datasets).
_MAIN_DATASETS = [
    "ds", "race", "idem", "idem2", "tiny",
    "t11-src", "t11-dst", "t12-src", "t12-dst",
    "t13-src", "t13-dst", "t13-other",
    "t14-src", "t14-dst",
    "t15",
    "pool", "pool-dst",
    "chain", "chain2", "fan", "fan2", "x", "y",
    # Scope/validation tests:
    "alpha", "beta", "gamma", "delta",
    "secret-ds", "rev-ds", "exp-ds", "tamper-ds",
    # test_28 mints keys for cc-0 .. cc-23, which must already be active.
    *[f"cc-{i}" for i in range(24)],
]


def _create_dataset(
    dataset_id: str,
    *,
    name: str | None = None,
    default_budget: float = 1.0,
    admin: str = ADMIN_KEY,
):
    return _request(
        "POST",
        "/admin/datasets",
        {
            "dataset_id": dataset_id,
            "name": name if name is not None else f"catalog {dataset_id}",
            "default_budget": default_budget,
        },
        api_key=admin,
    )


def _activate_dataset(dataset_id: str, version: int = 0, admin: str = ADMIN_KEY):
    return _request(
        "PATCH",
        f"/admin/datasets/{dataset_id}/lifecycle",
        {"version": version, "action": "activate"},
        api_key=admin,
    )


def _archive_dataset(dataset_id: str, version: int, admin: str = ADMIN_KEY):
    return _request(
        "PATCH",
        f"/admin/datasets/{dataset_id}/lifecycle",
        {"version": version, "action": "archive"},
        api_key=admin,
    )

# Populated in setUpClass after the server starts.
DATA_KEY: str = ""
DATA_KEY_ID: str = ""


def _request(
    method: str,
    path: str,
    body: dict | None = None,
    headers: dict | None = None,
    api_key: str | None = "__default__",
):
    url = f"http://127.0.0.1:{PORT}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if api_key == "__default__":
        api_key = DATA_KEY
    if api_key:
        req.add_header("X-API-Key", api_key)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _request_raw(
    method: str,
    path: str,
    body: dict | None = None,
    headers: dict | None = None,
    api_key: str | None = "__default__",
):
    url = f"http://127.0.0.1:{PORT}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if api_key == "__default__":
        api_key = DATA_KEY
    if api_key:
        req.add_header("X-API-Key", api_key)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _create_key(
    datasets: list[str],
    *,
    name: str = "test key",
    ttl_seconds: int | None = None,
    expires_at: str | None = None,
    admin: str = ADMIN_KEY,
):
    payload: dict = {"name": name, "dataset_ids": datasets}
    if ttl_seconds is not None:
        payload["ttl_seconds"] = ttl_seconds
    if expires_at is not None:
        payload["expires_at"] = expires_at
    return _request("POST", "/auth/keys", payload, api_key=admin)


class ApiTests(unittest.TestCase):
    server_started = False

    @classmethod
    def setUpClass(cls) -> None:
        config = uvicorn.Config(
            main_module.app, host="127.0.0.1", port=PORT, log_level="error"
        )
        cls.server = uvicorn.Server(config)
        cls.thread = threading.Thread(target=cls.server.run, daemon=True)
        cls.thread.start()
        for _ in range(100):
            try:
                _request("GET", "/health", api_key="")
                break
            except OSError:
                import time

                time.sleep(0.1)
        else:
            raise RuntimeError("test server failed to start")

        # Register and activate every legacy-test dataset through the admin
        # catalog first; "tiny" carries its per-dataset 0.5 budget.
        for dataset_id in _MAIN_DATASETS:
            default_budget = 0.5 if dataset_id == "tiny" else 1.0
            code, body = _create_dataset(
                dataset_id, default_budget=default_budget
            )
            assert code == 201, body
            assert body["status"] == "draft" and body["version"] == 0, body
            code, body = _activate_dataset(dataset_id)
            assert code == 200, body
            assert body["status"] == "active" and body["version"] == 1, body

        # Bootstrapped main data key covering every legacy-test dataset.
        global DATA_KEY, DATA_KEY_ID
        status, body = _create_key(_MAIN_DATASETS, name="main test key")
        assert status == 201, body
        DATA_KEY = body["api_key"]
        DATA_KEY_ID = body["key_id"]

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.should_exit = True
        cls.thread.join(timeout=5)

    def test_01_health(self) -> None:
        status, body = _request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"status": "ok"})

    def test_01b_docs_and_openapi_are_public(self) -> None:
        status, openapi = _request("GET", "/openapi.json", api_key="")
        self.assertEqual(status, 200)
        self.assertEqual(openapi["components"]["securitySchemes"]["ApiKeyAuth"]["name"],
                         "X-API-Key")
        status, html = _request_raw("GET", "/docs", api_key="")
        self.assertEqual(status, 200)
        self.assertIn(b"swagger", html.lower())

    def test_02_release_charges_budget(self) -> None:
        status, body = _request(
            "POST",
            "/releases",
            {
                "dataset_id": "ds",
                "query_id": "count-men",
                "epsilon": 0.3,
                "sensitivity": 1.0,
                "true_value": 42.0,
                "idempotency_key": "k1",
            },
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(body["budget_used"], 0.3)
        self.assertAlmostEqual(body["budget_remaining"], 0.7, places=9)
        self.assertAlmostEqual(body["released_value"], 42.0 + body["noise"], places=9)

    def test_03_idempotent_replay_identical(self) -> None:
        payload = {
            "dataset_id": "ds",
            "query_id": "count-men",
            "epsilon": 0.3,
            "sensitivity": 1.0,
            "true_value": 42.0,
            "idempotency_key": "k1",
        }
        _, first = _request("POST", "/releases", payload)
        _, second = _request("POST", "/releases", payload)
        self.assertEqual(first, second)

        status, budget = _request("GET", "/budgets/ds")
        self.assertEqual(status, 200)
        self.assertEqual(set(budget), {"dataset_id", "used_budget", "remaining_budget"})
        self.assertAlmostEqual(budget["used_budget"], 0.3, places=9)

    def test_04_same_key_changed_params_conflict(self) -> None:
        status, body = _request(
            "POST",
            "/releases",
            {
                "dataset_id": "ds",
                "query_id": "count-men",
                "epsilon": 0.3,
                "sensitivity": 1.0,
                "true_value": 999.0,
                "idempotency_key": "k1",
            },
        )
        self.assertEqual(status, 409)

    def test_05_insufficient_budget(self) -> None:
        status, body = _request(
            "POST",
            "/releases",
            {
                "dataset_id": "ds",
                "query_id": "q",
                "epsilon": 0.9,
                "sensitivity": 1.0,
                "true_value": 1.0,
                "idempotency_key": "k2",
            },
        )
        self.assertEqual(status, 402)
        self.assertEqual(body["detail"]["error"], "insufficient_privacy_budget")

    def test_06_exact_remaining_succeeds(self) -> None:
        status, _ = _request(
            "POST",
            "/releases",
            {
                "dataset_id": "ds",
                "query_id": "q",
                "epsilon": 0.7,
                "sensitivity": 1.0,
                "true_value": 1.0,
                "idempotency_key": "k3",
            },
        )
        self.assertEqual(status, 200)

    def test_07_concurrent_race_for_last_slice(self) -> None:
        def call(i: int):
            return _request(
                "POST",
                "/releases",
                {
                    "dataset_id": "race",
                    "query_id": "q",
                    "epsilon": 1.0,
                    "sensitivity": 1.0,
                    "true_value": 1.0,
                    "idempotency_key": f"r{i}",
                },
            )[0]

        with ThreadPoolExecutor(max_workers=16) as pool:
            codes = list(pool.map(call, range(16)))
        self.assertEqual(codes.count(200), 1)
        self.assertEqual(codes.count(402), 15)

    def test_08_concurrent_identical_key_single_charge(self) -> None:
        def call(_i: int):
            return _request(
                "POST",
                "/releases",
                {
                    "dataset_id": "idem",
                    "query_id": "q",
                    "epsilon": 0.2,
                    "sensitivity": 1.0,
                    "true_value": 1.0,
                    "idempotency_key": "same",
                },
            )

        with ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(call, range(16)))
        self.assertTrue(all(code == 200 for code, _ in results))
        self.assertEqual(len({body["id"] for _, body in results}), 1)
        _, budget = _request("GET", "/budgets/idem")
        self.assertAlmostEqual(budget["used_budget"], 0.2, places=9)

    def test_08b_concurrent_same_key_race_with_budget_update(self) -> None:
        """Same-key fan-out against a fresh dataset: every request does the
        budget upsert before hitting the unique constraint, exercising the
        loser-rollback-and-replay path. Must still result in one release and
        a single charge."""
        payload = {
            "dataset_id": "idem2",
            "query_id": "q",
            "epsilon": 0.2,
            "sensitivity": 1.0,
            "true_value": 1.0,
            "idempotency_key": "same2",
        }

        def call(_i: int):
            return _request("POST", "/releases", payload)

        with ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(call, range(32)))
        statuses = [code for code, _ in results]
        self.assertTrue(all(code == 200 for code in statuses), statuses)
        self.assertEqual(len({body["id"] for _, body in results}), 1)
        _, budget = _request("GET", "/budgets/idem2")
        self.assertAlmostEqual(budget["used_budget"], 0.2, places=9)
        self.assertAlmostEqual(budget["remaining_budget"], 0.8, places=9)

    def test_09_per_dataset_budget(self) -> None:
        status, body = _request("GET", "/budgets/tiny")
        self.assertEqual(status, 200)
        self.assertAlmostEqual(body["remaining_budget"], 0.5, places=9)

    def test_10_validation(self) -> None:
        bad = [
            {"dataset_id": "d", "query_id": "q", "epsilon": 0, "sensitivity": 1,
             "true_value": 1, "idempotency_key": "x"},
            {"dataset_id": "d", "query_id": "q", "epsilon": 0.1, "sensitivity": 1,
             "true_value": float("nan"), "idempotency_key": "x"},
            {"dataset_id": "d", "query_id": "q", "epsilon": 0.1, "sensitivity": 1,
             "true_value": 1, "idempotency_key": "x", "extra": 2},
        ]
        for payload in bad:
            status, _ = _request("POST", "/releases", payload)
            self.assertEqual(status, 422)

    # --- budget transfers ---

    def test_11_transfer_moves_unused_quota(self) -> None:
        status, body = _request(
            "POST",
            "/budgets/transfer",
            {"transfer_id": "tr1", "source_dataset": "t11-src",
             "target_dataset": "t11-dst", "epsilon": 0.4},
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(body["transfer_id"], "tr1")
        self.assertAlmostEqual(body["source"]["remaining_budget"], 0.6, places=9)
        self.assertEqual(body["source"]["used_budget"], 0.0)
        self.assertAlmostEqual(body["target"]["remaining_budget"], 1.4, places=9)
        self.assertEqual(body["target"]["used_budget"], 0.0)

        _, src = _request("GET", "/budgets/t11-src")
        self.assertAlmostEqual(src["remaining_budget"], 0.6, places=9)
        _, dst = _request("GET", "/budgets/t11-dst")
        self.assertAlmostEqual(dst["remaining_budget"], 1.4, places=9)

    def test_12_transfer_idempotent_replay(self) -> None:
        payload = {"transfer_id": "tr2", "source_dataset": "t12-src",
                   "target_dataset": "t12-dst", "epsilon": 0.1}
        _, first = _request("POST", "/budgets/transfer", payload)
        _, second = _request("POST", "/budgets/transfer", payload)
        self.assertEqual(first, second)

        # Replay must not move any extra quota.
        _, src = _request("GET", "/budgets/t12-src")
        self.assertAlmostEqual(src["remaining_budget"], 0.9, places=9)
        _, dst = _request("GET", "/budgets/t12-dst")
        self.assertAlmostEqual(dst["remaining_budget"], 1.1, places=9)

    def test_13_transfer_same_id_changed_params_conflict(self) -> None:
        # Establish the original transfer first.
        status, _ = _request(
            "POST",
            "/budgets/transfer",
            {"transfer_id": "tr3", "source_dataset": "t13-src",
             "target_dataset": "t13-dst", "epsilon": 0.1},
        )
        self.assertEqual(status, 200)

        for changed in (
            {"source_dataset": "t13-other"},
            {"target_dataset": "t13-other"},
            {"epsilon": 0.11},
        ):
            payload = {"transfer_id": "tr3", "source_dataset": "t13-src",
                       "target_dataset": "t13-dst", "epsilon": 0.1}
            payload.update(changed)
            status, _ = _request("POST", "/budgets/transfer", payload)
            self.assertEqual(status, 409, changed)

        # The conflicting attempts must not have moved anything beyond the
        # original 0.1.
        _, src = _request("GET", "/budgets/t13-src")
        self.assertAlmostEqual(src["remaining_budget"], 0.9, places=9)
        _, dst = _request("GET", "/budgets/t13-dst")
        self.assertAlmostEqual(dst["remaining_budget"], 1.1, places=9)

    def test_14_transfer_insufficient_source_balance(self) -> None:
        # Move half the quota away first, leaving 0.5.
        status, _ = _request(
            "POST",
            "/budgets/transfer",
            {"transfer_id": "tr4a", "source_dataset": "t14-src",
             "target_dataset": "t14-dst", "epsilon": 0.5},
        )
        self.assertEqual(status, 200)

        status, body = _request(
            "POST",
            "/budgets/transfer",
            {"transfer_id": "tr4b", "source_dataset": "t14-src",
             "target_dataset": "t14-dst", "epsilon": 0.6},
        )
        self.assertEqual(status, 402)
        self.assertEqual(body["detail"]["error"], "insufficient_transferable_budget")
        self.assertEqual(body["detail"]["source_dataset"], "t14-src")
        self.assertAlmostEqual(body["detail"]["available_remaining"], 0.5, places=9)
        self.assertAlmostEqual(body["detail"]["requested_epsilon"], 0.6, places=9)

        # Failed transfer changes nothing.
        _, src = _request("GET", "/budgets/t14-src")
        self.assertAlmostEqual(src["remaining_budget"], 0.5, places=9)

        # Transferring exactly the remaining quota is allowed.
        status, body = _request(
            "POST",
            "/budgets/transfer",
            {"transfer_id": "tr5", "source_dataset": "t14-src",
             "target_dataset": "t14-dst", "epsilon": 0.5},
        )
        self.assertEqual(status, 200, body)
        self.assertAlmostEqual(body["source"]["remaining_budget"], 0.0, places=9)
        self.assertAlmostEqual(body["target"]["remaining_budget"], 2.0, places=9)

    def test_15_transfer_same_source_and_target(self) -> None:
        status, body = _request(
            "POST",
            "/budgets/transfer",
            {"transfer_id": "tr6", "source_dataset": "t15",
             "target_dataset": "t15", "epsilon": 0.1},
        )
        self.assertEqual(status, 422)
        locs = [tuple(err["loc"]) for err in body["detail"]]
        self.assertIn(("body", "target_dataset"), locs)

    def test_16_transfer_validation(self) -> None:
        bad = [
            {"transfer_id": "x", "source_dataset": "a", "target_dataset": "b",
             "epsilon": 0},
            {"transfer_id": "x", "source_dataset": "a", "target_dataset": "b",
             "epsilon": -0.1},
            {"transfer_id": "x", "source_dataset": "a", "target_dataset": "b",
             "epsilon": float("nan")},
            {"transfer_id": "x", "source_dataset": "a", "target_dataset": "b",
             "epsilon": 0.1, "extra": 1},
            {"transfer_id": "", "source_dataset": "a", "target_dataset": "b",
             "epsilon": 0.1},
            {"transfer_id": "x", "source_dataset": "a", "target_dataset": "",
             "epsilon": 0.1},
        ]
        for payload in bad:
            status, _ = _request("POST", "/budgets/transfer", payload)
            self.assertEqual(status, 422, payload)

    def test_17_transfer_preserves_used_budget(self) -> None:
        # Spend 0.3 at pool (1.0 total -> 0.7 remaining), then move 0.5 out.
        status, _ = _request(
            "POST",
            "/releases",
            {"dataset_id": "pool", "query_id": "q", "epsilon": 0.3,
             "sensitivity": 1.0, "true_value": 1.0, "idempotency_key": "pool-k1"},
        )
        self.assertEqual(status, 200)
        status, body = _request(
            "POST",
            "/budgets/transfer",
            {"transfer_id": "tr7", "source_dataset": "pool",
             "target_dataset": "pool-dst", "epsilon": 0.5},
        )
        self.assertEqual(status, 200, body)
        self.assertAlmostEqual(body["source"]["used_budget"], 0.3, places=9)
        self.assertAlmostEqual(body["source"]["remaining_budget"], 0.2, places=9)
        self.assertEqual(body["target"]["used_budget"], 0.0)
        self.assertAlmostEqual(body["target"]["remaining_budget"], 1.5, places=9)

        # Already-spent epsilon cannot leave the source.
        status, _ = _request(
            "POST",
            "/budgets/transfer",
            {"transfer_id": "tr8", "source_dataset": "pool",
             "target_dataset": "pool-dst", "epsilon": 0.21},
        )
        self.assertEqual(status, 402)

        # The post-transfer total governs releases at both sides.
        status, body = _request(
            "POST",
            "/releases",
            {"dataset_id": "pool-dst", "query_id": "q", "epsilon": 1.5,
             "sensitivity": 1.0, "true_value": 1.0, "idempotency_key": "pool-k2"},
        )
        self.assertEqual(status, 200)
        self.assertAlmostEqual(body["budget_remaining"], 0.0, places=9)
        status, _ = _request(
            "POST",
            "/releases",
            {"dataset_id": "pool", "query_id": "q", "epsilon": 0.21,
             "sensitivity": 1.0, "true_value": 1.0, "idempotency_key": "pool-k3"},
        )
        self.assertEqual(status, 402)

    def test_18_concurrent_transfer_chain_serializes(self) -> None:
        """Ten 0.2 transfers chain -> chain2 out of a fresh 1.0 dataset;
        exactly five can succeed (draining it fully), the rest see 402."""
        def call(i: int):
            return _request(
                "POST",
                "/budgets/transfer",
                {"transfer_id": f"chain-{i}", "source_dataset": "chain",
                 "target_dataset": "chain2", "epsilon": 0.2},
            )[0]

        with ThreadPoolExecutor(max_workers=10) as pool:
            codes = list(pool.map(call, range(10)))
        self.assertEqual(sorted(codes), [200] * 5 + [402] * 5, codes)
        _, src = _request("GET", "/budgets/chain")
        self.assertAlmostEqual(src["remaining_budget"], 0.0, places=9)
        _, dst = _request("GET", "/budgets/chain2")
        self.assertAlmostEqual(dst["remaining_budget"], 2.0, places=9)

    def test_19_concurrent_same_transfer_id_single_move(self) -> None:
        payload = {"transfer_id": "same-tr", "source_dataset": "fan",
                   "target_dataset": "fan2", "epsilon": 0.2}

        def call(_i: int):
            return _request("POST", "/budgets/transfer", payload)

        with ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(call, range(16)))
        statuses = [code for code, _ in results]
        self.assertTrue(all(code == 200 for code in statuses), statuses)
        bodies = [body for _, body in results]
        self.assertEqual(len({b["transfer_id"] for b in bodies}), 1)
        _, src = _request("GET", "/budgets/fan")
        self.assertAlmostEqual(src["remaining_budget"], 0.8, places=9)
        _, dst = _request("GET", "/budgets/fan2")
        self.assertAlmostEqual(dst["remaining_budget"], 1.2, places=9)

    def test_20_concurrent_bidirectional_transfers(self) -> None:
        """Crossing transfers in both directions must never deadlock and the
        books must stay balanced: remaining quotas still sum to 2.0."""
        def call(i: int):
            if i % 2 == 0:
                payload = {"transfer_id": f"cross-{i}", "source_dataset": "x",
                           "target_dataset": "y", "epsilon": 0.05}
            else:
                payload = {"transfer_id": f"cross-{i}", "source_dataset": "y",
                           "target_dataset": "x", "epsilon": 0.05}
            return _request("POST", "/budgets/transfer", payload)[0]

        with ThreadPoolExecutor(max_workers=16) as pool:
            codes = list(pool.map(call, range(20)))
        self.assertTrue(all(code == 200 for code in codes), codes)
        _, x = _request("GET", "/budgets/x")
        _, y = _request("GET", "/budgets/y")
        self.assertAlmostEqual(x["remaining_budget"] + y["remaining_budget"], 2.0, places=9)

    # --- API-key authentication ---

    def test_21_data_endpoints_require_key(self) -> None:
        for method, path in (
            ("GET", "/budgets/ds"),
            ("POST", "/releases"),
            ("POST", "/budgets/transfer"),
        ):
            status, body = _request(method, path, {}, api_key="")
            self.assertEqual(status, 401, path)
            self.assertIn("X-API-Key", body["detail"])

        status, body = _request("GET", "/budgets/ds", api_key="not-a-real-key")
        self.assertEqual(status, 401)

        # A well-shaped token with an unknown id is also rejected (and does
        # not leak whether the id exists).
        fake = "dpk_" + "0" * 32 + "." + "A" * 43
        status, _ = _request("GET", "/budgets/ds", api_key=fake)
        self.assertEqual(status, 401)

    def test_22_create_key_requires_admin(self) -> None:
        # No key at all.
        status, _ = _request(
            "POST", "/auth/keys",
            {"name": "n", "dataset_ids": ["z"]}, api_key="",
        )
        self.assertEqual(status, 401)
        # Wrong admin token.
        status, _ = _request(
            "POST", "/auth/keys",
            {"name": "n", "dataset_ids": ["z"]}, api_key="wrong-admin",
        )
        self.assertEqual(status, 401)
        # A valid data-plane key is forbidden from managing keys.
        status, body = _request(
            "POST", "/auth/keys",
            {"name": "n", "dataset_ids": ["z"]}, api_key=DATA_KEY,
        )
        self.assertEqual(status, 403, body)

    def test_23_create_key_returns_plaintext_once(self) -> None:
        status, body = _create_key(["secret-ds"], name="scoped 23")
        self.assertEqual(status, 201, body)
        self.assertRegex(body["api_key"], r"^dpk_[0-9a-f]{32}\.[A-Za-z0-9_-]{43}$")
        self.assertEqual(body["key_id"], body["api_key"].split(".")[0][len("dpk_"):])
        self.assertEqual(body["dataset_ids"], ["secret-ds"])
        self.assertIsNone(body["expires_at"])

        # Only the hash is stored: the plaintext token never hits the DB.
        db_path = os.environ["DATABASE_PATH"]
        conn = connect()
        try:
            row = conn.execute(
                "SELECT key_hash, key_prefix, revoked, expires_at FROM api_keys WHERE key_id = ?",
                (body["key_id"],),
            ).fetchone()
            scopes = conn.execute(
                "SELECT dataset_id FROM api_key_scopes WHERE key_id = ?",
                (body["key_id"],),
            ).fetchall()
            # Force a WAL checkpoint so committed pages are in the main file.
            for _ in range(10):
                busy = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]
                if not busy:
                    break
                import time as _time

                _time.sleep(0.05)
        finally:
            conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(len(row["key_hash"]), 64)  # sha256 hex
        self.assertNotIn(body["api_key"], row["key_hash"])
        self.assertEqual(row["key_prefix"], body["api_key"][:12])
        self.assertEqual(row["revoked"], 0)
        self.assertIsNone(row["expires_at"])
        self.assertEqual([r["dataset_id"] for r in scopes], ["secret-ds"])

        # The plaintext token is not present anywhere in the database bytes.
        for suffix in ("", "-wal", "-shm"):
            sidecar = Path(db_path + suffix)
            if not sidecar.exists():
                continue
            self.assertNotIn(body["api_key"].encode(), sidecar.read_bytes())

    def test_24_scope_enforcement(self) -> None:
        status, body = _create_key(["alpha"], name="alpha only")
        self.assertEqual(status, 201, body)
        key = body["api_key"]

        release = {
            "dataset_id": "beta", "query_id": "q", "epsilon": 0.1,
            "sensitivity": 1.0, "true_value": 1.0, "idempotency_key": "scope-1",
        }
        status, body = _request("POST", "/releases", release, api_key=key)
        self.assertEqual(status, 403, body)
        self.assertEqual(body["detail"]["error"], "dataset_out_of_scope")
        self.assertEqual(body["detail"]["dataset_id"], "beta")

        status, body = _request("GET", "/budgets/beta", api_key=key)
        self.assertEqual(status, 403)

        # Transfer requires BOTH datasets in scope.
        for source, target in (("alpha", "beta"), ("beta", "alpha")):
            status, body = _request(
                "POST", "/budgets/transfer",
                {"transfer_id": f"tx-{source}-{target}",
                 "source_dataset": source, "target_dataset": target,
                 "epsilon": 0.1},
                api_key=key,
            )
            self.assertEqual(status, 403, (source, target, body))

        # In-scope access works.
        release["dataset_id"] = "alpha"
        status, body = _request("POST", "/releases", release, api_key=key)
        self.assertEqual(status, 200, body)
        status, body = _request("GET", "/budgets/alpha", api_key=key)
        self.assertEqual(status, 200)
        self.assertAlmostEqual(body["used_budget"], 0.1, places=9)

        # Transfer within two in-scope datasets works.
        status, body = _create_key(["gamma", "delta"], name="gd")
        key2 = body["api_key"]
        status, body = _request(
            "POST", "/budgets/transfer",
            {"transfer_id": "tx-gd", "source_dataset": "gamma",
             "target_dataset": "delta", "epsilon": 0.2},
            api_key=key2,
        )
        self.assertEqual(status, 200, body)

    def test_25_revocation(self) -> None:
        status, body = _create_key(["rev-ds"], name="to revoke")
        self.assertEqual(status, 201)
        key_id, token = body["key_id"], body["api_key"]

        status, _ = _request("GET", "/budgets/rev-ds", api_key=token)
        self.assertEqual(status, 200)

        # Admin cannot use a DELETE path with a data key.
        status, _ = _request("DELETE", f"/auth/keys/{key_id}", api_key=token)
        self.assertEqual(status, 403)

        status, body = _request("DELETE", f"/auth/keys/{key_id}", api_key=ADMIN_KEY)
        self.assertEqual(status, 200, body)
        self.assertEqual(body, {"key_id": key_id, "revoked": True})

        # Immediately unusable.
        status, _ = _request("GET", "/budgets/rev-ds", api_key=token)
        self.assertEqual(status, 401)

        # Revoking an unknown id is 404; re-revoking stays 200 (idempotent).
        status, _ = _request("DELETE", "/auth/keys/deadbeef" * 4, api_key=ADMIN_KEY)
        self.assertEqual(status, 404)
        status, _ = _request("DELETE", f"/auth/keys/{key_id}", api_key=ADMIN_KEY)
        self.assertEqual(status, 200)

    def test_26_expired_key_is_rejected(self) -> None:
        # Expiry in the past is itself a 422.
        status, _ = _create_key(
            ["exp-ds"], expires_at="2000-01-01T00:00:00Z", name="past"
        )
        self.assertEqual(status, 422)

        # ttl_seconds and expires_at are mutually exclusive.
        status, _ = _create_key(
            ["exp-ds"], ttl_seconds=60, expires_at="2099-01-01T00:00:00Z",
            name="both",
        )
        self.assertEqual(status, 422)

        # A 1-second TTL works now and stops working after expiry.
        status, body = _create_key(["exp-ds"], ttl_seconds=1, name="short")
        self.assertEqual(status, 201, body)
        self.assertIsNotNone(body["expires_at"])
        token = body["api_key"]
        status, _ = _request("GET", "/budgets/exp-ds", api_key=token)
        self.assertEqual(status, 200)

        import time

        time.sleep(1.2)
        status, body = _request("GET", "/budgets/exp-ds", api_key=token)
        self.assertEqual(status, 401)

    def test_27_create_key_validation(self) -> None:
        bad_bodies = [
            {"name": "", "dataset_ids": ["a"]},
            {"name": "n", "dataset_ids": []},
            {"name": "n", "dataset_ids": [""]},
            {"name": "n", "dataset_ids": ["a"], "ttl_seconds": 0},
            {"name": "n", "dataset_ids": ["a"], "extra": True},
        ]
        for payload in bad_bodies:
            status, body = _request("POST", "/auth/keys", payload, api_key=ADMIN_KEY)
            self.assertEqual(status, 422, payload)

    def test_28_concurrent_key_creation_and_revocation(self) -> None:
        """Fan-out minting must all succeed with distinct ids/tokens;
        concurrent revokes of the same id must not error."""
        def mint(i: int) -> tuple[int, dict]:
            return _request(
                "POST", "/auth/keys",
                {"name": f"concurrent {i}", "dataset_ids": [f"cc-{i}"]},
                api_key=ADMIN_KEY,
            )

        with ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(mint, range(24)))
        self.assertTrue(all(code == 201 for code, _ in results), results)
        ids = [body["key_id"] for _, body in results]
        tokens = [body["api_key"] for _, body in results]
        self.assertEqual(len(set(ids)), 24)
        self.assertEqual(len(set(tokens)), 24)

        target_id = ids[0]
        def revoke(_i: int):
            return _request("DELETE", f"/auth/keys/{target_id}", api_key=ADMIN_KEY)[0]

        with ThreadPoolExecutor(max_workers=16) as pool:
            codes = list(pool.map(revoke, range(16)))
        self.assertEqual(set(codes), {200}, codes)

        # Concurrent use of the revoked key is rejected but never 500s.
        token = tokens[0]
        def use(_i: int):
            return _request("GET", "/budgets/cc-0", api_key=token)[0]

        with ThreadPoolExecutor(max_workers=16) as pool:
            codes = list(pool.map(use, range(16)))
        self.assertEqual(set(codes), {401}, codes)

    def test_29_wrong_secret_is_unauthorized(self) -> None:
        status, body = _create_key(["tamper-ds"], name="tamper")
        self.assertEqual(status, 201)
        token = body["api_key"]
        # Flip the last character of the secret while keeping a valid shape.
        last = token[-1]
        flipped = "B" if last == "A" else "A"
        tampered = token[:-1] + flipped
        status, _ = _request("GET", "/budgets/tamper-ds", api_key=tampered)
        self.assertEqual(status, 401)


# --- Admin-managed dataset catalog ---
# (catalog tests run inside ApiTests so they share its class-level server)


def _grant_scope_directly(key_id: str, dataset_id: str) -> None:
    """Add a scope row outside the API, so unknown/draft datasets can be
    reached by a key and reach the catalog gate (rather than the 403 scope
    check)."""
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO api_key_scopes (key_id, dataset_id) VALUES (?, ?)",
            (key_id, dataset_id),
        )
    finally:
        conn.close()


    def test_30_admin_paths_require_admin_token(self) -> None:
        # No key.
        status, _ = _request(
            "POST", "/admin/datasets",
            {"dataset_id": "a30", "name": "n", "default_budget": 1.0},
            api_key="",
        )
        self.assertEqual(status, 401)
        # Wrong token.
        status, _ = _request("GET", "/admin/datasets", api_key="wrong-admin")
        self.assertEqual(status, 401)
        # A valid data-plane key is never an administrator.
        status, _ = _request("GET", "/admin/datasets", api_key=DATA_KEY)
        self.assertEqual(status, 403)
        status, _ = _request(
            "PATCH", "/admin/datasets/ds/lifecycle",
            {"version": 1, "action": "archive"}, api_key=DATA_KEY,
        )
        self.assertEqual(status, 403)

    def test_31_create_dataset_starts_draft(self) -> None:
        status, body = _create_dataset("cat31", default_budget=0.75)
        self.assertEqual(status, 201, body)
        self.assertEqual(
            set(body),
            {"dataset_id", "name", "status", "version", "default_budget",
             "created_at", "updated_at"},
        )
        self.assertEqual(body["dataset_id"], "cat31")
        self.assertEqual(body["status"], "draft")
        self.assertEqual(body["version"], 0)
        self.assertEqual(body["default_budget"], 0.75)
        self.assertTrue(body["created_at"] and body["updated_at"])

        # Duplicate dataset_id is 409.
        status, body = _create_dataset("cat31", default_budget=2.0)
        self.assertEqual(status, 409)
        self.assertEqual(body["detail"]["error"], "dataset_already_exists")
        self.assertEqual(body["detail"]["dataset_id"], "cat31")

    def test_32_create_dataset_validation(self) -> None:
        bad_bodies = [
            {"dataset_id": "", "name": "n", "default_budget": 1.0},
            {"dataset_id": "ok-id", "name": "", "default_budget": 1.0},
            {"dataset_id": "ok-id2", "name": "n", "default_budget": -0.01},
            {"dataset_id": "ok-id3", "name": "n", "default_budget": float("nan")},
            {"dataset_id": "ok-id4", "name": "n", "default_budget": 1.0,
             "extra": 1},
        ]
        for payload in bad_bodies:
            status, _ = _request(
                "POST", "/admin/datasets", payload, api_key=ADMIN_KEY
            )
            self.assertEqual(status, 422, payload)

        # Zero default budget is legal.
        status, body = _create_dataset("cat32-zero", default_budget=0.0)
        self.assertEqual(status, 201, body)
        self.assertEqual(body["default_budget"], 0.0)

    def test_33_list_datasets_and_status_filter(self) -> None:
        for ds in ("cat33-a", "cat33-b", "cat33-c"):
            status, _ = _create_dataset(ds)
            self.assertEqual(status, 201)
        status, _ = _activate_dataset("cat33-b")
        self.assertEqual(status, 200)

        status, body = _request("GET", "/admin/datasets", api_key=ADMIN_KEY)
        self.assertEqual(status, 200)
        by_id = {d["dataset_id"]: d for d in body["datasets"]}
        # Pre-registered legacy datasets plus the new ones are all listed,
        # ordered by dataset_id.
        ids = [d["dataset_id"] for d in body["datasets"]]
        self.assertEqual(ids, sorted(ids))
        self.assertLessEqual(
            {"cat33-a", "cat33-b", "cat33-c", "ds"}.difference(by_id), set()
        )
        self.assertEqual(by_id["cat33-b"]["status"], "active")
        self.assertEqual(by_id["cat33-a"]["status"], "draft")

        status, body = _request(
            "GET", "/admin/datasets?status=draft", api_key=ADMIN_KEY
        )
        self.assertEqual(status, 200)
        listed = {d["dataset_id"] for d in body["datasets"]}
        self.assertIn("cat33-a", listed)
        self.assertNotIn("cat33-b", listed)

        status, body = _request(
            "GET", "/admin/datasets?status=archived", api_key=ADMIN_KEY
        )
        self.assertEqual(status, 200)
        self.assertNotIn("cat33-a", {d["dataset_id"] for d in body["datasets"]})

        status, body = _request(
            "GET", "/admin/datasets?status=bogus", api_key=ADMIN_KEY
        )
        self.assertEqual(status, 422)
        locs = [tuple(err["loc"]) for err in body["detail"]]
        self.assertIn(("query", "status"), locs)

    def test_34_lifecycle_draft_active_archived(self) -> None:
        status, _ = _create_dataset("cat34")
        self.assertEqual(status, 201)

        # draft -> activate
        status, body = _activate_dataset("cat34", version=0)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["status"], "active")
        self.assertEqual(body["version"], 1)
        self.assertNotEqual(body["updated_at"], body["created_at"])

        # active -> archive
        status, body = _archive_dataset("cat34", version=1)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["status"], "archived")
        self.assertEqual(body["version"], 2)

    def test_35_illegal_transitions_are_409(self) -> None:
        status, _ = _create_dataset("cat35-a")
        self.assertEqual(status, 201)
        status, _ = _create_dataset("cat35-b")
        self.assertEqual(status, 201)
        status, _ = _activate_dataset("cat35-b")
        self.assertEqual(status, 200)

        # A draft cannot be archived directly.
        status, body = _archive_dataset("cat35-a", version=0)
        self.assertEqual(status, 409, body)
        self.assertEqual(body["detail"]["error"], "invalid_lifecycle_transition")
        self.assertEqual(body["detail"]["status"], "draft")

        # Activate the first, archive it, then both directions are illegal.
        status, _ = _activate_dataset("cat35-a")
        self.assertEqual(status, 200)
        status, _ = _archive_dataset("cat35-a", version=1)
        self.assertEqual(status, 200)
        status, body = _activate_dataset("cat35-a", version=2)
        self.assertEqual(status, 409)
        self.assertEqual(body["detail"]["error"], "invalid_lifecycle_transition")
        status, body = _archive_dataset("cat35-a", version=2)
        self.assertEqual(status, 409)
        self.assertEqual(body["detail"]["status"], "archived")

        # An active dataset cannot be activated twice.
        status, body = _activate_dataset("cat35-b", version=1)
        self.assertEqual(status, 409)

        # Unknown dataset on PATCH is 404.
        status, body = _activate_dataset("cat35-missing")
        self.assertEqual(status, 404)
        self.assertEqual(body["detail"]["error"], "dataset_not_found")

    def test_36_stale_version_is_409(self) -> None:
        status, _ = _create_dataset("cat36")
        self.assertEqual(status, 201)

        # Sending the same (draft, v0, activate) twice: the first advances to
        # v1 active, the second is a stale/duplicate version -> 409.
        status, first = _activate_dataset("cat36", version=0)
        self.assertEqual(status, 200)
        status, body = _activate_dataset("cat36", version=0)
        self.assertEqual(status, 409)
        self.assertEqual(body["detail"]["error"], "version_conflict")
        self.assertEqual(body["detail"]["current_version"], 1)
        self.assertEqual(body["detail"]["provided_version"], 0)
        self.assertEqual(first["version"], 1)

        # Future versions also conflict.
        status, body = _archive_dataset("cat36", version=99)
        self.assertEqual(status, 409)
        self.assertEqual(body["detail"]["error"], "version_conflict")

    def test_37_concurrent_activate_one_wins(self) -> None:
        status, _ = _create_dataset("cat37")
        self.assertEqual(status, 201)

        def call(_i: int):
            return _activate_dataset("cat37", version=0)[0]

        with ThreadPoolExecutor(max_workers=16) as pool:
            codes = list(pool.map(call, range(16)))
        self.assertEqual(codes.count(200), 1, codes)
        self.assertEqual(codes.count(409), 15, codes)

        status, body = _request("GET", "/admin/datasets?status=active",
                                api_key=ADMIN_KEY)
        by_id = {d["dataset_id"]: d for d in body["datasets"]}
        self.assertEqual(by_id["cat37"]["version"], 1)

    def test_38_new_grants_require_active(self) -> None:
        # Draft datasets cannot be granted to new keys.
        status, _ = _create_dataset("cat38-draft")
        self.assertEqual(status, 201)
        status, body = _create_key(["cat38-draft"], name="grant draft")
        self.assertEqual(status, 409, body)
        self.assertEqual(body["detail"]["error"], "dataset_not_active")
        self.assertEqual(body["detail"]["status"], "draft")

        # Unknown datasets cannot be granted either.
        status, body = _create_key(["cat38-missing"], name="grant missing")
        self.assertEqual(status, 404, body)
        self.assertEqual(body["detail"]["error"], "dataset_not_found")

        # Activated: the grant succeeds.
        status, _ = _activate_dataset("cat38-draft")
        self.assertEqual(status, 200)
        status, body = _create_key(["cat38-draft"], name="grant active")
        self.assertEqual(status, 201, body)

        # Archived: new grants are refused.
        status, _ = _archive_dataset("cat38-draft", version=1)
        self.assertEqual(status, 200)
        status, body = _create_key(["cat38-draft"], name="grant archived")
        self.assertEqual(status, 409)
        self.assertEqual(body["detail"]["status"], "archived")

    def test_39_release_requires_active_unknown_is_404(self) -> None:
        # Draft: an in-scope key gets 409 from the catalog gate.
        status, _ = _create_dataset("cat39-draft")
        self.assertEqual(status, 201)
        _, key_body = _create_key(["ds"], name="cat39 holder")
        _grant_scope_directly(key_body["key_id"], "cat39-draft")
        status, body = _request(
            "POST", "/releases",
            {"dataset_id": "cat39-draft", "query_id": "q", "epsilon": 0.1,
             "sensitivity": 1.0, "true_value": 1.0, "idempotency_key": "c39d"},
            api_key=key_body["api_key"],
        )
        self.assertEqual(status, 409, body)
        self.assertEqual(body["detail"]["error"], "dataset_not_active")

        # Unknown dataset that is nonetheless in scope: 404 (not 402 seeded
        # from a default, and not 403 scope).
        _grant_scope_directly(key_body["key_id"], "cat39-ghost")
        status, body = _request(
            "POST", "/releases",
            {"dataset_id": "cat39-ghost", "query_id": "q", "epsilon": 0.1,
             "sensitivity": 1.0, "true_value": 1.0, "idempotency_key": "c39g"},
            api_key=key_body["api_key"],
        )
        self.assertEqual(status, 404, body)
        self.assertEqual(body["detail"]["error"], "dataset_not_found")

    def test_40_archived_blocks_writes_but_keeps_history(self) -> None:
        status, _ = _create_dataset("cat40", default_budget=1.0)
        self.assertEqual(status, 201)
        status, _ = _activate_dataset("cat40")
        self.assertEqual(status, 200)
        status, key_body = _create_key(["cat40"], name="cat40 key")
        self.assertEqual(status, 201)
        key = key_body["api_key"]

        release = {
            "dataset_id": "cat40", "query_id": "q", "epsilon": 0.3,
            "sensitivity": 1.0, "true_value": 10.0, "idempotency_key": "c40-1",
        }
        status, first = _request("POST", "/releases", release, api_key=key)
        self.assertEqual(status, 200, first)
        status, budget_before = _request("GET", "/budgets/cat40", api_key=key)
        self.assertEqual(status, 200)

        # Archive.
        status, body = _archive_dataset("cat40", version=1)
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "archived")

        # A brand-new release is refused.
        status, body = _request(
            "POST", "/releases",
            {**release, "idempotency_key": "c40-2"}, api_key=key,
        )
        self.assertEqual(status, 409, body)
        self.assertEqual(body["detail"]["error"], "dataset_not_active")
        self.assertEqual(body["detail"]["status"], "archived")

        # Historical queries survive: budget view and the idempotent replay.
        status, budget_after = _request("GET", "/budgets/cat40", api_key=key)
        self.assertEqual(status, 200)
        self.assertEqual(budget_after, budget_before)
        status, replay = _request("POST", "/releases", release, api_key=key)
        self.assertEqual(status, 200)
        self.assertEqual(replay, first)

    def test_41_transfer_requires_active_both_sides(self) -> None:
        # active source, archived target -> 409 on target.
        for ds in ("cat41-a", "cat41-b", "cat41-c"):
            status, _ = _create_dataset(ds)
            self.assertEqual(status, 201)
            status, _ = _activate_dataset(ds)
            self.assertEqual(status, 200)
        status, key_body = _create_key(
            ["cat41-a", "cat41-b", "cat41-c"], name="cat41 key"
        )
        self.assertEqual(status, 201)
        key = key_body["api_key"]

        status, _ = _archive_dataset("cat41-b", version=1)
        self.assertEqual(status, 200)

        status, body = _request(
            "POST", "/budgets/transfer",
            {"transfer_id": "c41-1", "source_dataset": "cat41-a",
             "target_dataset": "cat41-b", "epsilon": 0.1},
            api_key=key,
        )
        self.assertEqual(status, 409, body)
        self.assertEqual(body["detail"]["error"], "dataset_not_active")
        self.assertEqual(body["detail"]["dataset_id"], "cat41-b")

        # Archived source is also refused.
        status, _ = _archive_dataset("cat41-a", version=1)
        self.assertEqual(status, 200)
        status, body = _request(
            "POST", "/budgets/transfer",
            {"transfer_id": "c41-2", "source_dataset": "cat41-a",
             "target_dataset": "cat41-c", "epsilon": 0.1},
            api_key=key,
        )
        self.assertEqual(status, 409)
        self.assertEqual(body["detail"]["dataset_id"], "cat41-a")

        # Unknown side is 404.
        _grant_scope_directly(key_body["key_id"], "cat41-ghost")
        status, body = _request(
            "POST", "/budgets/transfer",
            {"transfer_id": "c41-3", "source_dataset": "cat41-c",
             "target_dataset": "cat41-ghost", "epsilon": 0.1},
            api_key=key,
        )
        self.assertEqual(status, 404)
        self.assertEqual(body["detail"]["dataset_id"], "cat41-ghost")

        # Nothing moved.
        status, budget = _request("GET", "/budgets/cat41-c", api_key=key)
        self.assertEqual(status, 200)
        self.assertEqual(budget["used_budget"], 0.0)
        self.assertEqual(budget["remaining_budget"], 1.0)


class MigrationTests(unittest.TestCase):
    """Old schemas must upgrade in place (PRAGMA user_version)."""

    def test_existing_v1_database_is_upgraded(self) -> None:
        tmp = Path(_TMP) / "migration"
        tmp.mkdir(parents=True, exist_ok=True)
        db_path = tmp / "old.db"

        # Build a database exactly like a pre-auth deployment (version 0,
        # v1 tables, plus a live budget row).
        from app.db import MIGRATIONS

        conn = connect(str(db_path))
        try:
            conn.executescript(MIGRATIONS[0].replace("PRAGMA user_version = 1;", ""))
            conn.execute("PRAGMA user_version = 0")
            conn.execute(
                "INSERT INTO budgets (dataset_id, total, used) VALUES ('old-ds', 0.9, 0.1)"
            )
            conn.execute(
                "INSERT INTO releases (id, idempotency_key, dataset_id, query_id, "
                "epsilon, sensitivity, true_value, noise, released_value, "
                "budget_used, budget_remaining) VALUES "
                "('r1', 'ik1', 'rel-ds', 'q', 0.1, 1.0, 1.0, 0.0, 1.0, 0.1, 0.9)"
            )
        finally:
            conn.close()

        init_db(str(db_path))

        conn = connect(str(db_path))
        try:
            self.assertEqual(int(conn.execute("PRAGMA user_version").fetchone()[0]), 3)
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            self.assertIn("api_keys", tables)
            self.assertIn("api_key_scopes", tables)
            self.assertIn("datasets", tables)
            # Existing data survives the migration.
            row = conn.execute(
                "SELECT total, used FROM budgets WHERE dataset_id = 'old-ds'"
            ).fetchone()
            self.assertEqual((row["total"], row["used"]), (0.9, 0.1))

            # v3 backfills every referenced dataset as active, adopting the
            # existing budget total as its default.
            catalog = {
                r["dataset_id"]: r
                for r in conn.execute(
                    "SELECT dataset_id, name, status, version, default_budget "
                    "FROM datasets"
                ).fetchall()
            }
            self.assertIn("old-ds", catalog)
            self.assertEqual(catalog["old-ds"]["status"], "active")
            self.assertEqual(catalog["old-ds"]["version"], 0)
            self.assertEqual(catalog["old-ds"]["default_budget"], 0.9)
            self.assertIn("rel-ds", catalog)
            self.assertEqual(catalog["rel-ds"]["default_budget"], 1.0)
        finally:
            conn.close()

    def test_v2_database_with_key_scopes_is_backfilled(self) -> None:
        tmp = Path(_TMP) / "migration"
        db_path = tmp / "v2.db"

        from app.db import MIGRATIONS

        conn = connect(str(db_path))
        try:
            conn.executescript(MIGRATIONS[0])
            conn.executescript(MIGRATIONS[1])
            # A key scoped to a dataset that has no budget row yet.
            conn.execute(
                "INSERT INTO api_keys (key_id, name, key_hash, key_prefix, "
                "expires_at, revoked) VALUES ('kid', 'n', 'h', 'dpk_abcdef', NULL, 0)"
            )
            conn.execute(
                "INSERT INTO api_key_scopes (key_id, dataset_id) "
                "VALUES ('kid', 'scope-only-ds')"
            )
            self.assertEqual(int(conn.execute("PRAGMA user_version").fetchone()[0]), 2)
        finally:
            conn.close()

        init_db(str(db_path))

        conn = connect(str(db_path))
        try:
            self.assertEqual(int(conn.execute("PRAGMA user_version").fetchone()[0]), 3)
            row = conn.execute(
                "SELECT status, default_budget FROM datasets WHERE dataset_id = ?",
                ("scope-only-ds",),
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["status"], "active")
            # No budget row existed: default comes from the configured total.
            self.assertEqual(row["default_budget"], 1.0)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
