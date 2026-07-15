"""Write-side logic: idempotent event ingestion + transaction projection."""
from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.database import engine
from app.models import Event, Merchant, Transaction
from app.schemas import EventIn


def _insert_ignore(model):
    """Return a dialect-appropriate `INSERT ... ON CONFLICT DO NOTHING`.

    This is the heart of idempotency: it pushes the dedupe decision down to the
    database's unique constraint instead of doing a read-then-write race in
    Python."""
    if engine.dialect.name == "postgresql":
        return pg_insert(model)
    return sqlite_insert(model)


def derive_status(t: Transaction) -> str:
    """Collapse the independent flags into one filterable lifecycle status.

    Precedence SETTLED > FAILED > PROCESSED > INITIATED. A transaction that is
    both settled and failed is still reported SETTLED here, but is separately
    surfaced as a discrepancy (see queries.discrepancies)."""
    if t.has_settled:
        return "SETTLED"
    if t.has_failed:
        return "FAILED"
    if t.has_processed:
        return "PROCESSED"
    return "INITIATED"


def derive_reconciliation_status(t: Transaction) -> str:
    """Structural reconciliation state from the flags (clock-independent).

    DISCREPANCY beats everything: an inconsistent payment/settlement state is
    always a discrepancy regardless of what else is true."""
    processed_not_settled = t.has_processed and not t.has_settled and not t.has_failed
    settled_bad = t.has_settled and (t.has_failed or not t.has_processed)
    conflicting = t.has_failed and t.has_processed  # can't both succeed and fail
    if processed_not_settled or settled_bad or conflicting:
        return "DISCREPANCY"

    clean_success = (
        t.has_settled and t.has_processed and not t.has_failed
    )
    clean_failure = (
        t.has_failed and not t.has_processed and not t.has_settled
    )
    if clean_success or clean_failure:
        return "RECONCILED"

    return "PENDING"


_TYPE_TO_FLAG = {
    "payment_initiated": ("has_initiated", "initiated_at"),
    "payment_processed": ("has_processed", "processed_at"),
    "payment_failed": ("has_failed", "failed_at"),
    "settled": ("has_settled", "settled_at"),
}


def ingest_event(
    db: Session, event: EventIn, *, commit: bool = True
) -> tuple[str, Transaction | None]:
    """Ingest a single event idempotently.

    Returns ("created" | "duplicate", transaction). On a duplicate we do NOT
    touch the transaction projection — that is what keeps duplicate submissions
    from corrupting state.

    `commit=False` lets a bulk loader batch many events into one transaction
    (much faster than one fsync per event); it flushes instead so state stays
    queryable within the batch.
    """
    finalize = db.commit if commit else db.flush
    # 1) Ensure the merchant row exists (idempotent).
    db.execute(
        _insert_ignore(Merchant)
        .values(merchant_id=event.merchant_id, merchant_name=event.merchant_name)
        .on_conflict_do_nothing(index_elements=["merchant_id"])
    )

    # 2) Idempotent append to the event log. rowcount == 0 => duplicate.
    result = db.execute(
        _insert_ignore(Event)
        .values(
            event_id=event.event_id,
            event_type=event.event_type.value,
            transaction_id=event.transaction_id,
            merchant_id=event.merchant_id,
            amount=event.amount,
            currency=event.currency,
            event_timestamp=event.timestamp,
        )
        .on_conflict_do_nothing(index_elements=["event_id"])
    )

    if result.rowcount == 0:
        # Duplicate: count it (for the discrepancy report) but change nothing else.
        db.execute(
            update(Transaction)
            .where(Transaction.transaction_id == event.transaction_id)
            .values(duplicate_event_count=Transaction.duplicate_event_count + 1)
        )
        finalize()
        txn = db.get(Transaction, event.transaction_id)
        return "duplicate", txn

    # 3) Apply the (new) event to the transaction projection. Row-lock on
    #    Postgres so concurrent events for the same txn serialize; harmless
    #    no-op on SQLite (single writer).
    q = db.query(Transaction).filter(Transaction.transaction_id == event.transaction_id)
    if engine.dialect.name == "postgresql":
        q = q.with_for_update()
    txn = q.one_or_none()

    if txn is None:
        txn = Transaction(
            transaction_id=event.transaction_id,
            merchant_id=event.merchant_id,
            first_seen_at=event.timestamp,
        )
        db.add(txn)

    flag_attr, ts_attr = _TYPE_TO_FLAG[event.event_type.value]
    setattr(txn, flag_attr, True)
    # Keep the earliest timestamp per milestone.
    existing_ts = getattr(txn, ts_attr)
    if existing_ts is None or event.timestamp < existing_ts:
        setattr(txn, ts_attr, event.timestamp)

    txn.merchant_id = event.merchant_id
    txn.amount = event.amount
    txn.currency = event.currency
    txn.event_count = (txn.event_count or 0) + 1
    if txn.first_seen_at is None or event.timestamp < txn.first_seen_at:
        txn.first_seen_at = event.timestamp
    if txn.last_event_at is None or event.timestamp > txn.last_event_at:
        txn.last_event_at = event.timestamp
    txn.status = derive_status(txn)
    txn.reconciliation_status = derive_reconciliation_status(txn)

    finalize()
    return "created", txn
