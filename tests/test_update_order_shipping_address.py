"""
QA tests for update_order_shipping_address.

These tests use unittest.mock to avoid hitting the live Linnworks API.
Run with: pytest tests/test_update_order_shipping_address.py -v

Implementation note: the endpoint used under the hood is
Orders/SetOrderCustomerInfo (confirmed working May 2026). The proposed
SetOrderShippingAddress endpoint does not exist in the Linnworks API —
SetOrderCustomerInfo covers the same use case.
"""

import sys
import os
import pytest
from unittest.mock import patch, MagicMock

# ---------------------------------------------------------------------------
# Make sure the package root is on sys.path when running pytest from the
# repo root (e.g. `pytest tests/` without installing the package).
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ---------- Fixtures ----------

VALID_GUID = "a1b2c3d4-1234-5678-abcd-ef0123456789"
NUMERIC_ID = "596475"

# A full-shape order response as returned by Orders/GetOrdersById.
# Status 1 = Pending Dispatch (open).
OPEN_ORDER_RESPONSE = {
    "OrderId": VALID_GUID,
    "NumOrderId": 596475,
    "Status": 1,  # Pending Dispatch — open, editable
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

# Status 0 represents a processed/dispatched order in the test fixtures.
# In the real API, `Processed: True` is the canonical signal — Status 0 is
# additionally checked here so these mocks work without a full API response.
DISPATCHED_ORDER_RESPONSE = {
    "OrderId": VALID_GUID,
    "NumOrderId": 596475,
    "Status": 0,  # Processed/dispatched — must be blocked
    "Processed": True,
    "GeneralInfo": {"Status": 0},
    "CustomerInfo": {"Address": {}, "BillingAddress": {}},
}

VALID_ADDRESS = dict(
    full_name="Jane Smith",
    address1="10 Test Street",
    town="Bristol",
    post_code="BS1 1AA",
    country_code="GB",
    address2="Flat 3",
    region="Somerset",
    country="United Kingdom",
    phone="07700900000",
    email="jane@example.com",
)


# ---------- 1. dry_run=True (default) does not call the API ----------

def test_dry_run_default_does_not_write():
    """dry_run=True must return a dry_run result without calling SetOrderCustomerInfo."""
    import server

    with patch("server.call_linnworks") as mock_call:
        mock_call.return_value = OPEN_ORDER_RESPONSE

        result = server.update_order_shipping_address(
            order_id=VALID_GUID,
            **VALID_ADDRESS,
            # dry_run not passed — must default to True
        )

    assert result["dry_run"] is True
    assert result["success"] is True

    called_paths = [c.args[0] for c in mock_call.call_args_list]
    assert not any("SetOrderCustomerInfo" in p for p in called_paths), (
        "SetOrderCustomerInfo was called during a dry run"
    )


# ---------- 2. dry_run=False writes to the API ----------

def test_live_run_calls_set_address():
    """dry_run=False must call SetOrderCustomerInfo with the correct payload."""
    import server

    with patch("server.call_linnworks") as mock_call:
        mock_call.return_value = OPEN_ORDER_RESPONSE

        result = server.update_order_shipping_address(
            order_id=VALID_GUID,
            dry_run=False,
            **VALID_ADDRESS,
        )

    assert result["dry_run"] is False
    assert result["success"] is True

    called_paths = [c.args[0] for c in mock_call.call_args_list]
    assert any("SetOrderCustomerInfo" in p for p in called_paths), (
        "SetOrderCustomerInfo was never called on a live run"
    )


# ---------- 3. Numeric order ID is resolved to GUID ----------

def test_numeric_order_id_resolves_to_guid():
    """Passing a numeric order number must resolve it to a GUID before updating."""
    import server

    # Numeric path uses call_linnworks_get (GET endpoint); live path uses call_linnworks.
    # Both are patched to return the same open-order fixture.
    with patch("server.call_linnworks") as mock_post, \
         patch("server.call_linnworks_get") as mock_get:
        mock_post.return_value = OPEN_ORDER_RESPONSE
        mock_get.return_value = OPEN_ORDER_RESPONSE

        result = server.update_order_shipping_address(
            order_id=NUMERIC_ID,
            dry_run=False,
            **VALID_ADDRESS,
        )

    assert result["success"] is True
    # The resolved GUID must appear in the result
    assert result.get("order_id") == VALID_GUID


# ---------- 4. Dispatched orders are blocked ----------

def test_dispatched_order_is_blocked():
    """If the order is already dispatched/processed, return an error without writing."""
    import server

    with patch("server.call_linnworks") as mock_call:
        mock_call.return_value = DISPATCHED_ORDER_RESPONSE

        result = server.update_order_shipping_address(
            order_id=VALID_GUID,
            dry_run=False,
            **VALID_ADDRESS,
        )

    assert result["success"] is False
    assert "error" in result
    assert (
        "dispatched" in result["error"].lower()
        or "processed" in result["error"].lower()
    ), f"Expected a clear 'dispatched/processed' error message, got: {result['error']}"

    # Confirm the address endpoint was never called
    called_paths = [c.args[0] for c in mock_call.call_args_list]
    assert not any("SetOrderCustomerInfo" in p for p in called_paths)


# ---------- 5. Return value contains echoed address ----------

def test_return_value_echoes_address():
    """The result must include the address fields that were sent, for audit purposes."""
    import server

    with patch("server.call_linnworks") as mock_call:
        mock_call.return_value = OPEN_ORDER_RESPONSE

        result = server.update_order_shipping_address(
            order_id=VALID_GUID,
            dry_run=False,
            **VALID_ADDRESS,
        )

    assert "updated_address" in result
    addr = result["updated_address"]
    assert addr.get("post_code") == "BS1 1AA"
    assert addr.get("town") == "Bristol"
    assert addr.get("full_name") == "Jane Smith"
    assert addr.get("country_code") == "GB"


# ---------- 6. Linnworks errors are surfaced, not swallowed ----------

def test_linnworks_error_is_surfaced():
    """
    If Linnworks returns an error from SetOrderCustomerInfo, it should propagate
    as a RuntimeError (the call_linnworks convention), not be silently swallowed.

    The 401 auto-retry is handled inside call_linnworks itself — not at the
    tool level — so this test verifies the error-propagation path instead.
    """
    import server

    def side_effect(path, *args, **kwargs):
        if "SetOrderCustomerInfo" in path:
            raise RuntimeError("Linnworks SetOrderCustomerInfo failed: HTTP 500 — Internal Server Error")
        return OPEN_ORDER_RESPONSE

    with patch("server.call_linnworks", side_effect=side_effect):
        with pytest.raises(RuntimeError, match="SetOrderCustomerInfo"):
            server.update_order_shipping_address(
                order_id=VALID_GUID,
                dry_run=False,
                **VALID_ADDRESS,
            )


# ---------- 7. __version__ is present ----------

def test_version_is_set():
    """server.py must expose a __version__ string."""
    import server
    assert hasattr(server, "__version__")
    assert isinstance(server.__version__, str)
    assert server.__version__  # non-empty
