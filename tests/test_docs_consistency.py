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
