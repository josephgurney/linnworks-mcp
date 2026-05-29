"""
QA tests for cancel_order, refund_order, and refund_order_lines.

All tests use unittest.mock — no live Linnworks API calls.
Run with: pytest tests/test_cancel_and_refund.py -v

Note: ReturnsRefunds/CreateRefund and ActionRefund were implemented from the
Linnworks OpenAPI spec (returnsrefunds.json) and have not been live-tested
against a tenant as of May 2026.
"""

import sys
import os
import pytest
from unittest.mock import patch, call as mock_call_obj

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── Fixtures ─────────────────────────────────────────────────────────────────

GUID = "aaaaaaaa-1234-1234-1234-000000000001"
GUID_LOC = "bbbbbbbb-0000-0000-0000-000000000000"
ROW_ID_1 = "cccccccc-0001-0001-0001-000000000001"
ROW_ID_2 = "dddddddd-0002-0002-0002-000000000002"


def _make_raw_order(processed=False, postage=5.0):
    return {
        "OrderId": GUID,
        "NumOrderId": 123456,
        "Processed": processed,
        "FulfilmentLocationId": GUID_LOC,
        "GeneralInfo": {
            "ReferenceNum": "REF001",
            "ExternalReference": "EXT001",
            "Source": "Shopify",
            "SubSource": "Shopify",
            "Status": 1,
            "IsParked": False,
            "Marker": 0,
            "ReceivedDate": "2026-05-01T10:00:00",
        },
        "ShippingInfo": {"PostalServiceName": "Royal Mail", "TrackingNumber": None},
        "CustomerInfo": {
            "ChannelBuyerName": "Test Buyer",
            "Address": {"FullName": "Test Buyer", "EmailAddress": "test@example.com"},
            "BillingAddress": {},
        },
        "TotalsInfo": {
            "Subtotal": 20.00,
            "PostageCost": postage,
            "Tax": 4.00,
            "TotalCharge": 25.00 + postage,
            "Currency": "GBP",
            "TotalDiscount": 0.0,
        },
        "Items": [
            {
                "StockItemId": "item-guid-001",
                "SKU": "TEST-SKU-A",
                "Title": "Test Product A",
                "Quantity": 2,
                "RowId": ROW_ID_1,
                "PricePerUnit": 5.00,
                "CostIncTax": 10.00,
            },
            {
                "StockItemId": "item-guid-002",
                "SKU": "TEST-SKU-B",
                "Title": "Test Product B",
                "Quantity": 1,
                "RowId": ROW_ID_2,
                "PricePerUnit": 10.00,
                "CostIncTax": 10.00,
            },
        ],
        "Notes": [],
    }


OPEN_ORDER = _make_raw_order(processed=False)
PROCESSED_ORDER = _make_raw_order(processed=True)
PROCESSED_ORDER_NO_POSTAGE = _make_raw_order(processed=True, postage=0.0)

REFUND_OPTIONS_CAN = {
    "RefundOptions": {
        "CanRefund": True,
        "CannotRefundReason": "None",
    }
}
REFUND_OPTIONS_CANNOT = {
    "RefundOptions": {
        "CanRefund": False,
        "CannotRefundReason": "OrderIsFullyRefundedInLinnworks",
    }
}

CREATE_REFUND_RESP = {
    "RefundHeaderId": 42,
    "RefundReference": "REF-42",
    "Status": {"StatusHeader": "OPEN"},
    "CannotRefundReason": "None",
    "Errors": [],
    "RefundLines": [],
}

ACTION_REFUND_RESP = {
    "SuccessfullyActioned": True,
    "RefundHeaderId": 42,
    "RefundReference": "REF-42",
    "Status": {"StatusHeader": "PROCESSED"},
    "CannotRefundReason": "None",
    "Errors": [],
}


def _orders_by_id_side_effect(path, payload, **kwargs):
    """Route GetOrdersById calls to the right fixture."""
    if "GetOrdersById" in path:
        return [OPEN_ORDER]
    raise AssertionError(f"Unexpected path: {path}")


def _processed_orders_by_id_side_effect(path, payload, **kwargs):
    if "GetOrdersById" in path:
        return [PROCESSED_ORDER]
    raise AssertionError(f"Unexpected path: {path}")


# ── cancel_order ─────────────────────────────────────────────────────────────

class TestCancelOrder:

    def test_dry_run_default_does_not_call_cancel(self):
        """dry_run=True must not call Orders/CancelOrder."""
        import server

        called_paths = []

        def side_effect(path, payload, **kwargs):
            called_paths.append(path)
            if "GetOrdersById" in path:
                return [OPEN_ORDER]
            return {}

        with patch("server.call_linnworks", side_effect=side_effect):
            result = server.cancel_order(GUID)

        assert result["dry_run"] is True
        assert result["status"] == "would_cancel"
        assert not any("CancelOrder" in p for p in called_paths)

    def test_dry_run_shows_items(self):
        """Dry-run output must include the order's items."""
        import server

        with patch("server.call_linnworks", side_effect=_orders_by_id_side_effect):
            result = server.cancel_order(GUID, dry_run=True)

        assert len(result["items"]) == 2
        skus = {i["SKU"] for i in result["items"]}
        assert "TEST-SKU-A" in skus
        assert "TEST-SKU-B" in skus

    def test_processed_order_is_rejected(self):
        """cancel_order must refuse if the order is already processed."""
        import server

        def side_effect(path, payload, **kwargs):
            if "GetOrdersById" in path:
                return [PROCESSED_ORDER]
            raise AssertionError(f"Unexpected call: {path}")

        with patch("server.call_linnworks", side_effect=side_effect):
            result = server.cancel_order(GUID, dry_run=False)

        assert "error" in result
        assert "already processed" in result["error"].lower()

    def test_live_run_calls_cancel_endpoint(self):
        """dry_run=False must call Orders/CancelOrder."""
        import server

        cancel_payload = {}

        def side_effect(path, payload, **kwargs):
            if "GetOrdersById" in path:
                return [OPEN_ORDER]
            if "CancelOrder" in path:
                cancel_payload.update(payload)
                return "OK"
            raise AssertionError(f"Unexpected path: {path}")

        with patch("server.call_linnworks", side_effect=side_effect):
            result = server.cancel_order(GUID, note="test cancel", dry_run=False)

        assert result["dry_run"] is False
        assert result["status"] == "cancelled"
        assert cancel_payload.get("orderId") == GUID
        assert cancel_payload.get("note") == "test cancel"

    def test_fulfilment_location_passed_to_cancel(self):
        """The FulfilmentLocationId from the order must be forwarded to CancelOrder."""
        import server

        cancel_payload = {}

        def side_effect(path, payload, **kwargs):
            if "GetOrdersById" in path:
                return [OPEN_ORDER]
            if "CancelOrder" in path:
                cancel_payload.update(payload)
                return "OK"
            raise AssertionError(f"Unexpected path: {path}")

        with patch("server.call_linnworks", side_effect=side_effect):
            server.cancel_order(GUID, dry_run=False)

        assert cancel_payload.get("fulfilmentCenter") == GUID_LOC

    def test_invalid_order_id_returns_error(self):
        """Unresolvable order ID must return error dict, not raise."""
        import server

        with patch("server.call_linnworks", return_value=[]):
            result = server.cancel_order(GUID, dry_run=False)

        assert "error" in result


# ── refund_order ─────────────────────────────────────────────────────────────

class TestRefundOrder:

    def test_dry_run_does_not_call_create_refund(self):
        """dry_run=True must not call ReturnsRefunds/CreateRefund."""
        import server

        called = []

        def side_effect(path, payload, **kwargs):
            called.append(path)
            if "GetOrdersById" in path:
                return [PROCESSED_ORDER]
            raise AssertionError(f"Unexpected path in dry run: {path}")

        with patch("server.call_linnworks", side_effect=side_effect):
            result = server.refund_order(GUID, dry_run=True)

        assert result["dry_run"] is True
        assert not any("CreateRefund" in p for p in called)
        assert not any("ActionRefund" in p for p in called)

    def test_open_order_is_rejected(self):
        """refund_order must refuse for open (unprocessed) orders."""
        import server

        with patch("server.call_linnworks", side_effect=_orders_by_id_side_effect):
            result = server.refund_order(GUID, dry_run=False)

        assert "error" in result
        assert "not yet processed" in result["error"].lower()

    def test_dry_run_includes_all_item_lines(self):
        """Dry run must list refund lines for all items + shipping."""
        import server

        with patch("server.call_linnworks",
                   side_effect=_processed_orders_by_id_side_effect):
            result = server.refund_order(GUID, dry_run=True)

        # 2 items + 1 shipping = 3 lines
        assert len(result["refund_lines"]) == 3
        units = {rl["unit"] for rl in result["refund_lines"]}
        assert "Item" in units
        assert "Shipping" in units

    def test_dry_run_no_shipping_when_zero_postage(self):
        """No shipping refund line should appear when postage is zero."""
        import server

        def side_effect(path, payload, **kwargs):
            if "GetOrdersById" in path:
                return [PROCESSED_ORDER_NO_POSTAGE]
            raise AssertionError(f"Unexpected path: {path}")

        with patch("server.call_linnworks", side_effect=side_effect):
            result = server.refund_order(GUID, dry_run=True)

        units = [rl["unit"] for rl in result["refund_lines"]]
        assert "Shipping" not in units

    def test_dry_run_total_is_sum_of_lines(self):
        """total_refund must equal sum of all refund line amounts."""
        import server

        with patch("server.call_linnworks",
                   side_effect=_processed_orders_by_id_side_effect):
            result = server.refund_order(GUID, dry_run=True)

        expected = sum(rl["amount"] for rl in result["refund_lines"])
        assert abs(result["total_refund"] - expected) < 0.001

    def test_cannot_refund_reason_surfaces_error(self):
        """If GetRefundOptions says cannot refund, return error dict."""
        import server

        def side_effect(path, payload, **kwargs):
            if "GetOrdersById" in path:
                return [PROCESSED_ORDER]
            if "GetRefundOptions" in path:
                return REFUND_OPTIONS_CANNOT
            raise AssertionError(f"Unexpected path: {path}")

        with patch("server.call_linnworks", side_effect=side_effect):
            result = server.refund_order(GUID, dry_run=False)

        assert "error" in result
        assert "OrderIsFullyRefundedInLinnworks" in result["error"]

    def test_live_run_calls_create_then_action(self):
        """Live run must call CreateRefund then ActionRefund when push_to_channel=True."""
        import server

        calls = []

        def side_effect(path, payload, **kwargs):
            calls.append(path)
            if "GetOrdersById" in path:
                return [PROCESSED_ORDER]
            if "GetRefundOptions" in path:
                return REFUND_OPTIONS_CAN
            if "CreateRefund" in path:
                return CREATE_REFUND_RESP
            if "ActionRefund" in path:
                return ACTION_REFUND_RESP
            raise AssertionError(f"Unexpected path: {path}")

        with patch("server.call_linnworks", side_effect=side_effect):
            result = server.refund_order(GUID, dry_run=False, push_to_channel=True)

        assert any("CreateRefund" in p for p in calls)
        assert any("ActionRefund" in p for p in calls)
        assert result["actioned"] is True
        assert result["refund_header_id"] == 42

    def test_live_run_no_action_when_push_to_channel_false(self):
        """When push_to_channel=False, ActionRefund must NOT be called."""
        import server

        calls = []

        def side_effect(path, payload, **kwargs):
            calls.append(path)
            if "GetOrdersById" in path:
                return [PROCESSED_ORDER]
            if "GetRefundOptions" in path:
                return REFUND_OPTIONS_CAN
            if "CreateRefund" in path:
                return CREATE_REFUND_RESP
            raise AssertionError(f"Unexpected path: {path}")

        with patch("server.call_linnworks", side_effect=side_effect):
            result = server.refund_order(GUID, dry_run=False, push_to_channel=False)

        assert not any("ActionRefund" in p for p in calls)
        assert result["actioned"] is False

    def test_create_refund_payload_uses_request_wrapper(self):
        """CreateRefund must be called with the {'request': {...}} wrapper."""
        import server

        create_payload = {}

        def side_effect(path, payload, **kwargs):
            if "GetOrdersById" in path:
                return [PROCESSED_ORDER]
            if "GetRefundOptions" in path:
                return REFUND_OPTIONS_CAN
            if "CreateRefund" in path:
                create_payload.update(payload)
                return CREATE_REFUND_RESP
            if "ActionRefund" in path:
                return ACTION_REFUND_RESP
            raise AssertionError(f"Unexpected path: {path}")

        with patch("server.call_linnworks", side_effect=side_effect):
            server.refund_order(GUID, dry_run=False)

        assert "request" in create_payload, "CreateRefund must use {'request': {...}} wrapper"
        inner = create_payload["request"]
        assert inner.get("OrderId") == GUID
        assert "RefundLines" in inner


# ── refund_order_lines ────────────────────────────────────────────────────────

class TestRefundOrderLines:

    def test_dry_run_shows_specified_lines_only(self):
        """Dry run must only show the lines that were requested."""
        import server

        with patch("server.call_linnworks",
                   side_effect=_processed_orders_by_id_side_effect):
            result = server.refund_order_lines(
                GUID,
                lines=[{"row_id": ROW_ID_1}],
                dry_run=True,
            )

        assert result["dry_run"] is True
        assert len(result["refund_lines"]) == 1
        assert result["refund_lines"][0]["order_item_row_id"] == ROW_ID_1

    def test_unknown_row_id_returns_error(self):
        """row_id not in order items must return error dict."""
        import server

        with patch("server.call_linnworks",
                   side_effect=_processed_orders_by_id_side_effect):
            result = server.refund_order_lines(
                GUID,
                lines=[{"row_id": "nonexistent-row-id"}],
                dry_run=True,
            )

        assert "error" in result
        assert "not found" in result["error"].lower()

    def test_missing_row_id_returns_error(self):
        """Entry without 'row_id' must return error dict."""
        import server

        with patch("server.call_linnworks",
                   side_effect=_processed_orders_by_id_side_effect):
            result = server.refund_order_lines(
                GUID,
                lines=[{"amount": 5.0}],
                dry_run=True,
            )

        assert "error" in result

    def test_amount_default_is_cost_inc_tax(self):
        """If amount is not given, it should default to the item's cost_inc_tax."""
        import server

        with patch("server.call_linnworks",
                   side_effect=_processed_orders_by_id_side_effect):
            result = server.refund_order_lines(
                GUID,
                lines=[{"row_id": ROW_ID_1}],
                dry_run=True,
            )

        # PROCESSED_ORDER item 1 has CostIncTax=10.00
        assert result["refund_lines"][0]["amount"] == 10.00

    def test_custom_amount_overrides_default(self):
        """Supplying 'amount' must use that value instead of cost_inc_tax."""
        import server

        with patch("server.call_linnworks",
                   side_effect=_processed_orders_by_id_side_effect):
            result = server.refund_order_lines(
                GUID,
                lines=[{"row_id": ROW_ID_1, "amount": 3.50}],
                dry_run=True,
            )

        assert result["refund_lines"][0]["amount"] == 3.50

    def test_refund_postage_adds_shipping_line(self):
        """refund_postage=True must add a Shipping refund line."""
        import server

        with patch("server.call_linnworks",
                   side_effect=_processed_orders_by_id_side_effect):
            result = server.refund_order_lines(
                GUID,
                lines=[{"row_id": ROW_ID_1}],
                refund_postage=True,
                dry_run=True,
            )

        units = [rl["unit"] for rl in result["refund_lines"]]
        assert "Shipping" in units

    def test_no_shipping_line_by_default(self):
        """refund_postage=False (default) must not add a Shipping line."""
        import server

        with patch("server.call_linnworks",
                   side_effect=_processed_orders_by_id_side_effect):
            result = server.refund_order_lines(
                GUID,
                lines=[{"row_id": ROW_ID_1}],
                dry_run=True,
            )

        units = [rl["unit"] for rl in result["refund_lines"]]
        assert "Shipping" not in units

    def test_open_order_is_rejected(self):
        """refund_order_lines must refuse for unprocessed orders."""
        import server

        with patch("server.call_linnworks", side_effect=_orders_by_id_side_effect):
            result = server.refund_order_lines(
                GUID,
                lines=[{"row_id": ROW_ID_1}],
                dry_run=False,
            )

        assert "error" in result
        assert "not yet processed" in result["error"].lower()

    def test_live_run_create_refund_payload(self):
        """CreateRefund payload must include the requested lines with request wrapper."""
        import server

        create_payload = {}

        def side_effect(path, payload, **kwargs):
            if "GetOrdersById" in path:
                return [PROCESSED_ORDER]
            if "GetRefundOptions" in path:
                return REFUND_OPTIONS_CAN
            if "CreateRefund" in path:
                create_payload.update(payload)
                return CREATE_REFUND_RESP
            if "ActionRefund" in path:
                return ACTION_REFUND_RESP
            raise AssertionError(f"Unexpected path: {path}")

        with patch("server.call_linnworks", side_effect=side_effect):
            server.refund_order_lines(
                GUID,
                lines=[{"row_id": ROW_ID_1, "amount": 5.00}],
                dry_run=False,
            )

        assert "request" in create_payload
        inner = create_payload["request"]
        assert inner.get("OrderId") == GUID
        lines = inner.get("RefundLines", [])
        assert len(lines) == 1
        assert lines[0]["OrderItemRowId"] == ROW_ID_1
        assert lines[0]["Amount"] == 5.00


# ── _format_order_detail additions ───────────────────────────────────────────

def test_format_order_detail_row_id_per_item():
    """_format_order_detail must include row_id for each item."""
    import server

    raw = _make_raw_order()
    result = server._format_order_detail(raw)
    row_ids = [i["row_id"] for i in result["items"]]
    assert ROW_ID_1 in row_ids
    assert ROW_ID_2 in row_ids


def test_format_order_detail_row_id_absent_is_none():
    """row_id must be None when RowId is absent from the raw item."""
    import server

    raw = _make_raw_order()
    # Remove RowId from first item
    del raw["Items"][0]["RowId"]
    result = server._format_order_detail(raw)
    assert result["items"][0]["row_id"] is None


def test_format_order_detail_totals_present():
    """_format_order_detail must include a totals dict with currency and postage."""
    import server

    raw = _make_raw_order(postage=5.50)
    result = server._format_order_detail(raw)
    assert "totals" in result
    assert result["totals"]["currency"] == "GBP"
    assert result["totals"]["postage"] == 5.50


def test_format_order_detail_fulfilment_location_id():
    """_format_order_detail must include fulfilment_location_id."""
    import server

    raw = _make_raw_order()
    result = server._format_order_detail(raw)
    assert result["fulfilment_location_id"] == GUID_LOC


# ── version ───────────────────────────────────────────────────────────────────

def test_version_is_at_least_1_9_0():
    import server
    major, minor, patch = (int(x) for x in server.__version__.split("."))
    assert (major, minor, patch) >= (1, 9, 0), f"Expected >= 1.9.0, got {server.__version__}"
