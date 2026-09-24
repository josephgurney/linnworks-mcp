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


# ---------------------------------------------------------------------------
# Issue #88 — the parked flag on the paid/unpaid read-back, and the rate-limit
# gap at both call sites.
#
# Why these matter together: `unpaid` is the one action here suspected of a
# side effect beyond the field it names. Linnworks force-parks an order that is
# CREATED unpaid (proven live 18 Sep 2026, order 611398, via
# ChannelOrderAdapter.Save()) — but nothing has ever observed what
# Orders/ChangeStatus(0) does to an order that ALREADY EXISTS. A parked order
# is out of the dispatch queue, so "mark unpaid" would be doing something much
# larger than it sounds. The read-back therefore has to report the flag rather
# than assume it, and the prediction has to stay honestly unset until someone
# fires the proof.
# ---------------------------------------------------------------------------

def _mock2(parked_before=False, parked_after=None, status=1,
           resolve_rate_limits=(), readback_rate_limits=(),
           resolve_runtime_errors=(), readback_runtime_errors=(),
           known=(GUID,)):
    """A mock that can distinguish the resolve read from the read-back read.

    Both phases go through _resolve_order_guid → Orders/GetOrdersById, so the
    only way to tell them apart is to count the reads per guid: the first is
    the pre-write resolve, anything after it is the post-write read-back.

    parked_after=None means the flag does not move across the write.
    """
    import server as _server
    captured = {"lock": [], "status": []}
    state = {"status": status}
    reads: dict[str, int] = {}

    def _order(guid, parked):
        return {
            "OrderId": guid,
            "NumOrderId": 607046,
            "Processed": False,
            "FulfilmentLocationId": "00000000-0000-0000-0000-000000000000",
            "GeneralInfo": {
                "Status": state["status"],
                "IsParked": parked,
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
            out = []
            for g in ids:
                n = reads.get(g, 0)
                reads[g] = n + 1
                first = (n == 0)
                if first and g in resolve_rate_limits:
                    raise _server.RateLimitError(f"rate limited resolving {g}")
                if first and g in resolve_runtime_errors:
                    raise RuntimeError(f"boom resolving {g}")
                if not first and g in readback_rate_limits:
                    raise _server.RateLimitError(f"rate limited reading back {g}")
                if not first and g in readback_runtime_errors:
                    raise RuntimeError(f"boom reading back {g}")
                parked = parked_before if first else (
                    parked_before if parked_after is None else parked_after)
                out.append(_order(g, parked))
            return out
        if path.endswith("LockOrder"):
            captured["lock"].append(payload)
            return {}
        if path.endswith("ChangeStatus"):
            captured["status"].append(payload)
            state["status"] = payload.get("status")
            return {}
        raise AssertionError(f"Unexpected call_linnworks: {path}")

    def call_linnworks_get(path, params=None):
        raise AssertionError(f"Unexpected GET: {path}")

    return (
        [patch("server.call_linnworks", side_effect=call_linnworks),
         patch("server.call_linnworks_get", side_effect=call_linnworks_get)],
        captured,
        state,
    )


def _run(patches, fn):
    for p in patches:
        p.start()
    try:
        return fn()
    finally:
        for p in patches:
            p.stop()


# ---- AC1: the read-back reports the parked flag -------------------------

@pytest.mark.parametrize("parked", [True, False])
def test_paid_readback_reports_the_parked_flag(parked):
    """The LockOrder branch has always reported is_parked; the ChangeStatus
    branch never did, so a paid/unpaid flip could not tell you whether the
    order was in the dispatch queue afterwards."""
    import server
    patches, _, _ = _mock2(parked_before=parked, status=0)
    r = _run(patches, lambda: server.set_order_status([GUID], "paid", dry_run=False))
    assert r["results"][0]["is_parked"] is parked


def test_unpaid_readback_reports_the_parked_flag():
    import server
    patches, _, _ = _mock2(parked_before=False, parked_after=True, status=1)
    r = _run(patches, lambda: server.set_order_status([GUID], "unpaid", dry_run=False))
    assert r["results"][0]["is_parked"] is True


# ---- AC2: and whether it moved across the write -------------------------

def test_readback_records_a_parked_flag_that_changed():
    import server
    patches, _, _ = _mock2(parked_before=False, parked_after=True, status=1)
    r = _run(patches, lambda: server.set_order_status([GUID], "unpaid", dry_run=False))
    row = r["results"][0]
    assert row["parked_before"] is False
    assert row["is_parked"] is True
    assert row["parked_changed"] is True


def test_readback_records_a_parked_flag_that_did_not_change():
    import server
    patches, _, _ = _mock2(parked_before=False, parked_after=None, status=1)
    r = _run(patches, lambda: server.set_order_status([GUID], "unpaid", dry_run=False))
    row = r["results"][0]
    assert row["parked_before"] is False
    assert row["is_parked"] is False
    assert row["parked_changed"] is False


# ---- AC3: a rate limit while resolving ----------------------------------

def test_rate_limited_resolve_is_its_own_outcome_not_order_not_found():
    """RateLimitError deliberately does not subclass RuntimeError (v1.40.0,
    issue #37), so `except RuntimeError` never caught it: a 429 escaped the
    tool entirely. Reporting it as a resolve_error would be worse still —
    that reads as 'no such order'."""
    import server
    patches, captured, _ = _mock2(status=0, resolve_rate_limits=(GUID2,),
                                  known=(GUID, GUID2))
    r = _run(patches, lambda: server.set_order_status(
        [GUID, GUID2], "paid", dry_run=False))

    assert [row["order_id_input"] for row in r["rate_limited"]] == [GUID2]
    assert all(e["order_id_input"] != GUID2 for e in r["resolve_errors"])
    # The other order still acts.
    assert captured["status"] == [{"orderIds": [GUID], "status": 1}]


# ---- AC4: a rate limit during the read-back -----------------------------

def test_rate_limited_readback_says_the_write_may_have_landed():
    import server
    patches, captured, _ = _mock2(status=0, readback_rate_limits=(GUID,))
    r = _run(patches, lambda: server.set_order_status([GUID], "paid", dry_run=False))
    row = r["results"][0]

    assert captured["status"] == [{"orderIds": [GUID], "status": 1}]
    assert row["readback"] == "rate_limited"
    assert "changed" not in row, "a rate-limited read-back must not claim the status is unchanged"
    assert "may" in row["note"].lower() and "succeed" in row["note"].lower()


# ---- AC5: ordering, and RuntimeError behaves exactly as before ----------

def test_runtime_error_on_resolve_still_produces_a_resolve_error():
    import server
    patches, _, _ = _mock2(status=0, resolve_runtime_errors=(GUID2,),
                           known=(GUID, GUID2))
    r = _run(patches, lambda: server.set_order_status(
        [GUID, GUID2], "paid", dry_run=False))
    assert [e["order_id_input"] for e in r["resolve_errors"]] == [GUID2]
    assert r["rate_limited"] == []


def test_runtime_error_on_readback_still_produces_a_readback_error():
    import server
    patches, _, _ = _mock2(status=0, readback_runtime_errors=(GUID,))
    r = _run(patches, lambda: server.set_order_status([GUID], "paid", dry_run=False))
    row = r["results"][0]
    assert "readback_error" in row
    assert row.get("readback") != "rate_limited"


# ---- AC6: the proof constant ships unset --------------------------------

def test_the_unpaid_parks_constant_ships_unset():
    """Nothing in this repo has observed what ChangeStatus(0) does to an
    existing order. The constant must say so, not guess — this repo has
    already had to retract two confidently-generalised single-sample
    findings."""
    import server
    assert server.UNPAID_FLIP_PARKS_ORDER["state"] == "not_observed"
    assert server.UNPAID_FLIP_PARKS_ORDER["evidence"] is None


def test_every_consumer_goes_through_the_one_accessor():
    """No tool retypes the prediction text inline."""
    import server
    predicted, reason = server._unpaid_parks_prediction()
    assert predicted is None
    assert "not" in reason.lower() and "observed" in reason.lower()


# ---- AC7: the dry-run manifest derives its prediction from the constant --

@pytest.mark.parametrize("state,expected_parked,must_say", [
    ("parks",          True,  "will be parked"),
    ("does_not_park",  False, "will not be parked"),
    ("not_observed",   None,  "not yet observed"),
])
def test_unpaid_manifest_prediction_is_derived_from_the_constant(
        state, expected_parked, must_say):
    import server
    patches, _, _ = _mock2(status=1)
    with patch.dict(server.UNPAID_FLIP_PARKS_ORDER, {"state": state}, clear=False):
        r = _run(patches, lambda: server.set_order_status(
            [GUID], "unpaid", dry_run=True))
    row = r["manifest"][0]
    assert row["predicted_parked"] is expected_parked
    assert must_say in row["predicted_parked_reason"].lower()


def test_paid_manifest_makes_no_parked_prediction():
    import server
    patches, _, _ = _mock2(status=0)
    r = _run(patches, lambda: server.set_order_status([GUID], "paid", dry_run=True))
    assert "predicted_parked" not in r["manifest"][0]


# ---- AC8: the read-back warns when reality disagrees, in both directions -

def test_warns_when_parking_was_predicted_and_did_not_happen():
    import server
    patches, _, _ = _mock2(parked_before=False, parked_after=None, status=1)
    with patch.dict(server.UNPAID_FLIP_PARKS_ORDER, {"state": "parks"}, clear=False):
        r = _run(patches, lambda: server.set_order_status(
            [GUID], "unpaid", dry_run=False))
    row = r["results"][0]
    assert any("not parked" in w.lower() for w in row["warnings"])


def test_warns_when_parking_was_not_predicted_and_happened_anyway():
    import server
    patches, _, _ = _mock2(parked_before=False, parked_after=True, status=1)
    with patch.dict(server.UNPAID_FLIP_PARKS_ORDER,
                    {"state": "does_not_park"}, clear=False):
        r = _run(patches, lambda: server.set_order_status(
            [GUID], "unpaid", dry_run=False))
    row = r["results"][0]
    assert any("parked" in w.lower() for w in row["warnings"])
    assert any("dispatch queue" in w.lower() for w in row["warnings"])


def test_no_disagreement_warning_while_the_answer_is_unobserved():
    """With no prediction there is nothing to disagree with. Warning anyway
    would train the caller to ignore the warnings that matter."""
    import server
    patches, _, _ = _mock2(parked_before=False, parked_after=True, status=1)
    r = _run(patches, lambda: server.set_order_status([GUID], "unpaid", dry_run=False))
    assert not r["results"][0].get("warnings")
