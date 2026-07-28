"""
Tests for set_order_status (lock/unlock/paid/unpaid; park/unpark unsupported).

Endpoint behaviour these tests encode:
  - Orders/LockOrder  POST {"orderIds":[guid],"lockOrder":bool}  — lock/unlock.
  - Orders/ChangeStatus POST {"orderIds":[guid],"status":int}   — paid=1/unpaid=0
    per the enum documented in the ChangeStatus endpoint description
    (0=UNPAID,1=PAID,2=RETURN,3=PENDING,4=RESEND).
  - GeneralInfo.Status uses THAT enum (a live PAID order reads back as 1) — so
    paid/unpaid are read-back verified. Lock is NOT (no lock field on the order
    model), so the tool reports lock_readback="unavailable".
  - park/unpark have NO public endpoint — the tool rejects them.
"""
import pytest
from unittest.mock import patch

GUID = "44b3e74a-64c2-4247-b9ec-a2a580d26791"
GUID2 = "44b3e74a-64c2-4247-b9ec-a2a580d26792"


def _mock(status=0, is_parked=False, processed=False, known=(GUID,)):
    """Mock the call layer. Returns (patches, captured, state)."""
    captured = {"lock": [], "status": []}
    state = {"status": status}

    def _order(guid):
        return {
            "OrderId": guid,
            "NumOrderId": 607046,
            "Processed": processed,
            "FulfilmentLocationId": "00000000-0000-0000-0000-000000000000",
            "GeneralInfo": {
                "Status": state["status"],
                "IsParked": is_parked,
                "ReferenceNum": "7192780701942",
                "Source": "SHOPIFY",
                "SubSource": "SWH Shopify",
            },
            "CustomerInfo": {"Address": {"FullName": "Channon Andrew",
                                         "EmailAddress": "c@example.com"}},
            "Items": [],
        }

    def call_linnworks(path, payload):
        if path.endswith("GetOrdersById"):
            ids = [g for g in payload.get("pkOrderIds", []) if g in known]
            return [_order(g) for g in ids]
        if path.endswith("LockOrder"):
            captured["lock"].append(payload)
            return {}
        if path.endswith("ChangeStatus"):
            captured["status"].append(payload)
            state["status"] = payload.get("status")   # reflect for read-back
            return {}
        raise AssertionError(f"Unexpected call_linnworks: {path}")

    def call_linnworks_get(path, params=None):
        raise AssertionError(f"Unexpected GET: {path}")

    patches = [
        patch("server.call_linnworks", side_effect=call_linnworks),
        patch("server.call_linnworks_get", side_effect=call_linnworks_get),
    ]
    return patches, captured, state


def test_dry_run_lock_builds_manifest_no_write():
    import server
    patches, captured, _ = _mock()
    for p in patches:
        p.start()
    try:
        r = server.set_order_status([GUID], "lock", dry_run=True)
    finally:
        for p in patches:
            p.stop()
    assert r["dry_run"] is True
    assert r["endpoint"] == "Orders/LockOrder"
    assert r["resolved_count"] == 1
    assert r["manifest"][0]["intent"].startswith("Lock")
    assert captured["lock"] == []   # nothing written on a dry run


def test_live_lock_sends_lockorder_true():
    import server
    patches, captured, _ = _mock()
    for p in patches:
        p.start()
    try:
        r = server.set_order_status([GUID], "lock", dry_run=False)
    finally:
        for p in patches:
            p.stop()
    assert captured["lock"] == [{"orderIds": [GUID], "lockOrder": True}]
    assert r["results"][0]["lock_readback"] == "unavailable"


def test_live_unlock_sends_lockorder_false():
    import server
    patches, captured, _ = _mock()
    for p in patches:
        p.start()
    try:
        server.set_order_status([GUID], "unlock", dry_run=False)
    finally:
        for p in patches:
            p.stop()
    assert captured["lock"] == [{"orderIds": [GUID], "lockOrder": False}]


def test_live_paid_sends_status_1_and_reads_back():
    import server
    patches, captured, _ = _mock(status=0)
    for p in patches:
        p.start()
    try:
        r = server.set_order_status([GUID], "paid", dry_run=False)
    finally:
        for p in patches:
            p.stop()
    assert captured["status"] == [{"orderIds": [GUID], "status": 1}]
    rb = r["results"][0]
    assert rb["status_after"] == 1
    assert rb["status_after_label"] == "PAID"
    assert rb["changed"] is True


def test_live_unpaid_sends_status_0():
    import server
    patches, captured, _ = _mock(status=1)
    for p in patches:
        p.start()
    try:
        r = server.set_order_status([GUID], "unpaid", dry_run=False)
    finally:
        for p in patches:
            p.stop()
    assert captured["status"] == [{"orderIds": [GUID], "status": 0}]
    assert r["results"][0]["status_after_label"] == "UNPAID"


def test_park_is_rejected():
    import server
    patches, captured, _ = _mock()
    for p in patches:
        p.start()
    try:
        r = server.set_order_status([GUID], "park", dry_run=False)
    finally:
        for p in patches:
            p.stop()
    assert "error" in r
    assert "not supported" in r["error"]
    assert r["supported_actions"] == ["lock", "unlock", "paid", "unpaid"]
    assert captured["lock"] == [] and captured["status"] == []


def test_unknown_action_is_rejected():
    import server
    r = server.set_order_status([GUID], "frobnicate", dry_run=True)
    assert "error" in r and "Unknown action" in r["error"]


def test_unresolved_id_becomes_error_row_not_fatal():
    import server
    # Only GUID is known; GUID2 is not → resolve error, but GUID still acts.
    patches, captured, _ = _mock(known=(GUID,))
    for p in patches:
        p.start()
    try:
        r = server.set_order_status([GUID, GUID2], "lock", dry_run=False)
    finally:
        for p in patches:
            p.stop()
    assert r["resolved_count"] == 1
    assert len(r["resolve_errors"]) == 1
    assert captured["lock"] == [{"orderIds": [GUID], "lockOrder": True}]


def test_bare_string_order_id_accepted():
    import server
    patches, captured, _ = _mock()
    for p in patches:
        p.start()
    try:
        r = server.set_order_status(GUID, "lock", dry_run=True)  # not a list
    finally:
        for p in patches:
            p.stop()
    assert r["resolved_count"] == 1


def test_large_batch_stages_without_confirmed_count():
    import server
    ids = [GUID] * 26   # 26 > threshold 25
    patches, captured, _ = _mock()
    for p in patches:
        p.start()
    try:
        r = server.set_order_status(ids, "lock", dry_run=False)
    finally:
        for p in patches:
            p.stop()
    assert r.get("staged") is True
    assert r["confirmed_count"] is None
    assert captured["lock"] == []   # nothing written while staged
