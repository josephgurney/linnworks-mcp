"""
Tests for list_inventory_items — bulk active-inventory enumeration (issue #32).

Endpoint behaviour these tests encode (all live-confirmed 5 Aug 2026):
  - Stock/GetStockItemsFull has NO TotalEntries and NO top-level Quantity, and
    signals "past the last page" with HTTP 400 "No items found with given
    filter" — an end-of-results marker that must terminate auto-paging cleanly
    rather than surfacing as an error.
  - loadCompositeParents / loadVariationParents are INCLUDE flags: false makes
    those parents ABSENT from the result set (live: the vnm-catnip family
    returns 8 items with both true, 1 with both false).
  - dataRequirements is a STRING enum here ("StockLevels"/"Supplier"/…), not the
    integer [1] that GetStockItemsFullByIds wants. "StockLevels" populates a
    PER-LOCATION array; quantities are derived by summing it (cross-checked live
    against Stock/GetStockItems: Default 1 + Keen 6 == GET Quantity 7, and
    InOrders == the GET's InOrder).
  - "StockLevels": [] comes back BOTH when levels weren't requested and when an
    item has no stock rows, so the loaded-ness flag must be passed in, not
    inferred — unread must read as None, never as a zero that would pass a
    zero-stock cleanup filter.
  - The model carries IsVariationParent but NOT IsCompositeParent; that field is
    filled only on request, from the cached composite index.
"""
import pytest
from unittest.mock import patch

import server

DEFAULT_LOC = "00000000-0000-0000-0000-000000000000"
KEEN_LOC = "5bf788b5-db2b-4e6e-8ba8-4886cd82c93d"

SID_MULTI = "aaaaaaaa-0000-0000-0000-000000000001"
SID_ZERO = "bbbbbbbb-0000-0000-0000-000000000002"
SID_NOROWS = "cccccccc-0000-0000-0000-000000000003"


def _level(loc_id, name, level, available=None, in_order=0, due=0):
    return {
        "Location": {"StockLocationId": loc_id, "LocationName": name},
        "StockLevel": level,
        "Available": level if available is None else available,
        "InOrders": in_order,
        "Due": due,
        "MinimumLevel": 0,
    }


# MULTI has stock in two locations (1 + 6 = 7, mirroring the live cross-check);
# ZERO is at zero in both; NOROWS has no stock rows at all.
CATALOGUE = [
    {
        "StockItemId": SID_MULTI, "ItemNumber": "MULTI-1", "ItemTitle": "Multi Location",
        "BarcodeNumber": "111", "CategoryName": "Decks", "CategoryId": "cat-1",
        "PurchasePrice": 10.0, "IsVariationParent": False,
        "StockLevels": [
            _level(DEFAULT_LOC, "Default", 1),
            _level(KEEN_LOC, "Keen", 6, in_order=2, due=3),
        ],
    },
    {
        "StockItemId": SID_ZERO, "ItemNumber": "ZERO-1", "ItemTitle": "Dead Stock",
        "BarcodeNumber": "222", "CategoryName": "Wheels", "CategoryId": "cat-2",
        "PurchasePrice": 5.0, "IsVariationParent": True,
        "StockLevels": [
            _level(DEFAULT_LOC, "Default", 0),
            _level(KEEN_LOC, "Keen", 0),
        ],
    },
    {
        "StockItemId": SID_NOROWS, "ItemNumber": "NOROWS-1", "ItemTitle": "No Stock Rows",
        "BarcodeNumber": "333", "CategoryName": "Wheels", "CategoryId": "cat-2",
        "PurchasePrice": 7.0, "IsVariationParent": False,
        "StockLevels": [],
    },
]


def _fake_api(pages, calls=None):
    """
    Fake Stock/GetStockItemsFull: `pages` maps page number -> rows. A page not in
    the map raises the real HTTP 400 end-of-results error verbatim.
    """
    def _call(method_path, payload):
        assert method_path == "Stock/GetStockItemsFull"
        if calls is not None:
            calls.append(payload)
        page = payload["pageNumber"]
        if page not in pages:
            raise RuntimeError(
                'Linnworks Stock/GetStockItemsFull failed: HTTP 400 — '
                '{"Code":"-","Message":"No items found with given filter."}'
            )
        rows = pages[page]
        if not payload.get("dataRequirements"):
            # The endpoint returns an empty array when levels aren't requested.
            rows = [dict(r, StockLevels=[]) for r in rows]
        return rows
    return _call


# --- quantity derivation -----------------------------------------------------

def test_quantities_are_summed_across_locations():
    """No top-level Quantity exists — it must be summed from StockLevels."""
    with patch("server.call_linnworks", _fake_api({1: CATALOGUE})):
        r = server.list_inventory_items(per_page=200)
    multi = next(i for i in r["items"] if i["sku"] == "MULTI-1")
    assert multi["quantity"] == 7      # 1 (Default) + 6 (Keen)
    assert multi["available"] == 7
    assert multi["in_order"] == 2
    assert multi["due"] == 3
    assert [l["location_name"] for l in multi["locations"]] == ["Default", "Keen"]


def test_location_id_scopes_quantities_to_one_location():
    with patch("server.call_linnworks", _fake_api({1: CATALOGUE})):
        r = server.list_inventory_items(per_page=200, location_id=KEEN_LOC)
    multi = next(i for i in r["items"] if i["sku"] == "MULTI-1")
    assert multi["quantity"] == 6
    assert len(multi["locations"]) == 1
    assert multi["locations"][0]["location_name"] == "Keen"


def test_location_id_match_is_case_insensitive():
    with patch("server.call_linnworks", _fake_api({1: CATALOGUE})):
        r = server.list_inventory_items(per_page=200, location_id=KEEN_LOC.upper())
    multi = next(i for i in r["items"] if i["sku"] == "MULTI-1")
    assert multi["quantity"] == 6


def test_item_with_no_row_at_scoped_location_reports_zero():
    """Scoping to a location the item has no row at is genuinely zero, not None."""
    with patch("server.call_linnworks", _fake_api({1: CATALOGUE})):
        r = server.list_inventory_items(per_page=200, location_id="dddddddd-0000-0000-0000-00000000000f")
    assert all(i["quantity"] == 0 for i in r["items"])
    assert all(i["locations"] == [] for i in r["items"])


def test_unread_levels_are_none_not_zero():
    """
    The critical distinction: the API returns "StockLevels": [] both when levels
    weren't requested and when an item has none. Unread must be None so it can
    never pass a zero-stock cleanup filter as if it were dead stock.
    """
    with patch("server.call_linnworks", _fake_api({1: CATALOGUE})):
        unread = server.list_inventory_items(per_page=200, include_stock_levels=False)
        read = server.list_inventory_items(per_page=200)
    assert all(i["quantity"] is None for i in unread["items"])
    assert all(i["locations"] == [] for i in unread["items"])
    # Same shape on the wire, but an item read as having no rows is a real zero.
    norows = next(i for i in read["items"] if i["sku"] == "NOROWS-1")
    assert norows["quantity"] == 0


def test_data_requirements_uses_the_string_enum():
    """StockLevels is a string enum here — NOT the integer [1] of the ByIds sibling."""
    calls = []
    with patch("server.call_linnworks", _fake_api({1: CATALOGUE}, calls)):
        server.list_inventory_items(per_page=200)
        server.list_inventory_items(per_page=200, include_stock_levels=False)
    assert calls[0]["dataRequirements"] == ["StockLevels"]
    assert calls[1]["dataRequirements"] == []


# --- zero-stock filter -------------------------------------------------------

def test_zero_stock_only_filters_to_dead_stock():
    with patch("server.call_linnworks", _fake_api({1: CATALOGUE})):
        r = server.list_inventory_items(per_page=200, zero_stock_only=True)
    assert {i["sku"] for i in r["items"]} == {"ZERO-1", "NOROWS-1"}
    assert r["scanned_count"] == 3
    assert r["matched_count"] == 2


def test_zero_stock_only_respects_location_scope():
    """An item dead at one location but alive elsewhere is only dead in that scope."""
    with patch("server.call_linnworks", _fake_api({1: CATALOGUE})):
        everywhere = server.list_inventory_items(per_page=200, zero_stock_only=True)
        at_default = server.list_inventory_items(
            per_page=200, zero_stock_only=True, location_id=DEFAULT_LOC)
    assert "MULTI-1" not in {i["sku"] for i in everywhere["items"]}
    # MULTI-1 has 1 at Default, so it is still not zero there...
    assert "MULTI-1" not in {i["sku"] for i in at_default["items"]}
    # ...but scoping to a location it has no row at makes everything zero.
    with patch("server.call_linnworks", _fake_api({1: CATALOGUE})):
        elsewhere = server.list_inventory_items(
            per_page=200, zero_stock_only=True, location_id="dddddddd-0000-0000-0000-00000000000f")
    assert elsewhere["matched_count"] == 3


# --- paging ------------------------------------------------------------------

def test_page_past_end_terminates_cleanly_not_as_an_error():
    """HTTP 400 'No items found with given filter' is the end marker, not a failure."""
    pages = {1: CATALOGUE[:2], 2: CATALOGUE[2:]}  # page 3 raises the 400
    with patch("server.call_linnworks", _fake_api(pages)):
        r = server.list_inventory_items(all_pages=True, per_page=2)
    assert r["pages_fetched"] == 2
    assert r["scanned_count"] == 3
    assert r["complete"] is True


def test_single_page_mode_makes_exactly_one_call():
    calls = []
    pages = {1: CATALOGUE[:2], 2: CATALOGUE[2:]}
    with patch("server.call_linnworks", _fake_api(pages, calls)):
        r = server.list_inventory_items(page=2, per_page=2)
    assert len(calls) == 1
    assert calls[0]["pageNumber"] == 2
    assert r["complete"] is False
    assert r["page"] == 2


def test_short_page_ends_the_sweep():
    """A page shorter than per_page is the last one — no wasted call for the 400."""
    calls = []
    with patch("server.call_linnworks", _fake_api({1: CATALOGUE}, calls)):
        r = server.list_inventory_items(all_pages=True, per_page=200)
    assert len(calls) == 1
    assert r["pages_fetched"] == 1


def test_per_page_capped_at_the_api_maximum():
    """Linnworks 400s above 200 — the cap is applied client-side instead."""
    calls = []
    with patch("server.call_linnworks", _fake_api({1: CATALOGUE}, calls)):
        server.list_inventory_items(per_page=5000)
    assert calls[0]["entriesPerPage"] == 200


def test_rate_limit_is_retried_then_gives_up_without_a_partial_result():
    """A partial sweep would under-report stock — it must raise, not truncate."""
    def _always_429(method_path, payload):
        raise RuntimeError("Linnworks failed: HTTP 429 — rate limit")
    with patch("server.call_linnworks", _always_429), patch("server.time.sleep"):
        with pytest.raises(RuntimeError, match="repeated rate-limit"):
            server.list_inventory_items(per_page=200)


# --- parent include flags ----------------------------------------------------

def test_parent_flags_are_sent_as_include_flags_and_default_to_true():
    """
    Both default True so "every active item" really is everything. The previously
    documented sweep payload used false/false, which silently omits every
    composite and variation parent.
    """
    calls = []
    with patch("server.call_linnworks", _fake_api({1: CATALOGUE}, calls)):
        server.list_inventory_items(per_page=200)
        server.list_inventory_items(
            per_page=200, include_composite_parents=False, include_variation_parents=False)
    assert calls[0]["loadCompositeParents"] is True
    assert calls[0]["loadVariationParents"] is True
    assert calls[1]["loadCompositeParents"] is False
    assert calls[1]["loadVariationParents"] is False


def test_is_variation_parent_comes_from_the_model():
    with patch("server.call_linnworks", _fake_api({1: CATALOGUE})):
        r = server.list_inventory_items(per_page=200)
    by_sku = {i["sku"]: i for i in r["items"]}
    assert by_sku["ZERO-1"]["is_variation_parent"] is True
    assert by_sku["MULTI-1"]["is_variation_parent"] is False


# --- composite parent flag ---------------------------------------------------

def test_is_composite_parent_is_none_unless_resolved():
    """The model has no such field — None means 'not determined', not 'no'."""
    with patch("server.call_linnworks", _fake_api({1: CATALOGUE})):
        r = server.list_inventory_items(per_page=200)
    assert all(i["is_composite_parent"] is None for i in r["items"])
    assert r["scope"]["composite_parent_flag_resolved"] is False


def test_flag_composite_parents_fills_from_the_index():
    index = {"parents": {SID_MULTI: {"sku": "MULTI-1", "title": "Multi Location"}}}
    with patch("server.call_linnworks", _fake_api({1: CATALOGUE})), \
         patch("server._get_composite_index", return_value=index):
        r = server.list_inventory_items(per_page=200, flag_composite_parents=True)
    by_sku = {i["sku"]: i for i in r["items"]}
    assert by_sku["MULTI-1"]["is_composite_parent"] is True
    assert by_sku["ZERO-1"]["is_composite_parent"] is False
    assert r["scope"]["composite_parent_flag_resolved"] is True


# --- truncation & guards -----------------------------------------------------

def test_counts_span_everything_scanned_even_when_detail_is_truncated():
    """The verdict is never truncated, only the detail (house rule)."""
    with patch("server.call_linnworks", _fake_api({1: CATALOGUE})):
        r = server.list_inventory_items(per_page=200, max_items=1)
    assert r["count"] == 1
    assert r["truncated"] is True
    assert r["scanned_count"] == 3
    assert r["matched_count"] == 3


def test_location_id_without_stock_levels_is_rejected():
    with pytest.raises(ValueError, match="location_id requires include_stock_levels"):
        server.list_inventory_items(location_id=KEEN_LOC, include_stock_levels=False)


def test_zero_stock_only_without_stock_levels_is_rejected():
    with pytest.raises(ValueError, match="zero_stock_only requires include_stock_levels"):
        server.list_inventory_items(zero_stock_only=True, include_stock_levels=False)


def test_scope_block_reports_active_only():
    """Archived items are invisible to this endpoint — the response must say so."""
    with patch("server.call_linnworks", _fake_api({1: CATALOGUE})):
        r = server.list_inventory_items(per_page=200)
    assert r["scope"]["active_only"] is True
