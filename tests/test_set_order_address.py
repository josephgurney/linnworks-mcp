"""
Tests for set_order_address — the single order-address tool.

Ported from tests/test_update_order_shipping_address.py when the two overlapping
address tools were merged in v1.35.0. update_order_shipping_address was a strict
subset of this one (no address3, saveToCrm hard-coded False, no read-back after
the write) whose only distinguishing feature was that five arguments were
mandatory. That guard survives as require_complete=True; the tool itself is gone.

The two behaviours worth keeping in mind here:
  - Every field defaults to None = "keep current", so ONE tool covers both a
    one-line amendment and a whole-destination replacement.
  - require_complete checks what the CALLER supplied, not the merged result. A
    merged address can be complete and still wrong (new street, old town), which
    is exactly the mis-ship the guard exists to prevent.

All Linnworks calls are mocked — no live API access.
"""
import pytest
from unittest.mock import patch

import server

VALID_GUID = "a1b2c3d4-1234-5678-abcd-ef0123456789"
NUMERIC_ID = "596475"

# Orders/GetOrdersById shape. Status 1 = Pending Dispatch (open, editable).
OPEN_ORDER = {
    "OrderId": VALID_GUID,
    "NumOrderId": 596475,
    "Status": 1,
    "Processed": False,
    "GeneralInfo": {"Status": 1},
    "CustomerInfo": {
        "ChannelBuyerName": "jane123",
        "Address": {
            "FullName": "Jane Smith",
            "Address1": "1 Old Street",
            "Address2": "",
            "Address3": "",
            "Town": "London",
            "Region": "",
            "PostCode": "EC1A 1BB",
            "Country": "United Kingdom",
            "Continent": "Europe",
            "PhoneNumber": "",
            "EmailAddress": "jane@example.com",
            "Company": "",
            "CountryId": "00000000-0000-0000-0000-000000000000",
        },
        "BillingAddress": {},
    },
}

DISPATCHED_ORDER = {
    "OrderId": VALID_GUID,
    "NumOrderId": 596475,
    "Status": 0,
    "Processed": True,
    "GeneralInfo": {"Status": 0},
    "CustomerInfo": {"Address": {}, "BillingAddress": {}},
}

# A complete new destination — the whole-address-change case.
FULL_ADDRESS = dict(
    full_name="Jane Smith",
    address1="10 Test Street",
    address2="Flat 3",
    town="Bristol",
    region="Somerset",
    postcode="BS1 1AA",
    country="United Kingdom",
    phone="07700900000",
    email="jane@example.com",
)


def _paths(mock):
    return [c.args[0] for c in mock.call_args_list]


# --- dry run -----------------------------------------------------------------

def test_dry_run_is_the_default_and_writes_nothing():
    with patch("server.call_linnworks") as mock_call:
        mock_call.return_value = [OPEN_ORDER]
        result = server.set_order_address(order_id=VALID_GUID, **FULL_ADDRESS)

    assert result["dry_run"] is True
    assert result["status"] == "dry_run"
    assert not any("SetOrderCustomerInfo" in p for p in _paths(mock_call)), (
        "SetOrderCustomerInfo was called during a dry run"
    )


def test_dry_run_shows_a_before_after_diff():
    with patch("server.call_linnworks") as mock_call:
        mock_call.return_value = [OPEN_ORDER]
        result = server.set_order_address(order_id=VALID_GUID, town="Bristol")

    assert result["before"]["town"] == "London"
    assert result["after"]["town"] == "Bristol"
    # Untouched fields must not appear in the diff at all.
    assert "postcode" not in result["before"]


# --- live write --------------------------------------------------------------

def test_live_run_writes_and_reads_back():
    """The write is followed by a confirming read — a 2xx alone isn't proof."""
    with patch("server.call_linnworks") as mock_call:
        mock_call.return_value = [OPEN_ORDER]
        result = server.set_order_address(
            order_id=VALID_GUID, dry_run=False, **FULL_ADDRESS)

    assert result["status"] == "updated"
    paths = _paths(mock_call)
    assert any("SetOrderCustomerInfo" in p for p in paths), "never wrote"
    assert paths.index("Orders/SetOrderCustomerInfo") < len(paths) - 1, (
        "no read-back call after the write"
    )
    assert paths[-1] == "Orders/GetOrdersById"


def test_only_supplied_fields_are_changed_others_preserved():
    """The one-line amendment case: everything unsupplied keeps its old value."""
    with patch("server.call_linnworks") as mock_call:
        mock_call.return_value = [OPEN_ORDER]
        server.set_order_address(order_id=VALID_GUID, dry_run=False, address1="9 New Road")

    write = next(c for c in mock_call.call_args_list if c.args[0] == "Orders/SetOrderCustomerInfo")
    sent = write.args[1]["info"]["Address"]
    assert sent["Address1"] == "9 New Road"      # changed
    assert sent["Town"] == "London"              # preserved
    assert sent["PostCode"] == "EC1A 1BB"        # preserved
    assert sent["CountryId"] == "00000000-0000-0000-0000-000000000000"  # internal field kept


def test_no_changes_short_circuits_without_writing():
    with patch("server.call_linnworks") as mock_call:
        mock_call.return_value = [OPEN_ORDER]
        result = server.set_order_address(
            order_id=VALID_GUID, dry_run=False, town="London")

    assert result["status"] == "no_changes"
    assert not any("SetOrderCustomerInfo" in p for p in _paths(mock_call))


def test_save_to_crm_is_passed_through():
    with patch("server.call_linnworks") as mock_call:
        mock_call.return_value = [OPEN_ORDER]
        server.set_order_address(
            order_id=VALID_GUID, dry_run=False, town="Bristol", save_to_crm=True)

    write = next(c for c in mock_call.call_args_list if c.args[0] == "Orders/SetOrderCustomerInfo")
    assert write.args[1]["saveToCrm"] is True


# --- order id resolution -----------------------------------------------------

def test_numeric_order_id_resolves_to_guid():
    with patch("server.call_linnworks") as mock_post, \
         patch("server.call_linnworks_get") as mock_get:
        mock_post.return_value = [OPEN_ORDER]
        mock_get.return_value = OPEN_ORDER
        result = server.set_order_address(
            order_id=NUMERIC_ID, dry_run=False, **FULL_ADDRESS)

    assert result["order_guid"] == VALID_GUID
    mock_get.assert_called_once()
    assert "GetOrderDetailsByNumOrderId" in mock_get.call_args.args[0]


def test_unknown_order_returns_an_error_not_an_exception():
    with patch("server.call_linnworks") as mock_call:
        mock_call.return_value = []
        result = server.set_order_address(order_id=VALID_GUID, town="Bristol")

    assert result["status"] == "error"
    assert "No order found" in result["error"]


# --- processed-order refusal -------------------------------------------------

def test_processed_order_is_blocked_before_writing():
    with patch("server.call_linnworks") as mock_call:
        mock_call.return_value = [DISPATCHED_ORDER]
        result = server.set_order_address(
            order_id=VALID_GUID, dry_run=False, **FULL_ADDRESS)

    assert result["status"] == "error"
    assert "processed" in result["error"].lower()
    assert not any("SetOrderCustomerInfo" in p for p in _paths(mock_call))


# --- require_complete guard (inherited from the deleted tool) ----------------

def test_require_complete_refuses_a_partial_address():
    """The mis-ship this guard exists to prevent: new street, old town."""
    with patch("server.call_linnworks") as mock_call:
        mock_call.return_value = [OPEN_ORDER]
        result = server.set_order_address(
            order_id=VALID_GUID, address1="10 Test Street", require_complete=True)

    assert result["status"] == "error"
    assert result["missing_fields"] == ["country", "full_name", "postcode", "town"]
    # Fails before any API call at all — cheapest possible refusal.
    assert mock_call.call_count == 0


def test_require_complete_treats_blank_as_missing():
    with patch("server.call_linnworks") as mock_call:
        mock_call.return_value = [OPEN_ORDER]
        result = server.set_order_address(
            order_id=VALID_GUID, require_complete=True,
            full_name="Jane Smith", address1="10 Test Street",
            town="   ", postcode="BS1 1AA", country="United Kingdom")

    assert result["status"] == "error"
    assert result["missing_fields"] == ["town"]


def test_require_complete_passes_when_every_core_field_is_supplied():
    with patch("server.call_linnworks") as mock_call:
        mock_call.return_value = [OPEN_ORDER]
        result = server.set_order_address(
            order_id=VALID_GUID, dry_run=False, require_complete=True, **FULL_ADDRESS)

    assert result["status"] == "updated"


def test_partial_amendment_still_allowed_by_default():
    """require_complete defaults False — the by-line workflow is unaffected."""
    with patch("server.call_linnworks") as mock_call:
        mock_call.return_value = [OPEN_ORDER]
        result = server.set_order_address(
            order_id=VALID_GUID, dry_run=False, address1="10 Test Street")

    assert result["status"] == "updated"


# --- error propagation -------------------------------------------------------

def test_linnworks_errors_are_surfaced_not_swallowed():
    def side_effect(path, *args, **kwargs):
        if "SetOrderCustomerInfo" in path:
            raise RuntimeError(
                "Linnworks Orders/SetOrderCustomerInfo failed: HTTP 500 — Internal Server Error")
        return [OPEN_ORDER]

    with patch("server.call_linnworks", side_effect=side_effect):
        with pytest.raises(RuntimeError, match="SetOrderCustomerInfo"):
            server.set_order_address(order_id=VALID_GUID, dry_run=False, **FULL_ADDRESS)


# --- the merge itself --------------------------------------------------------

def test_the_removed_tool_is_really_gone():
    """update_order_shipping_address was merged into set_order_address (v1.35.0)."""
    assert not hasattr(server, "update_order_shipping_address")
