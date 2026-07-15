from tests.conftest import event


def test_ingest_creates_transaction(client):
    r = client.post("/events", json=event())
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "created"
    assert body["transaction_status"] == "INITIATED"


def test_duplicate_event_is_idempotent(client):
    e = event()
    first = client.post("/events", json=e)
    second = client.post("/events", json=e)
    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["status"] == "duplicate"

    # Only ONE event recorded, transaction state uncorrupted, dup counted.
    txn = client.get("/transactions/txn-1").json()
    assert txn["event_count"] == 1
    assert txn["duplicate_event_count"] == 1
    assert len(txn["events"]) == 1


def test_lifecycle_progression(client):
    client.post("/events", json=event(event_id="e1", event_type="payment_initiated"))
    client.post("/events", json=event(event_id="e2", event_type="payment_processed"))
    r = client.post("/events", json=event(event_id="e3", event_type="settled"))
    assert r.json()["transaction_status"] == "SETTLED"

    txn = client.get("/transactions/txn-1").json()
    assert txn["status"] == "SETTLED"
    assert txn["has_initiated"] and txn["has_processed"] and txn["has_settled"]
    assert txn["event_count"] == 3
    assert [ev["event_type"] for ev in txn["events"]] == [
        "payment_initiated",
        "payment_processed",
        "settled",
    ]


def test_out_of_order_events_converge(client):
    # settled arrives before initiated — final state must still be correct.
    client.post("/events", json=event(event_id="e2", event_type="settled",
                                      timestamp="2026-01-08T13:00:00+00:00"))
    client.post("/events", json=event(event_id="e1", event_type="payment_initiated",
                                      timestamp="2026-01-08T12:00:00+00:00"))
    txn = client.get("/transactions/txn-1").json()
    assert txn["status"] == "SETTLED"
    # first_seen tracks the earliest event timestamp regardless of arrival order.
    assert txn["first_seen_at"].startswith("2026-01-08T12:00:00")


def test_validation_rejects_bad_event(client):
    r = client.post("/events", json=event(event_type="nonsense"))
    assert r.status_code == 422

    r = client.post("/events", json=event(amount=-5))
    assert r.status_code == 422


def test_404_for_unknown_transaction(client):
    r = client.get("/transactions/does-not-exist")
    assert r.status_code == 404
    # Consistent error envelope.
    assert r.json()["error"]["type"] == "http_error"


def test_validation_error_envelope(client):
    r = client.post("/events", json=event(amount=-5))
    assert r.status_code == 422
    body = r.json()
    assert body["error"]["type"] == "validation_error"
    assert "details" in body["error"]


def test_root_redirects_to_docs(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code in (307, 308)
    assert r.headers["location"] == "/docs"
