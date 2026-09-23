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
