"""
Tests for the refresh_channel_listing dangling-listing pre-flight (issue #52).

BACKGROUND — the live case (diagnosed 9 Sep 2026)
------------------------------------------------
SWH Shopify reported this on the Echo Sonar 4 Wheel template (SKU
`Echo-hoar4eles-ck`, template 38812):

    Failed to create/update listing: Failed to check status of existing listing:
    Error reading JObject from JsonReader. Current JsonReader item is not an
    object: Null. Path 'data.nodes[1]', line 1, position 7351.

That listing was **entirely healthy** — product 9276972859638 ACTIVE, all 12
variants and all 8 media resolving, every channel-SKU reference valid. The fault
belonged to a SIBLING template on the same configurator (81): the Echo Sonar 3
Wheel, template 38827, whose stored ActiveListingId `9277472506102` had been
DELETED on Shopify (it was a duplicate product — the #39 pattern), while that
item's channel-SKU rows had already been re-pointed at the surviving product
`9276972695798`.

Two things make this class of failure nasty, and both are pinned below:

  1. The two Linnworks surfaces DISAGREE, and the healthy-looking one is the one
     everything else reads. `get_channel_listings` showed valid references; only
     the template's snapshot was dangling. So a mapping-based check is
     structurally blind to it.
  2. Shopify's `nodes(ids:)` is POSITIONAL and returns null — not an error — for
     a deleted id. Linnworks reads each slot as a JObject, so ONE null throws and
     takes every template batched with it down. The healthy listing was
     collateral damage, which is why blocking only the dangling row (and keeping
     the healthy ones pushable) is the required behaviour, not a nicety.

All Linnworks and Shopify calls are mocked; shapes mirror the live responses.
"""
from unittest.mock import patch

import pytest

# ── Fixtures ──────────────────────────────────────────────────────────────────

SID_OK   = "febd3dd7-2a3d-4d39-a695-275c0c81794b"   # Echo 4 Wheel — healthy
SID_DEAD = "cdbdb03f-d28d-4264-be6e-7d28c826d7b4"   # Echo 3 Wheel — dangling

SKU_OK   = "Echo-hoar4eles-ck"
SKU_DEAD = "Echo-hoar3eles-00"

GID_OK   = "9276972859638"
GID_DEAD = "9277472506102"      # deleted duplicate product

ITEMS = {
    SKU_OK:   {"StockItemId": SID_OK,   "ItemTitle": "Echo Sonar 4 Wheel Freeskates - Black",
               "RetailPrice": 179.95},
    SKU_DEAD: {"StockItemId": SID_DEAD, "ItemTitle": "Echo Sonar 3 Wheel Freeskates - 100",
               "RetailPrice": 224.95},
}

SWH_ROW = {"Source": "SHOPIFY", "SubSource": "SWH Shopify"}
CONFIGURATORS = [{"id": 81, "name": "Master - Skate Size", "channel_id": 18,
                  "sub_source": "SWH Shopify", "show_in_inventory": True}]
STORE = {"sub_source": "SWH Shopify", "shop_domain": "swh.myshopify.com",
         "access_token": "shpat_test"}


def _tpl(tid, sid, listing_id):
    return {
        "Id": tid, "StockItemId": sid, "ConfiguratorId": 81,
        "IsLocked": False, "IsAllowedToRevise": True,
        "NextSuggestedAction": "Update", "IsNextSuggestedActionAllowed": True,
        "Info": {
            "ActiveListingId": {"Value": listing_id},
            "Status": {"Value": "Errors while updating"},
            "Title": {"Value": "Echo Sonar"},
            "Price": {"Value": 0.0},
            "LastModificationTime": {"Value": "2026-02-11T10:22:46.3233333Z"},
        },
    }


TEMPLATES = {
    SID_OK:   _tpl(38812, SID_OK,   GID_OK),
    SID_DEAD: _tpl(38827, SID_DEAD, GID_DEAD),
}


class _Harness:
    def __init__(self):
        self.processed_template_ids = []

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
            return [{"StockItemId": sid, "ChannelSkus": []}
                    for sid in payload["inventoryItemIds"]]
        if "ProcessTemplates" in path:
            for tr in payload["request"]["TemplateRequests"]:
                self.processed_template_ids.append(tr["TemplateId"])
            return {}
        raise AssertionError(f"Unexpected call_linnworks path: {path}")

    def call_linnworks_get(self, path, params=None):
        params = params or {}
        if "GetInventoryItemChannelSKUs" in path:
            return [SWH_ROW]
        raise AssertionError(f"Unexpected call_linnworks_get path: {path}")


def _shopify_nodes(store, query, variables):
    """Shopify's real contract: a POSITIONAL array with null for dead ids."""
    return {"nodes": [
        None if gid.rsplit("/", 1)[-1] == GID_DEAD
        else {"__typename": "Product", "id": gid, "title": "Echo Sonar", "status": "ACTIVE"}
        for gid in variables["ids"]
    ]}


def _run(skus, *, store=STORE, graphql=_shopify_nodes, **kwargs):
    import server
    h = _Harness()
    kwargs.setdefault("check_staleness", False)
    with patch("server._fetch_glt_configurators", return_value=CONFIGURATORS), \
         patch("server.call_linnworks", side_effect=h.call_linnworks), \
         patch("server.call_linnworks_get", side_effect=h.call_linnworks_get), \
         patch("server._shopify_store_for", return_value=store), \
         patch("server._shopify_graphql", side_effect=graphql) as gql:
        out = server.refresh_channel_listing(skus, sub_source="SWH Shopify", **kwargs)
    return out, h, gql


def _row(out, template_id):
    return next((r for r in out["plan"] if r["template_id"] == template_id), None)


# ── The null slot itself — the read Linnworks gets wrong ─────────────────────

class TestNullSafeProbe:

    def test_null_slot_is_data_not_an_exception(self):
        """A null in the array must come back as exists=False, index-aligned —
        never raise, and never shift the other answers along by one."""
        import server
        with patch("server._shopify_graphql", side_effect=_shopify_nodes):
            got = server._shopify_listings_exist(
                STORE,
                [f"gid://shopify/Product/{GID_OK}",
                 f"gid://shopify/Product/{GID_DEAD}",
                 "gid://shopify/Product/111"],
            )
        assert got[f"gid://shopify/Product/{GID_OK}"]["exists"] is True
        assert got[f"gid://shopify/Product/{GID_DEAD}"]["exists"] is False
        assert got["gid://shopify/Product/111"]["exists"] is True

    def test_unrecognised_shape_reports_unknown_not_missing(self):
        """A response we cannot line up must not be read as 'all deleted' — that
        would block every push in the batch (issue #37's rule)."""
        import server
        with patch("server._shopify_graphql", return_value={"nodes": None}):
            got = server._shopify_listings_exist(STORE, ["gid://shopify/Product/1"])
        assert got["gid://shopify/Product/1"]["exists"] is None


# ── The collateral-damage case ───────────────────────────────────────────────

class TestDanglingListingIsBlocked:

    def test_dangling_template_is_excluded_from_the_plan(self):
        out, _, _ = _run([SKU_DEAD])
        assert _row(out, 38827) is None, "dangling template must not be pushable"
        blocked = next(u for u in out["unresolved"] if u.get("template_id") == 38827)
        assert blocked["blocked_reason"] == "dangling_listing"
        assert GID_DEAD in blocked["error"]
        assert out["dangling_listing_count"] == 1

    def test_healthy_sibling_stays_pushable(self):
        """The whole point: the healthy listing was only ever collateral damage,
        so it must survive the batch that contains a dangling one."""
        out, _, _ = _run([SKU_OK, SKU_DEAD])
        healthy = _row(out, 38812)
        assert healthy is not None
        assert healthy["listing_exists"] is True
        assert healthy["listing_check"] == "exists"
        assert out["dangling_listing_count"] == 1
        assert len(out["plan"]) == 1

    def test_live_run_never_pushes_a_dangling_template(self):
        out, h, _ = _run([SKU_OK, SKU_DEAD], dry_run=False)
        assert 38827 not in h.processed_template_ids
        assert h.processed_template_ids == [38812]

    def test_message_names_the_dangling_template(self):
        out, _, _ = _run([SKU_OK, SKU_DEAD])
        assert "DANGLING" in out["message"]
        assert "38827" in out["message"]


# ── Unknown must never read as gone ──────────────────────────────────────────

class TestGracefulDegradation:

    def test_no_shopify_credentials_blocks_nothing(self):
        out, _, _ = _run([SKU_OK, SKU_DEAD], store=None)
        assert out["listing_existence_checked"] is False
        assert out["dangling_listing_count"] == 0
        assert len(out["plan"]) == 2, "an unverifiable listing must stay pushable"
        assert "not_configured" in out["listing_existence_note"]
        for r in out["plan"]:
            assert r["listing_exists"] is None

    def test_shopify_error_blocks_nothing_and_says_so(self):
        def boom(store, query, variables):
            raise RuntimeError("Shopify API still throttled after backoff")
        out, _, _ = _run([SKU_OK, SKU_DEAD], graphql=boom)
        assert out["listing_existence_checked"] is False
        assert out["dangling_listing_count"] == 0
        assert len(out["plan"]) == 2
        assert "check_failed" in out["listing_existence_note"]

    def test_check_can_be_switched_off_and_then_makes_no_shopify_call(self):
        out, _, gql = _run([SKU_OK, SKU_DEAD], check_listing_exists=False)
        assert gql.call_count == 0
        assert out["listing_existence_checked"] is False
        assert len(out["plan"]) == 2

    def test_non_shopify_channel_is_unsupported_not_failed(self):
        """Amazon/TikTok listing ids are channel SKUs, not product ids — there is
        no cheap existence probe, so the answer is 'not checked', not 'gone'."""
        import server
        res = server._glt_listing_existence(
            [{"active_listing_id": "vnm_bearings_gold.FBA", "template_id": 1}],
            "The Warehouse Group", "AMAZON")
        assert res["checked"] is False
        assert "unsupported_channel" in res["reason"]
