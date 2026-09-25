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
