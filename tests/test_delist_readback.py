"""
Tests for the per-template delist read-back (issue #36).

The defect: `unpublish_channel_listing` verified take-downs with a single
PER-ITEM channel-SKU read, then applied that one result to EVERY template on the
item. On a multi-template item the first successful delete empties the
channel-SKU table, so a second template that was never deleted still read as
success.

Live proof (6 Aug 2026): `304035-000-825` (tpl 8481 + 41666) and
`304037-000-850` (tpl 12710 + 41653) each reported `taken_down: true` for both
templates; one of each survived with Linnworks returning `Status: "Not deleted"`.
Re-firing Delete returned a clean 2xx and changed nothing.

No customer impact that run — the Shopify products were already gone (verified
via the Shopify Admin API) — but on an item where the surviving template held the
live listing, this would report a clean take-down over a listing still selling.

The fix checks two independent surfaces and lets them disagree:
  - per TEMPLATE: re-open the item's templates, is this id gone?
  - per ITEM:     are any channel-SKU rows left?
"""
import pytest
from unittest.mock import patch

import server

SID = "aaaaaaaa-0000-0000-0000-000000000001"
SKU = "MULTI-TPL"
STORE = "SWH Shopify"
CHANNEL_ID = 18


def _configurators(*a, **k):
    return [{"id": 1, "name": "Conf", "channel_id": CHANNEL_ID, "sub_source": STORE,
             "show_in_inventory": True}]


def _tpl(tid, status="Listed"):
    return {
        "Id": tid, "StockItemId": SID, "ConfiguratorId": 4, "IsLocked": False,
        "NextSuggestedAction": "Delete",
        "Info": {"ActiveListingId": {"Value": f"listing-{tid}"}, "Status": {"Value": status}},
    }


def _channel_row():
    return {"Source": "SHOPIFY", "SubSource": STORE, "SKU": SKU,
            "ChannelReferenceId": "p:v:i", "ListedQuantity": 0}


def _run(open_templates_after, channel_rows_after, planned=(8481, 41666)):
    """
    Drive a live take-down. `open_templates_after` is what the post-delete
    template re-open returns; `channel_rows_after` what the channel-SKU read returns.
    """
    calls = {"open": 0, "chan": 0}

    def fake_post(path, payload):
        if path == "Inventory/GetInventoryItem":
            return {"StockItemId": SID, "ItemTitle": "Multi-template item"}
        if path == "GenericListings/OpenTemplatesByInventory":
            calls["open"] += 1
            if calls["open"] == 1:                    # planning read
                return {"TemplatesInfo": [_tpl(t) for t in planned]}
            return {"TemplatesInfo": open_templates_after}   # post-delete read-back
        if path == "GenericListings/ProcessTemplates":
            return {}
        raise AssertionError(f"unexpected POST {path}")

    def fake_get(path, params=None):
        # The same endpoint answers the pre-flight "is it listed?" check and the
        # post-delete read-back, so the first call must show the live listing.
        if path == "Inventory/GetInventoryItemChannelSKUs":
            calls["chan"] += 1
            return [_channel_row()] if calls["chan"] == 1 else channel_rows_after
        raise AssertionError(f"unexpected GET {path}")

    with patch.object(server, "_fetch_glt_configurators", side_effect=_configurators), \
         patch.object(server, "_resolve_sku_to_id", return_value=SID), \
         patch.object(server, "call_linnworks", side_effect=fake_post), \
         patch.object(server, "call_linnworks_get", side_effect=fake_get), \
         patch.object(server, "_fetch_channel_skus_for_ids",
                      return_value={SID.lower(): [_channel_row()]}):
        return server.unpublish_channel_listing(
            skus=[SKU], sub_source=STORE, channel="Shopify",
            confirmed_count=2, dry_run=False,
        )


# --- the regression ----------------------------------------------------------

def test_surviving_template_is_not_reported_as_taken_down():
    """
    THE bug. Template 8481 deleted, 41666 survived with "Not deleted", and the
    channel-SKU table is empty because the first delete cleared it.
    Old behaviour: both taken_down=true. Required: only 8481.
    """
    out = _run(open_templates_after=[_tpl(41666, "Not deleted")], channel_rows_after=[])
    by_tid = {r["template_id"]: r for r in out["results"]}

    assert by_tid[8481]["taken_down"] is True
    assert by_tid[8481]["outcome"] == "taken_down"

    assert by_tid[41666]["taken_down"] is False
    assert by_tid[41666]["outcome"] == "listing_gone_template_orphaned"
    assert by_tid[41666]["template_status_after"] == "Not deleted"
    assert "still exists" in by_tid[41666]["warning"]

    assert out["taken_down_count"] == 1
    assert out["orphaned_template_count"] == 1


def test_counts_do_not_overstate_success():
    out = _run(open_templates_after=[_tpl(41666, "Not deleted")], channel_rows_after=[])
    assert out["taken_down_count"] == 1
    assert out["taken_down_count"] != len(out["results"])
    assert "1 template(s) survived the Delete" in out["message"]


def test_both_templates_gone_is_a_clean_success():
    out = _run(open_templates_after=[], channel_rows_after=[])
    assert all(r["taken_down"] is True for r in out["results"])
    assert out["taken_down_count"] == 2
    assert out["orphaned_template_count"] == 0
    assert out["delete_failed_count"] == 0


def test_template_survives_AND_listing_row_remains_is_a_hard_failure():
    """The dangerous state: nothing was removed and the listing may still sell."""
    out = _run(open_templates_after=[_tpl(41666, "Not deleted")],
               channel_rows_after=[_channel_row()])
    by_tid = {r["template_id"]: r for r in out["results"]}
    assert by_tid[41666]["outcome"] == "delete_failed"
    assert by_tid[41666]["taken_down"] is False
    assert "may still be live" in by_tid[41666]["warning"]
    assert out["delete_failed_count"] == 1
    assert "may still be selling" in out["message"]


def test_template_deleted_but_channel_row_lagging():
    """Template gone, row still present — usually channel sync lag, not a failure."""
    out = _run(open_templates_after=[], channel_rows_after=[_channel_row()])
    for r in out["results"]:
        assert r["outcome"] == "template_deleted_listing_row_remains"
        assert r["taken_down"] is False
        assert r["still_listed"] is True


def test_template_readback_failure_yields_unconfirmed_not_success():
    """If we cannot verify, say so — never default to taken_down."""
    calls = {"open": 0}

    def fake_post(path, payload):
        if path == "Inventory/GetInventoryItem":
            return {"StockItemId": SID, "ItemTitle": "Multi-template item"}
        if path == "GenericListings/OpenTemplatesByInventory":
            calls["open"] += 1
            if calls["open"] == 1:
                return {"TemplatesInfo": [_tpl(8481)]}
            raise RuntimeError("HTTP 500 — boom")
        if path == "GenericListings/ProcessTemplates":
            return {}
        raise AssertionError(path)

    with patch.object(server, "_fetch_glt_configurators", side_effect=_configurators), \
         patch.object(server, "_resolve_sku_to_id", return_value=SID), \
         patch.object(server, "call_linnworks", side_effect=fake_post), \
         patch.object(server, "call_linnworks_get", return_value=[_channel_row()]), \
         patch.object(server, "_fetch_channel_skus_for_ids",
                      return_value={SID.lower(): [_channel_row()]}):
        out = server.unpublish_channel_listing(
            skus=[SKU], sub_source=STORE, channel="Shopify", dry_run=False)

    r = out["results"][0]
    assert r["taken_down"] is None
    assert r["outcome"] == "unconfirmed"
    assert "template_readback_error" in r
    assert out["taken_down_count"] == 0


def test_per_template_status_is_surfaced_verbatim():
    """`Status: "Not deleted"` is an unambiguous negative — don't discard it."""
    out = _run(open_templates_after=[_tpl(41666, "Not deleted")], channel_rows_after=[])
    orphan = next(r for r in out["results"] if r["template_id"] == 41666)
    assert orphan["template_status_after"] == "Not deleted"
    assert orphan["template_deleted"] is False


def test_single_template_item_still_works():
    """The common case must not regress."""
    out = _run(open_templates_after=[], channel_rows_after=[], planned=(8481,))
    assert len(out["results"]) == 1
    assert out["results"][0]["taken_down"] is True
    assert out["taken_down_count"] == 1
