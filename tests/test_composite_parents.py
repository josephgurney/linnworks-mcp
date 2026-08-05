"""
Tests for find_composite_parents — component -> composite parents (issue #31).

Endpoint behaviour these tests encode (all live-confirmed 5 Aug 2026):
  - Stock/GetStockItems (GET, keyWord="") enumerates the ACTIVE catalogue and
    carries a RELIABLE IsCompositeParent flag (unlike IsVariationParent).
  - Inventory/GetInventoryItemsCompositionByIds takes a WRAPPED
    {"request":{"InventoryItemIds":[...]}} body, caps at 100 ids per call, and
    OMITS items with no compositions from the response map.
  - The index is built once and cached for the process (TTL), so a batch pays
    the ~105s / ~215-call sweep once.
  - parent_count / listed_parent_count are counted across ALL parents even when
    the returned parents[] list is truncated — the safety verdict is never
    truncated, only the detail.
"""
import pytest
from unittest.mock import patch

import server

# --- Fixture catalogue -------------------------------------------------------
# Two composite parents, one of them listed:
#   BUNDLE-A (listed)   -> COMP-1 x1, COMP-2 x2
#   BUNDLE-B (unlisted) -> COMP-2 x1
# COMP-3 is a plain item that belongs to nothing.
SID_A = "aaaaaaaa-0000-0000-0000-000000000001"
SID_B = "bbbbbbbb-0000-0000-0000-000000000002"
SID_1 = "11111111-0000-0000-0000-000000000003"
SID_2 = "22222222-0000-0000-0000-000000000004"
SID_3 = "33333333-0000-0000-0000-000000000005"

CATALOGUE = [
    {"StockItemId": SID_A, "ItemNumber": "BUNDLE-A", "ItemTitle": "Bundle A",
     "IsCompositeParent": True},
    {"StockItemId": SID_B, "ItemNumber": "BUNDLE-B", "ItemTitle": "Bundle B",
     "IsCompositeParent": True},
    {"StockItemId": SID_1, "ItemNumber": "COMP-1", "ItemTitle": "Component One",
     "IsCompositeParent": False},
    {"StockItemId": SID_2, "ItemNumber": "COMP-2", "ItemTitle": "Component Two",
     "IsCompositeParent": False},
    {"StockItemId": SID_3, "ItemNumber": "COMP-3", "ItemTitle": "Lonely Item",
     "IsCompositeParent": False},
]

COMPOSITIONS = {
    SID_A: [
        {"LinkedStockItemId": SID_1, "SKU": "COMP-1", "Quantity": 1},
        {"LinkedStockItemId": SID_2, "SKU": "COMP-2", "Quantity": 2},
    ],
    SID_B: [
        {"LinkedStockItemId": SID_2, "SKU": "COMP-2", "Quantity": 1},
    ],
}


@pytest.fixture(autouse=True)
def _clear_index_cache():
    """The index cache is module-level state — reset it around every test."""
    server._composite_index_cache = None
    yield
    server._composite_index_cache = None


def _mock_api(catalogue=None, listed_ids=(SID_A,), pages=1):
    """Patch the three endpoints the tool touches. Returns (patches, captured)."""
    rows = CATALOGUE if catalogue is None else catalogue
    captured = {"sweep_pages": [], "composition_chunks": [], "channel_ids": []}

    def call_linnworks_get(path, params=None):
        if "GetStockItems" in path:
            page = (params or {}).get("pageNumber", 1)
            captured["sweep_pages"].append(page)
            # Spread the fixture rows evenly across `pages` so multi-page
            # pagination is exercised without needing 200 rows per page.
            per = -(-len(rows) // pages)
            start = (page - 1) * per
            return {"Data": rows[start:start + per], "TotalPages": pages,
                    "TotalEntries": len(rows)}
        raise AssertionError(f"Unexpected GET: {path}")

    def call_linnworks(path, payload):
        if "GetInventoryItemsCompositionByIds" in path:
            ids = payload["request"]["InventoryItemIds"]
            captured["composition_chunks"].append(list(ids))
            # Items with no compositions are OMITTED, not returned empty.
            return {"InventoryItemsCompositionByIds":
                    {i: COMPOSITIONS[i] for i in ids if i in COMPOSITIONS}}
        if "BatchGetInventoryItemChannelSKUs" in path:
            ids = payload["inventoryItemIds"]
            captured["channel_ids"].extend(ids)
            return [
                {"StockItemId": i,
                 "ChannelSkus": ([{"Source": "SHOPIFY", "SubSource": "SWH Shopify"}]
                                 if i in listed_ids else [])}
                for i in ids
            ]
        if "GetInventoryItem" in path:
            raise RuntimeError("HTTP 400 — Could not determine inventory item id from SKU")
        raise AssertionError(f"Unexpected POST: {path}")

    patches = [
        patch("server.call_linnworks_get", side_effect=call_linnworks_get),
        patch("server.call_linnworks", side_effect=call_linnworks),
        patch("server.time.sleep", return_value=None),
    ]
    return patches, captured


def _run(*args, **kwargs):
    patches, captured = _mock_api(**kwargs.pop("mock", {}))
    for p in patches:
        p.start()
    try:
        return server.find_composite_parents(*args, **kwargs), captured
    finally:
        for p in patches:
            p.stop()


# --- Core reverse lookup -----------------------------------------------------

def test_component_of_a_listed_bundle_is_not_safe_to_retire():
    res, _ = _run(["COMP-1"])
    row = res["results"][0]
    assert row["is_component"] is True
    assert row["parent_count"] == 1
    assert row["parents"][0]["parent_sku"] == "BUNDLE-A"
    assert row["parents"][0]["quantity"] == 1
    assert row["parents"][0]["parent_is_listed"] is True
    assert row["parents"][0]["parent_channels"] == ["SHOPIFY"]
    assert row["has_listed_parent"] is True
    assert row["safe_to_retire"] is False
    assert res["blocked_count"] == 1


def test_component_of_only_unlisted_bundles_is_safe_but_still_a_component():
    res, _ = _run(["COMP-1"], mock={"listed_ids": ()})
    row = res["results"][0]
    assert row["is_component"] is True
    assert row["listed_parent_count"] == 0
    assert row["safe_to_retire"] is True
    assert res["component_count"] == 1
    assert res["blocked_count"] == 0


def test_non_component_is_safe_to_retire():
    res, _ = _run(["COMP-3"])
    row = res["results"][0]
    assert row["is_component"] is False
    assert row["parent_count"] == 0
    assert row["parents"] == []
    assert row["safe_to_retire"] is True


def test_component_in_multiple_bundles_reports_every_parent():
    res, _ = _run(["COMP-2"])
    row = res["results"][0]
    assert row["parent_count"] == 2
    assert {p["parent_sku"] for p in row["parents"]} == {"BUNDLE-A", "BUNDLE-B"}
    # Listed parents sort first so a truncated list still shows the blocker.
    assert row["parents"][0]["parent_sku"] == "BUNDLE-A"
    assert row["listed_parent_count"] == 1
    assert row["safe_to_retire"] is False


def test_quantity_is_carried_per_parent():
    res, _ = _run(["COMP-2"])
    qty = {p["parent_sku"]: p["quantity"] for p in res["results"][0]["parents"]}
    assert qty == {"BUNDLE-A": 2, "BUNDLE-B": 1}


# --- Batch behaviour ---------------------------------------------------------

def test_one_index_build_serves_the_whole_batch():
    res, captured = _run(["COMP-1", "COMP-2", "COMP-3"])
    assert res["resolved_count"] == 3
    assert res["component_count"] == 2
    # The catalogue was swept once, not once per SKU.
    assert captured["sweep_pages"] == [1]
    assert len(captured["composition_chunks"]) == 1


def test_index_is_cached_across_calls():
    patches, captured = _mock_api()
    for p in patches:
        p.start()
    try:
        first = server.find_composite_parents(["COMP-1"])
        second = server.find_composite_parents(["COMP-2"])
    finally:
        for p in patches:
            p.stop()
    assert first["index"]["from_cache"] is False
    assert second["index"]["from_cache"] is True
    assert captured["sweep_pages"] == [1]  # swept once for both calls


def test_rebuild_index_forces_a_fresh_sweep():
    patches, captured = _mock_api()
    for p in patches:
        p.start()
    try:
        server.find_composite_parents(["COMP-1"])
        res = server.find_composite_parents(["COMP-1"], rebuild_index=True)
    finally:
        for p in patches:
            p.stop()
    assert res["index"]["from_cache"] is False
    assert captured["sweep_pages"] == [1, 1]


def test_only_composite_parents_are_sent_to_the_composition_endpoint():
    _, captured = _run(["COMP-1"])
    assert sorted(captured["composition_chunks"][0]) == sorted([SID_A, SID_B])


def test_composition_chunks_respect_the_100_id_cap():
    # 150 composite parents -> two chunks (the endpoint 400s above 100).
    big = [{"StockItemId": f"{i:08d}-0000-0000-0000-000000000000",
            "ItemNumber": f"P-{i}", "ItemTitle": f"Parent {i}",
            "IsCompositeParent": True} for i in range(150)]
    _, captured = _run(["COMP-1"], mock={"catalogue": big})
    assert [len(c) for c in captured["composition_chunks"]] == [100, 50]


def test_sweep_paginates_to_completion():
    _, captured = _run(["COMP-1"], mock={"catalogue": CATALOGUE, "pages": 3})
    assert captured["sweep_pages"] == [1, 2, 3]


# --- Truncation, resolution, degradation ------------------------------------

def test_truncation_caps_detail_but_not_the_verdict():
    res, _ = _run(["COMP-2"], max_parents_listed=1)
    row = res["results"][0]
    assert row["parents_truncated"] is True
    assert len(row["parents"]) == 1
    # Counts still span every parent, so the safety answer is complete.
    assert row["parent_count"] == 2
    assert row["listed_parent_count"] == 1
    assert row["safe_to_retire"] is False


def test_unresolvable_sku_is_reported_not_fatal():
    res, _ = _run(["COMP-1", "NOPE-404"])
    assert res["resolved_count"] == 1
    assert len(res["unresolved"]) == 1
    assert res["unresolved"][0]["sku"] == "NOPE-404"
    assert "archived" in res["unresolved"][0]["error"]
    assert res["results"][0]["sku"] == "COMP-1"


def test_sku_match_is_case_insensitive():
    res, _ = _run(["comp-1"])
    assert res["results"][0]["is_component"] is True


def test_listing_status_can_be_skipped():
    res, captured = _run(["COMP-1"], include_listing_status=False)
    row = res["results"][0]
    assert captured["channel_ids"] == []
    assert row["is_component"] is True
    # No channel read => no verdict is claimed, rather than a false "safe".
    assert row["listed_parent_count"] is None
    assert row["has_listed_parent"] is None
    assert row["safe_to_retire"] is None
    assert res["blocked_count"] is None


def test_listing_lookup_failure_degrades_with_a_warning():
    def boom(ids):
        raise RuntimeError("HTTP 429 — quota exceeded")

    patches, _ = _mock_api()
    for p in patches:
        p.start()
    try:
        with patch("server._fetch_channel_skus_for_ids", side_effect=boom):
            res = server.find_composite_parents(["COMP-1"])
    finally:
        for p in patches:
            p.stop()
    assert res["results"][0]["is_component"] is True  # reverse answer survives
    assert "listing_status_error" in res
    assert "unreliable" in res["listing_status_error"]


def test_empty_sku_list_is_rejected():
    with pytest.raises(ValueError):
        server.find_composite_parents([])


def test_index_stats_are_reported_for_transparency():
    res, _ = _run(["COMP-1"])
    idx = res["index"]
    assert idx["composite_parents"] == 2
    assert idx["indexed_components"] == 2   # COMP-1 and COMP-2
    assert idx["active_items"] == len(CATALOGUE)
    assert idx["api_calls"] == idx["sweep_pages"] + idx["composition_calls"]
    assert "active items only" in idx["scope"]


# --- The old "impossible" claim is retired ----------------------------------

def test_get_item_relationships_points_at_the_reverse_tool():
    def call_linnworks(path, payload):
        if "GetInventoryItem" in path:
            return {"StockItemId": SID_3, "ItemNumber": "COMP-3",
                    "ItemTitle": "Lonely Item"}
        raise AssertionError(path)

    def call_linnworks_get(path, params=None):
        if "GetInventoryItemCompositions" in path:
            return []
        if "GetVariationGroupByParentId" in path:
            return None
        if "SearchVariationGroups" in path:
            return {"Data": [], "TotalPages": 1}
        raise AssertionError(path)

    with patch("server.call_linnworks", side_effect=call_linnworks), \
         patch("server.call_linnworks_get", side_effect=call_linnworks_get):
        res = server.get_item_relationships("COMP-3")

    comp = res["composite"]
    assert comp["reverse_lookup_tool"] == "find_composite_parents"
    assert "find_composite_parents" in comp["note"]
    assert "cannot be determined" not in comp["note"]
