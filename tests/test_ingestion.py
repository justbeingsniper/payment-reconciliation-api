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


def test_unknown_route_uses_error_envelope(client):
    # Framework-raised 404s go through the same envelope, not the default shape.
    r = client.get("/definitely-not-a-route")
    assert r.status_code == 404
    assert r.json()["error"]["type"] == "http_error"


def test_batch_ingest_and_idempotency(client):
    batch = [
        event(event_id="b1", transaction_id="txn-b", event_type="payment_initiated"),
        event(event_id="b2", transaction_id="txn-b", event_type="payment_processed"),
    ]
    r = client.post("/events/batch", json=batch)
    assert r.json() == {"received": 2, "created": 2, "duplicates": 0}

    # Re-submitting the same batch changes nothing (idempotent).
    r = client.post("/events/batch", json=batch)
    assert r.json() == {"received": 2, "created": 0, "duplicates": 2}

    txn = client.get("/transactions/txn-b").json()
    assert txn["status"] == "PROCESSED"
    assert txn["event_count"] == 2


def test_event_history_is_bounded(client):
    for i, et in enumerate(["payment_initiated", "payment_processed", "settled"]):
        client.post("/events", json=event(event_id=f"h{i}", transaction_id="txn-h",
                                          event_type=et))
    full = client.get("/transactions/txn-h").json()
    assert len(full["events"]) == 3
    assert full["events_truncated"] is False

    capped = client.get("/transactions/txn-h?event_limit=1").json()
    assert len(capped["events"]) == 1
    assert capped["events_truncated"] is True
    assert capped["event_count"] == 3  # true total still reported
    # Bounded history returns the most recent event.
    assert capped["events"][0]["event_type"] == "settled"
