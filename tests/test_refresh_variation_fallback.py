"""
Tests for the refresh_channel_listing variation-group fallback (issue #26).

GLT variation listings hang off the variation PARENT: the parent holds the
template (and usually no channel-SKU row), the children hold the channel-SKU
mappings (and no templates). refresh_channel_listing must:

  - fall back from a template-less CHILD to its variation parent's template
    (via_variation_parent), deduping several children of one group into ONE
    parent-template push (covers_skus);
  - accept a PARENT SKU as "listed" when any of its children is mapped on the
    target store (listed_via_children);
  - leave the pre-existing per-item-template path untouched.

All Linnworks calls are mocked; endpoint shapes mirror the live-confirmed
responses recorded in CLAUDE.md (14 Jul 2026, issue #26).
"""
from unittest.mock import patch

# ── Fixture GUIDs / SKUs ──────────────────────────────────────────────────────

SID_CHILD_1 = "aaaaaaaa-0000-0000-0000-000000000001"
SID_CHILD_2 = "aaaaaaaa-0000-0000-0000-000000000002"
SID_PARENT  = "bbbbbbbb-0000-0000-0000-000000000001"
SID_PLAIN   = "cccccccc-0000-0000-0000-000000000001"

SKU_CHILD_1 = "vnm-test-bundle-adult-large"
SKU_CHILD_2 = "vnm-test-bundle-junior-small"
SKU_PARENT  = "vnm-test-bundle"
SKU_PLAIN   = "vnm-plain-item"

ITEMS = {
    SKU_CHILD_1: {"StockItemId": SID_CHILD_1, "ItemTitle": "Test Bundle adult-large"},
    SKU_CHILD_2: {"StockItemId": SID_CHILD_2, "ItemTitle": "Test Bundle junior-small"},
    SKU_PARENT:  {"StockItemId": SID_PARENT,  "ItemTitle": "Test Bundle"},
    SKU_PLAIN:   {"StockItemId": SID_PLAIN,   "ItemTitle": "Plain Item"},
}

SWH_ROW = {"Source": "SHOPIFY", "SubSource": "SWH Shopify"}

CONFIGURATORS = [
    {"id": 129, "name": "Master - Size", "channel_id": 18,
     "sub_source": "SWH Shopify", "show_in_inventory": True},
]

PARENT_TEMPLATE = {
    "Id": 39076, "StockItemId": SID_PARENT, "ConfiguratorId": 129,
    "IsLocked": False, "IsAllowedToRevise": True,
    "NextSuggestedAction": "Update", "IsNextSuggestedActionAllowed": True,
    "Info": {"ActiveListingId": {"Value": "9280707657974"},
             "Status": {"Value": "Listed"}},
}

PLAIN_TEMPLATE = {
    "Id": 43285, "StockItemId": SID_PLAIN, "ConfiguratorId": 141,
    "IsLocked": False, "IsAllowedToRevise": True,
    "NextSuggestedAction": "Update", "IsNextSuggestedActionAllowed": True,
    "Info": {"ActiveListingId": {"Value": "1111"},
             "Status": {"Value": "Listed"}},
}

VARIATION_MEMBERS = [
    {"pkStockItemId": SID_CHILD_1, "ItemNumber": SKU_CHILD_1, "ItemTitle": "adult-large"},
    {"pkStockItemId": SID_CHILD_2, "ItemNumber": SKU_CHILD_2, "ItemTitle": "junior-small"},
]


class _Harness:
    """Routes mocked call_linnworks / call_linnworks_get by endpoint path."""

    def __init__(self):
        self.search_variation_calls = 0
        self.open_template_batches = []  # list of InventoryItemIds lists

    def call_linnworks(self, path, payload):
        if "GetInventoryItem" == path.split("/")[-1]:
            sku = payload.get("sku")
            if sku in ITEMS:
                return ITEMS[sku]
            raise RuntimeError(f"HTTP 400 — no item {sku}")
        if "OpenTemplatesByInventory" in path:
            ids = payload["request"]["Parameters"]["InventoryItemIds"]
            self.open_template_batches.append(list(ids))
            out = []
            for t in (PARENT_TEMPLATE, PLAIN_TEMPLATE):
                if t["StockItemId"] in ids:
                    out.append(t)
            return {"TotalEntries": len(out), "TemplatesInfo": out}
        if "BatchGetInventoryItemChannelSKUs" in path:
            return [
                {"StockItemId": sid,
                 "ChannelSkus": [SWH_ROW] if sid in (SID_CHILD_1, SID_CHILD_2, SID_PLAIN) else []}
                for sid in payload["inventoryItemIds"]
            ]
        if "ProcessTemplates" in path:
            return {}
        raise AssertionError(f"Unexpected call_linnworks path: {path}")

    def call_linnworks_get(self, path, params=None):
        params = params or {}
        # Pre-flight staleness reads (issue #40) — this suite is about the
        # variation fallback, not staleness, so they return empty and the
        # staleness verdict simply has nothing comparable to report.
        if ("GetInventoryItemTitles" in path
                or "GetInventoryItemPrices" in path
                or "GetInventoryItemImages" in path):
            return []
        if "GetInventoryItemChannelSKUs" in path:
            sid = params.get("inventoryItemId")
            # Children + plain item carry the store mapping; the parent does NOT.
            return [SWH_ROW] if sid in (SID_CHILD_1, SID_CHILD_2, SID_PLAIN) else []
        if "GetVariationGroupByParentId" in path:
            sid = params.get("pkStockItemId")
            if sid == SID_PARENT:
                return {"pkVariationItemId": SID_PARENT, "VariationSKU": SKU_PARENT,
                        "VariationGroupName": "Test Bundle Group"}
            return None  # HTTP 200 null for non-parents
        if "GetVariationItems" in path:
            if params.get("pkVariationItemId") == SID_PARENT:
                return VARIATION_MEMBERS
            return []
        if "SearchVariationGroups" in path:
            self.search_variation_calls += 1
            text = (params.get("searchText") or "").lower()
            if "vnm-test-bundle" in text:
                return {"Data": [{"pkVariationItemId": SID_PARENT,
                                  "VariationSKU": SKU_PARENT,
                                  "VariationGroupName": "Test Bundle Group"}],
                        "TotalPages": 1}
            return {"Data": [], "TotalPages": 1}
        raise AssertionError(f"Unexpected call_linnworks_get path: {path}")


def _run(skus, **kwargs):
    import server
    h = _Harness()
    with patch("server._fetch_shopify_configurators", return_value=CONFIGURATORS), \
         patch("server.call_linnworks", side_effect=h.call_linnworks), \
         patch("server.call_linnworks_get", side_effect=h.call_linnworks_get):
        out = server.refresh_channel_listing(skus, sub_source="SWH Shopify", **kwargs)
    return out, h


class TestVariationChildFallback:

    def test_children_dedupe_to_one_parent_template_push(self):
        out, h = _run([SKU_CHILD_1, SKU_CHILD_2])
        assert out["dry_run"] is True
        assert len(out["plan"]) == 1
        row = out["plan"][0]
        assert row["template_id"] == 39076
        assert row["sku"] == SKU_PARENT
        assert row["stock_item_id"] == SID_PARENT
        assert row["via_variation_parent"] is True
        assert row["covers_skus"] == [SKU_CHILD_1, SKU_CHILD_2]
        assert row["action"] == "Update"
        assert out["unresolved"] == []

    def test_sibling_cache_avoids_second_variation_search(self):
        """The 2nd child of a confirmed group must be served from the sibling
        cache — one SearchVariationGroups sweep per group, not per SKU."""
        _, h = _run([SKU_CHILD_1, SKU_CHILD_2])
        assert h.search_variation_calls == 1

    def test_parent_sku_listed_via_children(self):
        out, _ = _run([SKU_PARENT])
        assert len(out["plan"]) == 1
        row = out["plan"][0]
        assert row["template_id"] == 39076
        assert row["listed_via_children"] == [SKU_CHILD_1, SKU_CHILD_2]
        assert "via_variation_parent" not in row
        assert out["unresolved"] == []

    def test_parent_and_children_together_still_one_push(self):
        out, _ = _run([SKU_PARENT, SKU_CHILD_1, SKU_CHILD_2])
        assert len(out["plan"]) == 1
        assert sorted(out["plan"][0]["covers_skus"]) == sorted(
            [SKU_PARENT, SKU_CHILD_1, SKU_CHILD_2]
        )

    def test_plain_item_path_unchanged(self):
        out, h = _run([SKU_PLAIN])
        assert len(out["plan"]) == 1
        row = out["plan"][0]
        assert row["template_id"] == 43285
        assert row["sku"] == SKU_PLAIN
        assert "via_variation_parent" not in row
        assert row["covers_skus"] == [SKU_PLAIN]
        # No variation lookups should have fired for a templated item
        assert h.search_variation_calls == 0

    def test_live_run_pushes_each_template_once(self):
        import server
        h = _Harness()
        pushed = []

        real = h.call_linnworks

        def spy(path, payload):
            if "ProcessTemplates" in path:
                pushed.extend(payload["request"]["TemplateRequests"])
            return real(path, payload)

        with patch("server._fetch_shopify_configurators", return_value=CONFIGURATORS), \
             patch("server.call_linnworks", side_effect=spy), \
             patch("server.call_linnworks_get", side_effect=h.call_linnworks_get):
            out = server.refresh_channel_listing(
                [SKU_CHILD_1, SKU_CHILD_2], sub_source="SWH Shopify", dry_run=False
            )
        assert len(pushed) == 1
        assert pushed[0] == {"TemplateId": 39076, "Action": "Update"}
        assert out["results"][0]["processed"] is True
