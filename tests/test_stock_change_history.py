"""
Tests for get_stock_change_history — stock movement history + dead-stock
summary (issue #33).

Endpoint behaviour these tests encode (all live-confirmed 6 Aug 2026):
  - Stock/GetItemChangesHistory REQUIRES locationId. The spec calls it optional
    ("If null then combined") but omitting it returns HTTP 400 "The request is
    invalid.", so there is no combined-across-locations mode.
  - pageNumber=-1 ("all pages" per the spec) returns HTTP 400 "Value must be at
    least 1" — real paging is the only option.
  - Rows come back NEWEST-FIRST, and `Level` is the level AFTER the change
    (verified on 15 consecutive row pairs: Level - ChangeQty == older Level).
  - The response carries NO ChangeSource field — the source lives in the
    free-text Note, so change_source is derived by _classify_change_source().
  - Past-the-end pages return an empty Data array with a 200 (unlike
    GetStockItemsFull, which 400s).

The derivation under test — out_of_stock_since is the OLDEST row in the current
unbroken trailing run of level_after == 0, not the newest row at zero — is the
core of the issue. The stacked-zero fixtures below are synthetic on purpose: no
run longer than 1 was observed at Default on this tenant, so the behaviour is
defensive and unit tests are the only way to pin it.
"""
import pytest
from datetime import datetime, timezone
from unittest.mock import patch

import server

SID = "aaaaaaaa-1111-2222-3333-444444444444"
ZERO_LOC = "00000000-0000-0000-0000-000000000000"
NOW = datetime(2026, 8, 6, 12, 0, 0, tzinfo=timezone.utc)


def _row(date, level, qty, note):
    """Build a raw StockItemChangeHistory row as Linnworks returns it."""
    return {
        "StockItemId": ZERO_LOC,  # the API really does echo the zero GUID here
        "Date": date,
        "Level": level,
        "StockValue": 0.0,
        "Note": note,
        "ChangeQty": qty,
        "ChangeValue": 0.0,
    }


def _fmt(rows):
    return [server._format_change_row(r) for r in rows]


# --- change_source classification -------------------------------------------

@pytest.mark.parametrize("note,expected", [
    ("Order 606666 ref: 026-3127867-5466751", "SALE"),
    ("Customer return for order 602944, ref: 09425CC6A612CB", "RETURN"),
    ("PO PO04012495866 delivered 1280 will@skatewarehouse.co.uk", "PO_DELIVERY"),
    ("PO  delivered 1 warehouse+03@thewarehousegroup.co.uk", "PO_DELIVERY"),
    ("PO PO04012495866 to OPEN. Due 1280 UserName: will@skatewarehouse.co.uk", "PO_BOOKING"),
    ("PO PO06102533297 deleted UserName: steve@thewarehousegroup.co.uk", "PO_DELETED"),
    ("PO PO16062236246 purchase item line update UserName: jo@x.co.uk", "PO_UPDATE"),
    ("Imported from file", "FILE_IMPORT"),
    ("FBA Sync By Channel SKU", "FBA_SYNC"),
    ("FBA Sync missing from report", "FBA_SYNC"),
    ("No quantity returned from channel", "FBA_SYNC"),
    ("Stock Count Adjustment UserName: warehouse+03@thewarehousegroup.co.uk", "STOCKTAKE"),
    ("DIRECT ADJUSTMENT BY steve@thewarehousegroup.co.uk", "ADJUSTMENT"),
    ("SCRAPPED BY RMA 107016/Order 564331", "SCRAP"),
    ("Record insertion", "OTHER"),
    ("", "OTHER"),
    (None, "OTHER"),
])
def test_classify_change_source(note, expected):
    """Every note shape observed live classifies to its canonical source."""
    assert server._classify_change_source(note) == expected


def test_classify_never_invents_a_source():
    """Unrecognised notes fall to OTHER rather than a wrong bucket."""
    assert server._classify_change_source("something entirely new") == "OTHER"
    assert server._classify_change_source("something entirely new") in server._KNOWN_CHANGE_SOURCES


# --- out_of_stock_since derivation ------------------------------------------

def test_out_of_stock_since_is_the_transition_not_the_newest_zero_row():
    """
    THE headline behaviour. An item dead since January still receives automated
    rows that rewrite 0; the summary must report the January transition, not the
    most recent zero row.
    """
    rows = _fmt([
        _row("2026-08-01T00:00:00Z", 0, 0, "Imported from file"),
        _row("2026-06-01T00:00:00Z", 0, 0, "FBA Sync By Channel SKU"),
        _row("2026-01-15T00:00:00Z", 0, -1, "Order 500001 ref: X"),   # <- the transition
        _row("2026-01-10T00:00:00Z", 1, 1, "PO  delivered 1 x@y.co.uk"),
    ])
    s = server._summarise_change_history(rows, current_level=0, truncated=False, now=NOW)

    assert s["out_of_stock_since"] == "2026-01-15T00:00:00Z"
    assert s["days_out_of_stock"] == 203
    # The naive reading would have been the 1 Aug import — 5 days.
    assert s["days_out_of_stock"] > 200


def test_zero_run_stops_at_the_first_non_zero_level():
    rows = _fmt([
        _row("2026-07-01T00:00:00Z", 0, -1, "Order 1 ref: X"),
        _row("2026-06-01T00:00:00Z", 3, 3, "PO  delivered 3 x@y.co.uk"),
        _row("2026-05-01T00:00:00Z", 0, -1, "Order 2 ref: Y"),  # earlier zero, must be ignored
    ])
    s = server._summarise_change_history(rows, current_level=0, truncated=False, now=NOW)
    assert s["out_of_stock_since"] == "2026-07-01T00:00:00Z"


def test_in_stock_item_has_no_out_of_stock_date():
    rows = _fmt([
        _row("2026-08-04T00:00:00Z", 167, -1, "Order 607561 ref: A"),
        _row("2026-08-03T00:00:00Z", 168, -1, "Order 607306 ref: B"),
    ])
    s = server._summarise_change_history(rows, current_level=167, truncated=False, now=NOW)
    assert s["out_of_stock_since"] is None
    assert s["days_out_of_stock"] is None


def test_current_level_overrides_history_when_they_disagree():
    """
    Stock levels are authoritative. If current stock says >0 the item is not out
    of stock, whatever the history tail looks like — and the disagreement is
    surfaced rather than silently resolved.
    """
    rows = _fmt([_row("2026-07-01T00:00:00Z", 0, -1, "Order 1 ref: X")])
    s = server._summarise_change_history(rows, current_level=5, truncated=False, now=NOW)
    assert s["out_of_stock_since"] is None
    assert s["level_mismatch"] is True
    assert s["level_from_history"] == 0
    assert s["current_level"] == 5


def test_truncated_trailing_zero_run_is_flagged_as_a_lower_bound():
    """
    If every row read is at zero AND the read was truncated, the true transition
    is older than anything fetched — the date must be reported as a lower bound.
    """
    rows = _fmt([
        _row("2026-08-01T00:00:00Z", 0, 0, "Imported from file"),
        _row("2026-07-01T00:00:00Z", 0, 0, "Imported from file"),
    ])
    s = server._summarise_change_history(rows, current_level=0, truncated=True, now=NOW)
    assert s["out_of_stock_since"] == "2026-07-01T00:00:00Z"
    assert s["out_of_stock_since_is_lower_bound"] is True


def test_untruncated_zero_run_is_not_a_lower_bound():
    rows = _fmt([
        _row("2026-08-01T00:00:00Z", 0, 0, "Imported from file"),
        _row("2026-07-01T00:00:00Z", 0, 0, "Imported from file"),
    ])
    s = server._summarise_change_history(rows, current_level=0, truncated=False, now=NOW)
    assert s["out_of_stock_since_is_lower_bound"] is False


def test_never_stocked_item_reports_a_date_but_no_movement():
    """
    A single level-0 file-import row (live: 10020ZZ058GE00) means the item has
    been at zero since we first saw it — a date, but last_movement_date None.
    """
    rows = _fmt([_row("2026-03-24T14:21:53Z", 0, 0, "Imported from file")])
    s = server._summarise_change_history(rows, current_level=0, truncated=False, now=NOW)
    assert s["out_of_stock_since"] == "2026-03-24T14:21:53Z"
    assert s["last_movement_date"] is None
    assert s["last_real_movement_date"] is None


# --- movement / source-derived dates ----------------------------------------

def test_last_real_movement_ignores_automated_sources():
    """
    The filter the issue asked for: automated rows must not reset the dead-stock
    clock. They still count as movement, just not as REAL movement.
    """
    rows = _fmt([
        _row("2026-08-01T00:00:00Z", 4, -1, "FBA Sync By Channel SKU"),
        _row("2026-07-01T00:00:00Z", 5, -1, "Imported from file"),
        _row("2026-02-01T00:00:00Z", 6, -1, "Order 1 ref: X"),
    ])
    s = server._summarise_change_history(rows, current_level=4, truncated=False, now=NOW)
    assert s["last_movement_date"] == "2026-08-01T00:00:00Z"
    assert s["last_real_movement_date"] == "2026-02-01T00:00:00Z"


def test_zero_quantity_rows_are_not_movement():
    """A PO booking / file import that changed nothing is not a stock movement."""
    rows = _fmt([
        _row("2026-08-01T00:00:00Z", 2, 0, "PO  to OPEN. Due 5 UserName: x@y.co.uk"),
        _row("2026-02-01T00:00:00Z", 2, -1, "Order 1 ref: X"),
    ])
    s = server._summarise_change_history(rows, current_level=2, truncated=False, now=NOW)
    assert s["last_movement_date"] == "2026-02-01T00:00:00Z"


def test_last_sale_and_last_received_dates():
    rows = _fmt([
        _row("2026-08-01T00:00:00Z", 1, -1, "Order 9 ref: X"),
        _row("2026-07-01T00:00:00Z", 2, 1, "Customer return for order 5, ref: Y"),
        _row("2026-06-01T00:00:00Z", 1, 1, "PO  delivered 1 x@y.co.uk"),
    ])
    s = server._summarise_change_history(rows, current_level=1, truncated=False, now=NOW)
    assert s["last_sale_date"] == "2026-08-01T00:00:00Z"
    # RETURN counts as received, and is newer than the PO delivery
    assert s["last_received_date"] == "2026-07-01T00:00:00Z"


def test_level_after_semantics_are_preserved_in_the_output_row():
    r = server._format_change_row(_row("2026-08-01T00:00:00Z", 40, -123, "Stock Count Adjustment UserName: x@y"))
    assert r["level_after"] == 40
    assert r["quantity"] == -123
    assert r["change_source"] == "STOCKTAKE"
    assert r["note"].startswith("Stock Count Adjustment")


# --- paging ------------------------------------------------------------------

def test_fetch_pages_until_short_page():
    """A short page ends paging cleanly (past-the-end returns empty Data + 200)."""
    pages = {1: [_row("2026-08-01T00:00:00Z", 1, -1, "Order 1 ref: X")] * 200,
             2: [_row("2026-07-01T00:00:00Z", 2, -1, "Order 2 ref: Y")] * 30}

    def fake_get(path, params):
        assert path == "Stock/GetItemChangesHistory"
        # locationId is REQUIRED — the tool must always send it
        assert params["locationId"] == ZERO_LOC
        assert params["pageNumber"] >= 1  # pageNumber=-1 is rejected by Linnworks
        return {"TotalEntries": 230, "Data": pages.get(params["pageNumber"], [])}

    with patch.object(server, "call_linnworks_get", side_effect=fake_get):
        rows, total, truncated = server._fetch_change_history_rows(SID, ZERO_LOC, max_pages=10)
    assert len(rows) == 230
    assert total == 230
    assert truncated is False


def test_fetch_stops_at_max_pages_and_reports_truncation():
    def fake_get(path, params):
        return {"TotalEntries": 5000, "Data": [_row("2026-08-01T00:00:00Z", 1, -1, "Order 1 ref: X")] * 200}

    with patch.object(server, "call_linnworks_get", side_effect=fake_get):
        rows, total, truncated = server._fetch_change_history_rows(SID, ZERO_LOC, max_pages=2)
    assert len(rows) == 400
    assert truncated is True


# --- tool-level behaviour ----------------------------------------------------

def _patch_tool(history_rows, levels):
    """Patch the three calls the tool makes: resolve, stock levels, history."""
    def fake_levels(path, payload):
        assert path == "Stock/GetStockLevel_Batch"
        return [{
            "pkStockItemId": SID,
            "StockItemLevels": [
                {"Location": {"StockLocationId": lid, "LocationName": name}, "StockLevel": qty}
                for lid, name, qty in levels
            ],
        }]

    def fake_get(path, params=None):
        if path == "Inventory/GetStockLocations":
            return [{"StockLocationId": lid, "LocationName": name} for lid, name, _ in levels]
        return {"TotalEntries": len(history_rows.get(params["locationId"], [])),
                "Data": history_rows.get(params["locationId"], [])}

    return (
        patch.object(server, "_resolve_sku_to_id", return_value=SID),
        patch.object(server, "call_linnworks", side_effect=fake_levels),
        patch.object(server, "call_linnworks_get", side_effect=fake_get),
    )


def test_tool_single_location_summary():
    rows = [
        _row("2026-06-24T13:55:54Z", 0, -1, "Order 604143 ref: X"),
        _row("2026-06-24T10:27:59Z", 1, 1, "Customer return for order 602944, ref: Y"),
    ]
    p1, p2, p3 = _patch_tool({ZERO_LOC: rows}, [(ZERO_LOC, "Default", 0)])
    with p1, p2, p3:
        out = server.get_stock_change_history("DEAD-SKU")
    res = out["results"][0]
    assert out["scope"] == "location"
    assert res["current_level"] == 0
    assert res["out_of_stock_since"] == "2026-06-24T13:55:54Z"
    assert res["movements"][0]["change_source"] == "SALE"


def test_tool_accepts_a_bare_string_sku():
    p1, p2, p3 = _patch_tool({ZERO_LOC: []}, [(ZERO_LOC, "Default", 0)])
    with p1, p2, p3:
        out = server.get_stock_change_history("ONE-SKU")
    assert out["count"] == 1
    assert out["results"][0]["sku"] == "ONE-SKU"


def test_tool_filters_movements_but_not_the_zero_derivation():
    """
    Excluding FBA_SYNC must not change out_of_stock_since — the level series is
    derived from raw rows because automated rows are genuine decrements here.
    """
    rows = [
        _row("2026-07-31T00:00:00Z", 0, -1, "No quantity returned from channel"),
        _row("2026-07-26T00:00:00Z", 1, -1, "FBA Sync By Channel SKU"),
        _row("2026-01-01T00:00:00Z", 2, -1, "Order 1 ref: X"),
    ]
    p1, p2, p3 = _patch_tool({ZERO_LOC: rows}, [(ZERO_LOC, "Default", 0)])
    with p1, p2, p3:
        unfiltered = server.get_stock_change_history("S")
        filtered = server.get_stock_change_history("S", exclude_change_sources=["FBA_SYNC"])

    assert unfiltered["results"][0]["out_of_stock_since"] == "2026-07-31T00:00:00Z"
    assert filtered["results"][0]["out_of_stock_since"] == "2026-07-31T00:00:00Z"
    # only the movement list shrank
    assert len(filtered["results"][0]["movements"]) == 1
    assert filtered["results"][0]["movements"][0]["change_source"] == "SALE"


def test_tool_rejects_unknown_change_source():
    with pytest.raises(ValueError, match="Unknown change_sources"):
        server.get_stock_change_history("S", change_sources=["NOT_A_SOURCE"])
    with pytest.raises(ValueError, match="Unknown exclude_change_sources"):
        server.get_stock_change_history("S", exclude_change_sources=["NOPE"])


def test_tool_unresolvable_sku_does_not_abort_the_batch():
    def fake_resolve(sku, cache=None):
        if sku == "BAD":
            raise RuntimeError("Could not determine inventory item id from SKU")
        return SID

    _, p2, p3 = _patch_tool({ZERO_LOC: []}, [(ZERO_LOC, "Default", 0)])
    with patch.object(server, "_resolve_sku_to_id", side_effect=fake_resolve), p2, p3:
        out = server.get_stock_change_history(["BAD", "GOOD"])
    assert out["count"] == 1
    assert out["unresolved_count"] == 1
    assert out["unresolved"][0]["sku"] == "BAD"
    assert "unarchive" in out["unresolved"][0]["hint"].lower()


def test_all_locations_takes_the_LATEST_zero_date():
    """
    "Zero everywhere since X" is the LATEST per-location zero date — every
    location has only been at zero since the last one of them hit zero. Taking
    the earliest (or Default alone) would overstate the dead time.
    """
    LOC_B = "bbbbbbbb-0000-0000-0000-000000000001"
    rows = {
        ZERO_LOC: [_row("2026-06-24T00:00:00Z", 0, -1, "Order 1 ref: X"),
                   _row("2026-06-01T00:00:00Z", 1, 1, "PO  delivered 1 x@y")],
        LOC_B:    [_row("2026-07-31T00:00:00Z", 0, -1, "FBA Sync By Channel SKU"),
                   _row("2026-07-01T00:00:00Z", 1, 1, "FBA Sync By Channel SKU")],
    }
    p1, p2, p3 = _patch_tool(rows, [(ZERO_LOC, "Default", 0), (LOC_B, "FBA", 0)])
    with p1, p2, p3:
        out = server.get_stock_change_history("S", all_locations=True, include_movements=False)
    res = out["results"][0]
    assert res["zero_at_all_locations"] is True
    assert res["zero_at_all_locations_since"] == "2026-07-31T00:00:00Z"
    assert res["locations_with_history"] == 2


def test_all_locations_no_date_when_stock_remains_somewhere():
    LOC_B = "bbbbbbbb-0000-0000-0000-000000000001"
    rows = {
        ZERO_LOC: [_row("2026-06-24T00:00:00Z", 0, -1, "Order 1 ref: X")],
        LOC_B:    [_row("2026-07-31T00:00:00Z", 9, 9, "PO  delivered 9 x@y")],
    }
    p1, p2, p3 = _patch_tool(rows, [(ZERO_LOC, "Default", 0), (LOC_B, "Store", 9)])
    with p1, p2, p3:
        out = server.get_stock_change_history("S", all_locations=True, include_movements=False)
    res = out["results"][0]
    assert res["zero_at_all_locations"] is False
    assert res["zero_at_all_locations_since"] is None


def test_max_movements_truncates_detail_but_not_the_summary():
    rows = [_row(f"2026-0{i}-01T00:00:00Z", 5, -1, f"Order {i} ref: X") for i in range(1, 8)]
    p1, p2, p3 = _patch_tool({ZERO_LOC: rows}, [(ZERO_LOC, "Default", 5)])
    with p1, p2, p3:
        out = server.get_stock_change_history("S", max_movements=2)
    res = out["results"][0]
    assert len(res["movements"]) == 2
    assert res["movements_truncated"] is True
    assert res["movement_count"] == 7  # summary still spans everything read


def test_include_movements_false_omits_rows():
    rows = [_row("2026-08-01T00:00:00Z", 5, -1, "Order 1 ref: X")]
    p1, p2, p3 = _patch_tool({ZERO_LOC: rows}, [(ZERO_LOC, "Default", 5)])
    with p1, p2, p3:
        out = server.get_stock_change_history("S", include_movements=False)
    assert "movements" not in out["results"][0]
    assert out["results"][0]["last_sale_date"] == "2026-08-01T00:00:00Z"


def test_empty_sku_list_returns_an_error_not_a_crash():
    out = server.get_stock_change_history([])
    assert "error" in out
    assert out["count"] == 0
