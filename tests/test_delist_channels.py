"""
Tests for the cross-channel delist path (issue #30):
  - unpublish_channel_listing(channel=...)  — any GLT channel, not just Shopify
  - delist_all_channel_listings             — fan-out across channels/accounts
  - delist_all_shopify_listings             — Shopify-scoped back-compat wrapper

Live-probed behaviour these tests encode (5 Aug 2026, this tenant):

  * GLT channel coverage: Shopify 67 configurators (per-store ChannelId),
    Amazon 10 (ONE account, ChannelId 2, SubSource "The Warehouse Group"),
    TikTok 5 (ChannelId 30), Magento 0, Walmart 0. eBay/Etsy → HTTP 400
    "Invalid parameter request" (not GLT channels at all).
  * AMAZON REGIONS: the channel-SKU table carries "The Warehouse Group",
    "… - Germany", "… - Spain", "… - Netherlands", "… - Sweden", "… - Italy",
    "… - France" — all fronted by the single account configurator. A regional
    sub_source therefore has to resolve to the account's ChannelId
    (resolution "account-prefix"), and the fan-out must collapse the regions
    into ONE take-down instead of re-deleting the same templates per region.
  * MULTIPLE TEMPLATES PER ITEM: vnm_bearings_gold returns TWO Amazon templates
    (32115 = ".FBA" channel SKU, 32381 = merchant) for one StockItemId. Keying
    templates by StockItemId drops one and leaves that listing live.
  * Info.ActiveListingId on Amazon is the channel SKU, not a numeric product id.
"""
import pytest
from unittest.mock import patch

SID_A = "c4b07a59-f0cf-4a92-97e6-76184bd5da8a"   # amazon+shopify+ebay item
SID_S = "2b03d809-35e6-421c-a92c-fcc58a2b13eb"   # shopify-only item

# Configurator catalogues per ChannelType, mirroring the live probe.
CATALOGUES = {
    "Shopify": [
        {"Info": {"Id": {"Value": 1}, "Name": {"Value": "Default"},
                  "ChannelId": {"Value": 18}, "SubSource": {"Value": "SWH Shopify"}}},
        {"Info": {"Id": {"Value": 2}, "Name": {"Value": "Default"},
                  "ChannelId": {"Value": 21}, "SubSource": {"Value": "Venom Skateboards"}}},
    ],
    "Amazon": [
        {"Info": {"Id": {"Value": 126}, "Name": {"Value": "Skateboard"},
                  "ChannelId": {"Value": 2}, "SubSource": {"Value": "The Warehouse Group"}}},
    ],
    "TikTok": [
        {"Info": {"Id": {"Value": 112}, "Name": {"Value": "Completes"},
                  "ChannelId": {"Value": 30}, "SubSource": {"Value": "SKATEWAREHOUSE_UK"}}},
    ],
    "Magento": [],
    "Walmart": [],
}

# Channel-SKU rows per stock item — the live shape for a heavily-listed item.
CHANNEL_SKUS = {
    SID_A: [
        {"Source": "AMAZON", "SubSource": "The Warehouse Group",
         "ChannelReferenceId": "B07NRCXGR2", "ListedQuantity": 664},
        {"Source": "AMAZON", "SubSource": "The Warehouse Group - Germany",
         "ChannelReferenceId": "B07NRCXGR2", "ListedQuantity": 0},
        {"Source": "AMAZON", "SubSource": "The Warehouse Group - Sweden",
         "ChannelReferenceId": "B07NRCXGR2", "ListedQuantity": 1361},
        {"Source": "SHOPIFY", "SubSource": "SWH Shopify",
         "ChannelReferenceId": "111:222:333", "ListedQuantity": 3},
        {"Source": "TIKTOK", "SubSource": "SKATEWAREHOUSE_UK",
         "ChannelReferenceId": "1729:1730", "ListedQuantity": 2},
        {"Source": "EBAY", "SubSource": "EBAY0",
         "ChannelReferenceId": "285327630143", "ListedQuantity": 5},
        {"Source": "Mirakl MP", "SubSource": "Decathlon",
         "ChannelReferenceId": "cfdd-abc", "ListedQuantity": 1},
    ],
    SID_S: [
        {"Source": "SHOPIFY", "SubSource": "SWH Shopify",
         "ChannelReferenceId": "9425839882486:508:521", "ListedQuantity": 2},
    ],
}

# (StockItemId, ChannelId) → templates. Amazon returns TWO for one item.
TEMPLATES = {
    (SID_A, 2): [
        {"Id": 32115, "StockItemId": SID_A, "ConfiguratorId": 126, "IsLocked": False,
         "NextSuggestedAction": "NotAllowed",
         "Info": {"ActiveListingId": {"Value": "vnm_bearings_gold.FBA"},
                  "Status": {"Value": "Listed"}}},
        {"Id": 32381, "StockItemId": SID_A, "ConfiguratorId": 126, "IsLocked": False,
         "NextSuggestedAction": "NotAllowed",
         "Info": {"ActiveListingId": {"Value": "vnm_bearings_gold"},
                  "Status": {"Value": "Listed"}}},
    ],
    (SID_A, 18): [
        {"Id": 689, "StockItemId": SID_A, "ConfiguratorId": 5, "IsLocked": False,
         "NextSuggestedAction": "Update",
         "Info": {"ActiveListingId": {"Value": "111"}, "Status": {"Value": "Listed"}}},
    ],
    (SID_A, 30): [
        {"Id": 30181, "StockItemId": SID_A, "ConfiguratorId": 112, "IsLocked": False,
         "NextSuggestedAction": "Update",
         "Info": {"ActiveListingId": {"Value": "1729"}, "Status": {"Value": "Listed"}}},
    ],
    (SID_S, 18): [
        {"Id": 43285, "StockItemId": SID_S, "ConfiguratorId": 7, "IsLocked": False,
         "NextSuggestedAction": "Update",
         "Info": {"ActiveListingId": {"Value": "9425839882486"},
                  "Status": {"Value": "Listed"}}},
    ],
}

SKU_TO_SID = {"vnm_bearings_gold": SID_A, "RS-102201": SID_S}


def _mock(delete_clears=True, lock_template=None):
    """Mock the call layer.

    delete_clears: whether a processed Delete removes that channel's rows from
        the channel-SKU table (the read-back signal).
    """
    captured = {"process": [], "opened": []}
    # Mutable copy so a Delete can be reflected in the read-back.
    live = {sid: [dict(r) for r in rows] for sid, rows in CHANNEL_SKUS.items()}
    deleted_templates: set[int] = set()

    def _source_for_template(tid):
        for (sid, cid), tpls in TEMPLATES.items():
            for t in tpls:
                if t["Id"] == tid:
                    return sid, {2: "AMAZON", 18: "SHOPIFY", 21: "SHOPIFY",
                                 30: "TIKTOK"}[cid]
        return None, None

    def call_linnworks(path, payload):
        if path.endswith("GetConfiguratorsInfoPaged"):
            ct = payload["request"]["ChannelType"]
            return {"ConfiguratorsInfo": CATALOGUES[ct]}
        if path.endswith("GetInventoryItem"):
            sku = payload.get("sku")
            if sku not in SKU_TO_SID:
                raise RuntimeError("HTTP 400 — could not determine inventory item id from SKU")
            return {"StockItemId": SKU_TO_SID[sku], "ItemTitle": f"Title for {sku}"}
        if path.endswith("OpenTemplatesByInventory"):
            req = payload["request"]
            cid = req["Parameters"]["ChannelId"]
            captured["opened"].append((req["ChannelType"], cid,
                                       tuple(req["Parameters"]["InventoryItemIds"])))
            out = []
            for sid in req["Parameters"]["InventoryItemIds"]:
                for t in TEMPLATES.get((sid, cid), []):
                    if t["Id"] in deleted_templates:
                        continue
                    tt = dict(t)
                    if lock_template is not None and t["Id"] == lock_template:
                        tt["IsLocked"] = True
                    out.append(tt)
            return {"TotalEntries": len(out), "TemplatesInfo": out}
        if path.endswith("ProcessTemplates"):
            req = payload["request"]
            captured["process"].append(req)
            for tr in req["TemplateRequests"]:
                tid = tr["TemplateId"]
                deleted_templates.add(tid)
                if delete_clears:
                    sid, src = _source_for_template(tid)
                    if sid:
                        live[sid] = [r for r in live[sid] if r["Source"] != src]
            return {}
        raise AssertionError(f"Unexpected call_linnworks: {path}")

    def call_linnworks_get(path, params=None):
        if path.endswith("GetInventoryItemChannelSKUs"):
            return live.get(params["inventoryItemId"], [])
        raise AssertionError(f"Unexpected GET: {path}")

    patches = [
        patch("server.call_linnworks", side_effect=call_linnworks),
        patch("server.call_linnworks_get", side_effect=call_linnworks_get),
    ]
    return patches, captured


# ── unpublish_channel_listing: channel generalisation ─────────────────────────

def test_amazon_plans_every_template_on_the_item():
    """An item with TWO Amazon templates must plan BOTH — dropping one leaves
    that listing live while the run reports success."""
    import server
    patches, captured = _mock()
    for p in patches: p.start()
    try:
        r = server.unpublish_channel_listing(
            ["vnm_bearings_gold"], sub_source="The Warehouse Group",
            channel="Amazon", dry_run=True)
    finally:
        for p in patches: p.stop()

    assert r["target_channel"] == "Amazon"
    assert r["target_source"] == "AMAZON"
    assert r["target_channel_id"] == 2
    assert r["sub_source_resolution"] == "exact"
    # Amazon Delete was live-proven 5 Aug 2026 (MOB-GRP-3080 / tpl 31703).
    assert r["delete_proven"] is True
    assert sorted(p["template_id"] for p in r["plan"]) == [32115, 32381]
    assert all(p["templates_on_item"] == 2 for p in r["plan"])
    # Amazon ActiveListingId is the channel SKU, not a numeric product id.
    assert {p["active_listing_id"] for p in r["plan"]} == {
        "vnm_bearings_gold.FBA", "vnm_bearings_gold"}
    assert not captured["process"]          # dry run writes nothing


def test_amazon_regional_sub_source_resolves_to_the_account_channel_id():
    """"The Warehouse Group - Germany" has no configurator of its own — it must
    resolve to the account (ChannelId 2) via the prefix fallback."""
    import server
    patches, _ = _mock()
    for p in patches: p.start()
    try:
        r = server.unpublish_channel_listing(
            ["vnm_bearings_gold"], sub_source="The Warehouse Group - Germany",
            channel="Amazon", dry_run=True)
    finally:
        for p in patches: p.stop()

    assert r["target_channel_id"] == 2
    assert r["sub_source_resolution"] == "account-prefix"
    assert len(r["plan"]) == 2


def test_sub_source_from_another_channel_is_rejected():
    """The prefix fallback is anchored to the channel's own accounts — a Shopify
    store must not resolve onto Amazon's single ChannelId."""
    import server
    patches, _ = _mock()
    for p in patches: p.start()
    try:
        r = server.unpublish_channel_listing(
            ["vnm_bearings_gold"], sub_source="SWH Shopify",
            channel="Amazon", dry_run=True)
    finally:
        for p in patches: p.stop()

    assert "error" in r and "not a known Amazon account/store" in r["error"]
    assert r["available_sub_sources"] == ["The Warehouse Group"]


def test_non_glt_channels_raise():
    """eBay / Etsy / Mirakl have no GLT templates and no listing API at all."""
    import server
    for ch in ("eBay", "Etsy", "Mirakl", "nonsense"):
        with pytest.raises(ValueError, match="not a GLT-managed channel"):
            server.unpublish_channel_listing(["vnm_bearings_gold"],
                                             sub_source="x", channel=ch, dry_run=True)


def test_shopify_default_path_unchanged():
    """Regression: the default (Shopify) call keeps working and stays proven."""
    import server
    patches, _ = _mock()
    for p in patches: p.start()
    try:
        r = server.unpublish_channel_listing(["RS-102201"], dry_run=True)
    finally:
        for p in patches: p.stop()

    assert r["target_channel"] == "Shopify"
    assert r["target_channel_id"] == 18
    assert r["delete_proven"] is True
    assert [p["template_id"] for p in r["plan"]] == [43285]


def test_not_listed_on_channel_is_unresolved_not_deleted():
    import server
    patches, captured = _mock()
    for p in patches: p.start()
    try:
        r = server.unpublish_channel_listing(
            ["RS-102201", "ZZZ-NOPE"], sub_source="The Warehouse Group",
            channel="Amazon", dry_run=False, confirmed_count=None)
    finally:
        for p in patches: p.stop()

    assert r["plan"] == []
    errs = {u["sku"]: u["error"] for u in r["unresolved"]}
    assert "not listed on Amazon" in errs["RS-102201"]
    assert "not found" in errs["ZZZ-NOPE"]
    assert not captured["process"]


def test_locked_template_is_skipped_but_siblings_still_planned():
    import server
    patches, _ = _mock(lock_template=32115)
    for p in patches: p.start()
    try:
        r = server.unpublish_channel_listing(
            ["vnm_bearings_gold"], sub_source="The Warehouse Group",
            channel="Amazon", dry_run=True)
    finally:
        for p in patches: p.stop()

    assert [p["template_id"] for p in r["plan"]] == [32381]
    assert any(u.get("template_id") == 32115 and "locked" in u["error"]
               for u in r["unresolved"])


def test_live_run_deletes_every_template_then_reads_back_once():
    """Read-back happens once per item AFTER all its templates are processed —
    a per-template read-back would report still_listed for rows the next
    template is about to remove."""
    import server
    patches, captured = _mock()
    for p in patches: p.start()
    try:
        r = server.unpublish_channel_listing(
            ["vnm_bearings_gold"], sub_source="The Warehouse Group",
            channel="Amazon", dry_run=False)
    finally:
        for p in patches: p.stop()

    assert len(captured["process"]) == 2
    for req in captured["process"]:
        assert req["ChannelType"] == "Amazon"
        assert req["ChannelName"] == "AMAZON"
        assert req["TemplateRequests"][0]["Action"] == "Delete"
    assert all(res["processed"] and res["taken_down"] for res in r["results"])
    assert r["dry_run"] is False


def test_still_listed_when_the_channel_row_survives():
    import server
    patches, _ = _mock(delete_clears=False)
    for p in patches: p.start()
    try:
        r = server.unpublish_channel_listing(
            ["vnm_bearings_gold"], sub_source="The Warehouse Group",
            channel="Amazon", dry_run=False)
    finally:
        for p in patches: p.stop()

    assert all(res["processed"] for res in r["results"])
    assert all(res["still_listed"] and res["taken_down"] is False for res in r["results"])


# ── delist_all_channel_listings: the fan-out ──────────────────────────────────

def test_fanout_collapses_amazon_regions_and_covers_every_glt_channel():
    import server
    patches, _ = _mock()
    for p in patches: p.start()
    try:
        r = server.delist_all_channel_listings(["vnm_bearings_gold"], dry_run=True)
    finally:
        for p in patches: p.stop()

    targets = {(t["channel"], t["channel_id"]): t for t in r["discovery"][0]["targets"]}
    assert set(targets) == {("Amazon", 2), ("Shopify", 18), ("TikTok", 30)}
    # All three Amazon regional rows collapse into ONE account take-down.
    amazon = targets[("Amazon", 2)]
    assert sorted(amazon["covers_sub_sources"]) == [
        "The Warehouse Group", "The Warehouse Group - Germany",
        "The Warehouse Group - Sweden"]
    assert amazon["sub_source"] == "The Warehouse Group"          # exact rep wins
    assert amazon["sub_source_resolution"] == "exact"
    # 2 Amazon templates + 1 Shopify + 1 TikTok, not one per region.
    assert sorted(p["template_id"] for p in r["plan"]) == [689, 30181, 32115, 32381]
    assert r["take_down_count"] == 4


def test_fanout_skips_non_glt_channels_with_a_reason():
    import server
    patches, _ = _mock()
    for p in patches: p.start()
    try:
        r = server.delist_all_channel_listings(["vnm_bearings_gold"], dry_run=True)
    finally:
        for p in patches: p.stop()

    skipped = {s["source"]: s for s in r["skipped_channels"]}
    assert set(skipped) == {"EBAY", "Mirakl MP"}
    for s in skipped.values():
        assert "not a GLT channel" in s["reason"]
    # …and they are never planned.
    assert not any(p["channel"] in ("EBAY", "Mirakl MP") for p in r["plan"])


def test_fanout_channels_filter_scopes_the_plan():
    import server
    patches, _ = _mock()
    for p in patches: p.start()
    try:
        r = server.delist_all_channel_listings(
            ["vnm_bearings_gold"], channels=["Amazon"], dry_run=True)
    finally:
        for p in patches: p.stop()

    assert sorted(p["template_id"] for p in r["plan"]) == [32115, 32381]
    # Shopify/TikTok rows are reported as skipped, not silently dropped.
    assert {s["source"] for s in r["skipped_channels"]} >= {"SHOPIFY", "TIKTOK"}


def test_unproven_channel_is_flagged_and_warned_about():
    """Shopify and Amazon Deletes are live-proven; TikTok is not, so its rows
    must carry delete_proven=false and the message must say so."""
    import server
    patches, _ = _mock()
    for p in patches: p.start()
    try:
        r = server.delist_all_channel_listings(
            ["vnm_bearings_gold"], channels=["TikTok"], dry_run=True)
    finally:
        for p in patches: p.stop()

    assert [p["template_id"] for p in r["plan"]] == [30181]
    assert all(p["delete_proven"] is False for p in r["plan"])
    assert "not live-proven" in r["message"].lower()


def test_proven_channels_carry_no_warning():
    import server
    patches, _ = _mock()
    for p in patches: p.start()
    try:
        r = server.delist_all_channel_listings(
            ["vnm_bearings_gold"], channels=["Shopify", "Amazon"], dry_run=True)
    finally:
        for p in patches: p.stop()

    assert all(p["delete_proven"] is True for p in r["plan"])
    assert "not live-proven" not in r["message"].lower()


def test_fanout_rejects_a_non_glt_channel_argument():
    import server
    with pytest.raises(ValueError, match="not a GLT-managed channel"):
        server.delist_all_channel_listings(["vnm_bearings_gold"], channels=["eBay"])


def test_shopify_wrapper_is_scoped_to_shopify():
    import server
    patches, captured = _mock()
    for p in patches: p.start()
    try:
        r = server.delist_all_shopify_listings(["vnm_bearings_gold"], dry_run=True)
    finally:
        for p in patches: p.stop()

    assert [p["channel"] for p in r["plan"]] == ["Shopify"]
    assert {s["source"] for s in r["skipped_channels"]} >= {"AMAZON", "TIKTOK", "EBAY"}
    assert not captured["process"]


def test_fanout_live_run_reports_surviving_sub_sources():
    """still_listed_sub_sources is the honest cross-region read-back — the
    delegated call only ever checks its own representative sub-source."""
    import server
    patches, _ = _mock(delete_clears=False)
    for p in patches: p.start()
    try:
        r = server.delist_all_channel_listings(
            ["vnm_bearings_gold"], channels=["Amazon"], dry_run=False)
    finally:
        for p in patches: p.stop()

    still = {s["channel"]: s["sub_sources"] for s in r["still_listed_sub_sources"]}
    assert still["Amazon"] == ["The Warehouse Group", "The Warehouse Group - Germany",
                               "The Warehouse Group - Sweden"]


def test_fanout_live_run_clean_when_rows_clear():
    import server
    patches, captured = _mock(delete_clears=True)
    for p in patches: p.start()
    try:
        r = server.delist_all_channel_listings(
            ["vnm_bearings_gold"], channels=["Amazon"], dry_run=False)
    finally:
        for p in patches: p.stop()

    assert len(captured["process"]) == 2
    assert r["still_listed_sub_sources"] == []
    assert all(res["taken_down"] for res in r["results"])


# ── staging / write-guard ─────────────────────────────────────────────────────

def test_fanout_stages_above_threshold_and_writes_nothing():
    import server
    assert server.WRITE_THRESHOLDS["delist_all_channel_listings"] == 10
    patches, captured = _mock()
    for p in patches: p.start()
    try:
        # 4 templates per item × 3 items = 12 planned take-downs > 10.
        r = server.delist_all_channel_listings(
            ["vnm_bearings_gold"] * 3, dry_run=False)
    finally:
        for p in patches: p.stop()

    assert r.get("staged") is True
    assert not captured["process"]
    assert r["plan"]                      # the manifest is still returned


def test_fanout_wrong_confirmed_count_blocks():
    import server
    patches, captured = _mock()
    for p in patches: p.start()
    try:
        r = server.delist_all_channel_listings(
            ["vnm_bearings_gold"] * 3, confirmed_count=99, dry_run=False)
    finally:
        for p in patches: p.stop()

    assert r.get("success") is False and r.get("staged") is False
    assert "does not match batch size" in r["message"]
    assert not captured["process"]
