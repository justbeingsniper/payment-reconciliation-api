from tests.conftest import event


def _seed(client):
    # txn-happy: initiated -> processed -> settled  (clean)
    client.post("/events", json=event(event_id="h1", transaction_id="txn-happy",
                                      event_type="payment_initiated"))
    client.post("/events", json=event(event_id="h2", transaction_id="txn-happy",
                                      event_type="payment_processed"))
    client.post("/events", json=event(event_id="h3", transaction_id="txn-happy",
                                      event_type="settled"))
    # txn-proc: processed but never settled  -> PROCESSED_NOT_SETTLED
    client.post("/events", json=event(event_id="p1", transaction_id="txn-proc",
                                      event_type="payment_initiated"))
    client.post("/events", json=event(event_id="p2", transaction_id="txn-proc",
                                      event_type="payment_processed"))
    # txn-badsettle: failed then settled -> SETTLED_FOR_FAILED_PAYMENT
    client.post("/events", json=event(event_id="b1", transaction_id="txn-badsettle",
                                      event_type="payment_initiated"))
    client.post("/events", json=event(event_id="b2", transaction_id="txn-badsettle",
                                      event_type="payment_failed"))
    client.post("/events", json=event(event_id="b3", transaction_id="txn-badsettle",
                                      event_type="settled"))


def test_summary_by_merchant(client):
    _seed(client)
    r = client.get("/reconciliation/summary?group_by=merchant")
    assert r.status_code == 200
    rows = r.json()["rows"]
    assert len(rows) == 1
    row = rows[0]
    assert row["group"]["merchant"] == "merchant_1"
    assert row["transaction_count"] == 3
    assert row["settled_count"] == 2  # txn-happy + txn-badsettle


def test_summary_multi_dimension(client):
    _seed(client)
    r = client.get("/reconciliation/summary?group_by=merchant,status")
    rows = r.json()["rows"]
    by_status = {row["group"]["status"]: row["transaction_count"] for row in rows}
    assert by_status["SETTLED"] == 2
    assert by_status["PROCESSED"] == 1


def test_discrepancies_detected(client):
    _seed(client)
    r = client.get("/reconciliation/discrepancies")
    assert r.status_code == 200
    counts = r.json()["counts_by_type"]
    assert counts["PROCESSED_NOT_SETTLED"] == 1
    assert counts["SETTLED_FOR_FAILED_PAYMENT"] == 1


def test_discrepancy_type_filter(client):
    _seed(client)
    r = client.get("/reconciliation/discrepancies?type=PROCESSED_NOT_SETTLED")
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["transaction_id"] == "txn-proc"


def test_duplicate_submission_flagged_as_discrepancy(client):
    client.post("/events", json=event(event_id="d1", transaction_id="txn-dup"))
    client.post("/events", json=event(event_id="d1", transaction_id="txn-dup"))
    r = client.get("/reconciliation/discrepancies?type=DUPLICATE_SUBMISSIONS")
    assert r.json()["counts_by_type"]["DUPLICATE_SUBMISSIONS"] == 1


def test_list_filters_and_pagination(client):
    _seed(client)
    r = client.get("/transactions?status=SETTLED&limit=1")
    body = r.json()
    assert body["pagination"]["total"] == 2
    assert body["pagination"]["returned"] == 1

    r = client.get("/transactions?merchant_id=merchant_1&sort_by=amount&sort_dir=asc")
    assert r.json()["pagination"]["total"] == 3


def test_reconciliation_status_materialized(client):
    _seed(client)
    # txn-happy => RECONCILED, txn-proc => DISCREPANCY (processed not settled),
    # txn-badsettle => DISCREPANCY (settled for failed).
    happy = client.get("/transactions/txn-happy").json()
    proc = client.get("/transactions/txn-proc").json()
    bad = client.get("/transactions/txn-badsettle").json()
    assert happy["reconciliation_status"] == "RECONCILED"
    assert proc["reconciliation_status"] == "DISCREPANCY"
    assert bad["reconciliation_status"] == "DISCREPANCY"


def test_filter_by_reconciliation_status(client):
    _seed(client)
    r = client.get("/transactions?reconciliation_status=DISCREPANCY")
    assert r.json()["pagination"]["total"] == 2  # txn-proc + txn-badsettle
    r = client.get("/transactions?reconciliation_status=RECONCILED")
    assert r.json()["pagination"]["total"] == 1  # txn-happy


def test_conflicting_state_detected(client):
    # A payment that is both processed AND failed is contradictory.
    client.post("/events", json=event(event_id="c1", transaction_id="txn-conflict",
                                      event_type="payment_initiated"))
    client.post("/events", json=event(event_id="c2", transaction_id="txn-conflict",
                                      event_type="payment_processed"))
    client.post("/events", json=event(event_id="c3", transaction_id="txn-conflict",
                                      event_type="payment_failed"))
    r = client.get("/reconciliation/discrepancies?type=CONFLICTING_STATE")
    assert r.json()["counts_by_type"]["CONFLICTING_STATE"] == 1


def test_money_precision_preserved(client):
    # 15248.29 must round-trip exactly (this is the reason for NUMERIC over float).
    client.post("/events", json=event(event_id="m1", transaction_id="txn-money",
                                      amount=15248.29))
    txn = client.get("/transactions/txn-money").json()
    assert txn["amount"] == 15248.29
    s = client.get("/reconciliation/summary?group_by=merchant").json()
    assert s["rows"][0]["total_amount"] == 15248.29
