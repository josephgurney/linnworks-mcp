"""
Tests for variation-child take-down and blocked-reason reporting (issue #35).

The first live run of the cleanup chain retired 1 of 42 dead items. 28 of the 37
blocked SKUs were variation CHILDREN: the child holds the channel-SKU rows but
the template hangs off the variation PARENT and serves every variant, so ending
one dead size means deleting a template that also owns its siblings. Refusing
outright made them unretirable; following the parent blindly would have killed
live listings in 19 of 21 groups.

So the rule under test is: follow the parent ONLY when nothing else is lost —
every other group member is already un-listed on this store, or is itself being
retired in the same operation. And when that does not hold, say exactly why
(`variation_child_live_siblings`) rather than collapsing into the same opaque
"no GLT template" string a genuinely template-less standalone gets.

All Linnworks calls are mocked; endpoint shapes mirror the live-confirmed
responses recorded in CLAUDE.md (issues #17, #26, #36).
"""
from unittest.mock import patch

# ── Fixture: four variation groups + two non-members ─────────────────────────
#
#  ALLDEAD  parent grp-alldead  children alldead-s, alldead-m   both listed
#  MIXED    parent grp-mixed    children mixed-dead, mixed-live both listed
#  SOLO     parent grp-solo     children solo-dead (listed), solo-unlisted (not)
#  CROSS    parent grp-cross    children cross-dead (SWH), cross-other (Venom)
#  plain-ok           standalone WITH its own template   (regression)
#  plain-notemplate   standalone with NO template at all (cause 2 in the issue)

SID = {
    "grp-alldead": "a0000000-0000-0000-0000-000000000000",
    "alldead-s":   "a0000000-0000-0000-0000-000000000001",
    "alldead-m":   "a0000000-0000-0000-0000-000000000002",
    "grp-mixed":   "b0000000-0000-0000-0000-000000000000",
    "mixed-dead":  "b0000000-0000-0000-0000-000000000001",
    "mixed-live":  "b0000000-0000-0000-0000-000000000002",
    "grp-solo":    "c0000000-0000-0000-0000-000000000000",
    "solo-dead":   "c0000000-0000-0000-0000-000000000001",
    "solo-unlisted": "c0000000-0000-0000-0000-000000000002",
    "grp-cross":   "d0000000-0000-0000-0000-000000000000",
    "cross-dead":  "d0000000-0000-0000-0000-000000000001",
    "cross-other": "d0000000-0000-0000-0000-000000000002",
    "plain-ok":         "e0000000-0000-0000-0000-000000000001",
    "plain-notemplate": "e0000000-0000-0000-0000-000000000002",
}
SID_TO_SKU = {v: k for k, v in SID.items()}

GROUPS = {
    "grp-alldead": {"name": "All Dead Line",  "children": ["alldead-s", "alldead-m"]},
    "grp-mixed":   {"name": "Breezy",         "children": ["mixed-dead", "mixed-live"]},
    "grp-solo":    {"name": "Solo Line",      "children": ["solo-dead", "solo-unlisted"]},
    "grp-cross":   {"name": "Cross Store",    "children": ["cross-dead", "cross-other"]},
}

SWH = {"Source": "SHOPIFY", "SubSource": "SWH Shopify",
       "ChannelReferenceId": "111:222:333", "ListedQuantity": 0}
VENOM = {"Source": "SHOPIFY", "SubSource": "Venom Skateboards",
         "ChannelReferenceId": "444:555:666", "ListedQuantity": 4}

# Which items carry a channel-SKU row. Variation PARENTS carry none — that is
# the whole shape of the problem.
CHANNEL_SKUS = {
    SID["alldead-s"]:  [dict(SWH)],
    SID["alldead-m"]:  [dict(SWH)],
    SID["mixed-dead"]: [dict(SWH)],
    SID["mixed-live"]: [dict(SWH)],
    SID["solo-dead"]:  [dict(SWH)],
    SID["cross-dead"]: [dict(SWH)],
    SID["cross-other"]: [dict(VENOM)],
    SID["plain-ok"]:         [dict(SWH)],
    SID["plain-notemplate"]: [dict(SWH)],
}

# (StockItemId, ChannelId) → templates. Only PARENTS and plain-ok have any.
def _tpl(tid, sid):
    return {"Id": tid, "StockItemId": sid, "ConfiguratorId": 129, "IsLocked": False,
            "NextSuggestedAction": "Update", "IsNextSuggestedActionAllowed": True,
            "Info": {"ActiveListingId": {"Value": str(tid)},
                     "Status": {"Value": "Listed"}}}

TEMPLATES = {
    (SID["grp-alldead"], 18): [_tpl(5001, SID["grp-alldead"])],
    (SID["grp-mixed"],   18): [_tpl(5002, SID["grp-mixed"])],
    (SID["grp-solo"],    18): [_tpl(5003, SID["grp-solo"])],
    (SID["grp-cross"],   18): [_tpl(5004, SID["grp-cross"])],
    (SID["plain-ok"],    18): [_tpl(5010, SID["plain-ok"])],
}

# Which items' SWH listing rows a template actually owns — used to model the
# live delete. A parent template owns its children's rows, never its own.
TEMPLATE_SERVES = {
    5001: ["alldead-s", "alldead-m"],
    5002: ["mixed-dead", "mixed-live"],
    5003: ["solo-dead"],
    5004: ["cross-dead"],
    5010: ["plain-ok"],
}

CATALOGUES = {
    "Shopify": [
        {"Info": {"Id": {"Value": 1}, "Name": {"Value": "Default"},
                  "ChannelId": {"Value": 18}, "SubSource": {"Value": "SWH Shopify"}}},
        {"Info": {"Id": {"Value": 2}, "Name": {"Value": "Default"},
                  "ChannelId": {"Value": 21}, "SubSource": {"Value": "Venom Skateboards"}}},
    ],
    "Amazon": [], "TikTok": [], "Magento": [], "Walmart": [],
}


def _mock(delete_clears=True):
    captured = {"process": [], "opened": []}
    live = {sid: [dict(r) for r in rows] for sid, rows in CHANNEL_SKUS.items()}
    deleted_templates: set[int] = set()

    def call_linnworks(path, payload):
        if path.endswith("GetConfiguratorsInfoPaged"):
            return {"ConfiguratorsInfo": CATALOGUES[payload["request"]["ChannelType"]]}
        if path.endswith("GetInventoryItem"):
            sku = payload.get("sku")
            if sku not in SID:
                raise RuntimeError("HTTP 400 — could not determine inventory item id from SKU")
            return {"StockItemId": SID[sku], "ItemTitle": f"Title for {sku}"}
        if path.endswith("BatchGetInventoryItemChannelSKUs"):
            return [{"StockItemId": i, "ChannelSkus": live.get(i, [])}
                    for i in payload["inventoryItemIds"]]
        if path.endswith("OpenTemplatesByInventory"):
            req = payload["request"]
            cid = req["Parameters"]["ChannelId"]
            captured["opened"].append((cid, tuple(req["Parameters"]["InventoryItemIds"])))
            out = []
            for sid in req["Parameters"]["InventoryItemIds"]:
                for t in TEMPLATES.get((sid, cid), []):
                    if t["Id"] not in deleted_templates:
                        out.append(dict(t))
            return {"TotalEntries": len(out), "TemplatesInfo": out}
        if path.endswith("ProcessTemplates"):
            req = payload["request"]
            captured["process"].append(req)
            for tr in req["TemplateRequests"]:
                tid = tr["TemplateId"]
                deleted_templates.add(tid)
                if delete_clears:
                    for sku in TEMPLATE_SERVES.get(tid, []):
                        live[SID[sku]] = [r for r in live[SID[sku]]
                                          if r["SubSource"] != "SWH Shopify"]
            return {}
        raise AssertionError(f"Unexpected call_linnworks: {path}")

    def call_linnworks_get(path, params=None):
        if path.endswith("GetInventoryItemChannelSKUs"):
            return live.get(params["inventoryItemId"], [])
        if path.endswith("GetVariationGroupByParentId"):
            sku = SID_TO_SKU.get(params["pkStockItemId"])
            if sku in GROUPS:
                return {"VariationSKU": sku, "pkVariationItemId": SID[sku],
                        "VariationGroupName": GROUPS[sku]["name"]}
            return None
        if path.endswith("SearchVariationGroups"):
            # searchType=ItemSKU — a SUBSTRING match over member SKUs, which is
            # why the tool must confirm exact membership via GetVariationItems.
            text = (params.get("searchText") or "").lower()
            data = [
                {"VariationSKU": p, "pkVariationItemId": SID[p],
                 "VariationGroupName": g["name"]}
                for p, g in GROUPS.items()
                if any(text in c.lower() for c in g["children"])
            ]
            return {"PageNumber": 1, "TotalPages": 1, "TotalEntries": len(data), "Data": data}
        if path.endswith("GetVariationItems"):
            sku = SID_TO_SKU.get(params["pkVariationItemId"])
            return [{"pkStockItemId": SID[c], "ItemNumber": c, "ItemTitle": c}
                    for c in GROUPS.get(sku, {}).get("children", [])]
        raise AssertionError(f"Unexpected GET: {path}")

    return [patch("server.call_linnworks", side_effect=call_linnworks),
            patch("server.call_linnworks_get", side_effect=call_linnworks_get)], captured


def _run(fn, *a, **kw):
    patches, captured = _mock(**kw.pop("_mock_kw", {}))
    for p in patches:
        p.start()
    try:
        return fn(*a, **kw), captured
    finally:
        for p in patches:
            p.stop()


# ── The safe case: nothing else in the group is listed ────────────────────────

def test_child_in_a_wholly_dead_group_is_retired_via_the_parent_template():
    import server
    r, cap = _run(server.unpublish_channel_listing,
                  ["alldead-s", "alldead-m"], dry_run=True)

    # ONE delete, not two — both children share the parent's single template.
    assert [p["template_id"] for p in r["plan"]] == [5001]
    row = r["plan"][0]
    assert row["via_variation_parent"] is True
    assert row["variation_parent_sku"] == "grp-alldead"
    assert sorted(row["covers_skus"]) == ["alldead-m", "alldead-s"]
    assert sorted(row["group_member_skus"]) == ["alldead-m", "alldead-s"]
    assert r["retirable_sku_count"] == 2
    assert r["unresolved"] == []
    assert not cap["process"]


def test_unlisted_sibling_does_not_block_the_group():
    """A sibling with no listing on this store has nothing to lose."""
    import server
    r, _ = _run(server.unpublish_channel_listing, ["solo-dead"], dry_run=True)

    assert [p["template_id"] for p in r["plan"]] == [5003]
    assert r["plan"][0]["variation_parent_sku"] == "grp-solo"


def test_sibling_listed_on_a_different_store_does_not_block():
    """Liveness is judged on the TARGET store only — deleting the SWH template
    cannot touch a sibling that is live on Venom."""
    import server
    r, _ = _run(server.unpublish_channel_listing, ["cross-dead"], dry_run=True)

    assert [p["template_id"] for p in r["plan"]] == [5004]


# ── The unsafe case: a live sibling ───────────────────────────────────────────

def test_live_sibling_blocks_the_take_down_and_says_so():
    import server
    r, cap = _run(server.unpublish_channel_listing, ["mixed-dead"], dry_run=True)

    assert r["plan"] == []
    assert not cap["process"]
    u = r["unresolved"][0]
    assert u["blocked_reason"] == "variation_child_live_siblings"
    assert u["variation_parent_sku"] == "grp-mixed"
    assert u["variation_group_name"] == "Breezy"
    assert u["live_sibling_count"] == 1
    assert u["live_siblings"] == ["mixed-live"]
    assert "would take those down too" in u["error"]


def test_passing_the_whole_group_unblocks_it():
    """The live sibling stops being a blocker once it is also being retired."""
    import server
    r, _ = _run(server.unpublish_channel_listing,
                ["mixed-dead", "mixed-live"], dry_run=True)

    assert [p["template_id"] for p in r["plan"]] == [5002]
    assert sorted(r["plan"][0]["covers_skus"]) == ["mixed-dead", "mixed-live"]


def test_also_retiring_skus_unblocks_a_group_split_across_calls():
    import server
    r, _ = _run(server.unpublish_channel_listing, ["mixed-dead"],
                also_retiring_skus=["mixed-live"], dry_run=True)

    assert [p["template_id"] for p in r["plan"]] == [5002]
    # It is not itself delisted here — only counted as going away.
    assert r["plan"][0]["covers_skus"] == ["mixed-dead"]


def test_opt_out_flag_refuses_parent_takedown_with_its_own_reason():
    """Turning the behaviour off must not relabel the SKU as template-less."""
    import server
    r, _ = _run(server.unpublish_channel_listing, ["alldead-s", "alldead-m"],
                allow_variation_parent_takedown=False, dry_run=True)

    assert r["plan"] == []
    reasons = {u["blocked_reason"] for u in r["unresolved"]}
    assert reasons == {"variation_child_parent_takedown_disabled"}
    assert all(u["variation_parent_sku"] == "grp-alldead" for u in r["unresolved"])


# ── Cause 2: template-less standalones are a DIFFERENT problem ────────────────

def test_template_less_standalone_is_distinguished_from_a_variation_child():
    import server
    r, _ = _run(server.unpublish_channel_listing,
                ["plain-notemplate", "mixed-dead"], dry_run=True)

    by_sku = {u["sku"]: u["blocked_reason"] for u in r["unresolved"]}
    assert by_sku["plain-notemplate"] == "no_glt_template"
    assert by_sku["mixed-dead"] == "variation_child_live_siblings"
    assert r["blocked_summary"] == {"no_glt_template": 1,
                                    "variation_child_live_siblings": 1}


def test_plain_item_with_its_own_template_is_unaffected():
    import server
    r, _ = _run(server.unpublish_channel_listing, ["plain-ok"], dry_run=True)

    assert [p["template_id"] for p in r["plan"]] == [5010]
    assert r["plan"][0].get("via_variation_parent") is None
    assert r["plan"][0]["template_stock_item_id"] == SID["plain-ok"]
    assert r["plan"][0]["listing_sids"] == [SID["plain-ok"]]


# ── Cause 3: the summary must not read as success ─────────────────────────────

def test_summary_names_the_unretirable_skus_and_why():
    import server
    r, _ = _run(server.unpublish_channel_listing,
                ["plain-ok", "mixed-dead", "plain-notemplate"], dry_run=True)

    assert r["blocked_count"] == 2
    assert r["retirable_sku_count"] == 1
    msg = r["message"]
    assert "covering 1 of 3 requested SKU(s)" in msg
    assert "2 of 3 requested SKU(s) CANNOT be taken down" in msg
    assert "variation_child_live_siblings" in msg


def test_nothing_retirable_still_explains_itself():
    import server
    r, _ = _run(server.unpublish_channel_listing,
                ["mixed-dead"], dry_run=False, confirmed_count=None)

    assert r["plan"] == []
    assert "CANNOT be taken down" in r["message"]


# ── Parent passed directly ────────────────────────────────────────────────────

def test_parent_sku_is_listed_via_its_children_and_names_the_blast_radius():
    import server
    r, _ = _run(server.unpublish_channel_listing, ["grp-mixed"], dry_run=True)

    assert [p["template_id"] for p in r["plan"]] == [5002]
    row = r["plan"][0]
    assert row["is_variation_parent"] is True
    assert sorted(row["listed_via_children"]) == ["mixed-dead", "mixed-live"]
    assert "ends the listing for all 2" in row["warning"]
    # Read-back must follow the CHILDREN — the parent has no rows of its own.
    assert sorted(row["listing_sids"]) == sorted(
        [SID["mixed-dead"], SID["mixed-live"]])


# ── Live run: the read-back must check the children, not the parent ───────────

def test_live_parent_takedown_verifies_the_childrens_listing_rows():
    import server
    r, cap = _run(server.unpublish_channel_listing,
                  ["alldead-s", "alldead-m"], dry_run=False, confirmed_count=None)

    assert [tr["TemplateId"] for req in cap["process"] for tr in req["TemplateRequests"]] == [5001]
    res = r["results"][0]
    assert res["outcome"] == "taken_down"
    assert res["taken_down"] is True
    assert res["still_listed"] is False
    assert res["via_variation_parent"] is True
    assert sorted(res["covers_skus"]) == ["alldead-m", "alldead-s"]
    assert r["taken_down_count"] == 1


def test_live_parent_takedown_is_not_scored_taken_down_when_a_child_row_survives():
    """The parent has NO channel-SKU rows, so reading the parent would always
    say "gone". Only the children's rows can prove the listing ended."""
    import server
    r, _ = _run(server.unpublish_channel_listing,
                ["alldead-s", "alldead-m"], dry_run=False, confirmed_count=None,
                _mock_kw={"delete_clears": False})

    res = r["results"][0]
    assert res["template_deleted"] is True
    assert res["still_listed"] is True
    assert res["taken_down"] is False
    assert res["outcome"] == "template_deleted_listing_row_remains"
    assert r["taken_down_count"] == 0


# ── Fan-out ───────────────────────────────────────────────────────────────────

def test_fanout_retires_a_whole_group_as_one_takedown():
    """Delegation is batched per target, so two children of one group produce
    ONE delete — not one that works and one that reports "no template"."""
    import server
    r, cap = _run(server.delist_all_channel_listings,
                  ["alldead-s", "alldead-m"], channels=["Shopify"], dry_run=True)

    assert [p["template_id"] for p in r["plan"]] == [5001]
    assert r["retirable_sku_count"] == 2
    assert not cap["process"]


def test_fanout_surfaces_blocked_reasons_in_its_summary():
    import server
    r, _ = _run(server.delist_all_channel_listings,
                ["mixed-dead", "plain-notemplate"], channels=["Shopify"], dry_run=True)

    assert r["plan"] == []
    assert r["blocked_summary"] == {"no_glt_template": 1,
                                    "variation_child_live_siblings": 1}
    assert "CANNOT be taken down" in r["message"]
    assert all(u.get("blocked_reason") for u in r["unresolved"])


def test_fanout_live_run_takes_the_group_down_once():
    import server
    r, cap = _run(server.delist_all_channel_listings,
                  ["alldead-s", "alldead-m"], channels=["Shopify"],
                  dry_run=False, confirmed_count=1)

    assert [tr["TemplateId"] for req in cap["process"] for tr in req["TemplateRequests"]] == [5001]
    assert r["taken_down_count"] == 1
    assert r["still_listed_sub_sources"] == []
