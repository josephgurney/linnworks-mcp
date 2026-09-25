"""
Tests for `unpublish_channel_listing(template_ids=...)` — the targeted delete
that lets a caller retire ONE named GLT template while leaving its live
sibling alone (issue #115).

Fixture shape mirrors the live Echo Sonar case (v1.50.0/v1.50.1): one item,
TWO Shopify templates on it — an ORPHAN whose stored ActiveListingId no
longer exists on Shopify (GID_DEAD), and a LIVE sibling whose ActiveListingId
is the surviving product (GID_LIVE), which is also the id the item's
channel-SKU row still points at.

All Linnworks calls and the Shopify existence probe are mocked; endpoint
shapes mirror what test_delist_channels.py / test_refresh_dangling_listing.py
already encode as live-confirmed.
"""
from unittest.mock import patch

import pytest

SID_S = "2b03d809-35e6-421c-a92c-fcc58a2b13eb"
SKU_S = "RS-102201"

TID_ORPHAN = 38539
TID_LIVE = 54649
TID_UNKNOWN = 99999

GID_DEAD = "9277472506102"   # deleted duplicate product — the orphan's stored listing
GID_LIVE = "9276972695798"   # the surviving, live product

CHANNEL_SKUS = {
    SID_S: [
        {"Source": "SHOPIFY", "SubSource": "SWH Shopify",
         "ChannelReferenceId": GID_LIVE, "ListedQuantity": 2},
    ],
}

CATALOGUES = {
    "Shopify": [
        {"Info": {"Id": {"Value": 1}, "Name": {"Value": "Default"},
                  "ChannelId": {"Value": 18}, "SubSource": {"Value": "SWH Shopify"}}},
    ],
    "Amazon": [
        {"Info": {"Id": {"Value": 126}, "Name": {"Value": "Skateboard"},
                  "ChannelId": {"Value": 2}, "SubSource": {"Value": "The Warehouse Group"}}},
    ],
    "TikTok": [], "Magento": [], "Walmart": [],
}


def _tpl(tid, sid, listing_id, status="Listed"):
    return {
        "Id": tid, "StockItemId": sid, "ConfiguratorId": 7, "IsLocked": False,
        "NextSuggestedAction": "Update",
        "Info": {"ActiveListingId": {"Value": listing_id}, "Status": {"Value": status}},
    }


TEMPLATES = {
    (SID_S, 18): [
        _tpl(TID_ORPHAN, SID_S, GID_DEAD),
        _tpl(TID_LIVE, SID_S, GID_LIVE),
    ],
}

SKU_TO_SID = {SKU_S: SID_S}
STORE = {"sub_source": "SWH Shopify", "shop_domain": "swh.myshopify.com",
         "access_token": "shpat_test"}


def _mock(delete_clears=True, extra_templates=None, extra_channel_skus=None,
          open_rate_limit_on=None):
    """Mock the Linnworks call layer (same shape as test_delist_channels.py).

    delete_clears: whether a processed Delete removes SHOPIFY rows from the
        channel-SKU table for the item that template serves.
    open_rate_limit_on: a stock_item_id whose OpenTemplatesByInventory call
        should raise RateLimitError once.
    """
    import server

    captured = {"process": [], "opened": []}
    live = {sid: [dict(r) for r in rows] for sid, rows in CHANNEL_SKUS.items()}
    for sid, rows in (extra_channel_skus or {}).items():
        live[sid] = [dict(r) for r in rows]
    templates = {k: [dict(t) for t in v] for k, v in TEMPLATES.items()}
    for k, v in (extra_templates or {}).items():
        templates[k] = [dict(t) for t in v]
    deleted_templates: set[int] = set()
    open_calls = {"count": 0}

    def _serves(tid):
        for (sid, cid), tpls in templates.items():
            for t in tpls:
                if t["Id"] == tid:
                    return sid
        return None

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
            ids = req["Parameters"]["InventoryItemIds"]
            captured["opened"].append((req["ChannelType"], cid, tuple(ids)))
            if open_rate_limit_on and open_rate_limit_on in ids and open_calls["count"] == 0:
                open_calls["count"] += 1
                raise server.RateLimitError("HTTP 429 — API calls quota exceeded!")
            out = []
            for sid in ids:
                for t in templates.get((sid, cid), []):
                    if t["Id"] in deleted_templates:
                        continue
                    out.append(dict(t))
            return {"TotalEntries": len(out), "TemplatesInfo": out}
        if path.endswith("ProcessTemplates"):
            req = payload["request"]
            captured["process"].append(req)
            for tr in req["TemplateRequests"]:
                tid = tr["TemplateId"]
                deleted_templates.add(tid)
                if delete_clears:
                    sid = _serves(tid)
                    if sid:
                        live[sid] = [r for r in live[sid] if r["Source"] != "SHOPIFY"]
            return {}
        raise AssertionError(f"Unexpected call_linnworks: {path}")

    def call_linnworks_get(path, params=None):
        if path.endswith("GetInventoryItemChannelSKUs"):
            return live.get(params["inventoryItemId"], [])
        if path.endswith("GetVariationGroupByParentId"):
            return None
        if path.endswith("SearchVariationGroups"):
            return {"PageNumber": 1, "TotalPages": 1, "TotalEntries": 0, "Data": []}
        if path.endswith("GetVariationItems"):
            return []
        raise AssertionError(f"Unexpected GET: {path}")

    patches = [
        patch("server.call_linnworks", side_effect=call_linnworks),
        patch("server.call_linnworks_get", side_effect=call_linnworks_get),
    ]
    return patches, captured


def _shopify_nodes(store, query, variables):
    """Real Shopify contract: a positional array, null for a deleted id."""
    return {"nodes": [
        None if gid.rsplit("/", 1)[-1] == GID_DEAD
        else {"__typename": "Product", "id": gid, "title": "RS-102201", "status": "ACTIVE"}
        for gid in variables["ids"]
    ]}


def _run(fn, *a, store=STORE, graphql=_shopify_nodes, mock_kwargs=None, **kw):
    import server
    patches, captured = _mock(**(mock_kwargs or {}))
    with patch("server._shopify_store_for", return_value=store), \
         patch("server._shopify_graphql", side_effect=graphql), \
         patches[0], patches[1]:
        out = fn(*a, **kw)
    return out, captured


# ── AC1: template_ids=None / absent is byte-identical to the old behaviour ──

class TestRegressionWhenTemplateIdsIsNone:

    def _expected_untargeted_shape(self, out):
        assert sorted(p["template_id"] for p in out["plan"]) == [TID_ORPHAN, TID_LIVE]
        assert out["unresolved"] == []
        assert out["blocked_summary"] == {}
        assert out["blocked_count"] == 0
        assert out["retirable_sku_count"] == 1
        assert out["rate_limited"] == []
        assert out["complete"] is True
        assert out["target_channel"] == "Shopify"
        assert out["target_channel_id"] == 18
        # None of the #115 keys exist at all when template_ids isn't passed.
        for key in ("template_ids_requested", "untouched_siblings",
                    "override_warnings", "listing_existence_checked"):
            assert key not in out

    def test_omitted_template_ids_matches_default_behaviour(self):
        import server
        out, captured = _run(server.unpublish_channel_listing, [SKU_S], dry_run=True)
        self._expected_untargeted_shape(out)
        # The dead-listing gate must never run — no Shopify probe touched.
        assert not captured["process"]

    def test_explicit_none_matches_omitted(self):
        import server
        omitted, _ = _run(server.unpublish_channel_listing, [SKU_S], dry_run=True)
        explicit, _ = _run(server.unpublish_channel_listing, [SKU_S],
                            template_ids=None, dry_run=True)
        assert omitted == explicit

    def test_none_never_calls_the_shopify_existence_probe(self):
        """A regression fixture with NO Shopify store configured must still
        plan every template — proving the gate is gated on template_ids, not
        merely on Shopify credentials being present."""
        import server
        out, captured = _run(server.unpublish_channel_listing, [SKU_S],
                              store=None, dry_run=True)
        self._expected_untargeted_shape(out)
        assert sorted(p["template_id"] for p in out["plan"]) == [TID_ORPHAN, TID_LIVE]

    def test_dry_run_plan_is_dict_for_dict_identical_to_pre_115_shape(self):
        """A literal, pinned expected dict for each plan row -- not just a
        subset of keys -- proving the #115 code path leaves the untargeted
        plan row shape byte-identical (QA round 1, non-blocking note)."""
        import server
        out, _ = _run(server.unpublish_channel_listing, [SKU_S], dry_run=True)

        row_common = {
            "action": "Delete", "channel": "Shopify", "channel_reference_id": GID_LIVE,
            "configurator_id": 7, "covers_skus": [SKU_S], "delete_proven": True,
            "listed_quantity": 2, "listing_sids": [SID_S], "next_suggested_action": "Update",
            "sku": SKU_S, "status": "Listed", "stock_item_id": SID_S,
            "sub_source": "SWH Shopify", "template_stock_item_id": SID_S,
            "templates_on_item": 2, "title": f"Title for {SKU_S}",
        }
        assert out["plan"] == [
            {**row_common, "active_listing_id": GID_DEAD, "template_id": TID_ORPHAN},
            {**row_common, "active_listing_id": GID_LIVE, "template_id": TID_LIVE},
        ]

    def test_live_run_results_are_dict_for_dict_identical_to_pre_115_shape(self):
        """Same, for the live-run `results` list."""
        import server
        out, _ = _run(server.unpublish_channel_listing, [SKU_S], dry_run=False)

        row_common = {
            "action": "Delete", "channel": "Shopify", "covers_skus": [SKU_S],
            "outcome": "taken_down", "processed": True, "sku": SKU_S,
            "still_listed": False, "sub_source": "SWH Shopify", "taken_down": True,
            "template_deleted": True, "template_status_after": None,
            "via_variation_parent": False,
        }
        assert out["results"] == [
            {**row_common, "template_id": TID_ORPHAN},
            {**row_common, "template_id": TID_LIVE},
        ]


# ── AC2: narrowing to one template id, and the sibling report ───────────────

class TestNarrowingAndSiblingReport:

    def test_targeting_the_dead_orphan_plans_exactly_that_one_row(self):
        import server
        out, captured = _run(
            server.unpublish_channel_listing, [SKU_S],
            template_ids=[TID_ORPHAN], dry_run=True)

        assert [p["template_id"] for p in out["plan"]] == [TID_ORPHAN]
        assert not captured["process"]

    def test_untouched_siblings_names_the_live_template(self):
        import server
        out, _ = _run(
            server.unpublish_channel_listing, [SKU_S],
            template_ids=[TID_ORPHAN], dry_run=True)

        assert len(out["untouched_siblings"]) == 1
        item = out["untouched_siblings"][0]
        assert item["stock_item_id"] == SID_S
        assert item["siblings"] == [
            {"template_id": TID_LIVE, "active_listing_id": GID_LIVE, "status": "Listed"},
        ]

    def test_untouched_siblings_absent_when_no_siblings_exist(self):
        """A single-template item targeted directly has nothing untouched."""
        import server
        out, _ = _run(
            server.unpublish_channel_listing, [SKU_S],
            template_ids=[TID_ORPHAN, TID_LIVE], dry_run=True)
        # Both templates targeted -> no siblings left un-targeted on this item.
        assert out["untouched_siblings"] == [] or all(
            not i["siblings"] for i in out["untouched_siblings"]
        )

    def test_untouched_siblings_excludes_items_with_no_targeted_template(self):
        """QA round 1 (non-blocking): a sibling row from a WHOLLY UNTARGETED
        item must never appear in the report -- untouched_siblings is scoped
        to items that actually have a targeted template."""
        import server
        other_sid = "9c000000-0000-0000-0000-000000000000"
        other_sku = "RS-OTHER"
        extra_templates = {
            (other_sid, 18): [_tpl(70001, other_sid, "1234567890")],
        }
        extra_channel_skus = {
            other_sid: [{"Source": "SHOPIFY", "SubSource": "SWH Shopify",
                         "ChannelReferenceId": "1234567890", "ListedQuantity": 1}],
        }
        SKU_TO_SID[other_sku] = other_sid
        try:
            out, _ = _run(
                server.unpublish_channel_listing, [SKU_S, other_sku],
                template_ids=[TID_ORPHAN], dry_run=True,
                mock_kwargs={"extra_templates": extra_templates,
                             "extra_channel_skus": extra_channel_skus})
        finally:
            del SKU_TO_SID[other_sku]

        sids_reported = {item["stock_item_id"] for item in out["untouched_siblings"]}
        assert sids_reported == {SID_S}
        assert other_sid not in sids_reported


# ── AC3: a template id not on the item ───────────────────────────────────────

class TestTemplateNotOnItem:

    def test_unknown_template_id_is_refused_and_never_deleted(self):
        import server
        out, captured = _run(
            server.unpublish_channel_listing, [SKU_S],
            template_ids=[TID_UNKNOWN], dry_run=False)

        assert out["plan"] == []
        u = next(u for u in out["unresolved"] if u.get("template_id") == TID_UNKNOWN)
        assert u["blocked_reason"] == "template_not_on_item"
        assert not captured["process"]

    def test_a_mix_of_known_and_unknown_ids_only_blocks_the_unknown_one(self):
        import server
        out, _ = _run(
            server.unpublish_channel_listing, [SKU_S],
            template_ids=[TID_ORPHAN, TID_UNKNOWN], dry_run=True)

        assert [p["template_id"] for p in out["plan"]] == [TID_ORPHAN]
        blocked_ids = {u.get("template_id") for u in out["unresolved"]}
        assert TID_UNKNOWN in blocked_ids
        assert TID_ORPHAN not in blocked_ids


# ── AC4: confirmed-still-live is refused by default ─────────────────────────

class TestListingStillLiveIsRefused:

    def test_confirmed_live_template_is_refused_and_never_deleted(self):
        import server
        out, captured = _run(
            server.unpublish_channel_listing, [SKU_S],
            template_ids=[TID_LIVE], dry_run=False)

        assert out["plan"] == []
        u = next(u for u in out["unresolved"] if u.get("template_id") == TID_LIVE)
        assert u["blocked_reason"] == "listing_still_live"
        assert not captured["process"]

    def test_orphan_is_unaffected_by_the_sibling_being_live(self):
        """Requesting the dead one alongside the live one only blocks the
        live one — the dead one still proceeds."""
        import server
        out, _ = _run(
            server.unpublish_channel_listing, [SKU_S],
            template_ids=[TID_ORPHAN, TID_LIVE], dry_run=True)

        assert [p["template_id"] for p in out["plan"]] == [TID_ORPHAN]
        u = next(u for u in out["unresolved"] if u.get("template_id") == TID_LIVE)
        assert u["blocked_reason"] == "listing_still_live"


# ── AC5: every "cannot verify" reason refuses by default ────────────────────

class TestUnverifiedExistenceIsRefused:

    def test_not_configured_refuses(self):
        import server
        out, captured = _run(
            server.unpublish_channel_listing, [SKU_S],
            template_ids=[TID_LIVE], store=None, dry_run=False)

        assert out["plan"] == []
        u = next(u for u in out["unresolved"] if u.get("template_id") == TID_LIVE)
        assert u["blocked_reason"] == "listing_existence_unverified"
        assert "not_configured" in u["error"]
        assert not captured["process"]

    def test_insufficient_scope_refuses(self):
        import server
        with patch("server._shopify_scope_block", return_value={
            "scopes_missing": ["read_products"],
        }):
            out, captured = _run(
                server.unpublish_channel_listing, [SKU_S],
                template_ids=[TID_LIVE], dry_run=False)

        assert out["plan"] == []
        u = next(u for u in out["unresolved"] if u.get("template_id") == TID_LIVE)
        assert u["blocked_reason"] == "listing_existence_unverified"
        assert "insufficient_scope" in u["error"]
        assert not captured["process"]

    def test_check_failed_refuses(self):
        import server
        def boom(store, query, variables):
            raise RuntimeError("Shopify API still throttled after backoff")

        out, captured = _run(
            server.unpublish_channel_listing, [SKU_S],
            template_ids=[TID_LIVE], graphql=boom, dry_run=False)

        assert out["plan"] == []
        u = next(u for u in out["unresolved"] if u.get("template_id") == TID_LIVE)
        assert u["blocked_reason"] == "listing_existence_unverified"
        assert "check_failed" in u["error"]
        assert not captured["process"]

    def test_unsupported_channel_refuses(self):
        """Amazon listing ids have no cheap existence probe at all."""
        import server
        amazon_templates = {
            ("amz-sid", 2): [
                _tpl(9001, "amz-sid", "vnm_amazon_item.FBA"),
                _tpl(9002, "amz-sid", "vnm_amazon_item"),
            ],
        }
        amazon_channel_skus = {
            "amz-sid": [{"Source": "AMAZON", "SubSource": "The Warehouse Group",
                        "ChannelReferenceId": "vnm_amazon_item", "ListedQuantity": 1}],
        }
        patches, captured = _mock(
            extra_templates=amazon_templates,
            extra_channel_skus=amazon_channel_skus,
        )
        SKU_TO_SID["vnm_amazon_item_sku"] = "amz-sid"
        try:
            import server
            with patches[0], patches[1]:
                out = server.unpublish_channel_listing(
                    ["vnm_amazon_item_sku"], sub_source="The Warehouse Group",
                    channel="Amazon", template_ids=[9001], dry_run=False)
        finally:
            del SKU_TO_SID["vnm_amazon_item_sku"]

        assert out["plan"] == []
        u = next(u for u in out["unresolved"] if u.get("template_id") == 9001)
        assert u["blocked_reason"] == "listing_existence_unverified"
        assert "unsupported_channel" in u["error"]
        assert not captured["process"]

    def test_exists_none_from_an_unrecognised_shopify_response_refuses(self):
        """An id-count mismatch in Shopify's own response -> exists=None for
        every id in that chunk — never a verdict either way."""
        import server
        def mismatched(store, query, variables):
            return {"nodes": None}

        out, captured = _run(
            server.unpublish_channel_listing, [SKU_S],
            template_ids=[TID_LIVE], graphql=mismatched, dry_run=False)

        assert out["plan"] == []
        u = next(u for u in out["unresolved"] if u.get("template_id") == TID_LIVE)
        assert u["blocked_reason"] == "listing_existence_unverified"
        assert not captured["process"]


# ── AC6: the override flag ───────────────────────────────────────────────────

class TestOverrideFlag:

    def test_override_lets_a_confirmed_live_delete_proceed(self):
        import server
        out, captured = _run(
            server.unpublish_channel_listing, [SKU_S],
            template_ids=[TID_LIVE], allow_live_listing_delete=True, dry_run=True)

        assert [p["template_id"] for p in out["plan"]] == [TID_LIVE]
        row = out["plan"][0]
        assert row["override_applied"] is True
        assert GID_LIVE in row["warning"]
        assert out["override_warnings"]
        assert GID_LIVE in out["override_warnings"][0]

    def test_override_lets_an_unverified_delete_proceed(self):
        import server
        out, _ = _run(
            server.unpublish_channel_listing, [SKU_S],
            template_ids=[TID_LIVE], store=None,
            allow_live_listing_delete=True, dry_run=True)

        assert [p["template_id"] for p in out["plan"]] == [TID_LIVE]

    def test_override_false_by_default(self):
        import server
        import inspect
        sig = inspect.signature(server.unpublish_channel_listing)
        assert sig.parameters["allow_live_listing_delete"].default is False
        assert sig.parameters["template_ids"].default is None


# ── AC7: live run sends ONLY the named template ids ──────────────────────────

class TestLiveRunOnlySendsNamedIds:

    def test_process_templates_payload_never_names_the_sibling(self):
        import server
        out, captured = _run(
            server.unpublish_channel_listing, [SKU_S],
            template_ids=[TID_ORPHAN], dry_run=False)

        assert captured["process"], "ProcessTemplates should have been called"
        for req in captured["process"]:
            ids_sent = [tr["TemplateId"] for tr in req["TemplateRequests"]]
            assert ids_sent == [TID_ORPHAN]
            assert TID_LIVE not in ids_sent


# ── AC8: read-back classification for a targeted delete ─────────────────────

class TestTargetedReadback:

    def test_ideal_case_target_gone_sibling_and_rows_intact(self):
        import server
        out, _ = _run(
            server.unpublish_channel_listing, [SKU_S],
            template_ids=[TID_ORPHAN], dry_run=False,
            mock_kwargs={"delete_clears": False})   # rows untouched by the delete

        res = out["results"][0]
        assert res["processed"] is True
        assert res["template_deleted"] is True
        assert res["siblings_intact"] is True
        assert res["still_listed"] is True
        assert res["outcome"] == "target_deleted"
        assert res["taken_down"] is True
        assert out["taken_down_count"] == 1

    def test_failure_case_target_gone_sibling_intact_rows_lost(self):
        """The trap: the Delete wipes the shared channel-SKU mapping even
        though the live sibling template survives."""
        import server
        out, _ = _run(
            server.unpublish_channel_listing, [SKU_S],
            template_ids=[TID_ORPHAN], dry_run=False,
            mock_kwargs={"delete_clears": True})    # simulates the trap

        res = out["results"][0]
        assert res["template_deleted"] is True
        assert res["siblings_intact"] is True
        assert res["still_listed"] is False
        assert res["outcome"] == "target_deleted_mapping_lost"
        assert res["taken_down"] is False
        assert "lost its linnworks mapping" in res["warning"].lower()
        assert out["taken_down_count"] == 0

    def test_status_not_deleted_is_still_a_failure(self):
        """A template Delete can 2xx and leave Status: 'Not deleted' — this
        must still be reported as a failure, per the untargeted precedent."""
        import server

        def call_linnworks(path, payload):
            if path.endswith("GetConfiguratorsInfoPaged"):
                return {"ConfiguratorsInfo": CATALOGUES["Shopify"]}
            if path.endswith("GetInventoryItem"):
                return {"StockItemId": SID_S, "ItemTitle": "RS-102201"}
            if path.endswith("OpenTemplatesByInventory"):
                out = [
                    _tpl(TID_ORPHAN, SID_S, GID_DEAD, status="Not deleted"),
                    _tpl(TID_LIVE, SID_S, GID_LIVE, status="Listed"),
                ]
                return {"TotalEntries": len(out), "TemplatesInfo": out}
            if path.endswith("ProcessTemplates"):
                return {}
            raise AssertionError(path)

        def call_linnworks_get(path, params=None):
            if path.endswith("GetInventoryItemChannelSKUs"):
                return CHANNEL_SKUS[SID_S]
            if path.endswith("GetVariationGroupByParentId"):
                return None
            if path.endswith("SearchVariationGroups"):
                return {"PageNumber": 1, "TotalPages": 1, "TotalEntries": 0, "Data": []}
            if path.endswith("GetVariationItems"):
                return []
            raise AssertionError(path)

        with patch("server.call_linnworks", side_effect=call_linnworks), \
             patch("server.call_linnworks_get", side_effect=call_linnworks_get), \
             patch("server._shopify_store_for", return_value=STORE), \
             patch("server._shopify_graphql", side_effect=_shopify_nodes):
            out = server.unpublish_channel_listing(
                [SKU_S], template_ids=[TID_ORPHAN], dry_run=False)

        res = out["results"][0]
        assert res["template_deleted"] is False
        assert res["outcome"] == "target_delete_failed"
        assert res["taken_down"] is False


# ── AC9: the variation whole-group gate still applies ────────────────────────

class TestVariationGateStillApplies:

    GRP_SID = "f0000000-0000-0000-0000-000000000000"
    CHILD_DEAD_SID = "f0000000-0000-0000-0000-000000000001"
    CHILD_LIVE_SID = "f0000000-0000-0000-0000-000000000002"
    GROUP_TID = 7001

    def _mock(self):
        swh = {"Source": "SHOPIFY", "SubSource": "SWH Shopify",
               "ChannelReferenceId": "111:222:333", "ListedQuantity": 0}
        channel_skus = {
            self.CHILD_DEAD_SID: [dict(swh)],
            self.CHILD_LIVE_SID: [dict(swh)],
        }
        sid_to_sku = {
            self.GRP_SID: "grp-x", self.CHILD_DEAD_SID: "child-dead",
            self.CHILD_LIVE_SID: "child-live",
        }
        group_tpl = _tpl(self.GROUP_TID, self.GRP_SID, "999999")

        def call_linnworks(path, payload):
            if path.endswith("GetConfiguratorsInfoPaged"):
                return {"ConfiguratorsInfo": CATALOGUES["Shopify"]}
            if path.endswith("GetInventoryItem"):
                sku = payload.get("sku")
                sid = {"child-dead": self.CHILD_DEAD_SID,
                       "child-live": self.CHILD_LIVE_SID}.get(sku)
                if not sid:
                    raise RuntimeError("HTTP 400 — not found")
                return {"StockItemId": sid, "ItemTitle": sku}
            if path.endswith("OpenTemplatesByInventory"):
                ids = payload["request"]["Parameters"]["InventoryItemIds"]
                out = [dict(group_tpl)] if self.GRP_SID in ids else []
                return {"TotalEntries": len(out), "TemplatesInfo": out}
            if path.endswith("BatchGetInventoryItemChannelSKUs"):
                ids = payload["inventoryItemIds"]
                return [{"StockItemId": i, "ChannelSkus": channel_skus.get(i, [])}
                        for i in ids]
            if path.endswith("ProcessTemplates"):
                raise AssertionError("must not delete a live-sibling group template")
            raise AssertionError(path)

        def call_linnworks_get(path, params=None):
            if path.endswith("GetInventoryItemChannelSKUs"):
                return channel_skus.get(params["inventoryItemId"], [])
            if path.endswith("GetVariationGroupByParentId"):
                sku = sid_to_sku.get(params["pkStockItemId"])
                if sku == "child-dead" or sku == "child-live":
                    return None
                return None
            if path.endswith("SearchVariationGroups"):
                text = (params.get("searchText") or "").lower()
                if "child" in text:
                    return {"PageNumber": 1, "TotalPages": 1, "TotalEntries": 1,
                            "Data": [{"VariationSKU": "grp-x", "pkVariationItemId": self.GRP_SID,
                                     "VariationGroupName": "Group X"}]}
                return {"PageNumber": 1, "TotalPages": 1, "TotalEntries": 0, "Data": []}
            if path.endswith("GetVariationItems"):
                return [
                    {"pkStockItemId": self.CHILD_DEAD_SID, "ItemNumber": "child-dead",
                     "ItemTitle": "child-dead"},
                    {"pkStockItemId": self.CHILD_LIVE_SID, "ItemNumber": "child-live",
                     "ItemTitle": "child-live"},
                ]
            raise AssertionError(path)

        return (
            patch("server.call_linnworks", side_effect=call_linnworks),
            patch("server.call_linnworks_get", side_effect=call_linnworks_get),
        )

    def test_child_reached_via_template_ids_is_still_blocked_by_live_sibling(self):
        import server
        p1, p2 = self._mock()
        with p1, p2:
            out = server.unpublish_channel_listing(
                ["child-dead"], template_ids=[self.GROUP_TID], dry_run=True)

        assert out["plan"] == []
        u = out["unresolved"][0]
        assert u["blocked_reason"] == "variation_child_live_siblings"


# ── AC10: staging counts PLANNED TEMPLATE ROWS, not requested SKUs ──────────

class TestStagingCountsPlannedRows:

    def test_threshold_is_10_or_lower(self):
        import server
        assert server.WRITE_THRESHOLDS["unpublish_channel_listing"] <= 10

    def test_many_skus_but_a_small_final_plan_does_not_stage(self):
        """12 SKUs requested (> 10), but template_ids narrows the FINAL plan
        to 1 row -- proving the guard counts plan rows, not input SKUs."""
        import server
        skus = [SKU_S] + [f"ZZZ-NOPE-{i}" for i in range(11)]
        out, captured = _run(
            server.unpublish_channel_listing, skus,
            template_ids=[TID_ORPHAN], dry_run=False)

        assert out.get("staged") is not True
        assert [p["template_id"] for p in out["plan"]] == [TID_ORPHAN]
        assert len(captured["process"]) == 1

    def test_more_than_ten_targeted_rows_does_stage(self):
        """11 distinct dead orphan templates across 11 distinct items -- the
        final PLAN exceeds the threshold, so it stages even though nothing is
        ambiguous about which templates would be deleted."""
        import server
        n = 11
        extra_templates = {}
        extra_channel_skus = {}
        extra_sku_to_sid = {}
        tids = []
        for i in range(n):
            sid = f"9{i:08d}-0000-0000-0000-000000000000"
            tid = 60000 + i
            gid_dead = f"7{i:08d}"
            gid_live = f"8{i:08d}"
            sku = f"MANY-{i}"
            extra_templates[(sid, 18)] = [
                _tpl(tid, sid, gid_dead), _tpl(tid + 500, sid, gid_live),
            ]
            extra_channel_skus[sid] = [
                {"Source": "SHOPIFY", "SubSource": "SWH Shopify",
                 "ChannelReferenceId": gid_live, "ListedQuantity": 1},
            ]
            extra_sku_to_sid[sku] = sid
            tids.append(tid)

        def many_nodes(store, query, variables):
            # Every "7..." id in this fixture is a deleted duplicate product.
            return {"nodes": [
                None if gid.rsplit("/", 1)[-1].startswith("7") else
                {"__typename": "Product", "id": gid, "title": "x", "status": "ACTIVE"}
                for gid in variables["ids"]
            ]}

        SKU_TO_SID.update(extra_sku_to_sid)
        try:
            out, captured = _run(
                server.unpublish_channel_listing, list(extra_sku_to_sid),
                template_ids=tids, dry_run=False, graphql=many_nodes,
                mock_kwargs={"extra_templates": extra_templates,
                             "extra_channel_skus": extra_channel_skus})
        finally:
            for sku in extra_sku_to_sid:
                del SKU_TO_SID[sku]

        assert out.get("staged") is True
        assert not captured["process"]
        assert len(out["plan"]) == n


# ── AC11: RateLimitError while opening templates degrades gracefully ────────

class TestRateLimitDuringOpen:

    def test_rate_limited_item_lands_in_rate_limited_not_a_blocked_reason(self):
        import server
        out, captured = _run(
            server.unpublish_channel_listing, [SKU_S],
            template_ids=[TID_ORPHAN], dry_run=True,
            mock_kwargs={"open_rate_limit_on": SID_S})

        assert out["complete"] is False
        assert any(r.get("stock_item_id") == SID_S for r in out["rate_limited"])
        codes = {u.get("blocked_reason") for u in out["unresolved"]}
        assert "template_not_on_item" not in codes
        assert "listing_still_live" not in codes
        assert "listing_existence_unverified" not in codes
        assert out["plan"] == []
        assert not captured["process"]


# ── AC11 (continued): RateLimitError / quota pause while CHECKING existence ──
#
# QA round 1 finding 1: only the "opening templates" half of AC11 was built.
# A Shopify quota pause while checking listing existence read as a data
# verdict (`listing_existence_unverified`, `complete: True`), and a bare
# RateLimitError raised from the existence path escaped the tool uncaught.

class TestRateLimitDuringExistenceCheck:

    def test_shopify_quota_pause_during_existence_check_is_rate_limited(self):
        """`_shopify_graphql` itself raises a plain RuntimeError once its own
        backoff ladder is exhausted ('quota pause, not a data problem') —
        that wording must be recognised as a throttle, not a genuine check
        failure, even though it isn't a RateLimitError instance."""
        import server

        def throttled(store, query, variables):
            raise RuntimeError(
                "Shopify API still throttled after backoff (swh.myshopify.com). "
                "Retry shortly — this is a quota pause, not a data problem."
            )

        out, captured = _run(
            server.unpublish_channel_listing, [SKU_S],
            template_ids=[TID_ORPHAN], dry_run=True, graphql=throttled)

        assert out["complete"] is False
        assert any(r.get("template_id") == TID_ORPHAN for r in out["rate_limited"])
        codes = {u.get("blocked_reason") for u in out["unresolved"]}
        assert "listing_existence_unverified" not in codes
        assert "template_not_on_item" not in codes
        assert "listing_still_live" not in codes
        assert out["plan"] == []
        assert not captured["process"]

    def test_rate_limit_error_during_existence_check_is_caught_not_raised(self):
        """A RateLimitError raised directly out of _shopify_graphql (e.g. a
        future caller that stops retrying internally) must be caught by the
        targeted gate rather than escaping unpublish_channel_listing."""
        import server

        def boom(store, query, variables):
            raise server.RateLimitError("HTTP 429 — API calls quota exceeded!")

        out, captured = _run(
            server.unpublish_channel_listing, [SKU_S],
            template_ids=[TID_ORPHAN], dry_run=True, graphql=boom)

        assert out["complete"] is False
        assert any(r.get("template_id") == TID_ORPHAN for r in out["rate_limited"])
        codes = {u.get("blocked_reason") for u in out["unresolved"]}
        assert "listing_existence_unverified" not in codes
        assert "template_not_on_item" not in codes
        assert "listing_still_live" not in codes
        assert out["plan"] == []
        assert not captured["process"]

    def test_ordinary_check_failure_without_quota_wording_is_still_unverified(self):
        """Regression guard: a genuine (non-quota) check_failed reason must
        keep refusing as listing_existence_unverified, not be swept into
        rate_limited just because it is also a RuntimeError."""
        import server

        def boom(store, query, variables):
            raise RuntimeError("Shopify GraphQL errors: something genuinely broke")

        out, captured = _run(
            server.unpublish_channel_listing, [SKU_S],
            template_ids=[TID_LIVE], graphql=boom, dry_run=False)

        assert out["complete"] is True
        assert out["rate_limited"] == []
        u = next(u for u in out["unresolved"] if u.get("template_id") == TID_LIVE)
        assert u["blocked_reason"] == "listing_existence_unverified"
        assert not captured["process"]
