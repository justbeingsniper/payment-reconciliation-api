"""Read-side logic. Every aggregation, filter, sort and page happens in SQL."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import Date, Integer, and_, cast, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.config import get_settings
from app.database import engine
from app.models import Event, Transaction

settings = get_settings()

SORTABLE = {
    "last_event_at": Transaction.last_event_at,
    "first_seen_at": Transaction.first_seen_at,
    "amount": Transaction.amount,
    "created_at": Transaction.created_at,
    "status": Transaction.status,
}


def _day_bucket(column):
    """Truncate a timestamp to a calendar day, portably.

    Postgres and SQLite disagree on date functions, so branch on the dialect."""
    if engine.dialect.name == "postgresql":
        return cast(column, Date)
    return func.date(column)  # SQLite parses ISO timestamps natively


# --------------------------------------------------------------------------- #
# GET /transactions
# --------------------------------------------------------------------------- #
def list_transactions(
    db: Session,
    *,
    merchant_id: str | None,
    status: str | None,
    reconciliation_status: str | None,
    start_date: datetime | None,
    end_date: datetime | None,
    sort_by: str,
    sort_dir: str,
    limit: int,
    offset: int,
) -> tuple[int, list[Transaction]]:
    filters = []
    if merchant_id:
        filters.append(Transaction.merchant_id == merchant_id)
    if status:
        filters.append(Transaction.status == status.upper())
    if reconciliation_status:
        filters.append(
            Transaction.reconciliation_status == reconciliation_status.upper()
        )
    if start_date:
        filters.append(Transaction.last_event_at >= start_date)
    if end_date:
        filters.append(Transaction.last_event_at <= end_date)

    base = select(Transaction).where(*filters)

    # Count via SQL (no fetching rows into Python to count them).
    total = db.execute(
        select(func.count()).select_from(base.subquery())
    ).scalar_one()

    col = SORTABLE.get(sort_by, Transaction.last_event_at)
    col = col.desc() if sort_dir.lower() == "desc" else col.asc()

    rows = db.execute(
        base.order_by(col).limit(limit).offset(offset)
    ).scalars().all()
    return total, rows


# --------------------------------------------------------------------------- #
# GET /transactions/{id}
# --------------------------------------------------------------------------- #
def get_transaction(
    db: Session, transaction_id: str, *, event_limit: int = 500
) -> tuple[Transaction | None, list[Event]]:
    """Return (transaction, event_history). History is bounded by event_limit
    (most recent N, returned oldest-first) so a pathological transaction with a
    huge event count can't blow up the response."""
    txn = db.execute(
        select(Transaction)
        .where(Transaction.transaction_id == transaction_id)
        .options(selectinload(Transaction.merchant))
    ).scalar_one_or_none()
    if txn is None:
        return None, []

    events = db.execute(
        select(Event)
        .where(Event.transaction_id == transaction_id)
        .order_by(Event.event_timestamp.desc())
        .limit(event_limit)
    ).scalars().all()
    events.reverse()  # present chronologically (oldest first)
    return txn, list(events)


# --------------------------------------------------------------------------- #
# GET /reconciliation/summary
# --------------------------------------------------------------------------- #
_ALLOWED_DIMENSIONS = {"merchant", "date", "status"}


def reconciliation_summary(
    db: Session,
    *,
    group_by: list[str],
    merchant_id: str | None = None,
    start_date: datetime | None = None,
    end_date: datetime | None = None,
) -> list[dict]:
    """Grouped counts + summed amount, entirely in SQL via GROUP BY.

    `group_by` is any combination of merchant / date / status."""
    dims = [d for d in group_by if d in _ALLOWED_DIMENSIONS]
    if not dims:
        dims = ["merchant"]

    dim_cols = {
        "merchant": Transaction.merchant_id,
        "date": _day_bucket(Transaction.last_event_at),
        "status": Transaction.status,
    }
    selected = [dim_cols[d].label(d) for d in dims]

    # Conditional aggregates give per-status breakdown in a single scan.
    def count_when(flag):
        return func.sum(cast(flag, Integer))

    stmt = select(
        *selected,
        func.count().label("transaction_count"),
        func.coalesce(func.sum(Transaction.amount), 0).label("total_amount"),
        count_when(Transaction.status == "SETTLED").label("settled_count"),
        count_when(Transaction.status == "FAILED").label("failed_count"),
        count_when(Transaction.status == "PROCESSED").label("processed_count"),
        count_when(Transaction.status == "INITIATED").label("initiated_count"),
    )

    filters = []
    if merchant_id:
        filters.append(Transaction.merchant_id == merchant_id)
    if start_date:
        filters.append(Transaction.last_event_at >= start_date)
    if end_date:
        filters.append(Transaction.last_event_at <= end_date)
    if filters:
        stmt = stmt.where(*filters)

    stmt = stmt.group_by(*selected).order_by(*selected)

    rows = db.execute(stmt).mappings().all()
    out = []
    for r in rows:
        group = {}
        for d in dims:
            val = r[d]
            group[d] = str(val) if val is not None else None
        raw_total = r["total_amount"] or 0
        # Keep money exact: quantize a Decimal to 2dp rather than round a float.
        total_amount = Decimal(str(raw_total)).quantize(Decimal("0.01"))
        out.append(
            {
                "group": group,
                "transaction_count": int(r["transaction_count"]),
                "total_amount": total_amount,
                "settled_count": int(r["settled_count"] or 0),
                "failed_count": int(r["failed_count"] or 0),
                "processed_count": int(r["processed_count"] or 0),
                "initiated_count": int(r["initiated_count"] or 0),
            }
        )
    return out


# --------------------------------------------------------------------------- #
# GET /reconciliation/discrepancies
# --------------------------------------------------------------------------- #
def _discrepancy_predicate(dtype: str, stuck_before: datetime):
    """Map each discrepancy type to a SQL predicate over the flags."""
    if dtype == "PROCESSED_NOT_SETTLED":
        # Money captured but never settled to the merchant.
        return and_(
            Transaction.has_processed.is_(True),
            Transaction.has_settled.is_(False),
            Transaction.has_failed.is_(False),
        )
    if dtype == "SETTLED_FOR_FAILED_PAYMENT":
        # A settlement exists for a payment that failed (or was never processed).
        return and_(
            Transaction.has_settled.is_(True),
            or_(
                Transaction.has_failed.is_(True),
                Transaction.has_processed.is_(False),
            ),
        )
    if dtype == "CONFLICTING_STATE":
        # Logically contradictory: a payment can't be both processed (captured)
        # and failed. Directly answers the spec's "conflicting state transitions".
        # Expected to be 0 on healthy data precisely because idempotency prevents
        # duplicate-induced corruption — this check catches upstream sending
        # genuinely contradictory events.
        return and_(
            Transaction.has_processed.is_(True),
            Transaction.has_failed.is_(True),
        )
    if dtype == "STUCK_PENDING":
        # Initiated, no terminal state, and older than the stale threshold.
        return and_(
            Transaction.has_initiated.is_(True),
            Transaction.has_processed.is_(False),
            Transaction.has_failed.is_(False),
            Transaction.has_settled.is_(False),
            Transaction.last_event_at < stuck_before,
        )
    if dtype == "DUPLICATE_SUBMISSIONS":
        # Received duplicate event submissions (idempotency kicked in).
        return Transaction.duplicate_event_count > 0
    raise ValueError(f"unknown discrepancy type {dtype}")


_DETAIL = {
    "PROCESSED_NOT_SETTLED": "Payment processed but never settled",
    "SETTLED_FOR_FAILED_PAYMENT": "Settlement recorded for a failed/unprocessed payment",
    "CONFLICTING_STATE": "Contradictory state: payment both processed and failed",
    "STUCK_PENDING": "Initiated but stuck with no terminal state",
    "DUPLICATE_SUBMISSIONS": "Received duplicate event submissions",
}

ALL_DISCREPANCY_TYPES = list(_DETAIL.keys())


def discrepancies(
    db: Session,
    *,
    dtype: str | None,
    merchant_id: str | None,
    limit: int,
    offset: int,
) -> tuple[dict[str, int], int, int, list[dict]]:
    """Returns (counts_by_type, distinct_transactions, total_issue_rows, items).

    A transaction can be flagged under more than one type, so it appears once
    per type in `items`; `total_issue_rows` sums the per-type counts while
    `distinct_transactions` de-duplicates."""
    stuck_before = datetime.now(timezone.utc) - timedelta(
        hours=settings.stuck_threshold_hours
    )
    types = [dtype] if dtype else ALL_DISCREPANCY_TYPES

    base_filters = [Transaction.merchant_id == merchant_id] if merchant_id else []

    # Per-type counts (one COUNT query per category — all indexed).
    counts: dict[str, int] = {}
    for t in types:
        counts[t] = db.execute(
            select(func.count())
            .select_from(Transaction)
            .where(_discrepancy_predicate(t, stuck_before), *base_filters)
        ).scalar_one()

    # Distinct transactions flagged by ANY of the selected types.
    distinct_transactions = db.execute(
        select(func.count())
        .select_from(Transaction)
        .where(
            or_(*[_discrepancy_predicate(t, stuck_before) for t in types]),
            *base_filters,
        )
    ).scalar_one()

    # Build a single UNION-style result by iterating types in priority order.
    items: list[dict] = []
    total = sum(counts.values())
    remaining_offset = offset
    remaining_limit = limit

    for t in types:
        n = counts[t]
        if remaining_offset >= n:
            remaining_offset -= n
            continue
        rows = db.execute(
            select(Transaction)
            .where(_discrepancy_predicate(t, stuck_before), *base_filters)
            .order_by(Transaction.last_event_at.desc())
            .limit(remaining_limit)
            .offset(remaining_offset)
        ).scalars().all()
        remaining_offset = 0
        for r in rows:
            items.append(
                {
                    "transaction_id": r.transaction_id,
                    "merchant_id": r.merchant_id,
                    "amount": r.amount,
                    "status": r.status,
                    "discrepancy_type": t,
                    "detail": _DETAIL[t],
                    "last_event_at": r.last_event_at,
                    "duplicate_event_count": r.duplicate_event_count,
                }
            )
        remaining_limit -= len(rows)
        if remaining_limit <= 0:
            break

    return counts, distinct_transactions, total, items
