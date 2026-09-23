"""Application configuration loaded from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _parse_dataset_budgets(raw: str) -> dict[str, float]:
    """Parse ``dataset1:0.5,dataset2:2.0`` into a mapping.

    Per-dataset budgets override the global default budget.
    """
    budgets: dict[str, float] = {}
    raw = raw.strip()
    if not raw:
        return budgets
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if ":" not in chunk:
            raise ValueError(
                f"DP_DATASET_BUDGETS entry {chunk!r} must look like 'dataset:epsilon'"
            )
        key, _, value = chunk.partition(":")
        key = key.strip()
        value = value.strip()
        if not key:
            raise ValueError(f"DP_DATASET_BUDGETS entry {chunk!r} has an empty dataset id")
        try:
            amount = float(value)
        except ValueError as exc:
            raise ValueError(
                f"DP_DATASET_BUDGETS budget for {key!r} is not a number: {value!r}"
            ) from exc
        if amount != amount or amount in (float("inf"), float("-inf")) or amount < 0:
            raise ValueError(f"DP_DATASET_BUDGETS budget for {key!r} must be a finite >= 0 number")
        budgets[key] = amount
    return budgets


@dataclass(frozen=True)
class Settings:
    # HMAC key used to derive reproducible Laplace noise.
    server_key: str
    # Default total epsilon budget applied to every dataset without an override.
    total_budget: float
    # Optional dataset-specific total budgets.
    dataset_budgets: dict[str, float] = field(default_factory=dict)
    # SQLite database file location.
    database_path: str = "/data/dp.db"
    # Admin token guarding /auth/keys. Empty => generate and persist one.
    admin_api_key: str = ""
    host: str = "0.0.0.0"
    port: int = 8000

    def budget_for(self, dataset_id: str) -> float:
        return self.dataset_budgets.get(dataset_id, self.total_budget)


def _load_settings() -> Settings:
    total_budget = float(os.getenv("DP_TOTAL_BUDGET", "1.0"))
    if total_budget != total_budget or total_budget in (float("inf"), float("-inf")) or total_budget < 0:
        raise ValueError("DP_TOTAL_BUDGET must be a finite number >= 0")

    dataset_budgets = _parse_dataset_budgets(os.getenv("DP_DATASET_BUDGETS", ""))

    server_key = os.getenv("DP_SERVER_KEY", "")
    database_path = os.getenv("DATABASE_PATH", "/data/dp.db")
    admin_api_key = os.getenv("DP_ADMIN_API_KEY", "").strip()

    return Settings(
        server_key=server_key,
        total_budget=total_budget,
        dataset_budgets=dataset_budgets,
        database_path=database_path,
        admin_api_key=admin_api_key,
        host=os.getenv("APP_HOST", "0.0.0.0"),
        port=int(os.getenv("APP_PORT", "8000")),
    )


settings = _load_settings()
