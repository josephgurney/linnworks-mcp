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
