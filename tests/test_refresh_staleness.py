"""
Tests for the refresh_channel_listing pre-flight staleness check (issue #40).

BACKGROUND — the silent no-op (live-proven 18 Aug 2026)
------------------------------------------------------
`ProcessTemplates` pushes the GLT template's STORED field snapshot, not the
item's current data (the #27 hazard). Two BLT items pushed cleanly, returned
`processed: true`, and changed NOTHING on Shopify: the template still pointed
at a Linnworks image URL that had since been deleted. A 2xx is indistinguishable
from a real revise, so the wrong image stayed live and read as "fixed".

WHAT THE TEMPLATE ACTUALLY EXPOSES (live-probed 19 Aug 2026, tpl 52731)
-----------------------------------------------------------------------
`OpenTemplatesByInventory` -> Info carries VALUES for Title / Price and
LastModificationTime (the snapshot build time), but only COUNTS or the literal
"Filled" for Images / Attributes / MetaFields / Description. So:

  - comparable: title, price (non-variation only), image COUNT
  - NOT comparable: image content/URL, description body, attributes, metafields

Three traps this suite pins down, each of which produces a WRONG verdict:

  1. Comparing Title against the base ItemTitle gives a FALSE "stale" whenever a
     channel title override exists. The real #40 items are exactly this shape:
     base "Zero Megadeth ...", channel override "Zero Megadeath ...", template
     matching the OVERRIDE. Compare against the effective channel value.
  2. A VARIATION template reports Price 0.0 (prices live per-variant), so a naive
     price comparison marks every variation group stale. Live: tpl 39076 -> 0.0.
  3. Matching image counts do NOT mean fresh — the #40 no-op had count 1 == 1
     while the URL had changed. So `comparable_fields_match` must never be
     reported as "this push will change nothing".

All Linnworks calls are mocked; shapes mirror the live responses above.
"""
from unittest.mock import patch

import pytest

# ── Fixtures ──────────────────────────────────────────────────────────────────

SID_BLT   = "63982650-0000-0000-0000-000000000001"   # non-variation, channel-title override
SID_PRICE = "63982650-0000-0000-0000-000000000002"   # non-variation, stale price
SID_IMG   = "63982650-0000-0000-0000-000000000003"   # non-variation, image count drift
SID_PAR   = "63982650-0000-0000-0000-000000000004"   # variation parent (Price 0.0)
SID_KID   = "63982650-0000-0000-0000-000000000005"   # variation child

SKU_BLT   = "ZDMEGADTH8.25-vtk-c-rawBLT"
SKU_PRICE = "stale-price-item"
SKU_IMG   = "image-drift-item"
SKU_PAR   = "vnm-catnip-MID-bundle"
SKU_KID   = "vnm-catnip-MID-bundle-adult-large"

ITEMS = {
    SKU_BLT:   {"StockItemId": SID_BLT,   "ItemTitle": 'Zero Megadeth x Venom Custom Complete Skateboard - 8.25"',
                "RetailPrice": 124.95},
    SKU_PRICE: {"StockItemId": SID_PRICE, "ItemTitle": "Stale Price Item",  "RetailPrice": 40.0},
    SKU_IMG:   {"StockItemId": SID_IMG,   "ItemTitle": "Image Drift Item",  "RetailPrice": 10.0},
    SKU_PAR:   {"StockItemId": SID_PAR,   "ItemTitle": 'Venom Catnip Core Complete Skateboard Bundle - 7.75"',
                "RetailPrice": 95.95},
    SKU_KID:   {"StockItemId": SID_KID,   "ItemTitle": "Catnip adult-large", "RetailPrice": 95.95},
}

SWH_ROW = {"Source": "SHOPIFY", "SubSource": "SWH Shopify"}
CONFIGURATORS = [{"id": 141, "name": "Master - Size", "channel_id": 18,
                  "sub_source": "SWH Shopify", "show_in_inventory": True}]

# Channel title overrides per stock item (SHOPIFY / SWH Shopify)
CHANNEL_TITLES = {
    # The #40 trap: template matches this override, NOT the base ItemTitle.
    SID_BLT: 'Zero Megadeath x Venom Custom Complete Skateboard - 8.25"',
}
CHANNEL_PRICES = {
    SID_BLT:   124.95,
    SID_PRICE: 55.0,      # item moved to 55.00; template still holds 40.00
    SID_IMG:   10.0,
    SID_PAR:   79.95,
}
IMAGE_COUNTS = {SID_BLT: 1, SID_PRICE: 1, SID_IMG: 3, SID_PAR: 1}


def _tpl(tid, sid, *, title, price, images, is_variation=False,
         last_mod="2026-08-12T13:08:46.4733333Z"):
    return {
        "Id": tid, "StockItemId": sid, "ConfiguratorId": 141,
        "IsLocked": False, "IsAllowedToRevise": True,
        "NextSuggestedAction": "Update", "IsNextSuggestedActionAllowed": True,
        "IsVariation": is_variation,
        "Info": {
            "ActiveListingId": {"Value": "9495050125558"},
            "Status": {"Value": "Listed"},
            "Title": {"Value": title},
            "Price": {"Value": price},
            "Images": {"Type": "Action", "Value": str(images)},
            "Description": {"Type": "Action", "Value": "Filled"},
            "Attributes": {"Type": "Action", "Value": "21"},
            "MetaFields": {"Type": "Action", "Value": "42"},
            "LastModificationTime": {"Value": last_mod},
            "LastUpdateTime": {"Value": "2026-08-18T15:48:36.4024711Z"},
        },
    }


TEMPLATES = {
    # #40 shape: everything comparable MATCHES (title matches the override,
    # price matches, image count 1 == 1) yet the push was a proven no-op.
    SID_BLT:   _tpl(52731, SID_BLT,   title=CHANNEL_TITLES[SID_BLT], price=124.95, images=1),
    SID_PRICE: _tpl(45388, SID_PRICE, title="Stale Price Item", price=40.0, images=1),
    SID_IMG:   _tpl(45394, SID_IMG,   title="Image Drift Item", price=10.0, images=1),
    # Variation parent: Price 0.0 (per-variant), 6 variants.
    SID_PAR:   _tpl(39076, SID_PAR,
                    title='Venom Catnip Core Complete Skateboard Bundle - 7.75"',
                    price=0.0, images=1, is_variation=True,
                    last_mod="2026-02-16T15:25:27.3Z"),
}


class _Harness:
    def __init__(self, rate_limit_titles_for=None):
        self.get_paths = []
        self.rate_limit_titles_for = rate_limit_titles_for or set()

    def call_linnworks(self, path, payload):
        leaf = path.split("/")[-1]
        if leaf == "GetInventoryItem":
            sku = payload.get("sku")
            if sku in ITEMS:
                return ITEMS[sku]
            raise RuntimeError(f"HTTP 400 — no item {sku}")
        if "OpenTemplatesByInventory" in path:
            ids = payload["request"]["Parameters"]["InventoryItemIds"]
            return {"TotalEntries": 0,
                    "TemplatesInfo": [TEMPLATES[s] for s in ids if s in TEMPLATES]}
        if "BatchGetInventoryItemChannelSKUs" in path:
            return [{"StockItemId": sid,
                     "ChannelSkus": [SWH_ROW] if sid in (SID_KID,) else []}
                    for sid in payload["inventoryItemIds"]]
        if "ProcessTemplates" in path:
            return {}
        raise AssertionError(f"Unexpected call_linnworks path: {path}")

    def call_linnworks_get(self, path, params=None):
        params = params or {}
        self.get_paths.append(path)
        sid = params.get("inventoryItemId")
        if "GetInventoryItemChannelSKUs" in path:
            return [SWH_ROW] if sid in (SID_BLT, SID_PRICE, SID_IMG, SID_KID) else []
        if "GetInventoryItemTitles" in path:
            if sid in self.rate_limit_titles_for:
                import server
                raise server.RateLimitError("HTTP 429 — API calls quota exceeded!")
            t = CHANNEL_TITLES.get(sid)
            return [{"Source": "SHOPIFY", "SubSource": "SWH Shopify", "Title": t}] if t else []
        if "GetInventoryItemPrices" in path:
            p = CHANNEL_PRICES.get(sid)
            return [{"Source": "SHOPIFY", "SubSource": "SWH Shopify", "Price": p}] if p is not None else []
        if "GetInventoryItemImages" in path:
            return [{"pkRowId": f"img{i}"} for i in range(IMAGE_COUNTS.get(sid, 0))]
        if "GetVariationGroupByParentId" in path:
            if params.get("pkStockItemId") == SID_PAR:
                return {"pkVariationItemId": SID_PAR, "VariationSKU": SKU_PAR,
                        "VariationGroupName": "Catnip Group"}
            return None
        if "GetVariationItems" in path:
            if params.get("pkVariationItemId") == SID_PAR:
                return [{"pkStockItemId": SID_KID, "ItemNumber": SKU_KID,
                         "ItemTitle": "adult-large"}]
            return []
        if "SearchVariationGroups" in path:
            if "catnip" in (params.get("searchText") or "").lower():
                return {"Data": [{"pkVariationItemId": SID_PAR, "VariationSKU": SKU_PAR,
                                  "VariationGroupName": "Catnip Group"}], "TotalPages": 1}
            return {"Data": [], "TotalPages": 1}
        raise AssertionError(f"Unexpected call_linnworks_get path: {path}")


def _run(skus, harness=None, **kwargs):
    import server
    h = harness or _Harness()
    # refresh_channel_listing resolves its target via _resolve_glt_target, which
    # fetches the catalogue through _fetch_glt_configurators(channel_type) rather
    # than the Shopify-scoped alias (issue #42) — patch the shared fetcher so the
    # fixture catalogue is used regardless of which one the code calls.
    with patch("server._fetch_glt_configurators", return_value=CONFIGURATORS), \
         patch("server.call_linnworks", side_effect=h.call_linnworks), \
         patch("server.call_linnworks_get", side_effect=h.call_linnworks_get):
        out = server.refresh_channel_listing(skus, sub_source="SWH Shopify", **kwargs)
    return out, h


def _row(out, template_id):
    return next(r for r in out["plan"] if r["template_id"] == template_id)


# ── Trap 1: channel-title override must not read as stale ────────────────────

class TestChannelOverrideNotStale:

    def test_template_matching_channel_title_override_is_not_stale(self):
        """The real #40 items: base ItemTitle differs from the channel override,
        and the template matches the OVERRIDE. Comparing against the base title
        would flag every override-carrying item as stale."""
        out, _ = _run([SKU_BLT])
        st = _row(out, 52731)["staleness"]
        assert "title" not in st["stale_fields"]
        assert st["compared"]["title"]["item"] == CHANNEL_TITLES[SID_BLT]
        assert st["compared"]["title"]["match"] is True

    def test_all_comparable_fields_match_on_the_proven_no_op(self):
        out, _ = _run([SKU_BLT])
        st = _row(out, 52731)["staleness"]
        assert st["stale_fields"] == []
        assert st["comparable_fields_match"] is True


# ── Trap 3: a match must NEVER be reported as "this will change nothing" ─────

class TestMatchIsNotAGuarantee:

    def test_no_will_change_field_is_ever_returned(self):
        """The issue asked for `will_change: false`. The #40 no-op had every
        comparable field matching, so asserting that would be exactly wrong."""
        out, _ = _run([SKU_BLT])
        st = _row(out, 52731)["staleness"]
        assert "will_change" not in st

    def test_undetectable_fields_are_named_even_when_everything_matches(self):
        out, _ = _run([SKU_BLT])
        st = _row(out, 52731)["staleness"]
        joined = " ".join(st["undetectable_fields"]).lower()
        assert "image" in joined and "url" in joined
        assert "description" in joined
        assert st["comparable_fields_match"] is True
        assert st["warning"]


# ── Detecting genuine staleness ──────────────────────────────────────────────

class TestDetectsRealStaleness:

    def test_price_mismatch_is_flagged(self):
        out, _ = _run([SKU_PRICE])
        st = _row(out, 45388)["staleness"]
        assert "price" in st["stale_fields"]
        assert st["compared"]["price"]["template"] == 40.0
        assert st["compared"]["price"]["item"] == 55.0
        assert st["comparable_fields_match"] is False

    def test_image_count_mismatch_is_flagged(self):
        out, _ = _run([SKU_IMG])
        st = _row(out, 45394)["staleness"]
        assert "image_count" in st["stale_fields"]
        assert st["compared"]["image_count"]["template"] == 1
        assert st["compared"]["image_count"]["item"] == 3

    def test_snapshot_age_is_reported(self):
        out, _ = _run([SKU_BLT])
        st = _row(out, 52731)["staleness"]
        assert st["template_last_modified"].startswith("2026-08-12")
        assert isinstance(st["snapshot_age_days"], int)
        assert st["snapshot_age_days"] >= 0

    def test_stale_plan_count_surfaced_at_top_level(self):
        out, _ = _run([SKU_BLT, SKU_PRICE])
        assert out["stale_plan_count"] == 1


# ── Trap 2: variation templates report Price 0.0 ─────────────────────────────

class TestVariationTemplatePriceSkipped:

    def test_variation_price_is_skipped_not_flagged_stale(self):
        """Live: tpl 39076 reports Price 0.0 because prices are per-variant.
        Comparing it would mark every variation group stale."""
        out, _ = _run([SKU_KID])
        st = _row(out, 39076)["staleness"]
        assert "price" not in st["stale_fields"]
        assert "price" not in st["compared"]
        assert any("variation" in s.lower() for s in st["skipped_comparisons"])

    def test_variation_snapshot_age_still_reported(self):
        out, _ = _run([SKU_KID])
        st = _row(out, 39076)["staleness"]
        assert st["template_last_modified"].startswith("2026-02-16")
        assert st["snapshot_age_days"] > 100


# ── Opting out / resilience ──────────────────────────────────────────────────

class TestOptOutAndResilience:

    def test_check_staleness_false_skips_the_extra_reads(self):
        out, h = _run([SKU_BLT], check_staleness=False)
        assert "staleness" not in _row(out, 52731)
        assert not any("GetInventoryItemTitles" in p for p in h.get_paths)
        assert not any("GetInventoryItemPrices" in p for p in h.get_paths)

    def test_rate_limit_during_staleness_read_does_not_sink_the_plan(self):
        h = _Harness(rate_limit_titles_for={SID_BLT})
        out, _ = _run([SKU_BLT], harness=h)
        row = _row(out, 52731)
        assert row["action"] == "Update"          # plan survives
        st = row["staleness"]
        assert st["checked"] is False
        assert st["comparable_fields_match"] is None
        assert "error" in st


# ── Live run carries the verdict through ─────────────────────────────────────

class TestLiveRunReportsStaleness:

    def test_results_carry_staleness_and_message_warns(self):
        out, _ = _run([SKU_PRICE], dry_run=False)
        res = out["results"][0]
        assert res["processed"] is True
        assert res["stale_fields"] == ["price"]
        assert "stale" in out["message"].lower()

    def test_message_warns_when_a_push_may_be_a_no_op(self):
        """All comparable fields match + an aged snapshot = the #40 shape."""
        out, _ = _run([SKU_BLT], dry_run=False)
        assert "no-op" in out["message"].lower()
