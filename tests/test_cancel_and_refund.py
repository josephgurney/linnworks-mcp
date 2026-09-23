"""
QA tests for cancel_order, refund_order, and refund_order_lines.

All tests use unittest.mock — no live Linnworks API calls.
Run with: pytest tests/test_cancel_and_refund.py -v

Proof state (issue #79, corrected — this header previously claimed the
refund endpoints had never been live-tested, which stopped being true on
18 Sep 2026): ReturnsRefunds/CreateRefund creates a real refund header, that
header is visible via GetRefundHeadersByOrderId (the read-back both refund
tools now perform), and _check_refund_eligibility has been proven both
refusing and permitting against real order data. NOT proven:
ReturnsRefunds/ActionRefund — the call that pushes a refund to a sales
channel — has never been shown to actually reach a channel. Every live
response that attempts a channel push carries a warning saying so (see
REFUND_CHANNEL_PUSH_WARNING in server.py).
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

# The refund-headers read-back fixture (issue #79) — a header matching
# CREATE_REFUND_RESP's RefundHeaderId, as GetRefundHeadersByOrderId would
# show once the refund is visible.
REFUND_HEADERS_MATCH = [
    {"RefundHeaderId": 42, "RefundReference": "REF-42", "Status": {"StatusHeader": "OPEN"}},
]
REFUND_HEADERS_EMPTY = []


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

    def test_refund_field_is_always_sent_because_linnworks_requires_it(self):
        """Orders/CancelOrder REQUIRES `refund` even though the public schema
        marks it optional. Omitting it returns a bare HTTP 400 "The request is
        invalid." naming nothing — live-confirmed 18 Sep 2026, when adding this
        single field was the only change that made an otherwise byte-identical
        payload succeed. Before that, cancel_order could not have worked."""
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
            server.cancel_order(GUID, note="test cancel", dry_run=False)

        assert "refund" in cancel_payload, (
            "cancel_order must send `refund` — Linnworks rejects the call without it"
        )
        assert cancel_payload["refund"] == 0.0

    def test_refund_amount_is_passed_through_when_supplied(self):
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
            server.cancel_order(GUID, refund=12.50, dry_run=False)

        assert cancel_payload["refund"] == 12.50

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
            if "GetRefundHeadersByOrderId" in path:
                return REFUND_HEADERS_MATCH
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
            if "GetRefundHeadersByOrderId" in path:
                return REFUND_HEADERS_MATCH
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
            if "GetRefundHeadersByOrderId" in path:
                return REFUND_HEADERS_MATCH
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
            if "GetRefundHeadersByOrderId" in path:
                return REFUND_HEADERS_MATCH
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


# ── Refund eligibility gate (v1.55.4) ────────────────────────────────────────

class TestRefundEligibilityGate:
    """The gate used to check only CannotRefundReason, which is "None" on BOTH
    a refundable channel order and a DIRECT order Linnworks says it cannot
    refund through a channel. Live-probed 18 Sep 2026:

        DIRECT   CanRefund=False  CanRefundInternally=True   reason="None"
        SHOPIFY  CanRefund=True   CanRefundInternally=True   reason="None"
    """

    def test_channel_push_refused_when_can_refund_is_false(self):
        import server
        err = server._check_refund_eligibility(
            {"CanRefund": False, "CanRefundInternally": True, "CannotRefundReason": "None"},
            push_to_channel=True,
        )
        assert err is not None
        assert "CanRefund=False" in err
        assert "push_to_channel=False" in err, "must name the workaround that actually works"

    def test_internal_refund_allowed_when_can_refund_is_false(self):
        """Proven live: a refund with push_to_channel=False succeeded on an
        order reading CanRefund=False. Refusing on that flag alone would break
        the case that demonstrably works."""
        import server
        assert server._check_refund_eligibility(
            {"CanRefund": False, "CanRefundInternally": True, "CannotRefundReason": "None"},
            push_to_channel=False,
        ) is None

    def test_channel_push_allowed_on_a_refundable_channel_order(self):
        import server
        assert server._check_refund_eligibility(
            {"CanRefund": True, "CanRefundInternally": True, "CannotRefundReason": "None"},
            push_to_channel=True,
        ) is None

    def test_internal_refund_refused_when_cannot_refund_internally(self):
        import server
        err = server._check_refund_eligibility(
            {"CanRefund": True, "CanRefundInternally": False, "CannotRefundReason": "None"},
            push_to_channel=False,
        )
        assert err is not None
        assert "CanRefundInternally=False" in err

    def test_explicit_cannot_refund_reason_still_refuses_first(self):
        import server
        err = server._check_refund_eligibility(
            {"CanRefund": True, "CanRefundInternally": True,
             "CannotRefundReason": "OrderIsFullyRefundedInLinnworks"},
            push_to_channel=True,
        )
        assert "OrderIsFullyRefundedInLinnworks" in err

    def test_gate_refuses_before_any_create_refund_call(self):
        """A refused refund must make no write — verified live, the refusal
        left zero refund headers on the order."""
        import server
        paths = []

        def side_effect(path, payload, **kwargs):
            paths.append(path)
            if "GetOrdersById" in path:
                return [_make_raw_order(processed=True)]
            if "GetRefundOptions" in path:
                return {"RefundOptions": {"CanRefund": False, "CanRefundInternally": True,
                                          "CannotRefundReason": "None"}}
            raise AssertionError(f"Unexpected write: {path}")

        with patch("server.call_linnworks", side_effect=side_effect):
            r = server.refund_order(GUID, push_to_channel=True, dry_run=False)

        assert r.get("error")
        assert not any("CreateRefund" in p for p in paths)
        assert not any("ActionRefund" in p for p in paths)

    def test_refusal_reports_both_flags_so_the_caller_can_see_why(self):
        import server

        def side_effect(path, payload, **kwargs):
            if "GetOrdersById" in path:
                return [_make_raw_order(processed=True)]
            if "GetRefundOptions" in path:
                return {"RefundOptions": {"CanRefund": False, "CanRefundInternally": True,
                                          "CannotRefundReason": "None"}}
            raise AssertionError(path)

        with patch("server.call_linnworks", side_effect=side_effect):
            r = server.refund_order(GUID, push_to_channel=True, dry_run=False)

        assert r["can_refund"] is False
        assert r["can_refund_internally"] is True


# ── Docstrings state the proof position, not "never live-tested" (AC2) ──────

class TestDocstringsStateProofPosition:

    def test_refund_order_docstring_no_longer_claims_never_live_tested(self):
        import server
        doc = server.refund_order.__doc__
        assert "have not been live-tested" not in doc
        assert "implemented from the Linnworks OpenAPI spec but have not" not in doc

    def test_refund_order_lines_docstring_no_longer_claims_never_live_tested(self):
        import server
        doc = server.refund_order_lines.__doc__
        assert "have not been live-tested" not in doc
        assert "implemented from the Linnworks OpenAPI spec but have not" not in doc

    def test_refund_order_docstring_states_what_is_and_is_not_proven(self):
        import server
        doc = server.refund_order.__doc__
        assert "PROVEN LIVE" in doc
        assert "CreateRefund" in doc
        assert "eligibility gate" in doc.lower()
        assert "NOT PROVEN" in doc
        assert "ActionRefund" in doc

    def test_refund_order_lines_docstring_states_what_is_and_is_not_proven(self):
        import server
        doc = server.refund_order_lines.__doc__
        assert "PROVEN LIVE" in doc
        assert "NOT PROVEN" in doc
        assert "ActionRefund" in doc

    def test_module_docstring_no_longer_claims_never_live_tested(self):
        # __doc__ is this test module's own docstring (the module-level
        # string at the top of this file) -- a plain global reference, no
        # import needed, and robust to whether "tests" is a package.
        assert "have not been live-tested" not in (__doc__ or "")


# ── channel_push_warning: presence, and single source of truth (AC3, AC4) ───

class TestChannelPushWarning:

    def _live_refund_side_effect(self, order, refund_headers=None):
        headers = refund_headers if refund_headers is not None else REFUND_HEADERS_MATCH

        def side_effect(path, payload, **kwargs):
            if "GetOrdersById" in path:
                return [order]
            if "GetRefundOptions" in path:
                return REFUND_OPTIONS_CAN
            if "CreateRefund" in path:
                return CREATE_REFUND_RESP
            if "ActionRefund" in path:
                return ACTION_REFUND_RESP
            if "GetRefundHeadersByOrderId" in path:
                return headers
            raise AssertionError(f"Unexpected path: {path}")

        return side_effect

    @staticmethod
    def _order_on(source):
        """#86 made the push proof PER CHANNEL, so a test must say which
        channel it exercises rather than relying on the fixture's default."""
        import copy
        o = copy.deepcopy(PROCESSED_ORDER)
        o["GeneralInfo"]["Source"] = source
        return o

    def test_no_warning_on_shopify_now_the_push_is_proven_there(self):
        """#86: proven live on Shopify 23 Sep 2026 (order 611711). A caller
        refunding a Shopify order is owed no caution, and carrying one anyway
        would train people to ignore it on the channels that still need it."""
        import server

        with patch("server.call_linnworks",
                   side_effect=self._live_refund_side_effect(self._order_on("Shopify"))):
            result = server.refund_order(GUID, dry_run=False, push_to_channel=True)

        assert "channel_push_warning" not in result

    def test_warning_still_carried_on_amazon_and_ebay(self):
        """The reason this is per-channel and not one flag. #86 proved SHOPIFY
        only; Amazon and eBay are untested, and #45/#47 both ended with
        Linnworks accepting a push that never reached the channel. Flipping a
        single boolean on Shopify's evidence would silence the warning exactly
        where a caller wrongly believing the customer was paid costs money."""
        import server

        for source in ("Amazon", "eBay"):
            with patch("server.call_linnworks",
                       side_effect=self._live_refund_side_effect(self._order_on(source))):
                result = server.refund_order(GUID, dry_run=False, push_to_channel=True)
            assert "channel_push_warning" in result, source
            w = result["channel_push_warning"]
            assert "NEVER been tested on AMAZON or EBAY" in w, source
            assert "not evidence the customer has been paid" in w.lower(), source

    def test_an_unknown_channel_is_treated_as_unproven(self):
        """The safe direction. A source we have no record for must not inherit
        Shopify's proof by silence."""
        import server

        with patch("server.call_linnworks",
                   side_effect=self._live_refund_side_effect(self._order_on("Etsy"))):
            result = server.refund_order(GUID, dry_run=False, push_to_channel=True)

        assert "channel_push_warning" in result

    def test_refund_order_lines_carries_warning_on_an_unproven_channel(self):
        import server

        with patch("server.call_linnworks",
                   side_effect=self._live_refund_side_effect(self._order_on("Amazon"))):
            result = server.refund_order_lines(
                GUID, lines=[{"row_id": ROW_ID_1}], dry_run=False, push_to_channel=True,
            )

        assert "channel_push_warning" in result
        assert "NEVER been tested on AMAZON or EBAY" in result["channel_push_warning"]

    def test_no_warning_when_push_to_channel_is_false(self):
        """No channel push was attempted, so no push warning is owed."""
        import server

        with patch("server.call_linnworks",
                   side_effect=self._live_refund_side_effect(PROCESSED_ORDER)):
            result = server.refund_order(GUID, dry_run=False, push_to_channel=False)

        assert "channel_push_warning" not in result

    def test_changing_the_single_warning_constant_changes_both_tools(self):
        """AC4: the warning text exists in exactly ONE place. Proven by
        changing server.REFUND_CHANNEL_PUSH_WARNING and observing BOTH
        refund_order and refund_order_lines reflect the new text — a test
        that only checks the string appears twice would not prove this."""
        import server

        custom_text = "CUSTOM-WARNING-MARKER-6f3a9c"
        with patch("server.REFUND_CHANNEL_PUSH_WARNING", custom_text), \
             patch("server.call_linnworks",
                   side_effect=self._live_refund_side_effect(self._order_on("Amazon"))):
            r1 = server.refund_order(GUID, dry_run=False, push_to_channel=True)
            r2 = server.refund_order_lines(
                GUID, lines=[{"row_id": ROW_ID_1}], dry_run=False, push_to_channel=True,
            )

        assert r1["channel_push_warning"] == custom_text
        assert r2["channel_push_warning"] == custom_text

    def test_marking_a_channel_proven_removes_the_warning_for_that_channel_only(self):
        """The successor to the old single-flag test, which patched
        REFUND_CHANNEL_PUSH_PROVEN. That constant is now DERIVED and no longer
        consulted, so patching it would pass while proving nothing. The single
        source of truth is the per-channel registry, so drive that -- and
        assert the other channels are UNAFFECTED, which is the whole point."""
        import server

        proven_amazon = {**server.REFUND_PUSH_CHANNELS,
                         "AMAZON": {"state": "proven", "evidence": "test"}}
        with patch.dict(server.REFUND_PUSH_CHANNELS, proven_amazon, clear=True), \
             patch("server.call_linnworks",
                   side_effect=self._live_refund_side_effect(self._order_on("Amazon"))):
            amazon = server.refund_order(GUID, dry_run=False, push_to_channel=True)
        assert "channel_push_warning" not in amazon

        # eBay must still be warned: proving one channel proves only that one.
        with patch.dict(server.REFUND_PUSH_CHANNELS, proven_amazon, clear=True), \
             patch("server.call_linnworks",
                   side_effect=self._live_refund_side_effect(self._order_on("eBay"))):
            ebay = server.refund_order(GUID, dry_run=False, push_to_channel=True)
        assert "channel_push_warning" in ebay

    def test_the_derived_proven_flag_requires_every_channel(self):
        """REFUND_CHANNEL_PUSH_PROVEN is kept as a name but derived, so it can
        never quietly claim more than has been shown. Today Shopify is proven
        and the other two are not, so it must read False."""
        import server

        assert server.REFUND_PUSH_CHANNELS["SHOPIFY"]["state"] == "proven"
        assert server.REFUND_CHANNEL_PUSH_PROVEN is False
        assert server.REFUND_PUSH_CHANNELS["SHOPIFY"]["evidence"]
        assert "611711" in server.REFUND_PUSH_CHANNELS["SHOPIFY"]["evidence"]


# ── actioned keeps its meaning; a new key flags the True+Errors contradiction
# (AC9) ───────────────────────────────────────────────────────────────────────
class TestActionedWithErrorsContradiction:
    ACTION_REFUND_TRUE_WITH_ERRORS = {
        "SuccessfullyActioned": True,
        "RefundHeaderId": 42,
        "RefundReference": "REF-42",
        "Status": {"StatusHeader": "PROCESSED"},
        "CannotRefundReason": "None",
        "Errors": [{"Error": "Channel push partially failed"}],
    }


    def _side_effect(self, order):
        def side_effect(path, payload, **kwargs):
            if "GetOrdersById" in path:
                return [order]
            if "GetRefundOptions" in path:
                return REFUND_OPTIONS_CAN
            if "CreateRefund" in path:
                return CREATE_REFUND_RESP
            if "ActionRefund" in path:
                return self.ACTION_REFUND_TRUE_WITH_ERRORS
            if "GetRefundHeadersByOrderId" in path:
                return REFUND_HEADERS_MATCH
            raise AssertionError(f"Unexpected path: {path}")
        return side_effect

    def test_actioned_key_unchanged_when_true_with_errors(self):
        import server

        with patch("server.call_linnworks", side_effect=self._side_effect(PROCESSED_ORDER)):
            result = server.refund_order(GUID, dry_run=False, push_to_channel=True)

        # `actioned` keeps its existing name, type and value: True, from
        # SuccessfullyActioned, regardless of the Errors array.
        assert result["actioned"] is True
        assert isinstance(result["actioned"], bool)

    def test_new_key_flags_the_contradiction_with_a_warning(self):
        import server

        with patch("server.call_linnworks", side_effect=self._side_effect(PROCESSED_ORDER)):
            result = server.refund_order(GUID, dry_run=False, push_to_channel=True)

        assert result["actioned_with_errors"] is True
        assert "warning" in result
        assert "SuccessfullyActioned" in result["warning"]

    def test_no_contradiction_flag_when_actioned_true_and_no_errors(self):
        import server

        with patch("server.call_linnworks",
                   side_effect=TestChannelPushWarning()._live_refund_side_effect(PROCESSED_ORDER)):
            result = server.refund_order(GUID, dry_run=False, push_to_channel=True)

        assert result["actioned"] is True
        assert result["actioned_with_errors"] is False

    def test_refund_order_lines_also_flags_the_contradiction(self):
        import server

        with patch("server.call_linnworks", side_effect=self._side_effect(PROCESSED_ORDER)):
            result = server.refund_order_lines(
                GUID, lines=[{"row_id": ROW_ID_1}], dry_run=False, push_to_channel=True,
            )

        assert result["actioned"] is True
        assert result["actioned_with_errors"] is True
        assert "warning" in result


# ── Refund read-back outcomes: confirmed / unconfirmed / read_back_failed
# (AC5, AC6) ──────────────────────────────────────────────────────────────────

class TestRefundReadback:

    def _side_effect(self, order, headers_behavior):
        """headers_behavior: a value returned by/raised from
        GetRefundHeadersByOrderId -- a list to return, or an exception
        instance/class to raise."""
        def side_effect(path, payload, **kwargs):
            if "GetOrdersById" in path:
                return [order]
            if "GetRefundOptions" in path:
                return REFUND_OPTIONS_CAN
            if "CreateRefund" in path:
                return CREATE_REFUND_RESP
            if "ActionRefund" in path:
                return ACTION_REFUND_RESP
            if "GetRefundHeadersByOrderId" in path:
                if isinstance(headers_behavior, Exception):
                    raise headers_behavior
                if isinstance(headers_behavior, type) and issubclass(headers_behavior, Exception):
                    raise headers_behavior("boom")
                return headers_behavior
            raise AssertionError(f"Unexpected path: {path}")
        return side_effect

    # -- refund_order --

    def test_refund_order_confirmed_when_header_matches(self):
        import server
        with patch("server.call_linnworks",
                   side_effect=self._side_effect(PROCESSED_ORDER, REFUND_HEADERS_MATCH)):
            result = server.refund_order(GUID, dry_run=False, push_to_channel=True)

        assert result["outcome"] == "confirmed"
        assert result["matched_refund_header"]["RefundHeaderId"] == 42
        assert "error" not in result

    def test_refund_order_unconfirmed_when_no_matching_header(self):
        import server
        with patch("server.call_linnworks",
                   side_effect=self._side_effect(PROCESSED_ORDER, REFUND_HEADERS_EMPTY)):
            result = server.refund_order(GUID, dry_run=False, push_to_channel=True)

        assert result["outcome"] == "unconfirmed"
        assert "error" not in result

    def test_refund_order_read_back_failed_on_non_rate_limit_error(self):
        import server
        with patch("server.call_linnworks",
                   side_effect=self._side_effect(PROCESSED_ORDER, RuntimeError("boom"))):
            result = server.refund_order(GUID, dry_run=False, push_to_channel=True)

        assert result["outcome"] == "read_back_failed"

    # -- refund_order_lines --

    def test_refund_order_lines_confirmed_when_header_matches(self):
        import server
        with patch("server.call_linnworks",
                   side_effect=self._side_effect(PROCESSED_ORDER, REFUND_HEADERS_MATCH)):
            result = server.refund_order_lines(
                GUID, lines=[{"row_id": ROW_ID_1}], dry_run=False,
            )

        assert result["outcome"] == "confirmed"

    def test_refund_order_lines_unconfirmed_when_no_matching_header(self):
        import server
        with patch("server.call_linnworks",
                   side_effect=self._side_effect(PROCESSED_ORDER, REFUND_HEADERS_EMPTY)):
            result = server.refund_order_lines(
                GUID, lines=[{"row_id": ROW_ID_1}], dry_run=False,
            )

        assert result["outcome"] == "unconfirmed"

    def test_refund_order_lines_read_back_failed_on_non_rate_limit_error(self):
        import server
        with patch("server.call_linnworks",
                   side_effect=self._side_effect(PROCESSED_ORDER, RuntimeError("boom"))):
            result = server.refund_order_lines(
                GUID, lines=[{"row_id": ROW_ID_1}], dry_run=False,
            )

        assert result["outcome"] == "read_back_failed"


# ── The unconfirmed message: unmistakable, never "failed" (AC6) ─────────────

class TestUnconfirmedMessage:

    def _side_effect(self, order):
        def side_effect(path, payload, **kwargs):
            if "GetOrdersById" in path:
                return [order]
            if "GetRefundOptions" in path:
                return REFUND_OPTIONS_CAN
            if "CreateRefund" in path:
                return CREATE_REFUND_RESP
            if "ActionRefund" in path:
                return ACTION_REFUND_RESP
            if "GetRefundHeadersByOrderId" in path:
                return REFUND_HEADERS_EMPTY
            raise AssertionError(f"Unexpected path: {path}")
        return side_effect

    def test_unconfirmed_message_instructs_do_not_rerun_and_check_linnworks(self):
        import server
        with patch("server.call_linnworks", side_effect=self._side_effect(PROCESSED_ORDER)):
            result = server.refund_order(GUID, dry_run=False, push_to_channel=True)

        assert result["outcome"] == "unconfirmed"
        message = result["unconfirmed_message"]
        assert "do not re-run" in message.lower()
        assert "linnworks" in message.lower()

    def test_unconfirmed_is_never_reported_as_failed_or_not_created(self):
        import server
        with patch("server.call_linnworks", side_effect=self._side_effect(PROCESSED_ORDER)):
            result = server.refund_order(GUID, dry_run=False, push_to_channel=True)

        assert "error" not in result
        assert result["outcome"] != "failed"
        assert result["outcome"] != "not_created"
        assert result["refund_header_id"] == 42  # the refund WAS created

    def test_refund_order_lines_unconfirmed_message_present(self):
        import server
        with patch("server.call_linnworks", side_effect=self._side_effect(PROCESSED_ORDER)):
            result = server.refund_order_lines(
                GUID, lines=[{"row_id": ROW_ID_1}], dry_run=False,
            )

        assert result["outcome"] == "unconfirmed"
        assert "do not re-run" in result["unconfirmed_message"].lower()
        assert "error" not in result


# ── Rate-limited read-backs: their own outcome, for all three tools (AC7) ───

class TestRateLimitedReadbacks:

    def test_cancel_order_rate_limited_readback(self):
        import server

        counter = {"n": 0}

        def side_effect(path, payload, **kwargs):
            if "GetOrdersById" in path:
                counter["n"] += 1
                if counter["n"] == 1:
                    return [OPEN_ORDER]
                raise server.RateLimitError("quota exceeded")
            if "CancelOrder" in path:
                return "OK"
            raise AssertionError(f"Unexpected path: {path}")

        with patch("server.call_linnworks", side_effect=side_effect):
            result = server.cancel_order(GUID, dry_run=False)

        assert result["outcome"] == "rate_limited"
        assert result["outcome"] != "unconfirmed"
        assert result["outcome"] != "not_cancelled"

    def test_refund_order_rate_limited_readback(self):
        import server

        def side_effect(path, payload, **kwargs):
            if "GetOrdersById" in path:
                return [PROCESSED_ORDER]
            if "GetRefundOptions" in path:
                return REFUND_OPTIONS_CAN
            if "CreateRefund" in path:
                return CREATE_REFUND_RESP
            if "ActionRefund" in path:
                return ACTION_REFUND_RESP
            if "GetRefundHeadersByOrderId" in path:
                raise server.RateLimitError("quota exceeded")
            raise AssertionError(f"Unexpected path: {path}")

        with patch("server.call_linnworks", side_effect=side_effect):
            result = server.refund_order(GUID, dry_run=False, push_to_channel=True)

        assert result["outcome"] == "rate_limited"
        assert result["outcome"] != "unconfirmed"
        assert result["outcome"] != "read_back_failed"
        assert "error" not in result

    def test_refund_order_lines_rate_limited_readback(self):
        import server

        def side_effect(path, payload, **kwargs):
            if "GetOrdersById" in path:
                return [PROCESSED_ORDER]
            if "GetRefundOptions" in path:
                return REFUND_OPTIONS_CAN
            if "CreateRefund" in path:
                return CREATE_REFUND_RESP
            if "ActionRefund" in path:
                return ACTION_REFUND_RESP
            if "GetRefundHeadersByOrderId" in path:
                raise server.RateLimitError("quota exceeded")
            raise AssertionError(f"Unexpected path: {path}")

        with patch("server.call_linnworks", side_effect=side_effect):
            result = server.refund_order_lines(
                GUID, lines=[{"row_id": ROW_ID_1}], dry_run=False,
            )

        assert result["outcome"] == "rate_limited"
        assert result["outcome"] != "unconfirmed"
        assert result["outcome"] != "read_back_failed"


# ── cancel_order read-back: GUID-keying, parked/status ignored, failure
# handling (AC8) ─────────────────────────────────────────────────────────────

class TestCancelOrderReadback:

    def test_readback_uses_resolved_guid_not_numeric_order_id(self):
        import server

        call_log = []

        def get_side(path, params=None, **kwargs):
            call_log.append(("GET", path))
            if "GetOrderDetailsByNumOrderId" in path:
                return _make_raw_order(processed=False)
            raise AssertionError(f"Unexpected GET: {path}")

        after = _make_raw_order(processed=True)

        def post_side(path, payload, **kwargs):
            call_log.append(("POST", path))
            if "GetOrdersById" in path:
                return [after]
            if "CancelOrder" in path:
                return "OK"
            raise AssertionError(f"Unexpected POST: {path}")

        with patch("server.call_linnworks", side_effect=post_side), \
             patch("server.call_linnworks_get", side_effect=get_side):
            result = server.cancel_order("123456", dry_run=False)

        assert result["outcome"] == "cancelled"
        get_calls = [c for c in call_log if c[0] == "GET"]
        post_calls = [c for c in call_log if c[0] == "POST"]
        # Only the initial numeric resolution used the GET route; the
        # read-back must go by GUID (POST GetOrdersById), never a second
        # GetOrderDetailsByNumOrderId lookup.
        assert len(get_calls) == 1
        assert any("GetOrdersById" in p for _, p in post_calls)

    def test_still_parked_and_unchanged_status_still_classified_cancelled(self):
        """Cancelling has been observed to leave an order still reading as
        parked with its old payment status -- only `processed` must decide
        the outcome."""
        import server

        after = _make_raw_order(processed=True)
        after["GeneralInfo"]["IsParked"] = True
        after["GeneralInfo"]["Status"] = 1  # unchanged from before cancellation

        counter = {"n": 0}

        def side_effect(path, payload, **kwargs):
            if "GetOrdersById" in path:
                counter["n"] += 1
                # First call: the pre-write read, confirming the order is
                # still open. Second call: the post-write read-back.
                return [OPEN_ORDER] if counter["n"] == 1 else [after]
            if "CancelOrder" in path:
                return "OK"
            raise AssertionError(f"Unexpected path: {path}")

        with patch("server.call_linnworks", side_effect=side_effect):
            result = server.cancel_order(GUID, dry_run=False)

        assert result["outcome"] == "cancelled"
        assert result["read_back_is_parked"] is True
        assert result["read_back_status"] == 1

    def test_failed_readback_reported_as_unconfirmed_not_failed_cancellation(self):
        import server

        counter = {"n": 0}

        def side_effect(path, payload, **kwargs):
            if "GetOrdersById" in path:
                counter["n"] += 1
                if counter["n"] == 1:
                    return [OPEN_ORDER]
                raise RuntimeError("No order found for GUID")
            if "CancelOrder" in path:
                return "OK"
            raise AssertionError(f"Unexpected path: {path}")

        with patch("server.call_linnworks", side_effect=side_effect):
            result = server.cancel_order(GUID, dry_run=False)

        assert result["outcome"] == "unconfirmed"
        assert result["outcome"] != "not_cancelled"
        # The write itself is not reported as failed -- status keeps its
        # existing meaning ("we performed the cancel call").
        assert result["status"] == "cancelled"


# ── refund_order_lines: quantity-without-amount over-refund guard (AC10) ────

class TestQuantityAmountGuard:

    def test_partial_quantity_without_amount_is_refused(self):
        """quantity=1 against a 3-unit line, no explicit amount: refuse
        rather than silently refunding the WHOLE line's cost."""
        import server

        three_unit_order = _make_raw_order(processed=True)
        three_unit_order["Items"][0]["Quantity"] = 3
        three_unit_order["Items"][0]["CostIncTax"] = 30.00

        def side_effect(path, payload, **kwargs):
            if "GetOrdersById" in path:
                return [three_unit_order]
            raise AssertionError(f"Unexpected write: {path}")

        with patch("server.call_linnworks", side_effect=side_effect):
            result = server.refund_order_lines(
                GUID,
                lines=[{"row_id": ROW_ID_1, "quantity": 1}],
                dry_run=False,
            )

        assert "error" in result
        assert ROW_ID_1 in str(result.get("quantity_mismatch_lines", result["error"]))
        assert "amount" in result["error"].lower()
        # No write must have happened -- the side_effect raises on anything
        # other than the initial GetOrdersById read.

    def test_quantity_matching_full_line_quantity_is_not_refused(self):
        """quantity == the line's own quantity: refunding "all of it" by
        quantity is consistent with the cost-based default, no refusal."""
        import server

        three_unit_order = _make_raw_order(processed=True)
        three_unit_order["Items"][0]["Quantity"] = 3
        three_unit_order["Items"][0]["CostIncTax"] = 30.00

        with patch("server.call_linnworks",
                   side_effect=lambda path, payload, **kw: (
                       [three_unit_order] if "GetOrdersById" in path
                       else (_ for _ in ()).throw(AssertionError(path))
                   )):
            result = server.refund_order_lines(
                GUID,
                lines=[{"row_id": ROW_ID_1, "quantity": 3}],
                dry_run=True,
            )

        assert "error" not in result
        assert result["refund_lines"][0]["amount"] == 30.00

    def test_explicit_amount_bypasses_the_guard_unchanged(self):
        """Behaviour when `amount` IS supplied is unchanged: no refusal,
        even though quantity=1 against a 3-unit line would otherwise
        trigger it."""
        import server

        three_unit_order = _make_raw_order(processed=True)
        three_unit_order["Items"][0]["Quantity"] = 3
        three_unit_order["Items"][0]["CostIncTax"] = 30.00

        with patch("server.call_linnworks",
                   side_effect=lambda path, payload, **kw: (
                       [three_unit_order] if "GetOrdersById" in path
                       else (_ for _ in ()).throw(AssertionError(path))
                   )):
            result = server.refund_order_lines(
                GUID,
                lines=[{"row_id": ROW_ID_1, "quantity": 1, "amount": 10.00}],
                dry_run=True,
            )

        assert "error" not in result
        assert result["refund_lines"][0]["amount"] == 10.00
        assert result["refund_lines"][0]["quantity"] == 1


# ── dry_run stays byte-identical: no read-back, no write call (AC12) ────────

class TestDryRunUnchanged:

    def test_cancel_order_dry_run_makes_exactly_one_call(self):
        import server

        calls = []

        def side_effect(path, payload, **kwargs):
            calls.append(path)
            if "GetOrdersById" in path:
                return [OPEN_ORDER]
            raise AssertionError(f"Unexpected call in dry run: {path}")

        with patch("server.call_linnworks", side_effect=side_effect):
            result = server.cancel_order(GUID, dry_run=True)

        assert len(calls) == 1
        assert "outcome" not in result
        assert set(result.keys()) == {
            "dry_run", "status", "message", "order_id", "num_order_id",
            "customer_name", "customer_email", "reference_num",
            "external_reference", "source", "items", "note",
        }

    def test_refund_order_dry_run_makes_exactly_one_call(self):
        import server

        calls = []

        def side_effect(path, payload, **kwargs):
            calls.append(path)
            if "GetOrdersById" in path:
                return [PROCESSED_ORDER]
            raise AssertionError(f"Unexpected call in dry run: {path}")

        with patch("server.call_linnworks", side_effect=side_effect):
            result = server.refund_order(GUID, dry_run=True)

        assert len(calls) == 1
        assert "outcome" not in result
        assert "channel_push_warning" not in result
        assert set(result.keys()) == {
            "dry_run", "order_id", "num_order_id", "customer_name",
            "customer_email", "reference_num", "external_reference",
            "total_refund", "currency", "push_to_channel", "refund_lines",
            "note", "message",
        }

    def test_refund_order_lines_dry_run_makes_exactly_one_call(self):
        import server

        calls = []

        def side_effect(path, payload, **kwargs):
            calls.append(path)
            if "GetOrdersById" in path:
                return [PROCESSED_ORDER]
            raise AssertionError(f"Unexpected call in dry run: {path}")

        with patch("server.call_linnworks", side_effect=side_effect):
            result = server.refund_order_lines(
                GUID, lines=[{"row_id": ROW_ID_1}], dry_run=True,
            )

        assert len(calls) == 1
        assert "outcome" not in result
        assert "channel_push_warning" not in result
        assert set(result.keys()) == {
            "dry_run", "order_id", "num_order_id", "customer_name",
            "customer_email", "reference_num", "external_reference",
            "total_refund", "currency", "push_to_channel", "refund_lines",
            "note", "message",
        }
