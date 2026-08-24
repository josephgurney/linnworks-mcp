"""
Tests for refresh_channel_listing generalised beyond Shopify (issue #42).

Before this, refresh_channel_listing was hard-wired to Shopify:
GLT_SHOPIFY_CHANNEL_TYPE/NAME and _fetch_shopify_configurators (a Shopify-only
alias). It now takes a `channel` argument and resolves identity through the
same GLT_CHANNELS registry and _resolve_glt_target already live-proven for
unpublish_channel_listing (issue #30) — so this suite pins down the traps that
were specific to extending a single-channel tool to a multi-channel one:

  - channel identity from the registry, not a new hard-coded string (a non-GLT
    channel like eBay must raise, naming the supported channels)
  - the channel-SKU "is it listed?" check must match the CHANNEL's Source, not
    always SHOPIFY, in both directions
  - an Amazon regional sub-source ("... - Germany") resolving to its account's
    ChannelId via the account-prefix rule
  - an item with SEVERAL templates on one channel (Amazon: merchant + FBA)
    producing one plan row PER TEMPLATE and, live, one push per template
  - the staleness title/price comparison using the override row for the
    channel being refreshed, not always Shopify's
  - a revise_proven flag on the registry driving the unproven-channel warning,
    so it can never go stale the way a hard-coded string would

All Linnworks calls are mocked.
"""
from unittest.mock import patch

import pytest

import server

# ── Fixtures ──────────────────────────────────────────────────────────────────

SID_AMZ           = "aaaaaaaa-1111-0000-0000-000000000001"  # two Amazon templates
SID_SHOPIFY_ONLY  = "aaaaaaaa-1111-0000-0000-000000000002"  # listed on Shopify only
SID_TITLE         = "aaaaaaaa-1111-0000-0000-000000000003"  # Amazon title-override fixture

SKU_AMZ          = "vnm_bearings_gold"
SKU_SHOPIFY_ONLY = "vnm-shopify-only-item"
SKU_TITLE        = "vnm-amazon-title-item"

ITEMS = {
    SKU_AMZ:          {"StockItemId": SID_AMZ, "ItemTitle": "Bearings Gold", "RetailPrice": 12.5},
    SKU_SHOPIFY_ONLY: {"StockItemId": SID_SHOPIFY_ONLY, "ItemTitle": "Shopify Only Item",
                       "RetailPrice": 9.99},
    SKU_TITLE:        {"StockItemId": SID_TITLE, "ItemTitle": "Base Title Without Override",
                       "RetailPrice": 20.0},
}

# Amazon channel-SKU rows carry both the account and a regional sub-source —
# live shape from issue #30 (a single account fronts several regions).
CHANNEL_SKUS = {
    SID_AMZ: [
        {"Source": "AMAZON", "SubSource": "The Warehouse Group",
         "ChannelReferenceId": "vnm_bearings_gold"},
        {"Source": "AMAZON", "SubSource": "The Warehouse Group - Germany",
         "ChannelReferenceId": "vnm_bearings_gold"},
    ],
    SID_SHOPIFY_ONLY: [
        {"Source": "SHOPIFY", "SubSource": "SWH Shopify", "ChannelReferenceId": "1:2:3"},
    ],
    SID_TITLE: [
        {"Source": "AMAZON", "SubSource": "The Warehouse Group",
         "ChannelReferenceId": "vnm-amazon-title-item"},
    ],
}

# Normalized shape returned by _fetch_glt_configurators, per ChannelType.
CATALOGUES = {
    "Shopify": [{"id": 7, "name": "Default", "channel_id": 18,
                "sub_source": "SWH Shopify", "show_in_inventory": True}],
    "Amazon":  [{"id": 126, "name": "Skateboard", "channel_id": 2,
                "sub_source": "The Warehouse Group", "show_in_inventory": True}],
}


def _tpl(tid, sid, *, title=None, price=None, is_locked=False,
         next_allowed=False, next_action="NotAllowed", images=1,
         last_mod="2026-08-01T00:00:00Z"):
    info = {
        "ActiveListingId": {"Value": f"listing-{tid}"},
        "Status": {"Value": "Listed"},
        "LastModificationTime": {"Value": last_mod},
        "Images": {"Type": "Action", "Value": str(images)},
    }
    if title is not None:
        info["Title"] = {"Value": title}
    if price is not None:
        info["Price"] = {"Value": price}
    return {
        "Id": tid, "StockItemId": sid, "ConfiguratorId": 126,
        "IsLocked": is_locked, "IsAllowedToRevise": True,
        # Amazon templates have been observed reporting "NotAllowed" while still
        # permitting a forced action — the auto-action logic must still find
        # "Revise" via IsAllowedToRevise (issue #42 trap).
        "NextSuggestedAction": next_action,
        "IsNextSuggestedActionAllowed": next_allowed,
        "Info": info,
    }


# One StockItemId, TWO templates on Amazon (ChannelId 2) — merchant + FBA
# (live-observed shape, issue #30).
TEMPLATES = {
    (SID_AMZ, 2): [
        _tpl(32115, SID_AMZ, title="Bearings Gold", price=12.5),
        _tpl(32381, SID_AMZ, title="Bearings Gold", price=12.5),
    ],
    (SID_TITLE, 2): [
        _tpl(9001, SID_TITLE, title="Amazon Override Title", price=20.0),
    ],
}

CHANNEL_TITLES = {
    SID_TITLE: [{"Source": "AMAZON", "SubSource": "The Warehouse Group",
                "Title": "Amazon Override Title"}],
}


class _Harness:
    def __init__(self, rate_limit_titles_for=None):
        self.rate_limit_titles_for = rate_limit_titles_for or set()
        self.open_calls: list[tuple] = []
        self.process_calls: list[dict] = []

    def call_linnworks(self, path, payload):
        leaf = path.split("/")[-1]
        if leaf == "GetInventoryItem":
            sku = payload.get("sku")
            if sku in ITEMS:
                return ITEMS[sku]
            raise RuntimeError(f"HTTP 400 — no item {sku}")
        if "OpenTemplatesByInventory" in path:
            ids = payload["request"]["Parameters"]["InventoryItemIds"]
            cid = payload["request"]["Parameters"]["ChannelId"]
            self.open_calls.append((tuple(ids), cid))
            out = []
            for sid in ids:
                out.extend(TEMPLATES.get((sid, cid), []))
            return {"TotalEntries": len(out), "TemplatesInfo": out}
        if "ProcessTemplates" in path:
            self.process_calls.append(payload["request"]["TemplateRequests"][0])
            return {}
        raise AssertionError(f"Unexpected call_linnworks path: {path}")

    def call_linnworks_get(self, path, params=None):
        params = params or {}
        sid = params.get("inventoryItemId")
        if "GetInventoryItemChannelSKUs" in path:
            return CHANNEL_SKUS.get(sid, [])
        if "GetInventoryItemTitles" in path:
            if sid in self.rate_limit_titles_for:
                raise RuntimeError("HTTP 500 — could not read current titles")
            return CHANNEL_TITLES.get(sid, [])
        if "GetInventoryItemPrices" in path:
            return []
        if "GetInventoryItemImages" in path:
            return [{"pkRowId": "img1"}]
        if "GetVariationGroupByParentId" in path:
            return None
        if "SearchVariationGroups" in path:
            return {"Data": [], "TotalPages": 1}
        if "GetVariationItems" in path:
            return []
        raise AssertionError(f"Unexpected call_linnworks_get path: {path}")


def _run(skus, harness=None, **kwargs):
    h = harness or _Harness()
    with patch("server._fetch_glt_configurators",
               side_effect=lambda channel="Shopify": CATALOGUES.get(channel, [])), \
         patch("server.call_linnworks", side_effect=h.call_linnworks), \
         patch("server.call_linnworks_get", side_effect=h.call_linnworks_get):
        out = server.refresh_channel_listing(skus, **kwargs)
    return out, h


# ── AC1: channel defaults to Shopify ─────────────────────────────────────────

class TestChannelDefault:

    def test_default_channel_is_shopify(self):
        out, _ = _run([SKU_SHOPIFY_ONLY], sub_source="SWH Shopify")
        assert out["target_channel"] == "Shopify"
        assert out["target_source"] == "SHOPIFY"
        assert out["revise_proven"] is True


# ── AC2: channel identity from the registry, non-GLT channel raises ─────────

class TestChannelIdentity:

    def test_non_glt_channel_raises_naming_supported_channels(self):
        with pytest.raises(ValueError) as exc:
            server.refresh_channel_listing([SKU_AMZ], channel="eBay")
        msg = str(exc.value)
        assert "not a GLT-managed channel" in msg
        assert "Shopify" in msg
        assert "Amazon" in msg


# ── AC3: listed check matches the channel's Source, both directions ─────────

class TestSourceMatching:

    def test_amazon_item_not_listed_on_shopify(self):
        out, _ = _run([SKU_AMZ], sub_source="SWH Shopify", channel="Shopify",
                      check_staleness=False)
        assert out["plan"] == []
        assert len(out["unresolved"]) == 1
        assert "not listed on Shopify" in out["unresolved"][0]["error"]

    def test_shopify_item_not_listed_on_amazon(self):
        out, _ = _run([SKU_SHOPIFY_ONLY], sub_source="The Warehouse Group",
                      channel="Amazon", check_staleness=False)
        assert out["plan"] == []
        assert len(out["unresolved"]) == 1
        assert "not listed on Amazon" in out["unresolved"][0]["error"]


# ── AC4: Amazon regional sub-source resolves via the account-prefix rule ────

class TestRegionalSubSourceResolution:

    def test_regional_sub_source_resolves_by_prefix(self):
        out, _ = _run([SKU_AMZ], sub_source="The Warehouse Group - Germany",
                      channel="Amazon", check_staleness=False)
        assert out["sub_source_resolution"] == "account-prefix"
        assert out["target_channel_id"] == 2

    def test_account_sub_source_resolves_exactly(self):
        out, _ = _run([SKU_AMZ], sub_source="The Warehouse Group",
                      channel="Amazon", check_staleness=False)
        assert out["sub_source_resolution"] == "exact"
        assert out["target_channel_id"] == 2


# ── AC5: several templates on one channel -> one plan row PER TEMPLATE ──────

class TestMultiTemplatePerItem:

    def test_two_amazon_templates_produce_two_plan_rows(self):
        out, _ = _run([SKU_AMZ], sub_source="The Warehouse Group", channel="Amazon",
                      check_staleness=False)
        assert len(out["plan"]) == 2
        tids = sorted(r["template_id"] for r in out["plan"])
        assert tids == [32115, 32381]
        for row in out["plan"]:
            assert row["covers_skus"] == [SKU_AMZ]
            assert row["templates_on_item"] == 2
            # NotAllowed + not next_allowed but IsAllowedToRevise=True -> Revise.
            assert row["action"] == "Revise"

    def test_a_fixture_with_a_single_template_still_collapses_to_one_row(self):
        """Sanity check the per-template plan didn't silently change the
        single-template shape acceptance criterion 1 requires."""
        out, _ = _run([SKU_TITLE], sub_source="The Warehouse Group", channel="Amazon",
                      check_staleness=False)
        assert len(out["plan"]) == 1

    def test_live_run_pushes_each_template_separately(self):
        h = _Harness()
        out, h = _run([SKU_AMZ], harness=h, sub_source="The Warehouse Group",
                      channel="Amazon", check_staleness=False, dry_run=False)
        assert len(h.process_calls) == 2
        pushed_ids = sorted(c["TemplateId"] for c in h.process_calls)
        assert pushed_ids == [32115, 32381]
        assert len(out["results"]) == 2
        assert all(r["processed"] for r in out["results"])


# ── AC6: staleness title/price comparison scoped to the refreshed channel ───

class TestChannelScopedStaleness:

    def test_amazon_title_override_matching_template_is_not_stale(self):
        out, _ = _run([SKU_TITLE], sub_source="The Warehouse Group", channel="Amazon")
        row = out["plan"][0]
        st = row["staleness"]
        assert "title" not in st["stale_fields"]
        assert st["compared"]["title"]["item"] == "Amazon Override Title"
        assert st["compared"]["title"]["item_value_from"] == "channel_override"

    def test_same_fixture_compared_against_the_base_title_would_be_stale(self):
        """Direct check on the shared helper: scoping the comparison to a
        channel with no override row on this item (so it falls back to the
        base ItemTitle) reproduces the wrong verdict this exact fixture would
        give without threading the channel through — the trap issue #42
        exists to close."""
        h = _Harness()
        tpl = TEMPLATES[(SID_TITLE, 2)][0]
        with patch("server.call_linnworks_get", side_effect=h.call_linnworks_get):
            st = server._glt_template_staleness(
                tpl, SID_TITLE, "Base Title Without Override", 20.0,
                "The Warehouse Group", channel_source="SHOPIFY",
            )
        assert "title" in st["stale_fields"]
        assert st["compared"]["title"]["item"] == "Base Title Without Override"
        assert st["compared"]["title"]["item_value_from"] == "base"


# ── AC9: a failed current-value read reports unchecked, never a match ───────

class TestStalenessReadFailure:

    def test_read_failure_on_amazon_reports_unchecked_not_matched(self):
        h = _Harness(rate_limit_titles_for={SID_TITLE})
        out, _ = _run([SKU_TITLE], harness=h, sub_source="The Warehouse Group",
                      channel="Amazon")
        row = out["plan"][0]
        st = row["staleness"]
        assert st["checked"] is False
        assert st["comparable_fields_match"] is None
        assert "error" in st
        # The plan row itself must still survive a failed staleness read.
        assert row["action"] == "Revise"


# ── AC7: revise_proven warning derived from the registry ────────────────────

class TestUnprovenChannelWarning:

    def test_proven_revise_channels_helper_names_only_shopify(self):
        assert server._proven_revise_channels() == "Shopify"

    def test_amazon_dry_run_warns_unproven_and_names_registry_channels(self):
        out, _ = _run([SKU_AMZ], sub_source="The Warehouse Group", channel="Amazon",
                      check_staleness=False)
        assert out["revise_proven"] is False
        assert "NOT yet live-proven on Amazon" in out["message"]
        assert "Shopify" in out["message"]

    def test_shopify_dry_run_carries_no_unproven_warning(self):
        out, _ = _run([SKU_SHOPIFY_ONLY], sub_source="SWH Shopify", channel="Shopify",
                      check_staleness=False)
        assert out["revise_proven"] is True
        assert "NOT yet live-proven" not in out["message"]

    def test_live_run_message_also_carries_the_unproven_warning(self):
        h = _Harness()
        out, _ = _run([SKU_AMZ], harness=h, sub_source="The Warehouse Group",
                      channel="Amazon", check_staleness=False, dry_run=False)
        assert "NOT yet live-proven on Amazon" in out["message"]


# ── AC8: dry_run defaults True, and a dry run never pushes ──────────────────

class TestDryRunDefault:

    def test_dry_run_defaults_true_and_amazon_gets_no_push_call(self):
        h = _Harness()
        out, h = _run([SKU_AMZ], harness=h, sub_source="The Warehouse Group",
                      channel="Amazon")
        assert out["dry_run"] is True
        assert h.process_calls == []


# ── AC10: live-run message states acceptance is not proof, read data back ───

class TestLiveRunReadbackMessage:

    def test_amazon_live_message_names_readback_over_detail_page(self):
        h = _Harness()
        out, _ = _run([SKU_AMZ], harness=h, sub_source="The Warehouse Group",
                      channel="Amazon", check_staleness=False, dry_run=False)
        msg = out["message"].lower()
        assert "not proof of a change" in msg
        assert "read the listing data back" in msg
        assert "detail page" in msg
        assert "lag the catalogue by up to a day" in msg
