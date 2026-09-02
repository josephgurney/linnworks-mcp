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
        # Deliberately updated for issue #45: Amazon has now been fired live
        # twice with no observable effect, so the old "NOT yet live-proven"
        # wording (which reads as "untried") is no longer accurate and the
        # tool must say so instead — see TestAmazonTriedAndIneffective below.
        out, _ = _run([SKU_AMZ], sub_source="The Warehouse Group", channel="Amazon",
                      check_staleness=False)
        assert out["revise_proven"] is False
        assert "fired live on Amazon" in out["message"]
        assert "no observable change" in out["message"]
        assert "Shopify" in out["message"]

    def test_shopify_dry_run_carries_no_unproven_warning(self):
        out, _ = _run([SKU_SHOPIFY_ONLY], sub_source="SWH Shopify", channel="Shopify",
                      check_staleness=False)
        assert out["revise_proven"] is True
        assert "NOT yet live-proven" not in out["message"]

    def test_live_run_message_also_carries_the_unproven_warning(self):
        # Deliberately updated for issue #45, same reason as above.
        h = _Harness()
        out, _ = _run([SKU_AMZ], harness=h, sub_source="The Warehouse Group",
                      channel="Amazon", check_staleness=False, dry_run=False)
        assert "fired live on Amazon" in out["message"]
        assert "no observable change" in out["message"]


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


# ── Issue #45 — Amazon revise: accepted, no observable effect (two live proofs) ──
#
# Two live revise pushes were fired against Amazon (24-25 Aug 2026, templates
# 32064 + 32239) and were accepted (processed:true) but produced no observable
# change on either listing. Amazon's registry entry and the tool's own warnings
# must therefore read "tried and shown ineffective", not "untried" — that is a
# meaningfully different, more accurate claim than the old "not yet
# live-proven" wording, which collapsed the two states into one.

class TestAmazonRegistryRecordsAttemptedNoEffect:
    """AC1: the Amazon entry carries a machine-readable attempted/no-effect
    record and revise_proven stays False."""

    def test_amazon_entry_is_attempted_but_not_proven(self):
        entry = server.GLT_CHANNELS["amazon"]
        assert entry["revise_attempted"] is True
        assert entry["revise_proven"] is False


class TestTikTokRegistryStillReadsAsNeverAttempted:
    """AC2: TikTok's entry is unchanged in substance (never attempted) and
    distinguishable from Amazon's on the new field."""

    def test_tiktok_entry_is_never_attempted_and_not_proven(self):
        entry = server.GLT_CHANNELS["tiktok"]
        assert entry["revise_attempted"] is False
        assert entry["revise_proven"] is False

    def test_amazon_and_tiktok_are_distinguishable_on_revise_attempted(self):
        amazon = server.GLT_CHANNELS["amazon"]
        tiktok = server.GLT_CHANNELS["tiktok"]
        # Both are equally NOT proven -- the new field is what tells them apart.
        assert amazon["revise_proven"] == tiktok["revise_proven"] is False
        assert amazon["revise_attempted"] != tiktok["revise_attempted"]


class TestAmazonTriedAndIneffective:
    """AC3 + AC4: dry run and live run against Amazon both state the revise
    was fired live and produced no observable change, not merely 'unproven',
    and revise_proven is still False on the live-run response."""

    def test_amazon_dry_run_states_fired_live_no_observable_change(self):
        out, _ = _run([SKU_AMZ], sub_source="The Warehouse Group", channel="Amazon",
                      check_staleness=False)
        assert "fired live on Amazon" in out["message"]
        assert "no observable change" in out["message"]
        assert "NOT yet live-proven on Amazon" not in out["message"]

    def test_amazon_live_run_carries_the_same_message_and_stays_unproven(self):
        h = _Harness()
        out, _ = _run([SKU_AMZ], harness=h, sub_source="The Warehouse Group",
                      channel="Amazon", check_staleness=False, dry_run=False)
        assert "fired live on Amazon" in out["message"]
        assert "no observable change" in out["message"]
        assert out["revise_proven"] is False
        assert all(row["revise_proven"] is False for row in out["plan"])


TIKTOK_CATALOGUE_ENTRY = {
    "TikTok": [{"id": 30, "name": "Default", "channel_id": 30,
                "sub_source": "SKATEWAREHOUSE_UK", "show_in_inventory": True}],
}


class TestTikTokStillReadsAsNeverAttempted:
    """AC5: a TikTok dry run still reads as never-attempted and its message
    text differs from Amazon's.

    SKU_AMZ isn't mapped to a TIKTOK channel-SKU row (see CHANNEL_SKUS), so it
    lands in `unresolved` -- but the dry-run message is built regardless of
    plan/unresolved size, so this still exercises the unproven-channel wording
    for a channel that has genuinely never been attempted live.
    """

    def test_tiktok_dry_run_never_attempted_wording(self):
        with patch.dict(CATALOGUES, TIKTOK_CATALOGUE_ENTRY):
            out, _ = _run([SKU_AMZ], sub_source="SKATEWAREHOUSE_UK", channel="TikTok",
                          check_staleness=False)
        assert "NEVER been attempted live" in out["message"]
        assert "fired live on Amazon" not in out["message"]

    def test_tiktok_message_textually_differs_from_amazon(self):
        with patch.dict(CATALOGUES, TIKTOK_CATALOGUE_ENTRY):
            tiktok_out, _ = _run([SKU_AMZ], sub_source="SKATEWAREHOUSE_UK", channel="TikTok",
                                 check_staleness=False)
        amazon_out, _ = _run([SKU_AMZ], sub_source="The Warehouse Group", channel="Amazon",
                             check_staleness=False)
        assert tiktok_out["message"] != amazon_out["message"]


class TestShopifyStillCarriesNoWarning:
    """AC6: Shopify dry run and live run carry no unproven/ineffective
    warning at all, and revise_proven is True."""

    def test_shopify_dry_run_clean(self):
        out, _ = _run([SKU_SHOPIFY_ONLY], sub_source="SWH Shopify", channel="Shopify",
                      check_staleness=False)
        assert out["revise_proven"] is True
        assert "fired live" not in out["message"]
        assert "NEVER been attempted" not in out["message"]

    def test_shopify_live_run_clean(self):
        h = _Harness()
        out, _ = _run([SKU_SHOPIFY_ONLY], harness=h, sub_source="SWH Shopify",
                      channel="Shopify", check_staleness=False, dry_run=False)
        assert out["revise_proven"] is True
        assert "fired live" not in out["message"]
        assert "NEVER been attempted" not in out["message"]


class TestProvenListStillDerivedFromRegistry:
    """AC7: the proven-channel list named in the warning is still derived
    from the registry, not hard-coded -- flipping a fixture flag changes it."""

    def test_flipping_amazon_proven_in_a_fixture_changes_the_named_list(self):
        with patch.dict(server.GLT_CHANNELS["amazon"], {"revise_proven": True}):
            assert "Amazon" in server._proven_revise_channels()
            out, _ = _run([SKU_AMZ], sub_source="The Warehouse Group", channel="Amazon",
                          check_staleness=False)
            assert out["revise_proven"] is True
            assert "fired live on Amazon" not in out["message"]
        # Reverted outside the patch -- the real registry is untouched.
        assert server.GLT_CHANNELS["amazon"]["revise_proven"] is False


class TestDocstringNoLongerClaimsAmazonVariationShapeUnobserved:
    """AC8: the docstring records one Amazon family observed carrying a
    template per child, and that this is one observation, not a rule."""

    def test_stale_unestablished_phrase_is_gone(self):
        doc = server.refresh_channel_listing.__doc__
        assert "unestablished — nothing in this repo has observed it either way" not in doc

    def test_docstring_records_the_bushings_observation(self):
        doc = server.refresh_channel_listing.__doc__
        assert "bushings" in doc
        assert "one observation, not a rule" in doc


class TestNoBehaviouralChange:
    """AC10 (partial -- the rest is covered by the full suite passing with
    only the two deliberately-updated substrings): plan building, action
    selection and the ProcessTemplates payload are unaffected by the new
    registry field and message wording."""

    def test_plan_shape_and_process_payload_unaffected_by_the_new_field(self):
        h = _Harness()
        out, h = _run([SKU_AMZ], harness=h, sub_source="The Warehouse Group",
                      channel="Amazon", check_staleness=False, dry_run=False)
        assert len(out["plan"]) == 2
        tids = sorted(r["template_id"] for r in out["plan"])
        assert tids == [32115, 32381]
        pushed_ids = sorted(c["TemplateId"] for c in h.process_calls)
        assert pushed_ids == [32115, 32381]
        assert all(c["Action"] == "Revise" for c in h.process_calls)
