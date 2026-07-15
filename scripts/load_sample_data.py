"""Load sample_events.json into the database via the ingestion path.

Runs events through the SAME idempotent `ingest_event` logic the API uses, so
the loaded state is identical to what you'd get by POSTing every event. Safe to
re-run: duplicates are ignored.

Usage:
    python -m scripts.load_sample_data [path/to/sample_events.json]
"""
import json
import sys
import time
from pathlib import Path

from app.database import Base, SessionLocal, engine
from app.schemas import EventIn
from app.services import ingest_event


def main(path: str) -> None:
    Base.metadata.create_all(bind=engine)
    events = json.loads(Path(path).read_text(encoding="utf-8"))
    print(f"Loading {len(events)} events from {path} ...")

    created = duplicates = errors = 0
    batch_size = 500
    start = time.perf_counter()
    db = SessionLocal()
    try:
        for i, raw in enumerate(events, 1):
            try:
                # commit=False => batch many events into one transaction.
                outcome, _ = ingest_event(db, EventIn(**raw), commit=False)
                if outcome == "created":
                    created += 1
                else:
                    duplicates += 1
            except Exception as exc:  # keep going; report at the end
                db.rollback()
                errors += 1
                if errors <= 5:
                    print(f"  ! event {raw.get('event_id')}: {exc}")
            if i % batch_size == 0:
                db.commit()
                print(f"  ...{i}")
        db.commit()
    finally:
        db.close()

    elapsed = time.perf_counter() - start
    print(
        f"Done in {elapsed:.1f}s — created={created} duplicates={duplicates} "
        f"errors={errors}"
    )


if __name__ == "__main__":
    default = Path(__file__).resolve().parents[1] / "sample_events.json"
    main(sys.argv[1] if len(sys.argv) > 1 else str(default))
