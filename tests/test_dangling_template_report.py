"""
Tests for the dangling-GLT-template reporter (issue #115).

Proof that a listing is dead is `Info.Status == "Not deleted"` and nothing
else — see docs/superpowers/specs/2026-09-25-dangling-glt-template-handling-design.md.

That status is Linnworks recording that it tried to remove a listing which was
already gone (docs/dangling-templates-swh-shopify.md). It is a PRECISE signal,
not a complete one: 141/141 and 42/42 "Not deleted" templates were dangling in
two independent sweeps, but ~4 of the census's 145 dangling templates carry some
other status, and "Listed" templates have never been swept.

So there is deliberately no `healthy` verdict. The vocabulary is
`dangling_proven` vs `not_proven_dangling`, and the second one means
"unproven", never "fine".
"""
from unittest.mock import patch

import pytest

import server


@pytest.mark.parametrize("status", ["Not deleted"])
def test_exact_status_is_the_only_proof_of_death(status):
    assert server._dangling_verdict(status) == "dangling_proven"


@pytest.mark.parametrize("status", [
    "Listed",
    "Errors while updating",
    "not deleted",      # case variance is NOT accepted (Review Focus 3)
    "",
    None,
    123,
])
def test_everything_else_is_unproven_not_healthy(status):
    assert server._dangling_verdict(status) == "not_proven_dangling"


def test_surrounding_whitespace_is_tolerated_but_case_is_not():
    # Whitespace is a transport artefact; case is a different string from the
    # API and must not be guessed at. Decided behaviour, not an accident of ==.
    assert server._dangling_verdict("  Not deleted  ") == "dangling_proven"
    assert server._dangling_verdict("NOT DELETED") == "not_proven_dangling"


def test_template_row_unwraps_the_type_value_envelope():
    t = {
        "Id": 38539,
        "ConfiguratorId": 81,
        "Info": {
            "ActiveListingId": {"Type": "Value", "Value": "9277472506102"},
            "Status": {"Type": "Value", "Value": "Not deleted"},
        },
    }
    row = server._template_row(t)
    assert row == {
        "template_id": 38539,
        "configurator_id": 81,
        "active_listing_id": "9277472506102",
        "status": "Not deleted",
        "verdict": "dangling_proven",
    }


def test_template_row_handles_missing_info_key():
    # Defensive branch: `Info` absent entirely must not raise, and every
    # unwrapped field must come back None rather than crashing on `.get`.
    t = {"Id": 1, "ConfiguratorId": 2}
    row = server._template_row(t)
    assert row == {
        "template_id": 1,
        "configurator_id": 2,
        "active_listing_id": None,
        "status": None,
        "verdict": "not_proven_dangling",
    }


def test_template_row_handles_non_dict_info():
    # Defensive branch: `Info` present but not a dict (e.g. malformed/partial
    # API response) must degrade the same way as it being absent.
    t = {"Id": 1, "ConfiguratorId": 2, "Info": "not-a-dict"}
    row = server._template_row(t)
    assert row == {
        "template_id": 1,
        "configurator_id": 2,
        "active_listing_id": None,
        "status": None,
        "verdict": "not_proven_dangling",
    }


# ---------- _open_item_templates ----------

_FAKE_CHANNEL = {"channel_type": "GenericStore", "channel_name": "SHOPIFY"}


def test_open_item_templates_ignores_total_entries_trap():
    # The single highest-risk rule in this task: TotalEntries echoes the input
    # count and will claim a template exists where none does. A response that
    # carries both TemplatesInfo and a misleading, non-matching TotalEntries
    # must yield ONLY TemplatesInfo.
    fake_response = {
        "TemplatesInfo": [{"Id": 1}, {"Id": 2}],
        "TotalEntries": 99,
    }
    with patch.object(server, "call_linnworks", return_value=fake_response):
        result = server._open_item_templates(_FAKE_CHANNEL, 42, "abc-123")
    assert result == [{"Id": 1}, {"Id": 2}]


@pytest.mark.parametrize("fake_response", [
    None,
    [],
    "",
    0,
    False,
    ["not", "a", "dict"],
    {},                          # dict but no TemplatesInfo key at all
    {"TemplatesInfo": None},     # key present but null
])
def test_open_item_templates_degrades_to_empty_list_on_bad_response(fake_response):
    with patch.object(server, "call_linnworks", return_value=fake_response):
        result = server._open_item_templates(_FAKE_CHANNEL, 42, "abc-123")
    assert result == []


def test_open_item_templates_sends_correct_request_payload():
    with patch.object(
        server, "call_linnworks", return_value={"TemplatesInfo": []}
    ) as mock_call:
        server._open_item_templates(_FAKE_CHANNEL, 42, "abc-123")

    assert mock_call.call_count == 1
    method_path, payload = mock_call.call_args[0]
    assert method_path == "GenericListings/OpenTemplatesByInventory"

    request = payload["request"]
    assert request["ChannelType"] == "GenericStore"
    assert request["ChannelName"] == "SHOPIFY"

    params = request["Parameters"]
    assert params["ChannelId"] == 42
    assert params["InventoryItemIds"] == ["abc-123"]


from unittest.mock import patch

SID = "aaaaaaaa-0000-0000-0000-000000000001"
PSID = "aaaaaaaa-0000-0000-0000-00000000000p"
STORE = "SWH Shopify"
CHANNEL_ID = 18


def _target(*a, **k):
    return {"ok": True, "channel_id": CHANNEL_ID, "resolution": "exact",
            "available_sub_sources": [STORE],
            "channel": {"channel_type": "Shopify", "channel_name": "SHOPIFY",
                        "source": "SHOPIFY"}}


def _tpl(tid, status="Listed", listing="listing-x"):
    return {"Id": tid, "ConfiguratorId": 81,
            "Info": {"ActiveListingId": {"Value": listing},
                     "Status": {"Value": status}}}


def _row(ref="9276972859638:1:1", source="SHOPIFY", sub=STORE):
    return {"Source": source, "SubSource": sub, "SKU": "SKU-A",
            "ChannelReferenceId": ref, "UpdateStatus": "Confirmed",
            "ListedQuantity": 3, "MaxListedQuantity": 0, "LastUpdate": None,
            "IgnoreSync": False, "IsMultiLocation": False,
            "ChannelSKURowId": "row-1"}


def test_reports_dangling_and_unproven_siblings_with_rows():
    with patch.object(server, "_resolve_glt_target", _target), \
         patch.object(server, "call_linnworks", lambda ep, p=None: {"StockItemId": SID, "ItemTitle": "T"}), \
         patch.object(server, "_open_item_templates",
                      lambda ch, cid, sid: [_tpl(38539, "Not deleted"), _tpl(54649, "Listed")]), \
         patch.object(server, "_fetch_channel_skus_for_ids",
                      lambda ids: {SID.lower(): [_row()]}):
        out = server.find_dangling_glt_templates(["SKU-A"])

    assert out["dangling_proven_count"] == 1
    assert out["not_proven_dangling_count"] == 1
    assert out["complete"] is True
    item = out["items"][0]
    assert {t["template_id"]: t["verdict"] for t in item["templates"]} == {
        38539: "dangling_proven", 54649: "not_proven_dangling"}
    assert item["channel_sku_rows"]["count"] == 1
    assert item["channel_sku_rows"]["channel_reference_ids"] == ["9276972859638:1:1"]
    assert "healthy" not in repr(out).lower()


def test_rows_from_another_store_are_not_counted():
    with patch.object(server, "_resolve_glt_target", _target), \
         patch.object(server, "call_linnworks", lambda ep, p=None: {"StockItemId": SID, "ItemTitle": "T"}), \
         patch.object(server, "_open_item_templates", lambda ch, cid, sid: [_tpl(1, "Not deleted")]), \
         patch.object(server, "_fetch_channel_skus_for_ids",
                      lambda ids: {SID.lower(): [_row(sub="Other Store"),
                                                 _row(source="EBAY", sub="EBAY0")]}):
        out = server.find_dangling_glt_templates(["SKU-A"])
    assert out["items"][0]["channel_sku_rows"]["count"] == 0


def test_variation_child_reports_the_parents_template_not_no_templates():
    """A Shopify variation child holds rows but no template of its own (#26).
    Reporting "no templates" would read as a clean bill of health for a SKU
    that was never examined."""
    def _open(ch, cid, sid):
        return [] if sid == SID else [_tpl(77000, "Not deleted")]

    with patch.object(server, "_resolve_glt_target", _target), \
         patch.object(server, "call_linnworks", lambda ep, p=None: {"StockItemId": SID, "ItemTitle": "T"}), \
         patch.object(server, "_open_item_templates", _open), \
         patch.object(server, "_fetch_channel_skus_for_ids", lambda ids: {SID.lower(): [_row()]}), \
         patch.object(server, "_resolve_variation",
                      lambda sku, sid: {"role": "child", "parent_sku": "PARENT-A",
                                        "parent_stock_item_id": PSID,
                                        "group_name": "G", "siblings": []}):
        out = server.find_dangling_glt_templates(["SKU-A"])

    item = out["items"][0]
    assert item["template_source"] == "variation_parent"
    assert item["parent_sku"] == "PARENT-A"
    assert item["templates"][0]["template_id"] == 77000
    assert item["no_templates"] is False


def test_item_with_no_templates_says_so_explicitly():
    """Review Focus 1 — never listed via GLT, and not a variation child."""
    with patch.object(server, "_resolve_glt_target", _target), \
         patch.object(server, "call_linnworks", lambda ep, p=None: {"StockItemId": SID, "ItemTitle": "T"}), \
         patch.object(server, "_open_item_templates", lambda ch, cid, sid: []), \
         patch.object(server, "_fetch_channel_skus_for_ids", lambda ids: {SID.lower(): []}), \
         patch.object(server, "_resolve_variation", lambda sku, sid: {"role": "standalone"}):
        out = server.find_dangling_glt_templates(["SKU-A"])

    item = out["items"][0]
    assert item["no_templates"] is True
    assert item["templates"] == []
    assert item["template_source"] == "none"
    assert "no GLT template" in item["note"]


def test_rate_limit_is_never_reported_as_dangling():
    def _boom(ep, p=None):
        raise server.RateLimitError("429 throttled")

    with patch.object(server, "_resolve_glt_target", _target), \
         patch.object(server, "call_linnworks", _boom):
        out = server.find_dangling_glt_templates(["SKU-A"])

    assert out["rate_limited"] and out["rate_limited"][0]["sku"] == "SKU-A"
    assert out["items"] == []
    assert out["dangling_proven_count"] == 0
    assert out["complete"] is False


def test_unknown_sku_lands_in_unresolved():
    def _missing(ep, p=None):
        raise RuntimeError("no such item")

    with patch.object(server, "_resolve_glt_target", _target), \
         patch.object(server, "call_linnworks", _missing):
        out = server.find_dangling_glt_templates(["NOPE"])

    assert out["unresolved"][0]["blocked_reason"] == "not_found"
    assert out["complete"] is False


def test_rate_limit_in_variation_fallback_is_bucketed_not_raised():
    """Fix round 1, finding 1. `_resolve_variation` can itself raise
    RateLimitError (it calls call_linnworks_get up to several times); the
    variation-child fallback's `except RuntimeError` does NOT catch it,
    since RateLimitError does not subclass RuntimeError in this codebase.
    Left uncaught, a 429 here would propagate out of the whole batch call
    instead of bucketing just this SKU — the same failure mode the other
    three call sites in this function already guard against."""
    def _boom(sku, sid):
        raise server.RateLimitError("429 throttled")

    with patch.object(server, "_resolve_glt_target", _target), \
         patch.object(server, "call_linnworks", lambda ep, p=None: {"StockItemId": SID, "ItemTitle": "T"}), \
         patch.object(server, "_open_item_templates", lambda ch, cid, sid: []), \
         patch.object(server, "_fetch_channel_skus_for_ids", lambda ids: {SID.lower(): []}), \
         patch.object(server, "_resolve_variation", _boom):
        out = server.find_dangling_glt_templates(["SKU-A"])

    assert out["rate_limited"] and out["rate_limited"][0]["sku"] == "SKU-A"
    assert out["items"] == []
    assert out["complete"] is False


def test_runtime_error_in_variation_fallback_is_unresolved_not_swallowed():
    """RuntimeError twin of test_rate_limit_in_variation_fallback_is_bucketed_not_raised.
    The old code swallowed a plain RuntimeError from `_resolve_variation` itself
    (`except RuntimeError: rel = {}`), which let the SKU fall through as if it
    were simply a standalone item with no templates — reported as
    `template_source: "none"` / `no_templates: True` / `dangling_proven: []`
    with `complete` still True. That is exactly the "clean bill of health for
    a SKU that was never examined" the surrounding comment warns against: we
    never actually learned whether this SKU is a variation child whose
    template hangs off an unexamined parent. It must land in `unresolved`
    with `channel_read_failed` instead, and `complete` must go False."""
    def _boom(sku, sid):
        raise RuntimeError("500 resolving variation relationship")

    with patch.object(server, "_resolve_glt_target", _target), \
         patch.object(server, "call_linnworks", lambda ep, p=None: {"StockItemId": SID, "ItemTitle": "T"}), \
         patch.object(server, "_open_item_templates", lambda ch, cid, sid: []), \
         patch.object(server, "_fetch_channel_skus_for_ids", lambda ids: {SID.lower(): []}), \
         patch.object(server, "_resolve_variation", _boom):
        out = server.find_dangling_glt_templates(["SKU-A"])

    assert out["items"] == []
    assert out["unresolved"] and out["unresolved"][0]["sku"] == "SKU-A"
    assert out["unresolved"][0]["blocked_reason"] == "channel_read_failed"
    assert out["complete"] is False


def test_runtime_error_reading_the_parents_templates_is_unresolved_not_swallowed():
    """Final review, Important finding 1. The RuntimeError twin of
    test_rate_limit_in_variation_fallback_is_bucketed_not_raised above: once a
    SKU is identified as a variation child, the read of the PARENT's
    templates (`_open_item_templates` on `parent_stock_item_id`) can itself
    fail with a plain RuntimeError. The old code swallowed it
    (`except RuntimeError: raw_templates = []`), which then reported the SKU
    as `template_source: "none"` / `no_templates: True` with `complete` still
    True — exactly the "clean bill of health for a SKU that was never
    examined" the comment two lines above explicitly warns against, and a
    silent violation of the "complete must be False when any read failed"
    rule. The child's own template read (first `_open_item_templates` call)
    still legitimately returns [] here — that is what makes it look like a
    variation child in the first place — only the PARENT read fails."""
    def _open(ch, cid, sid):
        if sid == SID:
            return []
        raise RuntimeError("500 on parent template read")

    with patch.object(server, "_resolve_glt_target", _target), \
         patch.object(server, "call_linnworks", lambda ep, p=None: {"StockItemId": SID, "ItemTitle": "T"}), \
         patch.object(server, "_open_item_templates", _open), \
         patch.object(server, "_fetch_channel_skus_for_ids", lambda ids: {SID.lower(): [_row()]}), \
         patch.object(server, "_resolve_variation",
                      lambda sku, sid: {"role": "child", "parent_sku": "PARENT-A",
                                        "parent_stock_item_id": PSID,
                                        "group_name": "G", "siblings": []}):
        out = server.find_dangling_glt_templates(["SKU-A"])

    assert out["items"] == []
    assert out["unresolved"] and out["unresolved"][0]["sku"] == "SKU-A"
    assert out["unresolved"][0]["blocked_reason"] == "channel_read_failed"
    assert out["complete"] is False
