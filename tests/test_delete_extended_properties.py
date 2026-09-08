"""
Tests for delete_extended_properties (issue #50) — the missing delete
counterpart to set_extended_properties, which is upsert-only.

Endpoint behaviour these tests encode:
  - Inventory/DeleteInventoryItemExtendedProperties returns 204 No Content,
    so it must go through call_linnworks_void, not call_linnworks.
  - The body is {"inventoryItemId": <guid>, "inventoryItemExtendedPropertyIds":
    [<pkRowId>, ...]} — a SINGLE item's row ids per call, unlike the sibling
    Create/Update endpoints, which take one flat list spanning many items.
    A multi-SKU batch therefore issues one call per distinct stock item.
"""
import pytest
from unittest.mock import patch

import server

SID1 = "11111111-0000-0000-0000-000000000001"
SID2 = "22222222-0000-0000-0000-000000000002"
ROW1 = "aaaaaaaa-0000-0000-0000-000000000001"
ROW2 = "bbbbbbbb-0000-0000-0000-000000000002"
ROW3 = "cccccccc-0000-0000-0000-000000000003"


def _prop_row(row_id, name, value, prop_type="Attribute"):
    return {"pkRowId": row_id, "ProperyName": name, "PropertyValue": value, "PropertyType": prop_type}


def _mocks(items_by_sku, props_by_sid, delete_effect=None, rate_limit_on=None):
    """
    items_by_sku:  {sku: stock_item_id or None (None -> not found)}
    props_by_sid:  {stock_item_id: [prop rows]}  -- mutated by a successful delete
    delete_effect: "noop" to simulate a delete that doesn't actually remove rows
    rate_limit_on: a set of things to raise RateLimitError for, e.g.
                   {"resolve:SKU-X", "read:<sid>", "delete:<sid>", "readback:<sid>"}
    """
    rate_limit_on = rate_limit_on or set()
    state = {"props": {sid: [dict(r) for r in rows] for sid, rows in props_by_sid.items()}}
    captured = {"void": [], "get": []}

    def call_linnworks(path, payload):
        if path.endswith("GetInventoryItem"):
            sku = payload.get("sku")
            if f"resolve:{sku}" in rate_limit_on:
                raise server.RateLimitError("quota exceeded")
            sid = items_by_sku.get(sku)
            if sid is None:
                raise RuntimeError("HTTP 400 — no such SKU")
            return {"StockItemId": sid, "ItemTitle": "Test Item"}
        if path.endswith("GetInventoryItemExtendedProperties"):
            sid = payload.get("inventoryItemId")
            captured["get"].append(sid)
            if f"read:{sid}" in rate_limit_on and captured["get"].count(sid) == 1:
                raise server.RateLimitError("quota exceeded")
            if f"readback:{sid}" in rate_limit_on and captured["get"].count(sid) > 1:
                raise server.RateLimitError("quota exceeded")
            return [dict(r) for r in state["props"].get(sid, [])]
        raise AssertionError(f"Unexpected call_linnworks: {path}")

    def call_linnworks_void(path, payload):
        captured["void"].append((path, payload))
        sid = payload.get("inventoryItemId")
        if f"delete:{sid}" in rate_limit_on:
            raise server.RateLimitError("quota exceeded")
        if delete_effect == "noop":
            return
        gone = set(payload["inventoryItemExtendedPropertyIds"])
        state["props"][sid] = [r for r in state["props"].get(sid, []) if r["pkRowId"] not in gone]

    return (
        [
            patch("server.call_linnworks", side_effect=call_linnworks),
            patch("server.call_linnworks_void", side_effect=call_linnworks_void),
        ],
        captured,
        state,
    )


def _run(patches, fn, *args, **kwargs):
    for p in patches:
        p.start()
    try:
        return fn(*args, **kwargs)
    finally:
        for p in patches:
            p.stop()


# ── criterion 1: tool registration ──────────────────────────────────────────

def test_tool_is_registered_and_count_rises_by_one():
    import asyncio
    names = [t.name for t in asyncio.run(server.mcp.list_tools())]
    assert "delete_extended_properties" in names


# ── criterion 2: validation before any API call ─────────────────────────────

def test_missing_sku_raises_valueerror_naming_index():
    with pytest.raises(ValueError, match=r"properties\[0\]"):
        server.delete_extended_properties([{"property_name": "Colour"}])


def test_missing_property_name_raises_valueerror_naming_index_and_sku():
    with pytest.raises(ValueError, match=r"properties\[0\].*SKU-1"):
        server.delete_extended_properties([{"sku": "SKU-1"}])


def test_validation_error_happens_before_any_api_call():
    with patch("server.call_linnworks") as mock_call:
        with pytest.raises(ValueError):
            server.delete_extended_properties([{"sku": "SKU-1"}])
        mock_call.assert_not_called()


# ── criterion 3: injection checks ────────────────────────────────────────────

def test_injection_check_called_on_property_name_and_expected_value():
    with patch("server._check_injection") as mock_inj:
        patches, captured, state = _mocks({"SKU-1": SID1}, {SID1: [_prop_row(ROW1, "Colour", "Red")]})
        _run(patches, server.delete_extended_properties,
             [{"sku": "SKU-1", "property_name": "Colour", "expected_value": "Red"}])
    calls = [c.args for c in mock_inj.call_args_list]
    assert ("property_name", "Colour") in calls
    assert ("expected_value", "Red") in calls


def test_injection_check_not_called_on_expected_value_when_absent():
    with patch("server._check_injection") as mock_inj:
        patches, captured, state = _mocks({"SKU-1": SID1}, {SID1: [_prop_row(ROW1, "Colour", "Red")]})
        _run(patches, server.delete_extended_properties,
             [{"sku": "SKU-1", "property_name": "Colour"}])
    fields = [c.args[0] for c in mock_inj.call_args_list]
    assert "expected_value" not in fields


# ── criterion 4: exact, case-sensitive property-name matching ───────────────

def test_differently_cased_name_does_not_match():
    patches, captured, state = _mocks({"SKU-1": SID1}, {SID1: [_prop_row(ROW1, "Colour", "Red")]})
    r = _run(patches, server.delete_extended_properties,
              [{"sku": "SKU-1", "property_name": "colour"}])
    row = r["manifest"][0]
    assert row["status"] == "unresolved"
    assert row["action"] is None
    assert "not found" in row["reason"]


# ── criterion 5: dry_run default, manifest shape, zero delete calls ─────────

def test_dry_run_is_default_and_writes_nothing():
    patches, captured, state = _mocks({"SKU-1": SID1}, {SID1: [_prop_row(ROW1, "Colour", "Red")]})
    r = _run(patches, server.delete_extended_properties,
              [{"sku": "SKU-1", "property_name": "Colour"}])
    assert r["dry_run"] is True
    assert captured["void"] == []
    row = r["manifest"][0]
    for key in ("sku", "stock_item_id", "property_name", "current_value", "row_id", "status"):
        assert key in row
    assert row["sku"] == "SKU-1"
    assert row["stock_item_id"] == SID1
    assert row["property_name"] == "Colour"
    assert row["current_value"] == "Red"
    assert row["row_id"] == ROW1
    assert row["status"] == "resolved"
    assert row["action"] == "delete"


def test_explicit_dry_run_true_also_writes_nothing():
    patches, captured, state = _mocks({"SKU-1": SID1}, {SID1: [_prop_row(ROW1, "Colour", "Red")]})
    r = _run(patches, server.delete_extended_properties,
              [{"sku": "SKU-1", "property_name": "Colour"}], dry_run=True)
    assert r["dry_run"] is True
    assert captured["void"] == []


# ── criterion 6: unresolved SKU / property, skipped, no exception ───────────

def test_unresolved_sku_produces_unresolved_row_and_other_rows_still_proceed():
    patches, captured, state = _mocks(
        {"SKU-1": SID1, "SKU-BOGUS": None},
        {SID1: [_prop_row(ROW1, "Colour", "Red")]},
    )
    r = _run(patches, server.delete_extended_properties, [
        {"sku": "SKU-BOGUS", "property_name": "Colour"},
        {"sku": "SKU-1", "property_name": "Colour"},
    ])
    bogus_row = next(m for m in r["manifest"] if m["sku"] == "SKU-BOGUS")
    good_row = next(m for m in r["manifest"] if m["sku"] == "SKU-1")
    assert bogus_row["status"] == "unresolved"
    assert bogus_row["stock_item_id"] is None
    assert good_row["status"] == "resolved"
    assert good_row["action"] == "delete"


def test_property_not_present_on_item_is_unresolved_not_fatal():
    patches, captured, state = _mocks({"SKU-1": SID1}, {SID1: [_prop_row(ROW1, "Colour", "Red")]})
    r = _run(patches, server.delete_extended_properties,
              [{"sku": "SKU-1", "property_name": "Material"}])
    row = r["manifest"][0]
    assert row["status"] == "unresolved"
    assert "Material" in row["reason"]
    assert "not found" in row["reason"]


# ── criterion 7: expected_value guard ────────────────────────────────────────

def test_expected_value_mismatch_blocks_the_row_and_shows_both_values():
    patches, captured, state = _mocks({"SKU-1": SID1}, {SID1: [_prop_row(ROW1, "Colour", "Red")]})
    r = _run(patches, server.delete_extended_properties,
              [{"sku": "SKU-1", "property_name": "Colour", "expected_value": "Blue"}])
    row = r["manifest"][0]
    assert row["status"] == "resolved"
    assert row["action"] == "blocked"
    assert row["expected_value"] == "Blue"
    assert row["current_value"] == "Red"
    assert "not found" not in row["reason"]
    assert "does not match" in row["reason"]


def test_expected_value_mismatch_is_not_deleted_on_live_run():
    patches, captured, state = _mocks({"SKU-1": SID1}, {SID1: [_prop_row(ROW1, "Colour", "Red")]})
    r = _run(patches, server.delete_extended_properties,
              [{"sku": "SKU-1", "property_name": "Colour", "expected_value": "Blue"}],
              dry_run=False)
    assert captured["void"] == []
    assert state["props"][SID1] == [_prop_row(ROW1, "Colour", "Red")]
    assert r["deleted"] == []


def test_expected_value_match_proceeds_to_delete():
    patches, captured, state = _mocks({"SKU-1": SID1}, {SID1: [_prop_row(ROW1, "Colour", "Red")]})
    r = _run(patches, server.delete_extended_properties,
              [{"sku": "SKU-1", "property_name": "Colour", "expected_value": "Red"}],
              dry_run=False)
    assert r["deleted"] == [ROW1]


# ── criterion 8: duplicate-name rows ─────────────────────────────────────────

def test_two_rows_same_name_all_included_in_manifest_and_deleted():
    patches, captured, state = _mocks(
        {"SKU-1": SID1},
        {SID1: [_prop_row(ROW1, "Tag", "one"), _prop_row(ROW2, "Tag", "two")]},
    )
    r = _run(patches, server.delete_extended_properties,
              [{"sku": "SKU-1", "property_name": "Tag"}], dry_run=False)
    to_delete_ids = {m["row_id"] for m in r["manifest"] if m["action"] == "delete"}
    assert to_delete_ids == {ROW1, ROW2}
    assert set(r["deleted"]) == {ROW1, ROW2}
    assert r["still_present"] == []
    assert state["props"][SID1] == []


# ── criterion 9: payload shape, per-item call, correct helper ───────────────

def test_live_run_groups_by_stock_item_and_uses_the_exact_payload_shape():
    patches, captured, state = _mocks(
        {"SKU-1": SID1, "SKU-2": SID2},
        {
            SID1: [_prop_row(ROW1, "Colour", "Red"), _prop_row(ROW2, "Material", "Wood")],
            SID2: [_prop_row(ROW3, "Colour", "Blue")],
        },
    )
    r = _run(patches, server.delete_extended_properties, [
        {"sku": "SKU-1", "property_name": "Colour"},
        {"sku": "SKU-1", "property_name": "Material"},
        {"sku": "SKU-2", "property_name": "Colour"},
    ], dry_run=False)

    assert len(captured["void"]) == 2  # one call per distinct stock item
    calls_by_sid = {payload["inventoryItemId"]: (path, payload) for path, payload in captured["void"]}

    path1, payload1 = calls_by_sid[SID1]
    assert path1.endswith("Inventory/DeleteInventoryItemExtendedProperties")
    assert set(payload1.keys()) == {"inventoryItemId", "inventoryItemExtendedPropertyIds"}
    assert payload1["inventoryItemId"] == SID1
    assert set(payload1["inventoryItemExtendedPropertyIds"]) == {ROW1, ROW2}

    path2, payload2 = calls_by_sid[SID2]
    assert payload2["inventoryItemId"] == SID2
    assert payload2["inventoryItemExtendedPropertyIds"] == [ROW3]

    assert set(r["deleted"]) == {ROW1, ROW2, ROW3}


def test_live_run_uses_call_linnworks_void_not_call_linnworks():
    patches, captured, state = _mocks({"SKU-1": SID1}, {SID1: [_prop_row(ROW1, "Colour", "Red")]})
    for p in patches:
        p.start()
    try:
        with patch("server.call_linnworks_void", wraps=server.call_linnworks_void) as void_spy:
            with patch("server.call_linnworks", side_effect=[
                {"StockItemId": SID1, "ItemTitle": "Test Item"},
                [dict(_prop_row(ROW1, "Colour", "Red"))],
                [],
            ]) as call_spy:
                r = server.delete_extended_properties(
                    [{"sku": "SKU-1", "property_name": "Colour"}], dry_run=False,
                )
        # call_linnworks_void must be the delete transport, never call_linnworks
        delete_paths = [c.args[0] for c in call_spy.call_args_list]
        assert not any(p.endswith("DeleteInventoryItemExtendedProperties") for p in delete_paths)
        assert void_spy.call_count == 1
        assert void_spy.call_args.args[0].endswith("DeleteInventoryItemExtendedProperties")
    finally:
        for p in patches:
            p.stop()


# ── criterion 10: fresh read-back, deleted vs still_present ─────────────────

def test_read_back_uses_a_fresh_call_and_confirms_deletion():
    patches, captured, state = _mocks({"SKU-1": SID1}, {SID1: [_prop_row(ROW1, "Colour", "Red")]})
    r = _run(patches, server.delete_extended_properties,
              [{"sku": "SKU-1", "property_name": "Colour"}], dry_run=False)
    # GetInventoryItemExtendedProperties called twice for SID1: once pre-write
    # (manifest build), once post-write (read-back) -- the read-back is fresh.
    assert captured["get"].count(SID1) == 2
    assert r["deleted"] == [ROW1]
    assert r["still_present"] == []


def test_read_back_that_still_returns_the_row_reports_still_present_not_deleted():
    """A 204/void success does not prove removal -- the read-back is the source
    of truth. If the endpoint no-ops, the row must NOT be reported as deleted."""
    patches, captured, state = _mocks(
        {"SKU-1": SID1}, {SID1: [_prop_row(ROW1, "Colour", "Red")]}, delete_effect="noop",
    )
    r = _run(patches, server.delete_extended_properties,
              [{"sku": "SKU-1", "property_name": "Colour"}], dry_run=False)
    assert r["deleted"] == []
    assert r["still_present"] == [ROW1]


# ── criterion 11: WRITE_THRESHOLDS + staging ─────────────────────────────────

def test_threshold_is_fifty():
    assert server.WRITE_THRESHOLDS["delete_extended_properties"] == 50


def test_batch_above_threshold_without_confirmed_count_stages_and_writes_nothing():
    props = [{"sku": f"SKU-{i}", "property_name": "Colour"} for i in range(51)]
    items_by_sku = {f"SKU-{i}": f"11111111-0000-0000-0000-{i:012d}" for i in range(51)}
    props_by_sid = {
        f"11111111-0000-0000-0000-{i:012d}": [_prop_row(f"22222222-0000-0000-0000-{i:012d}", "Colour", "Red")]
        for i in range(51)
    }
    patches, captured, state = _mocks(items_by_sku, props_by_sid)
    r = _run(patches, server.delete_extended_properties, props, dry_run=False)
    assert r["staged"] is True
    assert captured["void"] == []


def test_mismatched_confirmed_count_returns_error_and_writes_nothing():
    props = [{"sku": f"SKU-{i}", "property_name": "Colour"} for i in range(51)]
    items_by_sku = {f"SKU-{i}": f"11111111-0000-0000-0000-{i:012d}" for i in range(51)}
    props_by_sid = {
        f"11111111-0000-0000-0000-{i:012d}": [_prop_row(f"22222222-0000-0000-0000-{i:012d}", "Colour", "Red")]
        for i in range(51)
    }
    patches, captured, state = _mocks(items_by_sku, props_by_sid)
    r = _run(patches, server.delete_extended_properties, props, confirmed_count=50, dry_run=False)
    assert r["success"] is False
    assert captured["void"] == []


def test_confirmed_count_matching_resolved_rows_proceeds():
    props = [{"sku": f"SKU-{i}", "property_name": "Colour"} for i in range(51)]
    items_by_sku = {f"SKU-{i}": f"11111111-0000-0000-0000-{i:012d}" for i in range(51)}
    props_by_sid = {
        f"11111111-0000-0000-0000-{i:012d}": [_prop_row(f"22222222-0000-0000-0000-{i:012d}", "Colour", "Red")]
        for i in range(51)
    }
    patches, captured, state = _mocks(items_by_sku, props_by_sid)
    r = _run(patches, server.delete_extended_properties, props, confirmed_count=51, dry_run=False)
    assert r["dry_run"] is False
    assert len(r["deleted"]) == 51


# ── criterion 12: RateLimitError is never reported as "not found" ──────────

def test_rate_limit_while_resolving_sku_is_bucketed_not_labelled_a_miss():
    patches, captured, state = _mocks(
        {"SKU-1": SID1}, {SID1: [_prop_row(ROW1, "Colour", "Red")]},
        rate_limit_on={"resolve:SKU-1"},
    )
    r = _run(patches, server.delete_extended_properties,
              [{"sku": "SKU-1", "property_name": "Colour"}])
    assert r["complete"] is False
    assert len(r["rate_limited"]) == 1
    assert r["rate_limited"][0]["sku"] == "SKU-1"
    # It must not appear in manifest labelled "unresolved"/"not found".
    assert r["manifest"] == []


def test_rate_limit_while_reading_properties_is_bucketed_not_labelled_a_miss():
    patches, captured, state = _mocks(
        {"SKU-1": SID1}, {SID1: [_prop_row(ROW1, "Colour", "Red")]},
        rate_limit_on={"read:" + SID1},
    )
    r = _run(patches, server.delete_extended_properties,
              [{"sku": "SKU-1", "property_name": "Colour"}])
    assert r["complete"] is False
    assert len(r["rate_limited"]) == 1
    assert r["manifest"] == []


def test_clean_run_reports_complete_true():
    patches, captured, state = _mocks({"SKU-1": SID1}, {SID1: [_prop_row(ROW1, "Colour", "Red")]})
    r = _run(patches, server.delete_extended_properties,
              [{"sku": "SKU-1", "property_name": "Colour"}])
    assert r["complete"] is True
    assert r["rate_limited"] == []


# ── criterion 13: get_extended_properties gains row_id ──────────────────────

def test_get_extended_properties_returns_row_id_with_existing_keys_unchanged():
    def call_linnworks(path, payload):
        if path.endswith("GetInventoryItem"):
            return {"StockItemId": SID1, "ItemTitle": "Test Item"}
        if path.endswith("GetInventoryItemExtendedProperties"):
            return [_prop_row(ROW1, "Colour", "Red", "Attribute")]
        raise AssertionError(path)

    with patch("server.call_linnworks", side_effect=call_linnworks):
        r = server.get_extended_properties("SKU-1")

    prop = r["extended_properties"][0]
    assert prop["name"] == "Colour"
    assert prop["value"] == "Red"
    assert prop["type"] == "Attribute"
    assert prop["row_id"] == ROW1
