"""
Tests for the PROVISIONAL targeted dangling-template delete (issue #115).

This tool exists to answer ONE question on ONE SKU, and two of its three
plausible outcomes end with its own removal. See
docs/superpowers/specs/2026-09-25-dangling-glt-template-handling-design.md.

The danger it guards against: channel-SKU rows belong to the ITEM, not the
template. `unpublish_channel_listing`'s issue-#36 read-back records that "the
first successful delete empties that table for the whole item", and CLAUDE.md's
v1.50.0 note records that this tool's predecessor was deliberately NOT used on
the Echo orphan for exactly that reason. So deleting an orphan template may take
the LIVE sibling's mapping with it — the listing stays up but loses stock and
price sync, and nothing flags it.

Near-circularity, stated plainly: the proof of death (`Not deleted`) is also the
signal of a PRIOR delete failure. This tool is therefore engineered to fire on
templates that may well refuse to delete.
"""
from unittest.mock import patch

import pytest

import server

SID = "aaaaaaaa-0000-0000-0000-000000000001"
PSID = "aaaaaaaa-0000-0000-0000-00000000000p"
STORE = "SWH Shopify"
CHANNEL_ID = 18
ORPHAN = 38539
LIVE = 54649


def _target(*a, **k):
    return {"ok": True, "channel_id": CHANNEL_ID, "resolution": "exact",
            "available_sub_sources": [STORE],
            "channel": {"channel_type": "Shopify", "channel_name": "SHOPIFY",
                        "source": "SHOPIFY"}}


def _tpl(tid, status="Listed", listing="listing-x"):
    return {"Id": tid, "ConfiguratorId": 81,
            "Info": {"ActiveListingId": {"Value": listing},
                     "Status": {"Value": status}}}


def _row(ref="9276972859638:1:1"):
    return {"Source": "SHOPIFY", "SubSource": STORE, "SKU": "SKU-A",
            "ChannelReferenceId": ref, "UpdateStatus": "Confirmed",
            "ListedQuantity": 3, "MaxListedQuantity": 0, "LastUpdate": None,
            "IgnoreSync": False, "IsMultiLocation": False,
            "ChannelSKURowId": "row-1"}


def _item(ep, p=None):
    return {"StockItemId": SID, "ItemTitle": "T"}


def _both_templates(ch, cid, sid):
    return [_tpl(ORPHAN, "Not deleted"), _tpl(LIVE, "Listed")]


def _run(**kw):
    """Call the tool with the happy-path fixtures, overriding as needed."""
    opts = {"templates": _both_templates, "rows": lambda ids: {SID.lower(): [_row()]}}
    opts.update(kw.pop("_fixtures", {}))
    with patch.object(server, "_resolve_glt_target", _target), \
         patch.object(server, "call_linnworks", _item), \
         patch.object(server, "_open_item_templates", opts["templates"]), \
         patch.object(server, "_fetch_channel_skus_for_ids", opts["rows"]):
        return server.delete_dangling_glt_template(
            kw.pop("sku", "SKU-A"), kw.pop("template_id", ORPHAN), **kw)


def test_dry_run_is_the_default_and_plans_only_the_named_template():
    out = _run()
    assert out["dry_run"] is True
    assert out["plan"]["template_id"] == ORPHAN
    assert out["plan"]["siblings_untouched"] == [
        {"template_id": LIVE, "configurator_id": 81, "active_listing_id": "listing-x",
         "status": "Listed", "verdict": "not_proven_dangling"}]


def test_template_id_as_a_string_still_resolves():
    """Review Focus 2 — MCP callers routinely stringify numbers. Returning
    `template_not_on_item` for a template that is right there would send the
    caller hunting for a wrong id."""
    out = _run(template_id="38539")
    assert out.get("blocked_reason") != "template_not_on_item"
    assert out["plan"]["template_id"] == ORPHAN


def test_template_not_on_this_item_is_refused():
    out = _run(template_id=99999)
    assert out["blocked_reason"] == "template_not_on_item"
    assert out["success"] is False


@pytest.mark.parametrize("status", ["Listed", "Errors while updating", "", None])
def test_only_not_deleted_may_be_deleted(status):
    out = _run(_fixtures={"templates": lambda ch, cid, sid: [
        _tpl(ORPHAN, status), _tpl(LIVE, "Listed")]})
    assert out["blocked_reason"] == "dangling_not_proven"


def test_single_template_item_is_redirected_to_unpublish():
    out = _run(_fixtures={"templates": lambda ch, cid, sid: [_tpl(ORPHAN, "Not deleted")]})
    assert out["blocked_reason"] == "no_sibling_use_unpublish"
    assert "unpublish_channel_listing" in out["error"]


def test_variation_child_with_live_siblings_is_blocked():
    def _open(ch, cid, sid):
        return [] if sid == SID else [_tpl(ORPHAN, "Not deleted"), _tpl(LIVE, "Listed")]

    with patch.object(server, "_resolve_glt_target", _target), \
         patch.object(server, "call_linnworks", _item), \
         patch.object(server, "_open_item_templates", _open), \
         patch.object(server, "_fetch_channel_skus_for_ids",
                      lambda ids: {SID.lower(): [_row()], "sib-sid": [_row()]}), \
         patch.object(server, "_resolve_variation",
                      lambda sku, sid: {"role": "child", "parent_sku": "PARENT-A",
                                        "parent_stock_item_id": PSID, "group_name": "G",
                                        "siblings": [{"sku": "SIB-1",
                                                      "stock_item_id": "sib-sid"}]}):
        out = server.delete_dangling_glt_template("SKU-A", ORPHAN)

    assert out["blocked_reason"] == "variation_child_live_siblings"
    assert out["live_siblings"] == ["SIB-1"]


def test_override_permits_an_unproven_delete_and_warns():
    out = _run(template_id=LIVE, allow_unproven_delete=True)
    assert out.get("blocked_reason") is None
    assert "listing-x" in out["warning"]
    assert "NOT proven dead" in out["warning"]


def test_live_run_stages_a_manifest_before_it_will_fire():
    out = _run(dry_run=False)
    assert out["staged"] is True
    assert out["success"] is False
    assert out["confirmed_count"] is None
