"""
Guards the hand-maintained docs against drift from the code.

Every fact checked here has actually gone stale in this repo, silently, for
several releases at a time — none of it was caught by a human read-through:

  - CLAUDE.md's Tools section still said "51 tools (44 in v1.10.0 + 7 in
    v1.11.0)" at v1.33.0, when 82 were registered.
  - server.py's __version__ sat at 1.17.0 through seventeen minor releases.
  - CLAUDE.md's WRITE_THRESHOLDS table fell 11 rows behind the code — and that
    table is what tells Claude when a bulk write gets staged for confirmation.
  - update_order_shipping_address shipped in v1.4.0 and was missing from
    CLAUDE.md's tools table entirely until v1.34.0 — documenting it is what
    exposed it as a redundant near-duplicate of set_order_address, and it was
    merged away in v1.35.0. An undocumented tool hides design problems too.

These are cheap, offline, and require no credentials (conftest sets dummies).
When one fails, fix the doc — don't relax the test.
"""
import asyncio
import re
import sys
import tomllib
from pathlib import Path
from unittest import mock

import pytest

import server

ROOT = Path(__file__).resolve().parent.parent
CLAUDE_MD = (ROOT / "CLAUDE.md").read_text()
README_MD = (ROOT / "README.md").read_text()
PYPROJECT_VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]

TOOL_NAMES = sorted(t.name for t in asyncio.run(server.mcp.list_tools()))
TOOL_COUNT = len(TOOL_NAMES)


# --- version ----------------------------------------------------------------

def test_server_version_matches_pyproject():
    assert server.__version__ == PYPROJECT_VERSION, (
        f"server.__version__ is {server.__version__} but pyproject.toml says "
        f"{PYPROJECT_VERSION} — bump both on release."
    )


def test_claude_md_header_version_matches_pyproject():
    assert f"**Current version: {PYPROJECT_VERSION}**" in CLAUDE_MD


def test_readme_version_badge_matches_pyproject():
    assert f"badge/version-{PYPROJECT_VERSION}-blue" in README_MD


# --- tool count -------------------------------------------------------------

def test_claude_md_header_tool_count_is_current():
    assert f"— {TOOL_COUNT} tools" in CLAUDE_MD, (
        f"{TOOL_COUNT} tools are registered; CLAUDE.md's header says otherwise."
    )


def test_claude_md_tools_section_count_is_current():
    assert f"\n{TOOL_COUNT} tools." in CLAUDE_MD, (
        f"{TOOL_COUNT} tools are registered; the Tools section says otherwise."
    )


def test_readme_tool_count_badge_is_current():
    assert f"badge/tools-{TOOL_COUNT}-blue" in README_MD


# --- tool coverage ----------------------------------------------------------

def test_every_registered_tool_is_documented_in_claude_md():
    missing = [t for t in TOOL_NAMES if f"`{t}" not in CLAUDE_MD]
    assert not missing, f"Tools registered but absent from CLAUDE.md: {missing}"


def test_every_registered_tool_is_documented_in_readme():
    missing = [t for t in TOOL_NAMES if f"`{t}`" not in README_MD]
    assert not missing, f"Tools registered but absent from README.md: {missing}"


# --- write thresholds -------------------------------------------------------
# Both docs restate WRITE_THRESHOLDS as a markdown table; it is the contract for
# when a bulk write is staged for confirmation, so a stale row understates risk.

def _threshold_rows(text: str) -> dict:
    return {k: int(v) for k, v in re.findall(r"^\| \`([a-z_]+)\` \| (\d+)[ |]", text, re.M)}


@pytest.mark.parametrize("doc_name", ["CLAUDE.md", "README.md"])
def test_threshold_tables_match_the_code(doc_name):
    # Look the text up by name rather than parametrising on it — passing the file
    # contents as a param makes pytest embed the whole document in the test id,
    # so a single failure prints ~180KB of markdown.
    documented = _threshold_rows({"CLAUDE.md": CLAUDE_MD, "README.md": README_MD}[doc_name])
    code = {k: v for k, v in server.WRITE_THRESHOLDS.items() if k != "default"}

    missing = sorted(k for k in code if k not in documented)
    assert not missing, f"{doc_name} threshold table is missing: {missing}"

    wrong = {k: (code[k], documented[k]) for k in code if documented[k] != code[k]}
    assert not wrong, f"{doc_name} threshold mismatches (code, doc): {wrong}"


def test_every_documented_threshold_actually_exists_in_code():
    """Catches the reverse drift: a doc row for a tool that no longer has one."""
    code = set(server.WRITE_THRESHOLDS)
    for doc_name, text in (("CLAUDE.md", CLAUDE_MD), ("README.md", README_MD)):
        stale = sorted(k for k in _threshold_rows(text) if k not in code)
        assert not stale, f"{doc_name} lists thresholds not in WRITE_THRESHOLDS: {stale}"


# --- revise-proven channels (issue #45) --------------------------------------
# refresh_channel_listing's own GLT_CHANNELS registry, not just human prose, is
# the source of truth for which channels' Revise/Update is live-proven. Amazon
# was fired live twice (24-25 Aug 2026) and accepted with no observable effect
# -- "not yet live-proven" on its own reads as "untried", which stopped being
# true and both docs had drifted from the registry as a result.

def _refresh_channel_listing_readme_row() -> str:
    match = re.search(r"^\| `refresh_channel_listing` \|.*\|$", README_MD, re.M)
    assert match, "README.md is missing the refresh_channel_listing row"
    return match.group(0)


def _refresh_channel_listing_claude_row() -> str:
    match = re.search(r"^\| `refresh_channel_listing\(.*$", CLAUDE_MD, re.M)
    assert match, "CLAUDE.md is missing the refresh_channel_listing tools-table row"
    return match.group(0)


def test_every_registry_uses_the_one_push_observed_vocabulary():
    """#45 and #47 found the same defect one channel apart. They must not end
    up with two names for it: GLT_CHANNELS and EBAY_CHANNELS share the single
    PUSH_OBSERVED_STATES vocabulary, and no channel carries #45's interim
    revise_attempted bool any more.
    """
    for name, registry in (("GLT_CHANNELS", server.GLT_CHANNELS),
                           ("EBAY_CHANNELS", server.EBAY_CHANNELS)):
        for key, entry in registry.items():
            assert entry.get("push_observed_state") in server.PUSH_OBSERVED_STATES, (
                f"{name}['{key}'] is missing a valid push_observed_state"
            )
            assert "revise_attempted" not in entry, (
                f"{name}['{key}'] still carries the superseded revise_attempted bool"
            )


def test_a_registry_cannot_claim_proven_and_never_attempted_at_once():
    """The reason the enum replaced the bool pair. #45's own brief named this
    trap -- two sources of truth that can disagree -- and shipped without a
    guard; the import-time check is that guard, so assert it actually bites.
    """
    for key, entry in list(server.GLT_CHANNELS.items()) + list(server.EBAY_CHANNELS.items()):
        assert entry["revise_proven"] is (
            entry["push_observed_state"] == server.PUSH_PROVEN), key

    original = dict(server.GLT_CHANNELS["amazon"])
    try:
        # revise_proven True while nothing was ever pushed: the contradiction
        # the bool pair allowed. The validator must refuse it.
        server.GLT_CHANNELS["amazon"] = {
            **original, "revise_proven": True,
            "push_observed_state": server.PUSH_NEVER_ATTEMPTED,
        }
        with pytest.raises(ValueError, match="contradicts itself"):
            server._assert_push_observations_consistent()
    finally:
        server.GLT_CHANNELS["amazon"] = original
    # And the guard passes again once the contradiction is gone.
    server._assert_push_observations_consistent()


def test_tried_and_ineffective_channels_must_carry_their_evidence():
    """A channel recorded as accepted-but-not-processed with no reason gives a
    caller a warning it cannot act on -- and the warning text is interpolated
    from that field, so an empty one silently guts the message."""
    original = dict(server.GLT_CHANNELS["amazon"])
    assert original["push_observed_state"] == server.PUSH_ACCEPTED_NOT_PROCESSED
    assert original["push_observed_reason"]
    try:
        server.GLT_CHANNELS["amazon"] = {**original, "push_observed_reason": None}
        with pytest.raises(ValueError, match="no push_observed_reason"):
            server._assert_push_observations_consistent()
    finally:
        server.GLT_CHANNELS["amazon"] = original


def test_readme_and_claude_md_agree_with_the_registry_on_amazon_revise():
    """Amazon: revise_proven is False in the registry -- neither doc may
    claim it as proven, and both must state the tried-and-ineffective fact
    the registry now carries (push_observed_state) rather than merely 'untried'.
    """
    amazon = server.GLT_CHANNELS["amazon"]
    assert amazon["revise_proven"] is False
    assert amazon["push_observed_state"] == server.PUSH_ACCEPTED_NOT_PROCESSED

    readme_row = _refresh_channel_listing_readme_row()
    claude_row = _refresh_channel_listing_claude_row()

    for doc_name, row in (("README.md", readme_row), ("CLAUDE.md", claude_row)):
        assert "spec-based, not yet live-proven" not in row, (
            f"{doc_name} still collapses Amazon revise into merely 'not yet live-proven'"
        )
        assert "NOT LIVE-PROVEN ON AMAZON OR TIKTOK" not in row, (
            f"{doc_name} still lumps Amazon in with TikTok as equally untried"
        )
        assert "no observable change" in row.lower(), (
            f"{doc_name} no longer states the tried-and-ineffective fact for Amazon"
        )


def test_readme_and_claude_md_agree_with_the_registry_on_tiktok_revise():
    """TikTok: still genuinely never attempted -- neither doc may claim it
    was tried, and it must read distinctly from Amazon's wording."""
    tiktok = server.GLT_CHANNELS["tiktok"]
    assert tiktok["revise_proven"] is False
    assert tiktok["push_observed_state"] == server.PUSH_NEVER_ATTEMPTED

    readme_row = _refresh_channel_listing_readme_row()
    claude_row = _refresh_channel_listing_claude_row()

    for doc_name, row in (("README.md", readme_row), ("CLAUDE.md", claude_row)):
        assert "TikTok: fired live" not in row, (
            f"{doc_name} wrongly claims TikTok's revise was fired live"
        )


def test_docstring_no_longer_claims_amazon_variation_shape_is_unobserved():
    doc = server.refresh_channel_listing.__doc__
    assert "unestablished — nothing in this repo has observed it either way" not in doc
    assert "one observation, not a rule" in doc


def test_unpublish_docstring_agrees_with_the_delete_proven_flags():
    """The runtime warnings read GLT_CHANNELS, but the docstring is hand-written
    and said "live-proven on SHOPIFY only" for weeks after Amazon (v1.32.0) and
    TikTok (v1.42.0) were proven. Claude reads the docstring when choosing and
    using the tool, so it must not call a proven channel unproven."""
    doc = server.unpublish_channel_listing.__doc__
    assert "SHOPIFY only" not in doc
    assert "NOT YET LIVE-PROVEN" not in doc
    for cfg in server.GLT_CHANNELS.values():
        if cfg["delete_proven"]:
            assert cfg["channel_type"].upper() in doc, (
                f"unpublish_channel_listing's docstring doesn't name "
                f"{cfg['channel_type']} as a live-proven Delete channel"
            )


# --- refund channel-push proof state (issue #79) -----------------------------
# server.REFUND_CHANNEL_PUSH_PROVEN is the single source of truth for whether
# ReturnsRefunds/ActionRefund (the push to a sales channel) has been shown to
# actually reach one. README.md and CLAUDE.md's tools-table rows for
# refund_order and refund_order_lines must state the same position -- this is
# the same kind of drift #78 found had already happened once for GLT_CHANNELS.

def _readme_row(tool_name: str) -> str:
    match = re.search(rf"^\| `{tool_name}` \|.*\|$", README_MD, re.M)
    assert match, f"README.md is missing the {tool_name} row"
    return match.group(0)


def _claude_md_tools_table_row(tool_name: str) -> str:
    match = re.search(rf"^\| `{tool_name}\(.*$", CLAUDE_MD, re.M)
    assert match, f"CLAUDE.md is missing the {tool_name} tools-table row"
    return match.group(0)


def test_refund_docstrings_no_longer_claim_never_live_tested():
    for doc in (server.refund_order.__doc__, server.refund_order_lines.__doc__):
        assert "have not been live-tested" not in doc
        assert "implemented from the Linnworks OpenAPI spec but have not" not in doc


def test_refund_rows_no_longer_claim_spec_based_not_live_tested():
    for tool_name in ("refund_order", "refund_order_lines"):
        for doc_name, row in (
            ("README.md", _readme_row(tool_name)),
            ("CLAUDE.md", _claude_md_tools_table_row(tool_name)),
        ):
            assert "spec-based, not yet live-tested" not in row, (
                f"{doc_name}'s {tool_name} row still claims the refund "
                "endpoints have never been live-tested"
            )


def test_refund_rows_agree_with_the_code_per_channel_on_the_push_proof():
    """Rewritten by #86. The proof is PER CHANNEL now, so a single
    proven/unproven assertion can no longer express it: Shopify is proven and
    Amazon/eBay are not, and a row that states only one half misleads.

    The old version keyed off REFUND_CHANNEL_PUSH_PROVEN. That constant is now
    DERIVED (True only when every channel is proven) and would keep this guard
    green while the docs said 'never been shown to reach a channel' -- which is
    exactly the drift it exists to catch.
    """
    assert server.REFUND_PUSH_CHANNELS["SHOPIFY"]["state"] == "proven"
    assert server.REFUND_PUSH_CHANNELS["AMAZON"]["state"] == "never_attempted"
    assert server.REFUND_PUSH_CHANNELS["EBAY"]["state"] == "never_attempted"

    for tool_name in ("refund_order", "refund_order_lines"):
        for doc_name, row in (
            ("README.md", _readme_row(tool_name)),
            ("CLAUDE.md", _claude_md_tools_table_row(tool_name)),
        ):
            where = f"{doc_name}'s {tool_name} row"
            assert "ActionRefund" in row, f"{where} must name ActionRefund"
            # The blanket denial is now false and must be gone.
            assert "never been shown to actually reach a channel" not in row.lower(), (
                f"{where} still says the push has never reached a channel, but "
                "#86 proved it on Shopify")
            # Both halves must be stated: the proof AND its limit.
            assert "611711" in row, (
                f"{where} does not cite the order the Shopify push was proven on")
            assert "Amazon" in row and "eBay" in row, (
                f"{where} does not record that Amazon and eBay are still untested")


def test_refund_rows_do_not_generalise_the_shopify_proof():
    """#45 and #47 both ended with Linnworks accepting a push that never
    reached the channel. A row implying Shopify's result carries over to them
    is the single most costly thing these docs could say."""
    for tool_name in ("refund_order", "refund_order_lines"):
        for doc_name, row in (
            ("README.md", _readme_row(tool_name)),
            ("CLAUDE.md", _claude_md_tools_table_row(tool_name)),
        ):
            low = row.lower()
            for overclaim in ("proven on all channels", "proven for every channel",
                              "actionrefund is proven**", "push is proven."):
                assert overclaim not in low, (
                    f"{doc_name}'s {tool_name} row over-generalises the "
                    f"Shopify-only proof: {overclaim!r}")


def test_get_refund_headers_row_documents_the_readback_use():
    match = re.search(
        r"^\| `ReturnsRefunds/GetRefundHeadersByOrderId` \|.*$", CLAUDE_MD, re.M
    )
    assert match, "CLAUDE.md is missing the GetRefundHeadersByOrderId confirmed-endpoints row"
    row = match.group(0)
    assert "read-back" in row.lower()
    assert "refund_order" in row


def test_cancel_and_refund_test_header_no_longer_claims_never_live_tested():
    header = (ROOT / "tests" / "test_cancel_and_refund.py").read_text()
    assert "have not been live-tested" not in header.split('"""')[1]


# --- delete_extended_properties live proof (issue #90) -----------------------
# The proof succeeded (22 Sep 2026) -- dry run, expected_value block, a real
# delete, one call per distinct stock item, and the wrong-parameter-name probe
# were all fired against a throwaway SKU and read back. These guards exist so
# the docs cannot silently regress to "spec-based, not yet live-run" the way
# cancel_order sat unproven for months behind that exact phrase.

def _delete_extended_properties_confirmed_endpoint_row() -> str:
    match = re.search(
        r"^\| `Inventory/DeleteInventoryItemExtendedProperties` \|.*$", CLAUDE_MD, re.M
    )
    assert match, (
        "CLAUDE.md is missing the DeleteInventoryItemExtendedProperties "
        "confirmed-endpoints row"
    )
    return match.group(0)


def test_delete_extended_properties_tools_table_row_no_longer_claims_unproven():
    row = _claude_md_tools_table_row("delete_extended_properties")
    assert "Spec-based, not yet live-run" not in row, (
        "CLAUDE.md's delete_extended_properties row still claims the delete "
        "flow was never fired live, but it was proven 22 Sep 2026 (issue #90)"
    )
    assert "LIVE-PROVEN" in row


def test_delete_extended_properties_confirmed_endpoint_row_states_the_observed_status():
    row = _delete_extended_properties_confirmed_endpoint_row()
    assert "has not itself been fired live from this build" not in row, (
        "CLAUDE.md's DeleteInventoryItemExtendedProperties row still claims "
        "the delete was never fired live, but it was proven 22 Sep 2026 (issue #90)"
    )
    # The 204 was inferred before this proof; it must now read as an
    # observation, with the discarded-status-code caveat explaining why
    # instrumentation (not the tool itself) was needed to see it.
    assert "204" in row
    assert "CONFIRMED LIVE" in row


# --- CreateInventoryItemExtendedProperties create-only claim (issue #100) ---
# The v1.56.1/#90 proof found a single Create call against an item that
# already held a row with the same ProperyName behaved like an upsert. The
# confirmed-endpoints row had never been updated to reflect that and still
# opened with a bare "Creates new extended property rows" claim -- exactly
# the assumption #90's multi-match delete branch exists to guard against.
# These guards fail in both directions: if the bare claim reappears, and if
# the bounded, dated observed-upsert statement disappears.

def _create_extended_properties_confirmed_endpoint_row() -> str:
    match = re.search(
        r"^\| `Inventory/CreateInventoryItemExtendedProperties` \|.*$", CLAUDE_MD, re.M
    )
    assert match, (
        "CLAUDE.md is missing the CreateInventoryItemExtendedProperties "
        "confirmed-endpoints row"
    )
    return match.group(0)


def test_create_extended_properties_row_no_longer_claims_create_only():
    row = _create_extended_properties_confirmed_endpoint_row()
    assert not re.search(r"creates new extended property rows", row, re.I), (
        "CLAUDE.md's CreateInventoryItemExtendedProperties row still opens "
        "with the bare create-only claim, but a 22 Sep 2026 observation "
        "(issue #90, written up in issue #100) found it can upsert an "
        "existing row instead of adding a duplicate"
    )


def test_create_extended_properties_row_states_the_observed_upsert_bounded():
    row = _create_extended_properties_confirmed_endpoint_row()
    assert "22 Sep 2026" in row, (
        "the observed-upsert statement (dated 22 Sep 2026) is missing from "
        "the CreateInventoryItemExtendedProperties row"
    )
    assert "did **not**" in row or "did not" in row.lower(), (
        "the row no longer states that the observed Create did not add a "
        "second row"
    )
    # Bounded to one observation -- must not read as a general rule.
    assert "UNCONFIRMED" in row or "unconfirmed" in row, (
        "the row must state the uniqueness question is unconfirmed, not "
        "assert a general uniqueness rule"
    )
    assert "one item" in row.lower() and "one date" in row.lower(), (
        "the row must explicitly bound the finding to one observation on "
        "one item on one date"
    )
    # The still-true facts from issue #13 must survive the rewrite.
    assert "pkRowId" in row and "client-generated GUID" in row, (
        "the still-true pkRowId-required-for-create fact was dropped"
    )
    assert "zero-GUID" in row or "zero-guid" in row.lower(), (
        "the still-true zero-GUID primary-key-collision fact was dropped"
    )
    assert "NON-empty" in row and "NON-JSON" in row, (
        "the still-true non-empty, non-JSON 2xx success body caveat "
        "(issue #13) was dropped"
    )


CLAIM = "Spec-based, not yet live-run"


def test_no_version_note_still_calls_delete_extended_properties_unproven():
    """Every surviving 'not yet live-run' claim for this tool must be struck.

    The tools-table and confirmed-endpoints rows were corrected by #90, but the
    v1.49.0 note kept the original claim un-struck -- the first place a reader
    tracing the tool's history meets it. A note may only still contain the
    phrase inside a ``~~...~~`` strike-through carrying a SUPERSEDED pointer,
    or inside quotes (a later note describing the correction, as v1.56.2 does).
    """
    for note in re.findall(r"^> \*\*v[\d.]+.*$", CLAUDE_MD, re.M):
        if CLAIM not in note:
            continue
        struck = re.findall(r"~~.*?~~", note)
        if any(CLAIM in s for s in struck):
            assert "SUPERSEDED" in note, (
                "the struck claim needs a '<- SUPERSEDED' pointer to the entry "
                "that disproved it: " + note[:120]
            )
            continue
        # Not struck -- the only other allowed form is a quoted mention.
        asserted = re.sub(r"~~.*?~~", "", note)
        asserted = re.sub(r'"[^"]*"', "", asserted)
        assert CLAIM not in asserted, (
            "a version note still claims delete_extended_properties was never "
            "fired live, outside a strike-through: " + note[:120]
        )


# --- Orders/UpdateOrderItem per-field proof (issue #59) ---------------------
#
# AC14: the prose in CLAUDE.md and README.md must agree with the ONE
# machine-readable record in server.py. #45 and #47 each drifted because the
# same fact was retyped in several documents; these are the guards that stop a
# third occurrence, modelled on the revise-proven guards above.

def _update_order_item_endpoint_row() -> str:
    for line in CLAUDE_MD.splitlines():
        if line.startswith("| `Orders/UpdateOrderItem`"):
            return line
    raise AssertionError("CLAUDE.md has no Orders/UpdateOrderItem endpoint row")


def _relink_claude_row() -> str:
    for line in CLAUDE_MD.splitlines():
        if line.startswith("| `relink_order_line("):
            return line
    raise AssertionError("CLAUDE.md has no relink_order_line tool row")


def _relink_readme_row() -> str:
    for line in README_MD.splitlines():
        if line.startswith("| `relink_order_line`"):
            return line
    raise AssertionError("README.md has no relink_order_line row")


def test_no_doc_still_calls_update_order_item_never_fired_live():
    """It WAS fired live, from this build, on 2026-09-23. Any doc still saying
    otherwise is telling a reader the opposite of the registry."""
    assert server.UPDATE_ORDER_ITEM_FIELDS["ItemNumber"]["state"] != (
        server.UPDATE_ITEM_NEVER_ATTEMPTED)
    for name, row in (("CLAUDE.md endpoint row", _update_order_item_endpoint_row()),
                      ("CLAUDE.md tool row", _relink_claude_row()),
                      ("README.md row", _relink_readme_row())):
        low = row.lower()
        for stale in ("never fired it against a real order",
                      "never fired live from this build",
                      "not live-run from this build",
                      "⚠️ unproven endpoint"):
            assert stale not in low, f"{name} still says '{stale}'"


def test_docs_state_the_empty_item_source_trap():
    """The single most misusable finding: ItemSource persists for a non-empty
    value but an empty one is silently dropped behind a 200. A doc that records
    ItemSource as flatly proven would licence a caller to blank it."""
    assert server.UPDATE_ORDER_ITEM_FIELDS["ItemSource"]["state"] == (
        server.UPDATE_ITEM_PROVEN_NON_EMPTY_ONLY)
    for name, row in (("CLAUDE.md endpoint row", _update_order_item_endpoint_row()),
                      ("README.md row", _relink_readme_row())):
        assert "silently discarded" in row.lower(), (
            f"{name} omits that an empty ItemSource is silently discarded")


def test_docs_state_the_despatch_mapping_key_now_that_it_is_known():
    """Inverted by #103. This guard previously failed if a doc CLAIMED the key,
    because #59's run was raced and could not isolate it. #103 isolated it, so
    the guard now fails if a doc does NOT state it -- a reader who is not told
    that ItemNumber steers a despatch cannot know the repair does anything."""
    assert server.DESPATCH_MAPPING_KEY == "ItemNumber"
    for name, row in (("CLAUDE.md tool row", _relink_claude_row()),
                      ("README.md row", _relink_readme_row())):
        assert "ItemNumber" in row, f"{name} does not name the mapping key"
        assert "611708" in row, (
            f"{name} does not cite the order the key was proven on")
        # The exclusion is half the finding: without it a later reader may
        # reasonably wonder whether ChannelSKU was ever ruled out.
        assert "ChannelSKU" in row, (
            f"{name} does not record that ChannelSKU was excluded")


def test_no_doc_still_says_the_despatch_mapping_key_is_unknown():
    for name, row in (("CLAUDE.md tool row", _relink_claude_row()),
                      ("README.md row", _relink_readme_row())):
        low = row.lower()
        for stale in ("is still unknown", "both explain the result",
                      "do not assume writing"):
            assert stale not in low, f"{name} still says '{stale}'"


def test_docs_record_that_the_detector_cannot_see_a_mispointed_link():
    """Found during #59's live run. A caller told to 'confirm it reads as
    linked' without this caveat will accept a false pass."""
    row = _relink_claude_row().lower()
    assert "mis-pointed" in row or "mispointed" in row
    assert "mis-pointed" in _relink_readme_row().lower()


def test_runtime_warning_does_not_name_another_repository():
    """#78 AC11, re-asserted at the docs layer: the racing detail names
    order-sync-service and must stay in DESPATCH_MAPPING_EVIDENCE, never in the
    text a caller is shown."""
    assert "order-sync-service" in server.DESPATCH_MAPPING_EVIDENCE
    for forbidden in ("order-sync-service", "ordersync"):
        assert forbidden not in server._RELINK_ORDER_LINE_UNPROVEN_WARNING
        assert forbidden not in server.DESPATCH_MAPPING_WARNING_TEXT


# --- Orders/UpdateOrderItem payload shape / location scope (issue #105) -----
#
# AC6/AC7: the confirmed-endpoints row and the relink_order_line tools-table
# row must name the proven payload shape, say the proof was obtained at the
# Default location, and say a caller that hard-codes fulfilmentCenter to the
# zero GUID is not covered by that proof at non-Default locations -- and none
# of the three rows may overclaim that the endpoint is proven beyond Default
# while the registry still records the fulfilmentCenter-effect as unknown.

def _update_order_item_proof_scope_rows() -> dict:
    return {
        "CLAUDE.md Orders/UpdateOrderItem row": _update_order_item_endpoint_row(),
        "CLAUDE.md relink_order_line row": _relink_claude_row(),
        "README.md relink_order_line row": _relink_readme_row(),
    }


def test_docs_name_the_proven_payload_shape():
    for name, row in _update_order_item_proof_scope_rows().items():
        low = row.lower()
        assert "fulfilmentcenter" in low, (
            f"{name} does not name how fulfilmentCenter was chosen for the proof")
        assert "deriv" in low, (
            f"{name} does not say fulfilmentCenter was derived from the order")


def test_docs_state_the_proof_was_obtained_at_the_default_location():
    for name, row in _update_order_item_proof_scope_rows().items():
        low = row.lower()
        assert "default" in low, f"{name} does not name the Default location"
        assert "105" in row, f"{name} does not cite issue #105"


def test_docs_state_a_hard_coded_zero_guid_caller_is_not_covered_at_non_default():
    for name, row in _update_order_item_proof_scope_rows().items():
        low = row.lower()
        assert "hard-cod" in low, (
            f"{name} does not describe a caller that hard-codes fulfilmentCenter")
        assert "zero guid" in low, (
            f"{name} does not name the zero GUID that caller hard-codes")
        assert "not covered" in low, (
            f"{name} does not say that caller is not covered by the proof there")


def test_docs_do_not_claim_update_order_item_proven_beyond_default_location():
    """The registry says the fulfilmentCenter-effect is still unknown -- no
    doc may claim otherwise, or a reader is told the opposite of what has
    actually been tested."""
    assert server.FULFILMENT_CENTER_EFFECT_OBSERVED == server.UPDATE_ITEM_NEVER_ATTEMPTED

    overclaims = (
        "proven at non-default",
        "proven at non default",
        "proven at any location",
        "proven at every location",
        "proven regardless of location",
        "covered at non-default",
        "covered at non default",
        "fulfilmentcenter has been proven",
        "fulfilmentcenter's value has been proven",
    )
    for name, row in _update_order_item_proof_scope_rows().items():
        low = row.lower()
        for claim in overclaims:
            assert claim not in low, (
                f"{name} claims Orders/UpdateOrderItem is proven beyond the "
                f"Default location ({claim!r}), but "
                f"FULFILMENT_CENTER_EFFECT_OBSERVED is "
                f"{server.FULFILMENT_CENTER_EFFECT_OBSERVED!r}"
            )


# ══════════════════════════════════════════════════════════════════════════════
# Issue #106 — Orders/RemoveOrderItem is proven, but by ANOTHER repository.
# The docs must say both halves: that it works, and whose run showed it.
# ══════════════════════════════════════════════════════════════════════════════

def _remove_order_item_endpoint_row() -> str:
    for line in CLAUDE_MD.splitlines():
        if line.startswith("| `Orders/RemoveOrderItem`"):
            return line
    raise AssertionError("CLAUDE.md has no Orders/RemoveOrderItem endpoint row")


def _remove_order_item_claude_row() -> str:
    for line in CLAUDE_MD.splitlines():
        if line.startswith("| `remove_order_item("):
            return line
    raise AssertionError("CLAUDE.md has no remove_order_item tool row")


def _remove_order_item_readme_row() -> str:
    for line in README_MD.splitlines():
        if line.startswith("| `remove_order_item`"):
            return line
    raise AssertionError("README.md has no remove_order_item row")


def _remove_order_item_proven(name: str) -> bool:
    entry = server.REMOVE_ORDER_ITEM_OBSERVATIONS[name]
    return entry["state"] != server.UPDATE_ITEM_NEVER_ATTEMPTED


def test_the_endpoint_row_no_longer_calls_remove_order_item_unproven():
    """AC6. The registry says the endpoint removes a line; a doc that still
    says nothing has confirmed it contradicts the code."""
    row = _remove_order_item_endpoint_row()
    if _remove_order_item_proven("endpoint_removes_a_line"):
        assert "STILL UNPROVEN" not in row, (
            "CLAUDE.md's Orders/RemoveOrderItem row still says STILL UNPROVEN "
            "while REMOVE_ORDER_ITEM_OBSERVATIONS records the endpoint proven"
        )


def test_the_endpoint_row_names_the_run_that_proved_it():
    """AC8(c). Guard the positive facts too — a row reworded to some other
    unsupported claim passes a bare 'STILL UNPROVEN' check."""
    row = _remove_order_item_endpoint_row()
    assert "611288" in row, "the endpoint row does not name the proving order"
    assert "23 Sep 2026" in row, "the endpoint row does not date the proof"
    assert "order-sync-service" in row, (
        "the endpoint row does not name whose run this was; other-repo "
        "evidence that reads as this repo's is the drift #78 AC11 guards"
    )


def test_the_endpoint_row_still_names_what_the_run_did_not_establish():
    """AC6. A row that only reports the win reads as a full proof."""
    row = _remove_order_item_endpoint_row()
    for unknown in ("non-Default", "last", "re-add"):
        assert unknown in row, (
            f"the endpoint row does not say {unknown!r} remains untested, but "
            "REMOVE_ORDER_ITEM_OBSERVATIONS records it never attempted"
        )


def test_no_doc_claims_this_build_fired_remove_order_item():
    """AC7/AC8(b). CLAUDE.md's tools-table row claimed a live run from this
    build under issue #59. #59's own scope excluded it ('The
    remove_order_item proof (#89)'), and #89 is still open."""
    proven_here = [
        name for name, entry in server.REMOVE_ORDER_ITEM_OBSERVATIONS.items()
        if entry.get("proven_by") == server.UPDATE_ITEM_PROVEN_HERE
    ]
    if proven_here:
        return
    stale = ("Live-run from this build 23 Sep 2026 (issue #59), with exactly "
             "that checklist followed")
    for doc_name, doc in (("CLAUDE.md", CLAUDE_MD), ("README.md", README_MD)):
        assert stale not in doc, (
            f"{doc_name} claims this build fired remove_order_item, but every "
            "proven REMOVE_ORDER_ITEM_OBSERVATIONS row is another "
            "repository's (issue #89 is the route to a proof here)"
        )
    # Guard the positive fact too: a row that merely drops the false claim
    # leaves a reader with no idea whose run the proof came from.
    row = _remove_order_item_claude_row()
    assert "#89" in row, (
        "the remove_order_item tools row does not point at #89, the route to "
        "a proof from this build"
    )
    assert "order-sync-service" in row, (
        "the remove_order_item tools row does not name whose run proved the "
        "endpoint"
    )


def test_the_readme_row_agrees_with_the_registry():
    """AC7. 'Unproven endpoint' is no longer true of the endpoint itself."""
    row = _remove_order_item_readme_row()
    if _remove_order_item_proven("endpoint_removes_a_line"):
        assert "⚠️ Unproven endpoint" not in row, (
            "README still calls Orders/RemoveOrderItem an unproven endpoint "
            "while the registry records it proven"
        )
        assert "not from this build" in row or "another repo" in row, (
            "README must say whose run proved it, or the reader takes it as ours"
        )


def test_the_runtime_warning_is_not_quoted_into_the_docs_as_a_proof_claim():
    """The warning stays as it is (#89 owns rewording it). A doc that says
    the warning has been updated would be the drift, not the warning."""
    assert "never fired it against a real order" in server._REMOVE_ORDER_ITEM_UNPROVEN_WARNING


# Claims about the ENDPOINT that the 23 Sep run retired. Claims about THIS
# BUILD never having fired it are still true and must survive untouched --
# that distinction is the whole point of the provenance split.
_STALE_REMOVE_ORDER_ITEM_ENDPOINT_CLAIMS = (
    "STILL UNPROVEN",
    "only ever been probed for existence",
    "also unproven",
    "Unproven endpoint",
    "recalculated into the total is unknown",
)


def test_no_doc_line_still_calls_the_remove_order_item_endpoint_unproven():
    """AC8(a) says ANY doc, not just the confirmed-endpoints row.

    QA round 1 caught exactly this: the endpoint row was corrected while the
    tools-table row still opened by calling the endpoint existence-probed-only
    and still said total recalculation was unknown, and the
    Orders/UpdateOrderItem row still called RemoveOrderItem 'also unproven'.
    A row-scoped guard passed all three, and the result was a docs table that
    contradicted itself -- the defect this issue was raised to fix.
    """
    if not _remove_order_item_proven("endpoint_removes_a_line"):
        return
    offenders = []
    for doc_name, doc in (("CLAUDE.md", CLAUDE_MD), ("README.md", README_MD)):
        for number, line in enumerate(doc.splitlines(), 1):
            if "RemoveOrderItem" not in line and "remove_order_item" not in line:
                continue
            for claim in _STALE_REMOVE_ORDER_ITEM_ENDPOINT_CLAIMS:
                if claim in line:
                    offenders.append(f"{doc_name}:{number} still says {claim!r}")
    assert not offenders, (
        "REMOVE_ORDER_ITEM_OBSERVATIONS records the endpoint proven, but these "
        "lines still call it unproven:\n  " + "\n  ".join(offenders)
    )


# ---------------------------------------------------------------------------
# Issue #88 — set_order_status: the parked question, and the stale blanket
# "not yet live-fired" claims on ChangeStatus.
# ---------------------------------------------------------------------------

def _change_status_endpoint_row() -> str:
    match = re.search(r"^\| `Orders/ChangeStatus` \|.*$", CLAUDE_MD, re.M)
    assert match, "CLAUDE.md is missing the Orders/ChangeStatus confirmed-endpoints row"
    return match.group(0)


def _assert_unpaid_parking_docs_agree() -> None:
    """The guard itself, factored out so a test can prove it actually bites
    when the constant moves. AC9 asks for the demonstration, not the promise.
    """
    state = server.UNPAID_FLIP_PARKS_ORDER["state"]
    rows = (
        ("README.md", _readme_row("set_order_status")),
        ("CLAUDE.md", _claude_md_tools_table_row("set_order_status")),
    )
    for doc_name, row in rows:
        low = row.lower()
        where = f"{doc_name}'s set_order_status row"
        if state == "not_observed":
            assert "not yet observed" in low, (
                f"{where} must say the unpaid→parked question is not yet "
                "observed, because UNPAID_FLIP_PARKS_ORDER says nobody has "
                "looked")
            for overclaim in ("unpaid parks the order", "does not park the order"):
                assert overclaim not in low, (
                    f"{where} answers the parking question ({overclaim!r}) "
                    "while the code records no observation")
        elif state == "parks":
            assert "unpaid parks the order" in low, (
                f"{where} does not record that an unpaid flip parks the "
                "order, which the code now says it does")
            assert "not yet observed" not in low, (
                f"{where} still calls the parking question unobserved after "
                "the code recorded an answer")
        elif state == "does_not_park":
            assert "does not park the order" in low, (
                f"{where} does not record that an unpaid flip leaves the "
                "order unparked, which the code now says")
            assert "not yet observed" not in low, (
                f"{where} still calls the parking question unobserved after "
                "the code recorded an answer")
        else:
            raise AssertionError(f"unknown UNPAID_FLIP_PARKS_ORDER state {state!r}")


def test_set_order_status_rows_agree_with_the_code_on_the_parking_answer():
    _assert_unpaid_parking_docs_agree()


@pytest.mark.parametrize("answered_state", ["parks", "does_not_park"])
def test_the_parking_guard_bites_when_the_constant_moves(answered_state):
    """Mutate the constant to each answered state and prove the guard fails
    while the docs still say 'not yet observed'. A guard nobody has watched
    fail is not a guard."""
    assert server.UNPAID_FLIP_PARKS_ORDER["state"] == "not_observed", (
        "this demonstration assumes the shipped state is unset")
    with mock.patch.dict(server.UNPAID_FLIP_PARKS_ORDER,
                         {"state": answered_state}, clear=False):
        with pytest.raises(AssertionError):
            _assert_unpaid_parking_docs_agree()


def test_set_order_status_row_no_longer_blanket_claims_paid_unpaid_unfired():
    """The row said 'paid/unpaid not yet live-fired' full stop. That is now
    two claims wearing one coat: Orders/ChangeStatus HAS been fired live
    (status 4, order 611397, 18 Sep 2026, via create_order's resend path),
    while status 1/0 THROUGH THIS TOOL has not."""
    row = _claude_md_tools_table_row("set_order_status")
    assert "paid/unpaid not yet live-fired" not in row.lower(), (
        "CLAUDE.md's set_order_status row still carries the blanket claim; it "
        "must distinguish the endpoint (fired) from status 1/0 via this tool "
        "(not fired)")
    assert "611397" in row, (
        "the row should cite the order that proves Orders/ChangeStatus itself "
        "has been fired live")


def test_change_status_endpoint_row_no_longer_says_the_write_is_unfired():
    """Stale independently of #88: the v1.55.2 note has recorded
    ChangeStatus(4) fired live since 18 Sep 2026, while this row went on
    saying the write had never been fired."""
    row = _change_status_endpoint_row()
    assert "the ChangeStatus write not yet live-fired" not in row, (
        "CLAUDE.md's Orders/ChangeStatus row still says the write has never "
        "been fired, contradicting the v1.55.2 note")
    assert "611397" in row, "the row must cite the order it was fired on"
    assert "1/0" in row or "status 1" in row, (
        "the row must still scope status 1/0 via set_order_status as unfired")


def test_no_doc_claims_a_live_proof_of_the_unpaid_parking_question():
    """The failure mode this whole issue exists to avoid: a confident doc
    claim about a run nobody made."""
    assert server.UNPAID_FLIP_PARKS_ORDER["state"] == "not_observed"
    for doc_name, text in (("CLAUDE.md", CLAUDE_MD), ("README.md", README_MD)):
        low = text.lower()
        for overclaim in (
            "unpaid flip parks the order (live",
            "proven that an unpaid flip parks",
            "live-proven: unpaid parks",
        ):
            assert overclaim not in low, (
                f"{doc_name} claims a live proof of the parking question that "
                f"has not been run: {overclaim!r}")
