"""Test fixtures: a FastAPI TestClient backed by a throwaway SQLite file."""
import os
import tempfile

import pytest

# Point the app at a temp DB *before* importing it (config reads env at import).
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp.name}"

from fastapi.testclient import TestClient  # noqa: E402

from app.database import Base, engine  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture()
def client():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    with TestClient(app) as c:
        yield c


def event(**overrides):
    base = {
        "event_id": "evt-1",
        "event_type": "payment_initiated",
        "transaction_id": "txn-1",
        "merchant_id": "merchant_1",
        "merchant_name": "QuickMart",
        "amount": 100.0,
        "currency": "INR",
        "timestamp": "2026-01-08T12:00:00+00:00",
    }
    base.update(overrides)
    return base
