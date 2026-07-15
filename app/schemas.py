"""Pydantic request/response models — this is our API contract + validation."""
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Annotated, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer

T = TypeVar("T")

# Money: parsed/stored as Decimal for exactness, serialized to a JSON number so
# API responses stay clean. when_used="json" keeps Decimal in python-mode
# model_dump() (so internal round-trips stay exact) and only floats for output.
Money = Annotated[
    Decimal,
    PlainSerializer(lambda v: float(v), return_type=float, when_used="json"),
]


class EventType(str, Enum):
    payment_initiated = "payment_initiated"
    payment_processed = "payment_processed"
    payment_failed = "payment_failed"
    settled = "settled"


class TransactionStatus(str, Enum):
    INITIATED = "INITIATED"
    PROCESSED = "PROCESSED"
    FAILED = "FAILED"
    SETTLED = "SETTLED"


class ReconciliationStatus(str, Enum):
    RECONCILED = "RECONCILED"
    DISCREPANCY = "DISCREPANCY"
    PENDING = "PENDING"


# --------------------------------------------------------------------------- #
# Ingestion
# --------------------------------------------------------------------------- #
class EventIn(BaseModel):
    """Incoming event. `event_id` is optional — if a client omits it we cannot
    dedupe, so we require it here to make idempotency guarantees meaningful."""

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(..., min_length=1, description="Globally unique event id (idempotency key)")
    event_type: EventType
    transaction_id: str = Field(..., min_length=1)
    merchant_id: str = Field(..., min_length=1)
    merchant_name: str | None = None
    amount: Money = Field(..., ge=0)
    currency: str = Field(default="INR", min_length=3, max_length=3)
    timestamp: datetime


class IngestResult(BaseModel):
    event_id: str
    status: str = Field(description="'created' or 'duplicate'")
    transaction_id: str
    transaction_status: TransactionStatus | None = None


# --------------------------------------------------------------------------- #
# Transactions
# --------------------------------------------------------------------------- #
class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    event_id: str
    event_type: str
    amount: Money
    currency: str
    event_timestamp: datetime
    ingested_at: datetime


class MerchantOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    merchant_id: str
    merchant_name: str | None = None


class TransactionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    transaction_id: str
    merchant_id: str
    amount: Money | None = None
    currency: str
    status: TransactionStatus
    reconciliation_status: ReconciliationStatus
    has_initiated: bool
    has_processed: bool
    has_failed: bool
    has_settled: bool
    initiated_at: datetime | None = None
    processed_at: datetime | None = None
    failed_at: datetime | None = None
    settled_at: datetime | None = None
    first_seen_at: datetime | None = None
    last_event_at: datetime | None = None
    event_count: int
    duplicate_event_count: int


class TransactionDetail(TransactionOut):
    merchant: MerchantOut | None = None
    events: list[EventOut] = []
    # True when the history was capped by event_limit (more events exist than
    # were returned). event_count always reflects the true total.
    events_truncated: bool = False


class PaginationMeta(BaseModel):
    total: int
    limit: int
    offset: int
    returned: int


class Page(BaseModel, Generic[T]):
    pagination: PaginationMeta
    items: list[T]


# --------------------------------------------------------------------------- #
# Reconciliation
# --------------------------------------------------------------------------- #
class SummaryRow(BaseModel):
    group: dict[str, str | None]
    transaction_count: int
    total_amount: Money
    settled_count: int
    failed_count: int
    processed_count: int
    initiated_count: int


class SummaryResponse(BaseModel):
    group_by: list[str]
    rows: list[SummaryRow]


class DiscrepancyType(str, Enum):
    PROCESSED_NOT_SETTLED = "PROCESSED_NOT_SETTLED"
    SETTLED_FOR_FAILED_PAYMENT = "SETTLED_FOR_FAILED_PAYMENT"
    CONFLICTING_STATE = "CONFLICTING_STATE"
    STUCK_PENDING = "STUCK_PENDING"
    DUPLICATE_SUBMISSIONS = "DUPLICATE_SUBMISSIONS"


class DiscrepancyItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    transaction_id: str
    merchant_id: str
    amount: Money | None = None
    status: str
    discrepancy_type: DiscrepancyType
    detail: str
    last_event_at: datetime | None = None
    duplicate_event_count: int = 0


class DiscrepancyResponse(BaseModel):
    # counts_by_type / items are per (transaction x discrepancy_type): one
    # transaction can be flagged under multiple types and will appear once per
    # type. `distinct_transactions` is the de-duplicated transaction count, and
    # `pagination.total` counts issue-rows (the sum of counts_by_type).
    counts_by_type: dict[str, int]
    distinct_transactions: int
    pagination: PaginationMeta
    items: list[DiscrepancyItem]
