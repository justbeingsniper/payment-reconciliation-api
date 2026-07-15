"""SQLAlchemy models.

Design: an append-only `events` log (source of truth) + a materialized
`transactions` projection kept up to date on every ingest. See README
"Architecture" for the reasoning behind this split.

Money note: amounts use SQL NUMERIC(18, 2), not FLOAT. Binary floats cannot
represent decimal currency exactly (0.1 + 0.2 != 0.3), which is unacceptable for
a payments service — NUMERIC gives exact storage and exact SQL SUM aggregation.
On Postgres this is exact; SQLite has no native decimal type so it approximates,
but production runs on Postgres.
"""
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.types import UTCDateTime


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Merchant(Base):
    __tablename__ = "merchants"

    merchant_id: Mapped[str] = mapped_column(String, primary_key=True)
    merchant_name: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=_utcnow, server_default=func.now()
    )

    transactions: Mapped[list["Transaction"]] = relationship(back_populates="merchant")


class Event(Base):
    """Immutable, append-only. `event_id` PK is what makes ingestion idempotent:
    a resubmitted event collides on the PK and is dropped by the DB."""

    __tablename__ = "events"

    event_id: Mapped[str] = mapped_column(String, primary_key=True)
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    # NB: deliberately NOT a FK to transactions. The event log is the source of
    # truth and the transactions row is *derived* from events, so events must not
    # depend on the projection existing first (that dependency is backwards and
    # breaks ingestion under enforced FKs, e.g. Postgres).
    transaction_id: Mapped[str] = mapped_column(String, nullable=False)
    merchant_id: Mapped[str] = mapped_column(
        String, ForeignKey("merchants.merchant_id"), nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String, nullable=False, default="INR")
    # When the event actually happened (from the payload).
    event_timestamp: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False
    )
    # When *we* recorded it.
    ingested_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=_utcnow, server_default=func.now()
    )

    __table_args__ = (
        # History lookups for a single transaction (GET /transactions/{id}).
        Index("ix_events_txn_time", "transaction_id", "event_timestamp"),
        Index("ix_events_type", "event_type"),
    )


class Transaction(Base):
    """Materialized current state of a transaction, updated incrementally.

    We track the payment and settlement dimensions independently via boolean
    flags. This keeps updates order-independent and makes discrepancy detection
    a set of trivial, indexable SQL predicates."""

    __tablename__ = "transactions"

    transaction_id: Mapped[str] = mapped_column(String, primary_key=True)
    merchant_id: Mapped[str] = mapped_column(
        String, ForeignKey("merchants.merchant_id"), nullable=False
    )
    amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    currency: Mapped[str] = mapped_column(String, default="INR")

    # Derived lifecycle status (precedence: SETTLED > FAILED > PROCESSED >
    # INITIATED). Stored + indexed so GET /transactions?status=... is a single
    # index scan.
    status: Mapped[str] = mapped_column(String, nullable=False, default="INITIATED")

    # Structural reconciliation state, materialized on ingest:
    #   RECONCILED  - clean terminal state (settled success, or clean failure)
    #   DISCREPANCY - payment/settlement states are inconsistent
    #   PENDING     - still in flight (e.g. initiated, not yet resolved)
    # Time-based staleness (STUCK_PENDING) is layered on at read time in the
    # discrepancies endpoint, since "stale" depends on the current clock.
    reconciliation_status: Mapped[str] = mapped_column(
        String, nullable=False, default="PENDING"
    )

    # Independent state dimensions (the reconciliation primitives).
    has_initiated: Mapped[bool] = mapped_column(Boolean, default=False)
    has_processed: Mapped[bool] = mapped_column(Boolean, default=False)
    has_failed: Mapped[bool] = mapped_column(Boolean, default=False)
    has_settled: Mapped[bool] = mapped_column(Boolean, default=False)

    # First timestamp we saw for each milestone.
    initiated_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    processed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    failed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    settled_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    first_seen_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_event_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    # Total events applied, and how many duplicate submissions we rejected.
    event_count: Mapped[int] = mapped_column(Integer, default=0)
    duplicate_event_count: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=_utcnow, onupdate=_utcnow
    )

    merchant: Mapped["Merchant"] = relationship(back_populates="transactions")
    # No DB-level FK links events -> transactions (see Event.transaction_id), so
    # we spell out the join explicitly. viewonly: events are written directly by
    # the ingestion path, never through this relationship.
    events: Mapped[list["Event"]] = relationship(
        "Event",
        primaryjoin="Transaction.transaction_id == foreign(Event.transaction_id)",
        order_by="Event.event_timestamp",
        viewonly=True,
    )

    __table_args__ = (
        Index("ix_txn_merchant", "merchant_id"),
        Index("ix_txn_status", "status"),
        Index("ix_txn_recon_status", "reconciliation_status"),
        Index("ix_txn_last_event", "last_event_at"),
        # Composite for the common "this merchant, this status" filter.
        Index("ix_txn_merchant_status", "merchant_id", "status"),
        # Partial-ish indexes to accelerate discrepancy scans.
        Index("ix_txn_recon_flags", "has_processed", "has_settled", "has_failed"),
    )
