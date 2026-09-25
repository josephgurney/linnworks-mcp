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

Round 1 review additions (kept in the same file, not a second one):
  - Gate 1 no longer has a `None == None` hole (a non-numeric template_id
    refuses outright rather than becoming `wanted = None`, which could match a
    malformed row whose `Id` is also `None`).
  - The variation safety gate (variation_child_live_siblings) now runs BEFORE
    the scope gate (no_sibling_use_unpublish), because the standard Shopify
    variation shape (parent holds ONE template for every member) makes
    `siblings` empty and would otherwise misfire the scope gate first.
  - The variation gate's liveness check no longer fails OPEN: a sibling with
    no stock_item_id, or one missing from the batch-fetch response, is
    UNKNOWN and refuses, rather than being read as "not live".
  - Every test now asserts, via a recording Mock, that no
    GenericListings/ProcessTemplates call was ever made — this file is what
    Task 4 (which actually fires the delete) will be reviewed against.
"""
from unittest.mock import Mock, patch

import pytest

import server

SID = "aaaaaaaa-0000-0000-0000-000000000001"
PSID = "aaaaaaaa-0000-0000-0000-00000000000p"
STORE = "SWH Shopify"
CHANNEL_ID = 18
ORPHAN = 38539
LIVE = 54649

_PROCESS_TEMPLATES = "GenericListings/ProcessTemplates"


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


def _assert_never_wrote(mock_cl):
    """Used by every GATE-REFUSAL test in this file (not the Task 4 live-run
    tests further down, which assert the opposite). A gate that refuses must
    refuse before any write — this is now a live guard against a gate
    regressing to fire ProcessTemplates ahead of, or despite, its own
    refusal, not (as it read pre-Task-4) a statement that the tool never
    writes at all."""
    sent = [c.args[0] for c in mock_cl.call_args_list if c.args]
    assert _PROCESS_TEMPLATES not in sent, (
        f"delete_dangling_glt_template must never call {_PROCESS_TEMPLATES} in "
        f"Task 3 — calls were: {sent}")


def _run(**kw):
    """Call the tool with the happy-path fixtures, overriding as needed.

    Uses a recording Mock for call_linnworks so every call site — this
    includes every gate-refusal test — can assert no write was ever sent.
    """
    opts = {"templates": _both_templates, "rows": lambda ids: {SID.lower(): [_row()]}}
    opts.update(kw.pop("_fixtures", {}))
    mock_cl = Mock(side_effect=_item)
    with patch.object(server, "_resolve_glt_target", _target), \
         patch.object(server, "call_linnworks", mock_cl), \
         patch.object(server, "_open_item_templates", opts["templates"]), \
         patch.object(server, "_fetch_channel_skus_for_ids", opts["rows"]):
        out = server.delete_dangling_glt_template(
            kw.pop("sku", "SKU-A"), kw.pop("template_id", ORPHAN), **kw)
    _assert_never_wrote(mock_cl)
    return out


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


def test_non_numeric_template_id_is_refused_outright_not_coerced_to_none():
    """Round 1, CRITICAL — a non-numeric template_id used to fall through as
    `wanted = None`. `_template_row` reports `None` for a row whose `Id` is
    missing, so `None == None` could make a malformed row the deletion target
    while every real template scored as a harmless sibling. It must refuse
    before ever comparing against a row."""
    out = _run(template_id="not-a-number")
    assert out["blocked_reason"] == "template_not_on_item"
    assert out["success"] is False


def test_a_template_row_with_a_missing_id_can_never_be_selected():
    """Round 1, CRITICAL, the other half — even if a row with `Id: None` is
    present, a bad template_id must never match it."""
    def _templates_with_a_malformed_row(ch, cid, sid):
        malformed = {"Id": None, "ConfiguratorId": 1,
                     "Info": {"ActiveListingId": {"Value": "x"}, "Status": {"Value": "Listed"}}}
        return [malformed, _tpl(LIVE, "Listed")]

    out = _run(template_id="garbage",
               _fixtures={"templates": _templates_with_a_malformed_row})
    assert out["blocked_reason"] == "template_not_on_item"
    assert out["success"] is False


def test_template_id_as_string_matches_a_row_whose_id_is_also_a_string():
    """Round 1, CRITICAL, the mirror case named in the finding — comparing via
    `int(r["template_id"]) == wanted` rather than `==` means a template row
    whose `Id` arrives as a string still matches, instead of refusing every
    template on the item."""
    def _templates_with_string_ids(ch, cid, sid):
        return [{"Id": str(ORPHAN), "ConfiguratorId": 81,
                 "Info": {"ActiveListingId": {"Value": "listing-x"},
                          "Status": {"Value": "Not deleted"}}},
                _tpl(LIVE, "Listed")]

    out = _run(_fixtures={"templates": _templates_with_string_ids})
    assert out.get("blocked_reason") != "template_not_on_item"
    assert out["plan"]["template_id"] == ORPHAN


@pytest.mark.parametrize("status", ["Listed", "Errors while updating", "", None, "not deleted"])
def test_only_not_deleted_may_be_deleted(status):
    """`"not deleted"` (lowercase) is deliberately in this list — proof of
    death is `Info.Status == "Not deleted"` exactly; case is not tolerated."""
    out = _run(_fixtures={"templates": lambda ch, cid, sid: [
        _tpl(ORPHAN, status), _tpl(LIVE, "Listed")]})
    assert out["blocked_reason"] == "dangling_not_proven"


def test_whitespace_around_not_deleted_is_tolerated():
    """The mirror of the case-sensitivity test — surrounding whitespace IS
    tolerated (a transport artefact), per `_dangling_verdict`."""
    out = _run(_fixtures={"templates": lambda ch, cid, sid: [
        _tpl(ORPHAN, " Not deleted "), _tpl(LIVE, "Listed")]})
    assert out.get("blocked_reason") != "dangling_not_proven"
    assert out["success"] is True


def test_single_template_item_is_redirected_to_unpublish():
    out = _run(_fixtures={"templates": lambda ch, cid, sid: [_tpl(ORPHAN, "Not deleted")]})
    assert out["blocked_reason"] == "no_sibling_use_unpublish"
    assert "unpublish_channel_listing" in out["error"]


def test_variation_child_with_live_siblings_is_blocked():
    def _open(ch, cid, sid):
        return [] if sid == SID else [_tpl(ORPHAN, "Not deleted"), _tpl(LIVE, "Listed")]

    mock_cl = Mock(side_effect=_item)
    with patch.object(server, "_resolve_glt_target", _target), \
         patch.object(server, "call_linnworks", mock_cl), \
         patch.object(server, "_open_item_templates", _open), \
         patch.object(server, "_fetch_channel_skus_for_ids",
                      lambda ids: {SID.lower(): [_row()], "sib-sid": [_row()]}), \
         patch.object(server, "_resolve_variation",
                      lambda sku, sid: {"role": "child", "parent_sku": "PARENT-A",
                                        "parent_stock_item_id": PSID, "group_name": "G",
                                        "siblings": [{"sku": "SIB-1",
                                                      "stock_item_id": "sib-sid"}]}):
        out = server.delete_dangling_glt_template("SKU-A", ORPHAN)

    _assert_never_wrote(mock_cl)
    assert out["blocked_reason"] == "variation_child_live_siblings"
    assert out["live_siblings"] == ["SIB-1"]


def test_variation_child_whose_parent_has_only_one_template_is_still_gated_on_liveness():
    """Round 1, Important finding 2 — in the standard Shopify variation shape
    the parent holds exactly ONE template for every member, so `siblings`
    (template siblings, not variation siblings) is empty. Before the fix this
    fired the misleading `no_sibling_use_unpublish`, which sends the caller to
    a tool that refuses this exact shape with `variation_child_live_siblings`
    — a dead end with a confident label on it. The variation safety gate must
    fire first."""
    def _open(ch, cid, sid):
        return [] if sid == SID else [_tpl(ORPHAN, "Not deleted")]

    mock_cl = Mock(side_effect=_item)
    with patch.object(server, "_resolve_glt_target", _target), \
         patch.object(server, "call_linnworks", mock_cl), \
         patch.object(server, "_open_item_templates", _open), \
         patch.object(server, "_fetch_channel_skus_for_ids",
                      lambda ids: {SID.lower(): [_row()], "sib-sid": [_row()]}), \
         patch.object(server, "_resolve_variation",
                      lambda sku, sid: {"role": "child", "parent_sku": "PARENT-A",
                                        "parent_stock_item_id": PSID, "group_name": "G",
                                        "siblings": [{"sku": "SIB-1",
                                                      "stock_item_id": "sib-sid"}]}):
        out = server.delete_dangling_glt_template("SKU-A", ORPHAN)

    _assert_never_wrote(mock_cl)
    assert out["blocked_reason"] == "variation_child_live_siblings"
    assert out["blocked_reason"] != "no_sibling_use_unpublish"


def test_variation_sibling_missing_stock_item_id_is_unknown_not_live():
    """Round 1, Important finding 3, path 1 — a group member with no
    stock_item_id is filtered out of the batch-fetch request entirely. It
    must not be silently treated as 'not live'. The PARENT is given its own
    confirmed-not-live row (empty list, a present key) so this test isolates
    the sibling-with-no-id path rather than also tripping on the parent."""
    def _open(ch, cid, sid):
        return [] if sid == SID else [_tpl(ORPHAN, "Not deleted"), _tpl(LIVE, "Listed")]

    mock_cl = Mock(side_effect=_item)
    with patch.object(server, "_resolve_glt_target", _target), \
         patch.object(server, "call_linnworks", mock_cl), \
         patch.object(server, "_open_item_templates", _open), \
         patch.object(server, "_fetch_channel_skus_for_ids",
                      lambda ids: {SID.lower(): [_row()], PSID.lower(): []}), \
         patch.object(server, "_resolve_variation",
                      lambda sku, sid: {"role": "child", "parent_sku": "PARENT-A",
                                        "parent_stock_item_id": PSID, "group_name": "G",
                                        "siblings": [{"sku": "SIB-1", "stock_item_id": None}]}):
        out = server.delete_dangling_glt_template("SKU-A", ORPHAN)

    _assert_never_wrote(mock_cl)
    assert out["blocked_reason"] == "variation_child_live_siblings"
    assert out["unknown_siblings"] == ["SIB-1"]
    assert out["live_siblings"] == []


def test_variation_sibling_missing_from_batch_response_is_unknown_not_live():
    """Round 1, Important finding 3, path 2 — `_fetch_channel_skus_for_ids`
    only populates entries the API actually returned. A member with a real
    stock_item_id that the batch call simply omitted must also read as
    UNKNOWN, not as 'confirmed not live'. The PARENT is given its own
    confirmed-not-live row so this test isolates the omitted-sibling path."""
    def _open(ch, cid, sid):
        return [] if sid == SID else [_tpl(ORPHAN, "Not deleted"), _tpl(LIVE, "Listed")]

    mock_cl = Mock(side_effect=_item)
    with patch.object(server, "_resolve_glt_target", _target), \
         patch.object(server, "call_linnworks", mock_cl), \
         patch.object(server, "_open_item_templates", _open), \
         patch.object(server, "_fetch_channel_skus_for_ids",
                      lambda ids: {SID.lower(): [_row()], PSID.lower(): []}),  \
         patch.object(server, "_resolve_variation",
                      lambda sku, sid: {"role": "child", "parent_sku": "PARENT-A",
                                        "parent_stock_item_id": PSID, "group_name": "G",
                                        "siblings": [{"sku": "SIB-1",
                                                      "stock_item_id": "sib-sid"}]}):
        out = server.delete_dangling_glt_template("SKU-A", ORPHAN)

    _assert_never_wrote(mock_cl)
    assert out["blocked_reason"] == "variation_child_live_siblings"
    assert out["unknown_siblings"] == ["SIB-1"]
    assert out["live_siblings"] == []


def test_override_permits_an_unproven_delete_and_warns():
    """The two templates must carry DISTINCT listing ids — `_tpl`'s default
    gives both the same one, so a warning naming either would pass. This test
    exists specifically to check the warning names the right listing."""
    def _templates(ch, cid, sid):
        return [_tpl(ORPHAN, "Not deleted", listing="listing-orphan"),
                _tpl(LIVE, "Listed", listing="listing-live")]

    out = _run(template_id=LIVE, allow_unproven_delete=True,
               _fixtures={"templates": _templates})
    assert out.get("blocked_reason") is None
    assert out["plan"]["template_id"] == LIVE
    assert "listing-live" in out["warning"]
    assert "listing-orphan" not in out["warning"]
    assert "NOT proven dead" in out["warning"]


def test_override_does_not_bypass_gate_1_template_not_on_item():
    out = _run(template_id=99999, allow_unproven_delete=True)
    assert out["blocked_reason"] == "template_not_on_item"


def test_override_does_not_bypass_the_scope_gate():
    out = _run(_fixtures={"templates": lambda ch, cid, sid: [_tpl(ORPHAN, "Not deleted")]},
               allow_unproven_delete=True)
    assert out["blocked_reason"] == "no_sibling_use_unpublish"


def test_override_does_not_bypass_the_variation_safety_gate():
    def _open(ch, cid, sid):
        return [] if sid == SID else [_tpl(ORPHAN, "Listed"), _tpl(LIVE, "Listed")]

    mock_cl = Mock(side_effect=_item)
    with patch.object(server, "_resolve_glt_target", _target), \
         patch.object(server, "call_linnworks", mock_cl), \
         patch.object(server, "_open_item_templates", _open), \
         patch.object(server, "_fetch_channel_skus_for_ids",
                      lambda ids: {SID.lower(): [_row()], "sib-sid": [_row()]}), \
         patch.object(server, "_resolve_variation",
                      lambda sku, sid: {"role": "child", "parent_sku": "PARENT-A",
                                        "parent_stock_item_id": PSID, "group_name": "G",
                                        "siblings": [{"sku": "SIB-1",
                                                      "stock_item_id": "sib-sid"}]}):
        out = server.delete_dangling_glt_template(
            "SKU-A", ORPHAN, allow_unproven_delete=True)

    _assert_never_wrote(mock_cl)
    assert out["blocked_reason"] == "variation_child_live_siblings"


def test_live_run_stages_a_manifest_before_it_will_fire():
    out = _run(dry_run=False)
    assert out["staged"] is True
    assert out["success"] is False
    assert out["confirmed_count"] is None


def test_confirmed_count_matching_the_plan_passes_the_guard():
    """Once the guard passes, execution now proceeds for real (Task 4) — this
    exercises the live path, so it needs phase-aware fixtures. Task 3's
    static `_run` fixtures always show the target template as still present
    after the (fake) delete, which the Task 4 read-back would correctly
    report as `delete_refused_by_linnworks`, not `True`. `_live` (below)
    simulates the delete actually landing."""
    res, _ = _live([_tpl(LIVE, "Listed")], [_row()])
    assert "staged" not in res
    assert res["success"] is True
    assert res["dry_run"] is False


def test_confirmed_count_mismatch_is_refused():
    out = _run(dry_run=False, confirmed_count=2)
    assert out["success"] is False
    assert out["staged"] is False
    assert out["confirmed_count"] == 2


# ---------------------------------------------------------------------------
# Task 4 — live execution, evidence capture and outcome classification
# ---------------------------------------------------------------------------
#
# `_live` adapts the design brief's helper to this file's Mock-based
# convention: `call_linnworks` is a `Mock(side_effect=...)` throughout so
# every test — including the ones below that assert POSITIVELY on a
# ProcessTemplates call — reads it the same way `_assert_never_wrote` does
# for the gate-refusal tests above, via `call_args_list`.

def _process_templates_payloads(mock_cl):
    return [c.args[1] for c in mock_cl.call_args_list
            if c.args and c.args[0] == _PROCESS_TEMPLATES]


def _live(after_templates, after_rows, delete_raises=None, readback_raises=None,
          template_id=ORPHAN, before_templates=None, **kwargs):
    """Drive one live run. Returns (result, mock_cl); use
    `_process_templates_payloads(mock_cl)` to inspect what was sent.

    `_rows` is keyed off its `ids` argument (Round 1 review, T1) rather than
    always answering for SID regardless of what was asked — an
    implementation that reverted to snapshotting only the named SKU's own
    `sid` (deferred item A's exact regression) would otherwise still pass
    every one of these tests, since a fixture that ignores `ids` cannot tell
    the difference between being asked for one id or several.
    """
    state = {"phase": "before"}
    before_fn = before_templates or _both_templates

    def _open(ch, cid, sid):
        if state["phase"] == "before":
            return before_fn(ch, cid, sid)
        if readback_raises:
            raise readback_raises
        return after_templates

    def _rows(ids):
        full = {SID.lower(): ([_row()] if state["phase"] == "before" else after_rows)}
        return {i.lower(): full[i.lower()] for i in ids if i.lower() in full}

    def _call(ep, p=None):
        if ep == _PROCESS_TEMPLATES:
            if delete_raises:
                state["phase"] = "after"
                raise delete_raises
            state["phase"] = "after"
            return {}
        return _item(ep, p)

    mock_cl = Mock(side_effect=_call)
    with patch.object(server, "_resolve_glt_target", _target), \
         patch.object(server, "call_linnworks", mock_cl), \
         patch.object(server, "_open_item_templates", _open), \
         patch.object(server, "_fetch_channel_skus_for_ids", _rows):
        res = server.delete_dangling_glt_template(
            "SKU-A", template_id, dry_run=False, confirmed_count=1, **kwargs)
    return res, mock_cl


def test_the_sibling_template_id_never_appears_in_a_write_payload():
    """The single most important test here. If the sibling id reaches
    ProcessTemplates, a live listing ends."""
    res, mock_cl = _live([_tpl(LIVE, "Listed")], [_row()])
    payloads = _process_templates_payloads(mock_cl)
    assert len(payloads) == 1
    requests = payloads[0]["request"]["TemplateRequests"]
    assert requests == [{"TemplateId": ORPHAN, "Action": "Delete"}]
    assert str(LIVE) not in repr(payloads)


def test_ideal_outcome_target_gone_siblings_present_rows_intact():
    res, _ = _live([_tpl(LIVE, "Listed")], [_row()])
    assert res["outcome"] == "orphan_removed_siblings_intact"
    assert res["settles"] == "A and B"
    assert res["after"]["channel_sku_row_count"] == 1


def test_rows_lost_is_a_failure_with_a_loud_warning():
    res, _ = _live([_tpl(LIVE, "Listed")], [])
    assert res["outcome"] == "orphan_removed_sibling_mapping_lost"
    assert res["success"] is False
    assert res["settles"] == "A and B"
    assert "lost its Linnworks mapping" in res["warning"]


def test_target_surviving_is_delete_refused_and_settles_only_A():
    res, _ = _live([_tpl(ORPHAN, "Not deleted"), _tpl(LIVE, "Listed")], [_row()])
    assert res["outcome"] == "delete_refused_by_linnworks"
    assert res["settles"] == "A only"


def test_all_siblings_dangling_does_not_claim_a_live_sibling_survived():
    """Review Focus 4 — gate 3 passes on template count, but a dangling
    sibling is not a live listing, so nothing may imply one was protected."""
    state = {"phase": "before"}

    def _open(ch, cid, sid):
        if state["phase"] == "before":
            return [_tpl(ORPHAN, "Not deleted"), _tpl(LIVE, "Not deleted")]
        return [_tpl(LIVE, "Not deleted")]

    def _call(ep, p=None):
        if ep == _PROCESS_TEMPLATES:
            state["phase"] = "after"
            return {}
        return _item(ep, p)

    mock_cl = Mock(side_effect=_call)
    with patch.object(server, "_resolve_glt_target", _target), \
         patch.object(server, "call_linnworks", mock_cl), \
         patch.object(server, "_open_item_templates", _open), \
         patch.object(server, "_fetch_channel_skus_for_ids",
                      lambda ids: {SID.lower(): [_row()]}):
        res = server.delete_dangling_glt_template(
            "SKU-A", ORPHAN, dry_run=False, confirmed_count=1)

    # Round 1 review, T2 — this test's name claims the run does not overclaim
    # a live sibling survived; it must also say what the run DID conclude,
    # or a change that broke the actual outcome here would pass silently.
    assert res["outcome"] == "orphan_removed_siblings_intact"
    assert res["success"] is True
    assert res["live_siblings_present"] is False
    assert "no sibling on this item was proven live" in res["note"]


def test_rate_limit_during_readback_says_the_write_already_fired():
    """Review Focus 5 — the delete has been sent. Reporting this as unchanged
    or failed would invite a re-run of a write that may have succeeded."""
    res, mock_cl = _live([], [], readback_raises=server.RateLimitError("429"))
    payloads = _process_templates_payloads(mock_cl)
    assert len(payloads) == 1
    assert res["outcome"] == "unconfirmed"
    assert res["settles"] == "neither"
    assert res["complete"] is False
    assert "already sent" in res["warning"].lower()


def test_a_failed_delete_call_is_reported_not_raised():
    res, _ = _live([], [], delete_raises=RuntimeError("500 boom"))
    assert res["outcome"] == "unconfirmed"
    assert "500 boom" in res["error"]


def test_a_confirmed_live_run_never_returns_success_true_having_done_nothing():
    """Deferred item B — a confirmed live run used to return `success: True`
    the moment the guard passed, without the write ever being sent (Task 3's
    stub). Every branch reachable once the guard clears must now either
    perform the write, or say plainly why it did not — never bare success."""
    res, mock_cl = _live([_tpl(LIVE, "Listed")], [_row()])
    assert len(_process_templates_payloads(mock_cl)) == 1
    assert res["success"] is True
    assert res["processed"] is True


def test_rows_kept_across_flags_a_loss_on_either_snapshotted_id():
    """Deferred item A, the core comparison logic in isolation. The named
    SKU's own id staying flat must not mask a wipe on the OTHER snapshotted
    id (the variation parent's), and vice versa — folding both into a single
    combined total would let either case hide behind the other."""
    assert server._rows_kept_across({SID: 1, PSID: 1}, {SID: 1, PSID: 1}) is True
    # The named SKU's own rows stay flat; the PARENT's table is wiped.
    assert server._rows_kept_across({SID: 1, PSID: 3}, {SID: 1, PSID: 0}) is False
    # The reverse — the parent is untouched but the named SKU's own rows are
    # wiped — must be caught too.
    assert server._rows_kept_across({SID: 3, PSID: 0}, {SID: 0, PSID: 0}) is False
    # Nothing to lose on an id that started at zero.
    assert server._rows_kept_across({SID: 0, PSID: 0}, {SID: 0, PSID: 5}) is True


def test_variation_path_snapshots_both_stock_item_ids_before_and_after():
    """Deferred item A, end to end. When the template is reached through the
    variation PARENT (`via_parent=True`, so `template_sid != sid`), the
    before/after snapshots must cover BOTH ids, not just the named SKU's own
    `sid` — otherwise a wipe on the parent's table is invisible to the
    read-back. This drives the real via_parent branch and checks the
    plumbing populates `by_stock_item_id` for both ids; the loss-detection
    arithmetic itself is covered directly by
    test_rows_kept_across_flags_a_loss_on_either_snapshotted_id (gate 3's
    own liveness check requires the parent to already show zero live rows on
    this store to let a delete through at all, which makes a genuine
    'parent had rows, parent lost them' case impossible to also route past
    gate 3 in the same run)."""
    state = {"phase": "before"}

    def _open(ch, cid, target_sid):
        # The child (SID) has no template of its own; it hangs off the
        # parent (PSID), which is the standard Shopify variation shape.
        if target_sid == SID:
            return []
        if state["phase"] == "before":
            return _both_templates(ch, cid, target_sid)
        return [_tpl(LIVE, "Listed")]

    def _rows(ids):
        # The child's own rows carry the real channel-SKU mapping and are
        # what gets lost. The parent has none on THIS store either before or
        # after (e.g. it is cross-listed elsewhere) — required for it to
        # read as "not live" and clear gate 3's variation safety check.
        if state["phase"] == "before":
            return {SID.lower(): [_row()], PSID.lower(): []}
        return {SID.lower(): [], PSID.lower(): []}

    def _call(ep, p=None):
        if ep == _PROCESS_TEMPLATES:
            state["phase"] = "after"
            return {}
        return _item(ep, p)

    mock_cl = Mock(side_effect=_call)
    with patch.object(server, "_resolve_glt_target", _target), \
         patch.object(server, "call_linnworks", mock_cl), \
         patch.object(server, "_open_item_templates", _open), \
         patch.object(server, "_fetch_channel_skus_for_ids", _rows), \
         patch.object(server, "_resolve_variation",
                      lambda sku, sid: {"role": "child", "parent_sku": "PARENT-A",
                                        "parent_stock_item_id": PSID, "group_name": "G",
                                        "siblings": []}):
        res = server.delete_dangling_glt_template(
            "SKU-A", ORPHAN, dry_run=False, confirmed_count=1)

    payloads = _process_templates_payloads(mock_cl)
    assert len(payloads) == 1
    assert res["template_stock_item_id"] == PSID
    assert res["before"]["by_stock_item_id"] == {SID: 1, PSID: 0}
    assert res["after"]["by_stock_item_id"] == {SID: 0, PSID: 0}
    assert res["outcome"] == "orphan_removed_sibling_mapping_lost"
    assert res["success"] is False


# ---------------------------------------------------------------------------
# Task 4, round 1 review fixes
# ---------------------------------------------------------------------------
# Write path traced clean by the reviewer; every fix below is in the
# outcome classifier — the evidence-producing half this task exists for.

def test_sibling_template_missing_from_the_readback_is_reported_not_masked():
    """C1 — CRITICAL. `siblings_present` was computed and then read only by
    `out["siblings_still_present"]` and the success f-string; no branch
    used it to decide anything, so the final `else` fired on
    `target_gone and rows_kept` alone. Here the read-back shows a DIFFERENT
    template in LIVE's place (not merely an empty response), so a bare
    `target_gone` check cannot catch it either — the classifier must check
    the SIBLING specifically."""
    res, _ = _live([_tpl(99999, "Listed")], [_row()])
    assert res["outcome"] == "orphan_removed_sibling_template_lost"
    assert res["success"] is False
    assert res["siblings_still_present"] == []
    assert str(LIVE) in res["warning"]


def test_empty_readback_after_a_successful_delete_is_not_the_ideal_outcome():
    """C1's 'empty TemplatesInfo' case, named explicitly in the finding —
    everything gone must not score as siblings_intact just because there is
    nothing left in the read-back to contradict it."""
    res, _ = _live([], [_row()])
    assert res["outcome"] == "orphan_removed_sibling_template_lost"
    assert res["success"] is False
    assert res["siblings_still_present"] == []


def test_live_run_with_string_template_ids_resolves_and_classifies_correctly():
    """C2 — CRITICAL. A raw string `Id` from Linnworks (`_template_row`
    passed it through untouched before this fix) must not silently fail to
    match `wanted` (always an int, from gate 1's own `int(template_id)`)
    anywhere downstream: neither in `siblings` (a raw `!=` compare would
    wrongly count the TARGET as its own sibling) nor in the outcome
    classifier's `target_gone` / sibling-presence membership checks (which
    would report a delete that LANDED as `delete_refused_by_linnworks`).
    Before this round, only a dry run ever exercised a string id here."""
    def _string_id_templates(ch, cid, sid):
        return [{"Id": str(ORPHAN), "ConfiguratorId": 81,
                 "Info": {"ActiveListingId": {"Value": "listing-x"},
                          "Status": {"Value": "Not deleted"}}},
                {"Id": str(LIVE), "ConfiguratorId": 81,
                 "Info": {"ActiveListingId": {"Value": "listing-x"},
                          "Status": {"Value": "Listed"}}}]

    after_string_id_live = [{"Id": str(LIVE), "ConfiguratorId": 81,
                             "Info": {"ActiveListingId": {"Value": "listing-x"},
                                      "Status": {"Value": "Listed"}}}]

    res, mock_cl = _live(after_string_id_live, [_row()],
                         before_templates=_string_id_templates)
    payloads = _process_templates_payloads(mock_cl)
    assert len(payloads) == 1
    assert payloads[0]["request"]["TemplateRequests"] == [
        {"TemplateId": ORPHAN, "Action": "Delete"}]
    assert res["outcome"] == "orphan_removed_siblings_intact"
    assert res["success"] is True
    assert res["siblings_still_present"] == [LIVE]


def test_item_wide_row_loss_on_another_channel_is_caught_not_masked_by_this_store():
    """C3 — CRITICAL. #36's documented failure mode is the item's WHOLE
    channel-SKU table emptying, not just this store's rows. A before-count
    filtered to this store alone can read 0 (exempt) while the item is
    live and mapped on a DIFFERENT channel that the delete then wipes — the
    silent oversell this tool exists to catch, just reached via another
    channel. This store's own filtered figures never move in this test,
    which is exactly what the old store-filtered comparison would have
    called 'kept'."""
    state = {"phase": "before"}

    def _open(ch, cid, sid):
        if state["phase"] == "before":
            return _both_templates(ch, cid, sid)
        return [_tpl(LIVE, "Listed")]

    def _rows(ids):
        other_channel_row = {**_row(), "SubSource": "EBAY0", "Source": "EBAY"}
        full = {SID.lower(): [other_channel_row] if state["phase"] == "before" else []}
        return {i.lower(): full[i.lower()] for i in ids if i.lower() in full}

    def _call(ep, p=None):
        if ep == _PROCESS_TEMPLATES:
            state["phase"] = "after"
            return {}
        return _item(ep, p)

    mock_cl = Mock(side_effect=_call)
    with patch.object(server, "_resolve_glt_target", _target), \
         patch.object(server, "call_linnworks", mock_cl), \
         patch.object(server, "_open_item_templates", _open), \
         patch.object(server, "_fetch_channel_skus_for_ids", _rows):
        res = server.delete_dangling_glt_template(
            "SKU-A", ORPHAN, dry_run=False, confirmed_count=1)

    assert res["outcome"] == "orphan_removed_sibling_mapping_lost"
    assert res["success"] is False
    assert res["before"]["channel_sku_row_count"] == 0
    assert res["after"]["channel_sku_row_count"] == 0
    assert res["before"]["by_stock_item_id"] == {SID: 1}
    assert res["after"]["by_stock_item_id"] == {SID: 0}


def test_before_count_of_zero_settles_A_only_not_A_and_B():
    """I1 — a run with no channel-SKU rows anywhere provides no evidence on
    question B (does the delete take the sibling's rows); it must not
    silently claim 'A and B'."""
    def _rows(ids):
        return {i.lower(): [] for i in ids}

    def _open(ch, cid, sid):
        return _both_templates(ch, cid, sid) if _open.phase == "before" else [_tpl(LIVE, "Listed")]
    _open.phase = "before"

    def _call(ep, p=None):
        if ep == _PROCESS_TEMPLATES:
            _open.phase = "after"
            return {}
        return _item(ep, p)

    mock_cl = Mock(side_effect=_call)
    with patch.object(server, "_resolve_glt_target", _target), \
         patch.object(server, "call_linnworks", mock_cl), \
         patch.object(server, "_open_item_templates", _open), \
         patch.object(server, "_fetch_channel_skus_for_ids", _rows):
        res = server.delete_dangling_glt_template(
            "SKU-A", ORPHAN, dry_run=False, confirmed_count=1)

    assert res["outcome"] == "orphan_removed_siblings_intact"
    assert res["settles"] == "A only"
    assert "UNTESTABLE" in res["note"]


def test_before_snapshot_omission_refuses_before_any_write():
    """I2 — an id OMITTED from the before-snapshot response is not the same
    as a CONFIRMED zero rows (which is exempt from the loss check); an
    omission must refuse rather than be silently read as nothing to lose."""
    def _rows(ids):
        return {}   # SID entirely absent from the response

    mock_cl = Mock(side_effect=_item)
    with patch.object(server, "_resolve_glt_target", _target), \
         patch.object(server, "call_linnworks", mock_cl), \
         patch.object(server, "_open_item_templates", _both_templates), \
         patch.object(server, "_fetch_channel_skus_for_ids", _rows):
        res = server.delete_dangling_glt_template(
            "SKU-A", ORPHAN, dry_run=False, confirmed_count=1)

    _assert_never_wrote(mock_cl)
    assert res["blocked_reason"] == "before_snapshot_incomplete"
    assert res["success"] is False
    assert SID in res["missing_stock_item_ids"]


def test_before_snapshot_omission_refuses_even_on_a_dry_run():
    """The same evidence gap makes a DRY RUN's plan untrustworthy too, not
    just a live run's write — the refusal is not conditioned on dry_run."""
    def _rows(ids):
        return {}

    mock_cl = Mock(side_effect=_item)
    with patch.object(server, "_resolve_glt_target", _target), \
         patch.object(server, "call_linnworks", mock_cl), \
         patch.object(server, "_open_item_templates", _both_templates), \
         patch.object(server, "_fetch_channel_skus_for_ids", _rows):
        res = server.delete_dangling_glt_template("SKU-A", ORPHAN)

    _assert_never_wrote(mock_cl)
    assert res["blocked_reason"] == "before_snapshot_incomplete"


def test_a_transport_error_during_the_delete_says_it_may_have_reached_linnworks():
    """I3 — a network timeout/connection failure (`requests` raises
    subclasses of `OSError`, previously uncaught here) must not escape as a
    raw traceback with nothing recorded. I4 — and it must NOT claim
    `processed: False`; the request may well have reached Linnworks and
    applied server-side."""
    res, _ = _live([], [], delete_raises=ConnectionError("timed out"))
    assert res["outcome"] == "unconfirmed"
    assert res["processed"] == "unknown"
    assert "MAY HAVE REACHED LINNWORKS" in res["warning"]
    assert "timed out" in res["error"]


def test_a_runtime_error_during_the_delete_is_processed_unknown_not_false():
    """I4 — `call_linnworks` raises RuntimeError only AFTER a response was
    received (non-2xx), so the request definitively reached Linnworks.
    `processed: False` on this path (the pre-round-1 behaviour) flatly
    contradicts the warning telling the caller not to assume nothing
    happened."""
    res, _ = _live([], [], delete_raises=RuntimeError("500 boom"))
    assert res["outcome"] == "unconfirmed"
    assert res["processed"] == "unknown"
    assert "500 boom" in res["error"]


def test_rate_limit_during_the_delete_is_the_benign_processed_false_case():
    """T3 — only RuntimeError was tested for the delete call; RateLimitError
    must report the OPPOSITE `processed` value (I4): the request never got
    past the rate limiter, so nothing was ever handed to Linnworks to apply."""
    res, _ = _live([], [], delete_raises=server.RateLimitError("429"))
    assert res["outcome"] == "unconfirmed"
    assert res["processed"] is False
    assert res["success"] is False


def test_mapping_lost_warning_quotes_per_id_not_a_misleading_combined_total():
    """I5 — on a run touching two stock item ids, the COMBINED before/after
    totals can hold steady while one id lost rows and the other gained
    (`rows_kept` correctly reads False from the per-id check), which would
    let the warning read 'went from 1 to 1' on exactly the run it exists to
    flag. The message must be believed instantly, so it must quote the
    per-id pairs."""
    state = {"phase": "before"}

    def _open(ch, cid, target_sid):
        if target_sid == SID:
            return []
        if state["phase"] == "before":
            return _both_templates(ch, cid, target_sid)
        return [_tpl(LIVE, "Listed")]

    def _rows(ids):
        if state["phase"] == "before":
            full = {SID.lower(): [_row()], PSID.lower(): []}
        else:
            full = {SID.lower(): [], PSID.lower(): [_row(ref="new-ref")]}
        return {i.lower(): full[i.lower()] for i in ids if i.lower() in full}

    def _call(ep, p=None):
        if ep == _PROCESS_TEMPLATES:
            state["phase"] = "after"
            return {}
        return _item(ep, p)

    mock_cl = Mock(side_effect=_call)
    with patch.object(server, "_resolve_glt_target", _target), \
         patch.object(server, "call_linnworks", mock_cl), \
         patch.object(server, "_open_item_templates", _open), \
         patch.object(server, "_fetch_channel_skus_for_ids", _rows), \
         patch.object(server, "_resolve_variation",
                      lambda sku, sid: {"role": "child", "parent_sku": "PARENT-A",
                                        "parent_stock_item_id": PSID, "group_name": "G",
                                        "siblings": []}):
        res = server.delete_dangling_glt_template(
            "SKU-A", ORPHAN, dry_run=False, confirmed_count=1)

    assert res["outcome"] == "orphan_removed_sibling_mapping_lost"
    assert f"{SID}: 1 → 0" in res["warning"]
    assert f"{PSID}: 0 → 1" in res["warning"]
    assert "went from 1 to 1" not in res["warning"]


def test_a_rising_row_count_after_delete_is_noted_not_silently_passed():
    """M1 — rows appearing after a delete is anomalous. `_rows_kept_across`
    correctly still calls a rise 'kept' (a rise is not a loss), but it must
    be surfaced to a human, not silently absorbed into a clean success
    message."""
    res, _ = _live([_tpl(LIVE, "Listed")], [_row(), _row(ref="second-ref")])
    assert res["outcome"] == "orphan_removed_siblings_intact"
    assert res["success"] is True
    assert "ROSE after the delete" in res["note"]
    assert SID in res["note"]


def test_a_removed_and_recreated_mapping_holds_the_count_but_is_still_a_loss():
    """M2 — a reference-id SET comparison is strictly stronger than
    counting a removed-and-recreated mapping holds the COUNT steady while
    still being a real loss of the ORIGINAL mapping."""
    res, _ = _live([_tpl(LIVE, "Listed")], [_row(ref="a-different-reference")])
    assert res["outcome"] == "orphan_removed_sibling_mapping_lost"
    assert res["success"] is False


def test_override_warning_survives_a_post_write_delete_error_not_dropped():
    """M3 — the most dangerous combination is `allow_unproven_delete` plus
    an inconclusive write; losing the override text there loses the fact
    the template was never proven dead. Both post-write error returns build
    their dict fresh via `{**out, ...}`, which would otherwise silently
    replace `out["warning"]`."""
    def _templates(ch, cid, sid):
        return [_tpl(LIVE, "Listed", listing="listing-live"),
                _tpl(ORPHAN, "Not deleted", listing="listing-orphan")]

    res, _ = _live([], [], delete_raises=RuntimeError("500 boom"),
                   template_id=LIVE, before_templates=_templates,
                   allow_unproven_delete=True)
    assert "NOT proven dead" in res["warning"]
    assert "MAY HAVE REACHED LINNWORKS" in res["warning"]


def test_override_warning_survives_a_readback_failure_too():
    """M3, the other of the two post-write error returns named in the
    finding."""
    def _templates(ch, cid, sid):
        return [_tpl(LIVE, "Listed", listing="listing-live"),
                _tpl(ORPHAN, "Not deleted", listing="listing-orphan")]

    res, _ = _live([], [], readback_raises=server.RateLimitError("429"),
                   template_id=LIVE, before_templates=_templates,
                   allow_unproven_delete=True)
    assert "NOT proven dead" in res["warning"]
    assert "already sent" in res["warning"].lower()


def test_complete_is_true_on_every_definitive_outcome():
    """M4 — `complete` was never set on success paths and read as `None`;
    find_dangling_glt_templates sets it unconditionally once its own read
    succeeds, and this tool should match."""
    res, _ = _live([_tpl(LIVE, "Listed")], [_row()])
    assert res["complete"] is True

    res2, _ = _live([_tpl(ORPHAN, "Not deleted"), _tpl(LIVE, "Listed")], [_row()])
    assert res2["complete"] is True
