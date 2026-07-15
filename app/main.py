"""FastAPI application: ingestion + transaction + reconciliation APIs."""
import os
import threading
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app import queries, services
from app.config import get_settings
from app.database import Base, engine, get_db
from app.schemas import (
    DiscrepancyResponse,
    DiscrepancyType,
    EventIn,
    IngestResult,
    Page,
    PaginationMeta,
    ReconciliationStatus,
    SummaryResponse,
    TransactionDetail,
    TransactionOut,
    TransactionStatus,
)

settings = get_settings()


def _load_sample_async() -> None:
    """Load the bundled sample file (runs in a background thread so /health stays
    responsive during the ~15s load)."""
    try:
        sample = Path(__file__).resolve().parents[1] / "sample_events.json"
        if not sample.exists():
            return
        from scripts.load_sample_data import main as load_sample
        print("[seed] events table empty; loading bundled sample data...")
        load_sample(str(sample))
    except Exception as exc:  # never let seeding crash the process
        print(f"[seed] skipped: {exc}")


def _seed_if_empty() -> None:
    """Demo convenience: on the hosted deployment we can't shell in on the free
    tier, so — when SEED_ON_STARTUP is set — self-load the bundled sample data
    over the internal DB connection. Guarded to run only when the events table
    is empty, so cold-start restarts never re-seed or wipe data. Never triggers
    locally (there you run scripts/load_sample_data.py explicitly).

    Wrapped so a seeding hiccup only logs — it must never take down the app."""
    try:
        from app.database import SessionLocal
        from app.models import Event

        with SessionLocal() as db:
            if db.execute(select(func.count()).select_from(Event)).scalar_one() > 0:
                return  # already seeded

        # No Alembic: since the DB is empty, rebuild the schema so it matches the
        # current models. CASCADE clears any legacy constraints that are no longer
        # in our metadata (e.g. an old events->transactions FK from a prior deploy).
        with engine.begin() as conn:
            if engine.dialect.name == "postgresql":
                conn.execute(
                    text("DROP TABLE IF EXISTS events, transactions, merchants CASCADE")
                )
            else:
                Base.metadata.drop_all(bind=conn)
        Base.metadata.create_all(bind=engine)
        threading.Thread(target=_load_sample_async, daemon=True).start()
    except Exception as exc:  # never let seeding break startup
        print(f"[seed] skipped: {exc}")


@asynccontextmanager
async def lifespan(_: FastAPI):
    # For a take-home this is fine; a real service would use Alembic migrations.
    Base.metadata.create_all(bind=engine)
    if os.getenv("SEED_ON_STARTUP", "").lower() in ("1", "true", "yes"):
        _seed_if_empty()
    yield


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    lifespan=lifespan,
    description=(
        "Ingest payment lifecycle events, track transaction state, and report "
        "reconciliation discrepancies. Interactive docs at /docs."
    ),
)


# --------------------------------------------------------------------------- #
# Consistent error envelope: every error comes back as {"error": {...}}.
# --------------------------------------------------------------------------- #
@app.exception_handler(HTTPException)
async def http_exception_handler(_: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"type": "http_error", "message": exc.detail}},
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(_: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "type": "validation_error",
                "message": "Request validation failed",
                "details": jsonable_encoder(exc.errors()),
            }
        },
    )


@app.get("/", include_in_schema=False)
def root():
    # Friendly landing: send humans to the interactive docs.
    return RedirectResponse(url="/docs")


@app.get("/health", tags=["meta"])
def health(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1"))
    return {"status": "ok"}


# --------------------------------------------------------------------------- #
# 1. Ingest
# --------------------------------------------------------------------------- #
@app.post(
    "/events",
    response_model=IngestResult,
    tags=["events"],
    summary="Ingest a payment lifecycle event (idempotent)",
)
def ingest(event: EventIn, db: Session = Depends(get_db)):
    outcome, txn = services.ingest_event(db, event)
    http_status = status.HTTP_201_CREATED if outcome == "created" else status.HTTP_200_OK
    body = IngestResult(
        event_id=event.event_id,
        status=outcome,
        transaction_id=event.transaction_id,
        transaction_status=(TransactionStatus(txn.status) if txn else None),
    )
    return JSONResponse(status_code=http_status, content=body.model_dump(mode="json"))


@app.post(
    "/events/batch",
    tags=["events"],
    summary="Ingest many events in one call (idempotent, convenience endpoint)",
)
def ingest_batch(events: list[EventIn], db: Session = Depends(get_db)):
    created = duplicates = 0
    for e in events:
        outcome, _ = services.ingest_event(db, e)
        if outcome == "created":
            created += 1
        else:
            duplicates += 1
    return {"received": len(events), "created": created, "duplicates": duplicates}


# --------------------------------------------------------------------------- #
# 2. List transactions
# --------------------------------------------------------------------------- #
@app.get(
    "/transactions",
    response_model=Page[TransactionOut],
    tags=["transactions"],
    summary="List/filter/sort/paginate transactions",
)
def list_transactions(
    db: Session = Depends(get_db),
    merchant_id: str | None = Query(None),
    status: TransactionStatus | None = Query(None),
    reconciliation_status: ReconciliationStatus | None = Query(
        None, description="Filter by RECONCILED / DISCREPANCY / PENDING"
    ),
    start_date: datetime | None = Query(None, description="Filter last_event_at >="),
    end_date: datetime | None = Query(None, description="Filter last_event_at <="),
    sort_by: str = Query("last_event_at", enum=list(queries.SORTABLE.keys())),
    sort_dir: str = Query("desc", enum=["asc", "desc"]),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    total, rows = queries.list_transactions(
        db,
        merchant_id=merchant_id,
        status=status.value if status else None,
        reconciliation_status=(
            reconciliation_status.value if reconciliation_status else None
        ),
        start_date=start_date,
        end_date=end_date,
        sort_by=sort_by,
        sort_dir=sort_dir,
        limit=limit,
        offset=offset,
    )
    return Page[TransactionOut](
        pagination=PaginationMeta(
            total=total, limit=limit, offset=offset, returned=len(rows)
        ),
        items=[TransactionOut.model_validate(r) for r in rows],
    )


# --------------------------------------------------------------------------- #
# 3. Transaction detail
# --------------------------------------------------------------------------- #
@app.get(
    "/transactions/{transaction_id}",
    response_model=TransactionDetail,
    tags=["transactions"],
    summary="Transaction details + merchant + full event history",
)
def get_transaction(transaction_id: str, db: Session = Depends(get_db)):
    txn = queries.get_transaction(db, transaction_id)
    if txn is None:
        raise HTTPException(status_code=404, detail="transaction not found")
    return txn


# --------------------------------------------------------------------------- #
# 4. Reconciliation summary
# --------------------------------------------------------------------------- #
@app.get(
    "/reconciliation/summary",
    response_model=SummaryResponse,
    tags=["reconciliation"],
    summary="Grouped reconciliation summary (by merchant/date/status)",
)
def reconciliation_summary(
    db: Session = Depends(get_db),
    group_by: str = Query(
        "merchant",
        description="Comma-separated dimensions: any of merchant,date,status",
    ),
    merchant_id: str | None = Query(None),
    start_date: datetime | None = Query(None),
    end_date: datetime | None = Query(None),
):
    dims = [d.strip() for d in group_by.split(",") if d.strip()]
    rows = queries.reconciliation_summary(
        db,
        group_by=dims,
        merchant_id=merchant_id,
        start_date=start_date,
        end_date=end_date,
    )
    return SummaryResponse(group_by=dims, rows=rows)


# --------------------------------------------------------------------------- #
# 5. Reconciliation discrepancies
# --------------------------------------------------------------------------- #
@app.get(
    "/reconciliation/discrepancies",
    response_model=DiscrepancyResponse,
    tags=["reconciliation"],
    summary="Transactions whose payment & settlement state are inconsistent",
)
def reconciliation_discrepancies(
    db: Session = Depends(get_db),
    type: DiscrepancyType | None = Query(None, description="Filter to one type"),
    merchant_id: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    counts, total, items = queries.discrepancies(
        db,
        dtype=type.value if type else None,
        merchant_id=merchant_id,
        limit=limit,
        offset=offset,
    )
    return DiscrepancyResponse(
        counts_by_type=counts,
        pagination=PaginationMeta(
            total=total, limit=limit, offset=offset, returned=len(items)
        ),
        items=items,
    )
