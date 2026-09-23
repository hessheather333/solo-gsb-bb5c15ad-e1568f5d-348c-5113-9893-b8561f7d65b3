"""Pydantic request/response models."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Literal, Optional
from typing_extensions import Annotated

from pydantic import (
    BaseModel,
    Field,
    StringConstraints,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticCustomError

_ID_PATTERN = re.compile(r"^[\w.:@\- ]+$")

Identifier = Annotated[
    str,
    StringConstraints(min_length=1, max_length=128, strip_whitespace=True, pattern=_ID_PATTERN),
]
IdempotencyKey = Annotated[
    str,
    StringConstraints(min_length=1, max_length=128, strip_whitespace=True, pattern=_ID_PATTERN),
]
FiniteFloat = Annotated[float, Field(allow_inf_nan=False)]


class ReleaseRequest(BaseModel):
    dataset_id: Identifier = Field(..., description="Dataset whose privacy budget is consumed")
    query_id: Identifier = Field(..., description="Stable identifier of the query/mechanism")
    epsilon: FiniteFloat = Field(..., gt=0, description="Privacy loss charged for this release")
    sensitivity: FiniteFloat = Field(..., gt=0, description="L1 sensitivity of true_value")
    true_value: FiniteFloat = Field(..., description="Real aggregate before noise is added")
    idempotency_key: IdempotencyKey = Field(
        ..., description="Client-generated key; retries with the same key replay the release"
    )

    model_config = {"extra": "forbid"}


class ReleaseResponse(BaseModel):
    id: str
    idempotency_key: str
    dataset_id: str
    query_id: str
    epsilon: float
    sensitivity: float
    released_value: float
    noise: float
    budget_used: float
    budget_remaining: float


class BudgetResponse(BaseModel):
    dataset_id: str
    used_budget: float
    remaining_budget: float


class TransferRequest(BaseModel):
    transfer_id: IdempotencyKey = Field(
        ..., description="Client-generated key; retries replay the same transfer"
    )
    source_dataset: Identifier = Field(..., description="Dataset whose unused quota is moved out")
    target_dataset: Identifier = Field(..., description="Dataset receiving the quota")
    epsilon: FiniteFloat = Field(..., gt=0, description="Unused epsilon quota moved between datasets")

    @field_validator("target_dataset")
    @classmethod
    def _datasets_must_differ(cls, value: str, info: ValidationInfo) -> str:
        source = info.data.get("source_dataset")
        if source is not None and value == source:
            raise PydanticCustomError(
                "same_source_and_target",
                "target_dataset must differ from source_dataset",
            )
        return value

    model_config = {"extra": "forbid"}


class TransferResponse(BaseModel):
    transfer_id: str
    source_dataset: str
    target_dataset: str
    epsilon: float
    source: BudgetResponse
    target: BudgetResponse


class HealthResponse(BaseModel):
    status: str


# --- API keys ---

KeyName = Annotated[
    str,
    StringConstraints(min_length=1, max_length=200, strip_whitespace=True),
]


class CreateApiKeyRequest(BaseModel):
    name: KeyName = Field(..., description="Human-readable label for the key")
    dataset_ids: list[Identifier] = Field(
        ...,
        min_length=1,
        max_length=100,
        description="Datasets this key may access; duplicates are ignored",
    )
    # Provide exactly one of ttl_seconds / expires_at; neither = never expires.
    ttl_seconds: Optional[int] = Field(
        default=None, ge=1, description="Key lifetime from creation, in seconds"
    )
    expires_at: Optional[datetime] = Field(
        default=None,
        description="UTC ISO-8601 expiry (a naive value is interpreted as UTC)",
    )

    @field_validator("dataset_ids")
    @classmethod
    def _dedupe_datasets(cls, value: list[str]) -> list[str]:
        seen: set[str] = set()
        unique: list[str] = []
        for dataset_id in value:
            if dataset_id not in seen:
                seen.add(dataset_id)
                unique.append(dataset_id)
        return unique

    @model_validator(mode="after")
    def _expiry_options(self) -> "CreateApiKeyRequest":
        if self.ttl_seconds is not None and self.expires_at is not None:
            raise PydanticCustomError(
                "mutually_exclusive_expiry",
                "provide at most one of ttl_seconds and expires_at",
            )
        if self.expires_at is not None and self.expires_at.tzinfo is None:
            # Naive timestamps are interpreted as UTC rather than rejected,
            # which is friendlier for curl-driven callers.
            self.expires_at = self.expires_at.replace(tzinfo=timezone.utc)
        if self.expires_at is not None and self.expires_at <= datetime.now(timezone.utc):
            raise PydanticCustomError(
                "expires_at_in_past",
                "expires_at must be in the future",
            )
        return self

    model_config = {"extra": "forbid"}


class ApiKeyResponse(BaseModel):
    key_id: str
    name: str
    # The plaintext token. It is returned ONLY here, at creation time.
    api_key: str = Field(..., description="Keep it safe: it is shown only once")
    dataset_ids: list[str]
    expires_at: Optional[datetime] = None
    created_at: str


class ApiKeyRevokedResponse(BaseModel):
    key_id: str
    revoked: bool


# --- Admin-managed dataset catalog ---

DatasetStatus = Literal["draft", "active", "archived"]
LifecycleAction = Literal["activate", "archive"]

# Zero is a legal default budget (a dataset can exist with no spendable
# quota), but NaN/Infinity and negatives are rejected by allow_inf_nan/ge.
DefaultBudget = Annotated[float, Field(allow_inf_nan=False, ge=0)]
DatasetVersion = Annotated[int, Field(ge=0)]
DatasetName = Annotated[
    str,
    StringConstraints(min_length=1, max_length=200, strip_whitespace=True),
]


class CreateDatasetRequest(BaseModel):
    dataset_id: Identifier = Field(
        ..., description="Stable identifier; the dataset is created in 'draft'"
    )
    name: DatasetName = Field(..., description="Human-readable label")
    default_budget: DefaultBudget = Field(
        ..., description="Total epsilon seeded when the budget row is first used"
    )

    model_config = {"extra": "forbid"}


class DatasetResponse(BaseModel):
    dataset_id: str
    name: str
    status: DatasetStatus
    version: int
    default_budget: float
    created_at: str
    updated_at: str


class DatasetListResponse(BaseModel):
    datasets: list[DatasetResponse]


class LifecycleRequest(BaseModel):
    # Optimistic-locking token: must equal the row's current version. The
    # version is incremented on every successful transition.
    version: DatasetVersion
    action: LifecycleAction = Field(
        ...,
        description="'activate': draft -> active; 'archive': active -> archived",
    )

    model_config = {"extra": "forbid"}

