"""
Tests for `revise_ebay_listing_description` (issue #43) — the first eBay write
capability in this server, via a separate, non-GLT `Listings/` API family.

Live-probed shapes these tests encode (25 Aug 2026, this tenant):

  * The channel-SKU table's eBay SubSource is "EBAY0" — the same identifier
    the Listings API's configurators use.
  * `Inventory/GetInventoryItemDescriptions`' EBAY row uses a DIFFERENT
    sub_source, "EBAY0_UK" — same tenant, same SKU. An exact match against
    the channel-SKU store id would silently miss the real description
    override.
  * A multi-variation eBay listing is ONE template covering MANY Linnworks
    SKUs, all sharing one `ChannelReferenceId` (the eBay item id) in the
    channel-SKU table — a naive per-SKU loop would revise the same listing
    once per SKU.
  * Live templates read while building this feature never contained seller
    design-wrapper markers ("Check out our eBay reviews", etc.) in
    `Description` — it reads as plain inner content, matching the item's own
    channel description row byte-for-byte.
  * `Listings/ProcesseBayListings` was NOT fired live in this build (issue
    #43) — `EBAY_CHANNELS["ebay"]["revise_proven"]` stays False and every
    live-run result is reported "unconfirmed", never "success".

Issue #47 (26 Aug 2026, docs-only correction): the push WAS since fired live
against a real eBay listing and turned out to be ACCEPTED by Linnworks but
never observed to reach the channel — a distinct fact from `revise_proven`
(which still stays False). `EBAY_CHANNELS["ebay"]` now also carries
`push_observed_state` / `push_observed_reason` recording that observation as
data, and every message/plan-row/result-row that mentions it must DERIVE from
those two fields rather than hard-code the wording — the tests below pin
that, plus a stale-phrase guard asserting the old "never fired against a
real listing" claim no longer survives (uncorrected) anywhere in server.py,
README.md or CLAUDE.md.
"""
from unittest.mock import patch

import pytest

import server


# ── Fixture stock items ───────────────────────────────────────────────────
SID_ADULT = "aaaaaaaa-0000-0000-0000-00000000000a"   # shares a listing with...
SID_JUNIOR = "aaaaaaaa-0000-0000-0000-00000000000b"  # ...this one (dedupe target)
SID_OTHER_STORE = "aaaaaaaa-0000-0000-0000-00000000000c"  # eBay row, wrong store
SID_NO_DESC = "aaaaaaaa-0000-0000-0000-00000000000d"      # no usable description
SID_NOT_EBAY = "aaaaaaaa-0000-0000-0000-00000000000e"     # only a Shopify row
SID_NEVER_SYNCED = "aaaaaaaa-0000-0000-0000-00000000000f"  # LastUpdate never confirmed

SKU_ADULT = "vnm-triplepads-yellowblack-adt"
SKU_JUNIOR = "vnm-triplepads-yellowblack-jnr"
SKU_OTHER_STORE = "vnm-other-store-item"
SKU_NO_DESC = "vnm-no-description-item"
SKU_NOT_EBAY = "vnm-shopify-only-item"
SKU_NEVER_SYNCED = "vnm-never-synced-item"

LISTING_ID = "285656695376"
NEVER_SYNCED_LISTING_ID = "222222222222"

SKU_TO_SID = {
    SKU_ADULT: SID_ADULT,
    SKU_JUNIOR: SID_JUNIOR,
    SKU_OTHER_STORE: SID_OTHER_STORE,
    SKU_NO_DESC: SID_NO_DESC,
    SKU_NOT_EBAY: SID_NOT_EBAY,
    SKU_NEVER_SYNCED: SID_NEVER_SYNCED,
}

CHANNEL_SKUS = {
    SID_ADULT: [
        {"Source": "EBAY", "SubSource": "EBAY0", "ChannelReferenceId": LISTING_ID,
         "ListedQuantity": 5, "LastUpdate": "2026-08-01T10:00:00"},
    ],
    SID_JUNIOR: [
        {"Source": "EBAY", "SubSource": "EBAY0", "ChannelReferenceId": LISTING_ID,
         "ListedQuantity": 3, "LastUpdate": "2026-08-01T10:00:00"},
    ],
    SID_OTHER_STORE: [
        {"Source": "EBAY", "SubSource": "EBAY0_FR", "ChannelReferenceId": "999999999999",
         "ListedQuantity": 1, "LastUpdate": "2026-08-01T10:00:00"},
    ],
    SID_NO_DESC: [
        {"Source": "EBAY", "SubSource": "EBAY0", "ChannelReferenceId": "111111111111",
         "ListedQuantity": 2, "LastUpdate": "2026-08-01T10:00:00"},
    ],
    SID_NOT_EBAY: [
        {"Source": "SHOPIFY", "SubSource": "SWH Shopify", "ChannelReferenceId": "1:2:3",
         "ListedQuantity": 1, "LastUpdate": "2026-08-01T10:00:00"},
    ],
    SID_NEVER_SYNCED: [
        {"Source": "EBAY", "SubSource": "EBAY0", "ChannelReferenceId": NEVER_SYNCED_LISTING_ID,
         "ListedQuantity": 30, "LastUpdate": "0001-01-01T00:00:00"},
    ],
}

WRAPPER_DESC = (
    "<p>Pack of pads for junior and adult riders.</p>"
    "<p>Check out our eBay reviews!</p>"
    "<p>Need Help Deciding? Message us.</p>"
)

DESCRIPTIONS = {
    SID_ADULT: [
        {"Source": "EBAY", "SubSource": "EBAY0_UK", "Description": WRAPPER_DESC,
         "pkRowId": "d1"},
    ],
    SID_JUNIOR: [
        {"Source": "EBAY", "SubSource": "EBAY0_UK", "Description": WRAPPER_DESC,
         "pkRowId": "d2"},
    ],
    SID_OTHER_STORE: [
        {"Source": "EBAY", "SubSource": "EBAY0_FR_UK", "Description": "<p>Other store</p>",
         "pkRowId": "d3"},
    ],
    SID_NO_DESC: [],   # no description rows at all
    SID_NEVER_SYNCED: [
        {"Source": "EBAY", "SubSource": "EBAY0_UK", "Description": "<p>Never-synced item</p>",
         "pkRowId": "d4"},
    ],
}


def _mock(rate_limit_sku=None, template_for_listing=None):
    """Mock the call layer. rate_limit_sku, if set, makes GetInventoryItem for
    that one SKU raise RateLimitError. template_for_listing, if set, is what
    _find_ebay_template_for_listing should locate on a live run."""
    push_calls = []

    def call_linnworks(path, payload):
        if path.endswith("GetInventoryItem"):
            sku = payload.get("sku")
            if sku == rate_limit_sku:
                raise server.RateLimitError(f"rate limited resolving {sku}")
            if sku not in SKU_TO_SID:
                raise RuntimeError(f"HTTP 400 — no inventory item for SKU '{sku}'")
            return {"StockItemId": SKU_TO_SID[sku], "ItemTitle": f"Title for {sku}"}
        if path.endswith("BatchGetInventoryItemChannelSKUs"):
            ids = payload["inventoryItemIds"]
            return [{"StockItemId": sid, "ChannelSkus": CHANNEL_SKUS.get(sid, [])}
                    for sid in ids]
        if path.endswith("GeteBayTemplates"):
            cfg_id = payload["parameters"]["ConfigId"]
            page = payload["parameters"]["PageNumber"]
            if page > 1:
                return {"Items": [], "TotalItems": 1}
            if template_for_listing and cfg_id == "cfg-1":
                return {"Items": [template_for_listing], "TotalItems": 1}
            return {"Items": [], "TotalItems": 0}
        if path.endswith("ProcesseBayListings"):
            push_calls.append(payload)
            return {}
        raise AssertionError(f"Unexpected call_linnworks: {path}")

    def call_linnworks_get(path, params=None):
        if path.endswith("GetInventoryItemDescriptions"):
            sid = params["inventoryItemId"]
            return DESCRIPTIONS.get(sid, [])
        if path.endswith("GeteBayConfigurators"):
            return [{"pkConfigId": "cfg-1", "ConfigName": "Test config",
                     "AssociatedTemplates": 5}]
        raise AssertionError(f"Unexpected GET: {path}")

    def call_linnworks_void(path, payload):
        if path.endswith("ProcesseBayListings"):
            push_calls.append(payload)
            return None
        raise AssertionError(f"Unexpected void call: {path}")

    patches = [
        patch.object(server, "call_linnworks", side_effect=call_linnworks),
        patch.object(server, "call_linnworks_get", side_effect=call_linnworks_get),
        patch.object(server, "call_linnworks_void", side_effect=call_linnworks_void),
    ]
    return patches, push_calls


def _run(patches, **kwargs):
    for p in patches:
        p.start()
    try:
        return server.revise_ebay_listing_description(**kwargs)
    finally:
        for p in patches:
            p.stop()


# ── AC4: store matching, case-insensitive; exactly one bucket per SKU ──────

def test_store_matching_is_case_insensitive_and_buckets_are_exclusive():
    patches, _ = _mock()
    r = _run(patches, skus=[SKU_ADULT, SKU_OTHER_STORE], store="ebay0", dry_run=True)

    covered = {sku for row in r["plan"] for sku in row["covers_skus"]}
    assert SKU_ADULT in covered
    assert any(n["sku"] == SKU_OTHER_STORE for n in r["not_listed"])
    assert not any(n["sku"] == SKU_ADULT for n in r["not_listed"])
    assert r["unresolved"] == []
    assert r["rate_limited"] == []


def test_sku_with_no_ebay_row_is_not_listed():
    patches, _ = _mock()
    r = _run(patches, skus=[SKU_NOT_EBAY], store="EBAY0", dry_run=True)
    assert r["plan"] == []
    assert len(r["not_listed"]) == 1
    assert r["not_listed"][0]["sku"] == SKU_NOT_EBAY


def test_unresolved_sku_reported_as_unresolved():
    patches, _ = _mock()
    r = _run(patches, skus=["totally-unknown-sku"], store="EBAY0", dry_run=True)
    assert r["plan"] == []
    assert r["not_listed"] == []
    assert len(r["unresolved"]) == 1


# ── AC5: listing-id dedupe ───────────────────────────────────────────────

def test_skus_sharing_one_listing_id_collapse_to_one_plan_row():
    patches, _ = _mock()
    r = _run(patches, skus=[SKU_ADULT, SKU_JUNIOR], store="EBAY0", dry_run=True)

    assert len(r["plan"]) == 1
    row = r["plan"][0]
    assert row["listing_id"] == LISTING_ID
    assert sorted(row["covers_skus"]) == sorted([SKU_ADULT, SKU_JUNIOR])


def test_nineteen_sku_multi_variation_listing_produces_one_plan_row_and_one_push():
    skus = [f"vnm-19var-{i}" for i in range(19)]
    sids = [f"bbbbbbbb-0000-0000-0000-{i:012d}" for i in range(19)]
    sku_to_sid = dict(zip(skus, sids))
    channel_skus = {
        sid: [{"Source": "EBAY", "SubSource": "EBAY0",
               "ChannelReferenceId": "999888777666", "ListedQuantity": 1}]
        for sid in sids
    }
    descriptions = {
        sid: [{"Source": "EBAY", "SubSource": "EBAY0_UK",
               "Description": "<p>Shared listing description</p>", "pkRowId": f"d{i}"}]
        for i, sid in enumerate(sids)
    }
    template = {
        "TemplateId": "tpl-19", "InventoryItemId": sids[0], "ConfigId": "cfg-1",
        "SKU": "vnm-19var-group", "ListingIds": ["999888777666"],
        "Title": "Group title", "Description": "<p>stale</p>",
    }

    push_calls = []

    def call_linnworks(path, payload):
        if path.endswith("GetInventoryItem"):
            sku = payload.get("sku")
            if sku not in sku_to_sid:
                raise RuntimeError(f"no item for {sku}")
            return {"StockItemId": sku_to_sid[sku], "ItemTitle": sku}
        if path.endswith("BatchGetInventoryItemChannelSKUs"):
            return [{"StockItemId": sid, "ChannelSkus": channel_skus.get(sid, [])}
                    for sid in payload["inventoryItemIds"]]
        if path.endswith("GeteBayTemplates"):
            if payload["parameters"]["PageNumber"] > 1:
                return {"Items": [], "TotalItems": 1}
            return {"Items": [template], "TotalItems": 1}
        if path.endswith("ProcesseBayListings"):
            push_calls.append(payload)
            return {}
        raise AssertionError(f"Unexpected call_linnworks: {path}")

    def call_linnworks_get(path, params=None):
        if path.endswith("GetInventoryItemDescriptions"):
            return descriptions.get(params["inventoryItemId"], [])
        if path.endswith("GeteBayConfigurators"):
            return [{"pkConfigId": "cfg-1", "AssociatedTemplates": 19}]
        raise AssertionError(f"Unexpected GET: {path}")

    def call_linnworks_void(path, payload):
        if path.endswith("ProcesseBayListings"):
            push_calls.append(payload)
            return None
        raise AssertionError

    patches = [
        patch.object(server, "call_linnworks", side_effect=call_linnworks),
        patch.object(server, "call_linnworks_get", side_effect=call_linnworks_get),
        patch.object(server, "call_linnworks_void", side_effect=call_linnworks_void),
    ]
    dry = _run(patches, skus=skus, store="EBAY0", dry_run=True)
    assert len(dry["plan"]) == 1
    assert len(dry["plan"][0]["covers_skus"]) == 19

    live = _run(patches, skus=skus, store="EBAY0", confirmed_count=len(skus), dry_run=False)
    assert len(push_calls) == 1, f"expected ONE push call, got {len(push_calls)}"
    assert len(live["results"]) == 1


# ── AC6: missing-description block ──────────────────────────────────────

def test_sku_with_no_usable_description_is_blocked_with_named_reason():
    patches, push_calls = _mock()
    dry = _run(patches, skus=[SKU_NO_DESC], store="EBAY0", dry_run=True)

    assert len(dry["plan"]) == 1
    row = dry["plan"][0]
    assert row["blocked"] is True
    assert row["blocked_reason"]
    assert row["description"] is None

    live = _run(patches, skus=[SKU_NO_DESC], store="EBAY0", dry_run=False)
    assert live["results"][0]["outcome"] == "blocked"
    assert push_calls == [], "a SKU with no usable description must never be pushed"


# ── AC7: seller design-template / wrapper preservation ─────────────────────

def test_wrapper_markers_survive_untouched_in_the_planned_description():
    patches, _ = _mock()
    r = _run(patches, skus=[SKU_ADULT], store="EBAY0", dry_run=True)

    pushed_description = r["plan"][0]["description"]
    assert "Check out our eBay reviews" in pushed_description
    assert "Need Help Deciding?" in pushed_description
    assert pushed_description == WRAPPER_DESC


# ── Stale-snapshot family (QA round 1 blocking finding): whatever gets ─────
# pushed (Title/Price on the STORED template) must be shown in the dry-run
# manifest, and a live push must be read back afterwards.

def test_dry_run_manifest_shows_the_stored_template_title_and_flags_it_stale():
    stale_template = {
        "TemplateId": "tpl-1", "InventoryItemId": SID_ADULT, "ConfigId": "cfg-1",
        "SKU": SKU_ADULT, "ListingIds": [LISTING_ID],
        "Title": "OLD 2025 TITLE", "Price": 79.95, "Description": "old",
    }
    patches, _ = _mock(template_for_listing=stale_template)
    r = _run(patches, skus=[SKU_ADULT, SKU_JUNIOR], store="EBAY0", dry_run=True)

    row = r["plan"][0]
    assert row["template_found"] is True
    staleness = row["staleness"]
    assert staleness["stored_title"] == "OLD 2025 TITLE"
    assert staleness["stored_price"] == 79.95
    # the resolved item's current title ("Title for <sku>") disagrees with the
    # stored template's title — this must be visible BEFORE any write happens.
    assert staleness["title_stale"] is True
    assert staleness["warning"]


def test_dry_run_manifest_shows_the_stored_template_even_when_not_stale():
    fresh_template = {
        "TemplateId": "tpl-1", "InventoryItemId": SID_ADULT, "ConfigId": "cfg-1",
        "SKU": SKU_ADULT, "ListingIds": [LISTING_ID],
        "Title": f"Title for {SKU_ADULT}", "Price": 19.95, "Description": "old",
    }
    patches, _ = _mock(template_for_listing=fresh_template)
    r = _run(patches, skus=[SKU_ADULT], store="EBAY0", dry_run=True)

    row = r["plan"][0]
    assert row["template_found"] is True
    assert row["staleness"]["title_stale"] is False
    assert row["staleness"]["stored_price"] == 19.95


def test_live_push_is_read_back_afterwards_and_confirms_or_denies_the_match():
    """Acceptance alone is never enough — after a live push, the tool re-reads
    the STORED template (a cheap, config-scoped re-check) and reports whether
    its Description now matches what was sent."""
    template = {
        "TemplateId": "tpl-1", "InventoryItemId": SID_ADULT, "ConfigId": "cfg-1",
        "SKU": SKU_ADULT, "ListingIds": [LISTING_ID], "Title": "T", "Description": "old",
    }
    state = {"description": template["Description"]}

    def call_linnworks(path, payload):
        if path.endswith("GetInventoryItem"):
            sku = payload.get("sku")
            return {"StockItemId": SKU_TO_SID[sku], "ItemTitle": sku}
        if path.endswith("BatchGetInventoryItemChannelSKUs"):
            return [{"StockItemId": sid, "ChannelSkus": CHANNEL_SKUS.get(sid, [])}
                    for sid in payload["inventoryItemIds"]]
        if path.endswith("GeteBayTemplates"):
            if payload["parameters"]["PageNumber"] > 1:
                return {"Items": [], "TotalItems": 1}
            return {"Items": [{**template, "Description": state["description"]}],
                    "TotalItems": 1}
        raise AssertionError(f"Unexpected call_linnworks: {path}")

    def call_linnworks_get(path, params=None):
        if path.endswith("GetInventoryItemDescriptions"):
            return DESCRIPTIONS.get(params["inventoryItemId"], [])
        if path.endswith("GeteBayConfigurators"):
            return [{"pkConfigId": "cfg-1", "AssociatedTemplates": 5}]
        raise AssertionError(f"Unexpected GET: {path}")

    def call_linnworks_void(path, payload):
        if path.endswith("ProcesseBayListings"):
            state["description"] = payload["items"][0]["Description"]
            return None
        raise AssertionError

    patches = [
        patch.object(server, "call_linnworks", side_effect=call_linnworks),
        patch.object(server, "call_linnworks_get", side_effect=call_linnworks_get),
        patch.object(server, "call_linnworks_void", side_effect=call_linnworks_void),
    ]
    r = _run(patches, skus=[SKU_ADULT, SKU_JUNIOR], store="EBAY0", dry_run=False)

    assert r["results"][0]["outcome"] == "unconfirmed"
    assert r["results"][0]["post_push_description_matches"] is True
    assert state["description"] == WRAPPER_DESC


# ── A channel-SKU row proves a MAPPING, not a live listing (QA round 1) ────

def test_never_synced_channel_sku_row_is_flagged_not_silently_trusted():
    # A locatable template isolates the never-synced behaviour under test
    # from the (QA round 3) unlocatable-template block — this test is about
    # the never-synced warning, not template resolution.
    template = {
        "TemplateId": "tpl-never-synced", "InventoryItemId": SID_NEVER_SYNCED,
        "ConfigId": "cfg-1", "SKU": SKU_NEVER_SYNCED,
        "ListingIds": [NEVER_SYNCED_LISTING_ID], "Title": "T", "Description": "old",
    }
    patches, _ = _mock(template_for_listing=template)
    r = _run(patches, skus=[SKU_NEVER_SYNCED], store="EBAY0", dry_run=True)

    assert len(r["plan"]) == 1
    row = r["plan"][0]
    assert row["never_synced_skus"] == [SKU_NEVER_SYNCED]
    assert row["never_synced_warning"]
    assert "0001-01-01" in row["never_synced_warning"]
    # a never-synced mapping is a caution, not proof the listing is dead —
    # the SKU is still planned, not force-blocked (as long as its template
    # can be located; see test_dry_run_marks_unlocatable_template_as_blocked
    # _not_pushable for the separate template-locate block).
    assert row["template_found"] is True
    assert row["blocked"] is False


def test_synced_channel_sku_rows_carry_no_never_synced_warning():
    patches, _ = _mock()
    r = _run(patches, skus=[SKU_ADULT, SKU_JUNIOR], store="EBAY0", dry_run=True)

    row = r["plan"][0]
    assert row["never_synced_skus"] == []
    assert "never_synced_warning" not in row


# ── AC8: write safety — dry_run default, threshold, injection, manifest ────

def test_dry_run_is_the_default_and_makes_zero_write_calls():
    patches, push_calls = _mock()
    r = _run(patches, skus=[SKU_ADULT, SKU_JUNIOR])   # dry_run not passed
    assert r["dry_run"] is True
    assert push_calls == []


def test_dry_run_previews_the_stored_template_but_makes_zero_write_calls():
    """A dry run now DOES sweep GeteBayConfigurators/GeteBayTemplates (a read)
    per unblocked plan row, to preview the stored Title/Price the live push
    would send (the stale-snapshot trap — v1.27.1 echo). It must still never
    call ProcesseBayListings (the write)."""
    template = {
        "TemplateId": "tpl-1", "InventoryItemId": SID_ADULT, "ConfigId": "cfg-1",
        "SKU": SKU_ADULT, "ListingIds": [LISTING_ID], "Title": "T", "Description": "old",
    }

    def call_linnworks(path, payload):
        if path.endswith("GetInventoryItem"):
            sku = payload.get("sku")
            return {"StockItemId": SKU_TO_SID[sku], "ItemTitle": sku}
        if path.endswith("BatchGetInventoryItemChannelSKUs"):
            return [{"StockItemId": sid, "ChannelSkus": CHANNEL_SKUS.get(sid, [])}
                    for sid in payload["inventoryItemIds"]]
        if path.endswith("GeteBayTemplates"):
            if payload["parameters"]["PageNumber"] > 1:
                return {"Items": [], "TotalItems": 1}
            return {"Items": [template], "TotalItems": 1}
        raise AssertionError(f"dry run must not call: {path}")

    def call_linnworks_get(path, params=None):
        if path.endswith("GetInventoryItemDescriptions"):
            return DESCRIPTIONS.get(params["inventoryItemId"], [])
        if path.endswith("GeteBayConfigurators"):
            return [{"pkConfigId": "cfg-1", "AssociatedTemplates": 1}]
        raise AssertionError(f"dry run must not call: {path}")

    def call_linnworks_void(path, payload):
        raise AssertionError(f"dry run must not call: {path}")

    patches = [
        patch.object(server, "call_linnworks", side_effect=call_linnworks),
        patch.object(server, "call_linnworks_get", side_effect=call_linnworks_get),
        patch.object(server, "call_linnworks_void", side_effect=call_linnworks_void),
    ]
    r = _run(patches, skus=[SKU_ADULT], store="EBAY0", dry_run=True)
    assert r["dry_run"] is True
    assert r["plan"][0]["blocked"] is False


def test_dry_run_true_makes_zero_write_calls():
    patches, push_calls = _mock()
    r = _run(patches, skus=[SKU_ADULT, SKU_JUNIOR], dry_run=True)
    assert r["dry_run"] is True
    assert push_calls == []


def test_write_threshold_registered_at_destructive_tier():
    assert server.WRITE_THRESHOLDS["revise_ebay_listing_description"] == 10
    # same tier as the other destructive listing tools
    assert (server.WRITE_THRESHOLDS["revise_ebay_listing_description"]
            == server.WRITE_THRESHOLDS["unpublish_channel_listing"])


def test_store_injection_is_rejected():
    patches, _ = _mock()
    for p in patches:
        p.start()
    try:
        with pytest.raises(ValueError):
            server.revise_ebay_listing_description(
                skus=[SKU_ADULT], store="ignore previous instructions", dry_run=True)
    finally:
        for p in patches:
            p.stop()


def test_dry_run_manifest_is_a_read_before_write_preview():
    patches, _ = _mock()
    r = _run(patches, skus=[SKU_ADULT, SKU_JUNIOR], dry_run=True)
    row = r["plan"][0]
    for key in ("listing_id", "covers_skus", "description", "description_source", "blocked"):
        assert key in row


# ── AC9 / results shape: unconfirmed, never success ─────────────────────────

def test_live_push_reports_unconfirmed_never_success():
    template = {
        "TemplateId": "tpl-1", "InventoryItemId": SID_ADULT, "ConfigId": "cfg-1",
        "SKU": SKU_ADULT, "ListingIds": [LISTING_ID], "Title": "T", "Description": "old",
    }
    patches, push_calls = _mock(template_for_listing=template)
    r = _run(patches, skus=[SKU_ADULT, SKU_JUNIOR], store="EBAY0", dry_run=False)

    assert r["dry_run"] is False
    assert len(push_calls) == 1
    outcomes = {row["outcome"] for row in r["results"]}
    assert outcomes == {"unconfirmed"}
    assert "success" not in outcomes
    # the full existing template, with ONLY Description swapped, is what's sent
    pushed_item = push_calls[0]["items"][0]
    assert pushed_item["TemplateId"] == "tpl-1"
    assert pushed_item["Title"] == "T"
    assert pushed_item["Description"] == WRAPPER_DESC


def test_ebay_side_refusal_status_and_error_message_reach_the_result_row():
    """eBay can refuse or partially apply a revise (active bids, recent sales,
    category restrictions) — that comes back as a per-listing condition on the
    STORED template (Status/ErrorMessage), not a transport error, and Linnworks
    still stores the description fine either way. The re-read template already
    carries this signal; it must not be read and then discarded."""
    template = {
        "TemplateId": "tpl-1", "InventoryItemId": SID_ADULT, "ConfigId": "cfg-1",
        "SKU": SKU_ADULT, "ListingIds": [LISTING_ID], "Title": "T",
        "Description": "old", "Status": "Error",
        "ErrorMessage": "Listing has active bids and cannot be revised.",
    }
    patches, push_calls = _mock(template_for_listing=template)
    r = _run(patches, skus=[SKU_ADULT, SKU_JUNIOR], store="EBAY0", dry_run=False)

    assert len(push_calls) == 1
    row = r["results"][0]
    assert row["outcome"] == "unconfirmed"
    assert row["post_push_status"] == "Error"
    assert row["post_push_error_message"] == "Listing has active bids and cannot be revised."


def test_ebay_side_refusal_fields_are_none_not_missing_when_clean():
    template = {
        "TemplateId": "tpl-1", "InventoryItemId": SID_ADULT, "ConfigId": "cfg-1",
        "SKU": SKU_ADULT, "ListingIds": [LISTING_ID], "Title": "T",
        "Description": "old", "Status": "Listed", "ErrorMessage": "",
    }
    patches, _ = _mock(template_for_listing=template)
    r = _run(patches, skus=[SKU_ADULT, SKU_JUNIOR], store="EBAY0", dry_run=False)
    row = r["results"][0]
    assert row["post_push_status"] == "Listed"
    assert row["post_push_error_message"] in (None, "")


def test_live_push_template_not_found_is_blocked_not_pushed():
    patches, push_calls = _mock(template_for_listing=None)
    r = _run(patches, skus=[SKU_ADULT, SKU_JUNIOR], store="EBAY0", dry_run=False)
    assert push_calls == []
    assert r["results"][0]["outcome"] == "blocked"


# ── AC10: not-yet-proven flag derived from a registry, not hard-coded ──────

def test_revise_proven_flag_and_message_are_derived_from_the_registry():
    patches, _ = _mock()
    r = _run(patches, skus=[SKU_ADULT], store="EBAY0", dry_run=True)
    assert r["revise_proven"] is False
    assert "none" in r["verification_note"]
    assert "vi.vipr.ebaydesc.com/itmdesc" in r["verification_note"]
    # QA round 2: the flag must be surfaced on EVERY plan row, not just the
    # top-level response — refresh_channel_listing's precedent does both.
    assert r["plan"], "expected at least one plan row"
    for row in r["plan"]:
        assert row["revise_proven"] is False

    # Flip the registry and confirm the message — AND the per-row flag — are
    # DERIVED, not hard-coded — the same fix already applied twice to
    # GLT_CHANNELS in this codebase.
    with patch.dict(server.EBAY_CHANNELS["ebay"], {"revise_proven": True}):
        patches2, _ = _mock()
        r2 = _run(patches2, skus=[SKU_ADULT], store="EBAY0", dry_run=True)
        assert r2["revise_proven"] is True
        assert "EBAY" in r2["verification_note"]
        assert "none" not in r2["verification_note"]
        for row in r2["plan"]:
            assert row["revise_proven"] is True


# ── AC11: quota failures bucketed separately, never mislabelled ────────────

def test_rate_limited_resolution_is_never_reported_as_not_listed_or_unresolved():
    patches, _ = _mock(rate_limit_sku=SKU_ADULT)
    r = _run(patches, skus=[SKU_ADULT, SKU_JUNIOR], store="EBAY0", dry_run=True)

    assert any(row["sku"] == SKU_ADULT for row in r["rate_limited"])
    assert not any(row.get("sku") == SKU_ADULT for row in r["not_listed"])
    assert not any(row.get("sku") == SKU_ADULT for row in r["unresolved"])
    assert r["complete"] is False


def test_complete_is_true_when_nothing_rate_limited():
    patches, _ = _mock()
    r = _run(patches, skus=[SKU_ADULT], store="EBAY0", dry_run=True)
    assert r["complete"] is True
    assert r["rate_limited"] == []


# ── QA round 3 blocking finding: the template-locate sweep (this tool's ────
# heaviest quota consumer — no by-listing-id lookup exists on this API) must
# never fold a RateLimitError into "template not found". Covers both places
# it can fire (GeteBayTemplates itself, and GeteBayConfigurators when the
# config id isn't already known) plus the post-push read-back, and the
# related fix that an unlocatable template is reflected in `blocked` so the
# dry-run pushable count can't promise a push the live run would refuse.

def test_rate_limit_while_sweeping_templates_is_bucketed_not_reported_as_not_found():
    def call_linnworks(path, payload):
        if path.endswith("GetInventoryItem"):
            sku = payload.get("sku")
            return {"StockItemId": SKU_TO_SID[sku], "ItemTitle": sku}
        if path.endswith("BatchGetInventoryItemChannelSKUs"):
            return [{"StockItemId": sid, "ChannelSkus": CHANNEL_SKUS.get(sid, [])}
                    for sid in payload["inventoryItemIds"]]
        if path.endswith("GeteBayTemplates"):
            raise server.RateLimitError("quota exceeded sweeping eBay templates")
        raise AssertionError(f"Unexpected call_linnworks: {path}")

    def call_linnworks_get(path, params=None):
        if path.endswith("GetInventoryItemDescriptions"):
            return DESCRIPTIONS.get(params["inventoryItemId"], [])
        if path.endswith("GeteBayConfigurators"):
            return [{"pkConfigId": "cfg-1", "AssociatedTemplates": 5}]
        raise AssertionError(f"Unexpected GET: {path}")

    def call_linnworks_void(path, payload):
        raise AssertionError("must never push when the template couldn't be located")

    patches = [
        patch.object(server, "call_linnworks", side_effect=call_linnworks),
        patch.object(server, "call_linnworks_get", side_effect=call_linnworks_get),
        patch.object(server, "call_linnworks_void", side_effect=call_linnworks_void),
    ]

    dry = _run(patches, skus=[SKU_ADULT], store="EBAY0", dry_run=True)
    assert len(dry["plan"]) == 1
    row = dry["plan"][0]
    assert row["blocked"] is True
    assert row["blocked_reason"] == "rate_limited_locating_template"
    assert "not found" not in row["blocked_reason"]
    assert any(rl.get("listing_id") == LISTING_ID for rl in dry["rate_limited"])
    assert dry["complete"] is False
    # never mislabelled as either of these buckets
    assert not any(n.get("sku") == SKU_ADULT for n in dry["not_listed"])
    assert not any(u.get("sku") == SKU_ADULT for u in dry["unresolved"])

    live = _run(patches, skus=[SKU_ADULT], store="EBAY0", dry_run=False)
    assert live["results"][0]["outcome"] == "rate_limited"
    assert live["results"][0]["outcome"] != "blocked"
    assert live["complete"] is False


def test_rate_limit_while_listing_configurators_is_bucketed_not_reported_as_not_found():
    def call_linnworks(path, payload):
        if path.endswith("GetInventoryItem"):
            sku = payload.get("sku")
            return {"StockItemId": SKU_TO_SID[sku], "ItemTitle": sku}
        if path.endswith("BatchGetInventoryItemChannelSKUs"):
            return [{"StockItemId": sid, "ChannelSkus": CHANNEL_SKUS.get(sid, [])}
                    for sid in payload["inventoryItemIds"]]
        raise AssertionError(f"Unexpected call_linnworks: {path}")

    def call_linnworks_get(path, params=None):
        if path.endswith("GetInventoryItemDescriptions"):
            return DESCRIPTIONS.get(params["inventoryItemId"], [])
        if path.endswith("GeteBayConfigurators"):
            raise server.RateLimitError("quota exceeded listing eBay configurators")
        raise AssertionError(f"Unexpected GET: {path}")

    def call_linnworks_void(path, payload):
        raise AssertionError("must never push when the template couldn't be located")

    patches = [
        patch.object(server, "call_linnworks", side_effect=call_linnworks),
        patch.object(server, "call_linnworks_get", side_effect=call_linnworks_get),
        patch.object(server, "call_linnworks_void", side_effect=call_linnworks_void),
    ]

    dry = _run(patches, skus=[SKU_ADULT], store="EBAY0", dry_run=True)
    row = dry["plan"][0]
    assert row["blocked"] is True
    assert row["blocked_reason"] == "rate_limited_locating_template"
    assert dry["complete"] is False
    assert any(rl.get("listing_id") == LISTING_ID for rl in dry["rate_limited"])


def test_dry_run_marks_unlocatable_template_as_blocked_not_pushable():
    """The related fix: a row whose template genuinely can't be found (no
    quota issue) must count as blocked in the dry-run manifest, not
    pushable — otherwise the manifest promises a push the live run then
    refuses (server.py: 'could not locate the eBay template...')."""
    patches, push_calls = _mock(template_for_listing=None)
    dry = _run(patches, skus=[SKU_ADULT], store="EBAY0", dry_run=True)

    row = dry["plan"][0]
    assert row["blocked"] is True
    assert row["blocked_reason"] == "could not locate the eBay template serving this listing id"
    assert dry["message"].startswith("0 listing(s) would be revised, 1 blocked")
    assert push_calls == []


def test_post_push_read_back_rate_limit_is_bucketed_not_silently_none():
    """A quota failure on the (successful-push's) config-scoped read-back
    must be distinguishable from an ordinary 'read back found nothing'."""
    template = {
        "TemplateId": "tpl-1", "InventoryItemId": SID_ADULT, "ConfigId": "cfg-1",
        "SKU": SKU_ADULT, "ListingIds": [LISTING_ID], "Title": "T", "Description": "old",
    }
    calls = {"templates": 0}

    def call_linnworks(path, payload):
        if path.endswith("GetInventoryItem"):
            sku = payload.get("sku")
            return {"StockItemId": SKU_TO_SID[sku], "ItemTitle": sku}
        if path.endswith("BatchGetInventoryItemChannelSKUs"):
            return [{"StockItemId": sid, "ChannelSkus": CHANNEL_SKUS.get(sid, [])}
                    for sid in payload["inventoryItemIds"]]
        if path.endswith("GeteBayTemplates"):
            calls["templates"] += 1
            if calls["templates"] == 1:
                # planning sweep: finds the template fine
                return {"Items": [template], "TotalItems": 1}
            # post-push read-back: quota exhausted
            raise server.RateLimitError("quota exceeded on read-back")
        raise AssertionError(f"Unexpected call_linnworks: {path}")

    def call_linnworks_get(path, params=None):
        if path.endswith("GetInventoryItemDescriptions"):
            return DESCRIPTIONS.get(params["inventoryItemId"], [])
        if path.endswith("GeteBayConfigurators"):
            return [{"pkConfigId": "cfg-1", "AssociatedTemplates": 5}]
        raise AssertionError(f"Unexpected GET: {path}")

    push_calls = []

    def call_linnworks_void(path, payload):
        if path.endswith("ProcesseBayListings"):
            push_calls.append(payload)
            return None
        raise AssertionError

    patches = [
        patch.object(server, "call_linnworks", side_effect=call_linnworks),
        patch.object(server, "call_linnworks_get", side_effect=call_linnworks_get),
        patch.object(server, "call_linnworks_void", side_effect=call_linnworks_void),
    ]
    r = _run(patches, skus=[SKU_ADULT, SKU_JUNIOR], store="EBAY0", dry_run=False)

    assert len(push_calls) == 1, "the push itself succeeded — only the read-back was rate-limited"
    row = r["results"][0]
    assert row["outcome"] == "unconfirmed"
    assert row["post_push_description_matches"] is None
    assert row["post_push_read_back_rate_limited"] is True
    assert any(rl.get("listing_id") == LISTING_ID for rl in r["rate_limited"])
    assert r["complete"] is False


# ── Issue #47: "accepted but never observed to process" — a distinct fact ──
# from `revise_proven`, recorded as DATA on EBAY_CHANNELS and derived into
# every message, plan row and result row, rather than hard-coded prose.

import ast as _ast
import re as _re
from pathlib import Path as _Path

_REPO_ROOT = _Path(__file__).resolve().parent.parent
_SERVER_SRC = (_REPO_ROOT / "server.py").read_text(encoding="utf-8")
_README_SRC = (_REPO_ROOT / "README.md").read_text(encoding="utf-8")
_CLAUDE_MD_SRC = (_REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")


def _normalize_comment_prose(text: str) -> str:
    """Collapse a `#`-commented block (or a docstring) into one whitespace-
    normalised line, so a phrase that happens to wrap across source lines is
    still findable as a plain substring."""
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            stripped = stripped[1:].strip()
        lines.append(stripped)
    return _re.sub(r"\s+", " ", " ".join(lines))


def test_ac1_ebay_channels_registry_defaults_revise_proven_and_observation_fields():
    entry = server.EBAY_CHANNELS["ebay"]
    assert entry["revise_proven"] is False
    # AC1: a field recording the observation state (that a live push was
    # submitted and no channel-side effect was observed)...
    assert "push_observed_state" in entry
    assert entry["push_observed_state"] == "accepted_but_not_processed"
    # ...and a human-readable reason string.
    assert "push_observed_reason" in entry
    reason = entry["push_observed_reason"]
    assert isinstance(reason, str) and len(reason) > 20
    assert "accepted" in reason.lower()
    assert "not" in reason.lower()


def test_ac2_verification_note_is_derived_from_the_registry_reason():
    """Patching the registry entry's reason changes the emitted text — proving
    the message is DERIVED, not hard-coded (the twice-fixed GLT_CHANNELS
    lesson, now applied here). Checks BOTH `verification_note` and the
    dry-run `message` (round 1 fixed only the former and shipped a message
    that stayed counts-only)."""
    original_reason = server.EBAY_CHANNELS["ebay"]["push_observed_reason"]

    patches, _ = _mock()
    r = _run(patches, skus=[SKU_ADULT], store="EBAY0", dry_run=True)
    assert original_reason in r["verification_note"]
    assert original_reason in r["message"]

    with patch.dict(server.EBAY_CHANNELS["ebay"],
                     {"push_observed_reason": "TOTALLY DIFFERENT REASON TEXT xyzzy"}):
        patches2, _ = _mock()
        r2 = _run(patches2, skus=[SKU_ADULT], store="EBAY0", dry_run=True)
        assert "TOTALLY DIFFERENT REASON TEXT xyzzy" in r2["verification_note"]
        assert original_reason not in r2["verification_note"]
        assert "TOTALLY DIFFERENT REASON TEXT xyzzy" in r2["message"]
        assert original_reason not in r2["message"]

    # And it reverts once the patch is undone — proving the message reads the
    # registry live at call time, not a cached copy.
    patches3, _ = _mock()
    r3 = _run(patches3, skus=[SKU_ADULT], store="EBAY0", dry_run=True)
    assert original_reason in r3["verification_note"]
    assert original_reason in r3["message"]


def test_ac2_reason_string_literal_appears_exactly_once_in_server_source():
    """The reason text must be written ONCE, in the registry — every message
    that shows it must interpolate the field, never retype the literal.
    Parsed with `ast` (not a raw substring search) because Python's implicit
    adjacent-string-literal concatenation means the runtime string is not
    contiguous text in the source file — `ast` reconstructs it exactly."""
    reason = server.EBAY_CHANNELS["ebay"]["push_observed_reason"]
    tree = _ast.parse(_SERVER_SRC)
    matches = [
        node for node in _ast.walk(tree)
        if isinstance(node, _ast.Constant)
        and isinstance(node.value, str)
        and node.value == reason
    ]
    assert len(matches) == 1, (
        f"push_observed_reason literal must appear exactly once in server.py "
        f"(in the EBAY_CHANNELS registry) — found {len(matches)} occurrence(s)."
    )


def test_ac3_live_run_message_states_not_observed_and_names_the_ui_route():
    template = {
        "TemplateId": "tpl-1", "InventoryItemId": SID_ADULT, "ConfigId": "cfg-1",
        "SKU": SKU_ADULT, "ListingIds": [LISTING_ID], "Title": "T", "Description": "old",
    }
    patches, _ = _mock(template_for_listing=template)
    r = _run(patches, skus=[SKU_ADULT, SKU_JUNIOR], store="EBAY0", dry_run=False)

    msg = r["message"]
    # Fact 1: an accepted push has not been observed to reach the channel.
    assert "accepted" in msg.lower()
    assert "not" in msg.lower() and "observed" in msg.lower()
    # Fact 2: the working route today is the Linnworks listing UI, after
    # reviewing this tool's dry-run manifest.
    assert "linnworks listing ui" in msg.lower()
    assert "dry-run manifest" in msg.lower() or "dry run manifest" in msg.lower()


def test_ac4_plan_row_and_result_row_carry_push_observed_state():
    template = {
        "TemplateId": "tpl-1", "InventoryItemId": SID_ADULT, "ConfigId": "cfg-1",
        "SKU": SKU_ADULT, "ListingIds": [LISTING_ID], "Title": "T", "Description": "old",
    }
    patches, _ = _mock(template_for_listing=template)
    dry = _run(patches, skus=[SKU_ADULT], store="EBAY0", dry_run=True)
    assert dry["plan"], "expected at least one plan row"
    for row in dry["plan"]:
        assert "push_observed_state" in row
        assert row["push_observed_state"] == server.EBAY_CHANNELS["ebay"]["push_observed_state"]
        # alongside the existing per-row revise_proven, not replacing it
        assert "revise_proven" in row

    patches2, _ = _mock(template_for_listing=template)
    live = _run(patches2, skus=[SKU_ADULT], store="EBAY0", dry_run=False)
    assert live["results"], "expected at least one result row"
    for row in live["results"]:
        assert "push_observed_state" in row
        assert row["push_observed_state"] == server.EBAY_CHANNELS["ebay"]["push_observed_state"]
        assert "revise_proven" in row


def test_ac5_docstring_no_longer_claims_the_push_was_never_fired():
    doc = server.revise_ebay_listing_description.__doc__
    assert doc is not None
    lowered = doc.lower()
    assert "never been fired against a real listing" not in lowered
    assert "never called against a real listing" not in lowered
    # States it WAS fired, gives the date, and the observed outcome.
    assert "fired live on 26 aug 2026" in lowered
    assert "accepted" in lowered
    # Names the UI as the current workaround.
    assert "linnworks listing ui" in lowered


def test_ac6_module_note_attributes_wrapper_survival_to_the_ui_push_only():
    """The wrapper is composed at push time by the design template, and it
    survived a UI-INITIATED revise with both furniture blocks intact — this
    must be explicitly attributed to the UI push, not claimed as proof about
    the API path, and scoped to the configurator it was observed on."""
    # The module note is a plain `#`-commented block, not a docstring —
    # normalise it so a phrase wrapped across source lines is still findable.
    normalized = _normalize_comment_prose(_SERVER_SRC)
    assert "composed by the SELLER'S DESIGN TEMPLATE AT PUSH TIME" in normalized
    assert "UI-INITIATED push" in normalized
    assert "BOTH furniture blocks intact" in normalized
    assert "NOT evidence about the API path" in normalized
    assert '"EVRi - Single Image" configurator' in normalized
    assert "not all 33 on this tenant" in normalized
    # Must not overclaim a general "wrapper preservation proven" result.
    assert "wrapper preservation proven for this tool" in normalized
    assert "that has not been shown" in normalized


def test_ac7_stale_never_fired_phrase_does_not_survive_uncorrected_anywhere():
    """No claim that the eBay push has never been fired / was never called
    against a real listing may survive as a LIVE (uncorrected) assertion in
    server.py, README.md or CLAUDE.md. Historical version-log entries may
    keep the old wording only when explicitly marked superseded via the
    repo's own ~~strikethrough~~ convention (see v1.44.0/v1.42.1 precedent)
    — so those spans are stripped before checking."""
    banned = [
        "never been fired against a real listing",
        "never called against a real listing",
        "not fired live by this build",
        "never fired live by this build",
        "not live-fired",
    ]
    for name, text in (
        ("server.py", _SERVER_SRC),
        ("README.md", _README_SRC),
        ("CLAUDE.md", _CLAUDE_MD_SRC),
    ):
        # Normalise line-wrapped comments/docstrings so a phrase split across
        # lines is still detected, then drop anything marked superseded.
        collapsed = _re.sub(r"\s+", " ", text)
        live_only = _re.sub(r"~~.*?~~", " ", collapsed, flags=_re.S)
        lowered = live_only.lower()
        for phrase in banned:
            assert phrase not in lowered, (
                f"{name} still asserts (uncorrected) that the eBay push {phrase!r}"
            )


def test_ac8_claude_md_tables_no_longer_contradict_the_v1_48_1_entry():
    assert "FIRED LIVE 26 Aug 2026" in _CLAUDE_MD_SRC
    # the confirmed-endpoints row for the write endpoint must say it was
    # fired, not that it was never called
    idx = _CLAUDE_MD_SRC.index("| `Listings/ProcesseBayListings` | POST |")
    row = _CLAUDE_MD_SRC[idx:idx + 2000]
    assert "ACCEPTED, NOT OBSERVED TO PROCESS" in row
    assert "Called against a real listing" in row


def test_ac8_readme_row_describes_observed_state_not_merely_unproven():
    idx = _README_SRC.index("`revise_ebay_listing_description`")
    row = _README_SRC[idx:idx + 500]
    assert "Fired live 26 Aug 2026" in row
    assert "no channel-side effect was observed" in row
    assert row.strip().rstrip("|").strip() != ""


def test_ac9_tenant_traps_recorded_in_the_endpoint_tables():
    """Both new tenant traps must be readable from the endpoint tables
    themselves, not only from the version-log note."""
    create_idx = _CLAUDE_MD_SRC.index("| `Listings/CreateEbayTemplates` |")
    create_row = _CLAUDE_MD_SRC[create_idx:create_idx + 600]
    assert "REPLACES the item's template on EVERY call" in create_row
    assert "new `TemplateId`" in create_row or "fresh `TemplateId`" in create_row
    assert "Never use it to poll status" in create_row

    ops_idx = _CLAUDE_MD_SRC.index("| `Listings/GetEbayListingOperations` |")
    ops_row = _CLAUDE_MD_SRC[ops_idx:ops_idx + 400]
    assert "400" in ops_row
    assert "body" in ops_row.lower() and "query" in ops_row.lower()


def test_ac10_tool_signature_threshold_default_and_outcome_vocabulary_unchanged():
    import asyncio
    tool_names = [t.name for t in asyncio.run(server.mcp.list_tools())]
    assert len(tool_names) == 85
    assert "revise_ebay_listing_description" in tool_names
    assert server.WRITE_THRESHOLDS["revise_ebay_listing_description"] == 10

    import inspect
    sig = inspect.signature(server.revise_ebay_listing_description)
    assert list(sig.parameters) == ["skus", "store", "confirmed_count", "dry_run"]
    assert sig.parameters["dry_run"].default is True
    assert sig.parameters["store"].default == "EBAY0"
    assert sig.parameters["confirmed_count"].default is None

    template = {
        "TemplateId": "tpl-1", "InventoryItemId": SID_ADULT, "ConfigId": "cfg-1",
        "SKU": SKU_ADULT, "ListingIds": [LISTING_ID], "Title": "T", "Description": "old",
    }
    patches, _ = _mock(template_for_listing=template)
    r = _run(patches, skus=[SKU_ADULT], store="EBAY0", dry_run=False)
    outcomes = {row["outcome"] for row in r["results"]}
    assert outcomes <= {"unconfirmed", "blocked", "push_failed", "rate_limited"}
