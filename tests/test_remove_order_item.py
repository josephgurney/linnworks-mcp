"""
QA tests for remove_order_item (issue #57).

All tests use unittest.mock — no live Linnworks API calls.
Run with: pytest tests/test_remove_order_item.py -v

NOTE: Orders/RemoveOrderItem has only ever been probed for existence with a
deliberately invalid payload against the zero GUID (issue #52). It has never
been called against a real order. These tests prove the tool's own logic —
resolution, refusals, payload shape, and read-back classification — and
nothing about whether Linnworks actually removes the line. See CLAUDE.md's
post_merge_verification checklist for the owner-run live proof.
"""
import sys
import os
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server


# ── Fixtures ─────────────────────────────────────────────────────────────────

GUID = "aaaaaaaa-1234-1234-1234-000000000002"
GUID_LOC = "bbbbbbbb-0000-0000-0000-000000000001"
ROW_ID_1 = "cccccccc-1111-1111-1111-000000000001"
ROW_ID_2 = "dddddddd-2222-2222-2222-000000000002"
ROW_ID_3 = "eeeeeeee-3333-3333-3333-000000000003"


def _order(processed=False, items=None, total=25.0):
    return {
        "OrderId": GUID,
        "NumOrderId": 654321,
        "Processed": processed,
        "FulfilmentLocationId": GUID_LOC,
        "GeneralInfo": {
            "ReferenceNum": "REF057",
            "ExternalReference": "EXT057",
            "Source": "Shopify",
            "SubSource": "Shopify",
            "Status": 1,
            "IsParked": False,
            "Marker": 0,
            "ReceivedDate": "2026-09-01T10:00:00",
        },
        "ShippingInfo": {"PostalServiceName": "Royal Mail", "TrackingNumber": None},
        "CustomerInfo": {
            "ChannelBuyerName": "Test Buyer",
            "Address": {"FullName": "Test Buyer", "EmailAddress": "test@example.com"},
            "BillingAddress": {},
        },
        "TotalsInfo": {
            "Subtotal": 20.0,
            "PostageCost": 5.0,
            "Tax": 0.0,
            "TotalCharge": total,
            "Currency": "GBP",
            "TotalDiscount": 0.0,
        },
        "Items": items if items is not None else [
            {
                "StockItemId": "item-guid-a",
                "SKU": "TEST-SKU-A",
                "Title": "Test Product A",
                "Quantity": 2,
                "RowId": ROW_ID_1,
                "PricePerUnit": 5.0,
                "CostIncTax": 10.0,
            },
            {
                "StockItemId": "item-guid-b",
                "SKU": "TEST-SKU-B",
                "Title": "Test Product B",
                "Quantity": 1,
                "RowId": ROW_ID_2,
                "PricePerUnit": 10.0,
                "CostIncTax": 10.0,
            },
        ],
        "Notes": [],
    }


TWO_LINE_ORDER = _order()
PROCESSED_ORDER = _order(processed=True)

ONE_LINE_ORDER = _order(items=[
    {
        "StockItemId": "item-guid-a",
        "SKU": "TEST-SKU-A",
        "Title": "Test Product A",
        "Quantity": 1,
        "RowId": ROW_ID_1,
        "PricePerUnit": 5.0,
        "CostIncTax": 5.0,
    },
], total=5.0)

COMPOSITE_ORDER = _order(items=[
    {
        "StockItemId": "item-guid-bundle",
        "SKU": "BUNDLE-SKU",
        "Title": "Bundle Product",
        "Quantity": 1,
        "RowId": ROW_ID_3,
        "PricePerUnit": 20.0,
        "CostIncTax": 20.0,
        "CompositeSubItems": [
            {"SKU": "CHILD-SKU-1", "Quantity": 2},
            {"SKU": "CHILD-SKU-2", "Quantity": 1},
        ],
    },
    {
        "StockItemId": "item-guid-b",
        "SKU": "TEST-SKU-B",
        "Title": "Test Product B",
        "Quantity": 1,
        "RowId": ROW_ID_2,
        "PricePerUnit": 10.0,
        "CostIncTax": 10.0,
    },
])


def _post_side_effect(order):
    def _side(path, payload, **kwargs):
        if "GetOrdersById" in path:
            return [order]
        raise AssertionError(f"Unexpected call_linnworks path: {path}")
    return _side


def _with_removed_line(order, row_id):
    """A fresh copy of `order` with the given row_id's line stripped out."""
    after = dict(order)
    after["Items"] = [i for i in order["Items"] if i["RowId"] != row_id]
    return after


# ── dry run makes no write call ───────────────────────────────────────────────

class TestDryRun:

    def test_dry_run_makes_no_write_call(self):
        calls = []

        def side(path, payload, **kwargs):
            calls.append(path)
            if "GetOrdersById" in path:
                return [TWO_LINE_ORDER]
            raise AssertionError(f"Unexpected call: {path}")

        with patch("server.call_linnworks", side_effect=side):
            result = server.remove_order_item(GUID, ROW_ID_1, dry_run=True)

        assert result["dry_run"] is True
        assert not any("RemoveOrderItem" in p for p in calls)

    def test_dry_run_manifest_names_order_and_targeted_line(self):
        with patch("server.call_linnworks", side_effect=_post_side_effect(TWO_LINE_ORDER)):
            result = server.remove_order_item(GUID, ROW_ID_1, dry_run=True)

        assert result["order_id"] == GUID
        assert result["num_order_id"] == 654321
        assert result["customer_name"] == "Test Buyer"
        assert result["reference_num"] == "REF057"
        assert result["removed_line"]["sku"] == "TEST-SKU-A"
        assert result["removed_line"]["title"] == "Test Product A"
        assert result["removed_line"]["quantity"] == 2
        assert result["removed_line"]["price_per_unit"] == 5.0
        assert result["removed_line"]["line_total"] == 10.0


# ── numeric and GUID id resolution ────────────────────────────────────────────

class TestOrderIdResolution:

    def test_guid_order_id_resolves_via_get_orders_by_id(self):
        with patch("server.call_linnworks", side_effect=_post_side_effect(TWO_LINE_ORDER)):
            result = server.remove_order_item(GUID, ROW_ID_1, dry_run=True)

        assert result["order_id"] == GUID

    def test_numeric_order_id_resolves_via_get_order_details(self):
        def get_side(path, params=None, **kwargs):
            if "GetOrderDetailsByNumOrderId" in path:
                return TWO_LINE_ORDER
            raise AssertionError(f"Unexpected GET: {path}")

        with patch("server.call_linnworks_get", side_effect=get_side):
            result = server.remove_order_item("654321", ROW_ID_1, dry_run=True)

        assert result["order_id"] == GUID
        assert result["num_order_id"] == 654321

    def test_unresolvable_order_id_returns_error_dict_not_raise(self):
        with patch("server.call_linnworks", return_value=[]):
            result = server.remove_order_item(GUID, ROW_ID_1, dry_run=True)

        assert "error" in result


# ── unknown row_id ─────────────────────────────────────────────────────────────

class TestUnknownRowId:

    def test_unknown_row_id_returns_error_naming_it_and_no_write(self):
        calls = []

        def side(path, payload, **kwargs):
            calls.append(path)
            if "GetOrdersById" in path:
                return [TWO_LINE_ORDER]
            raise AssertionError(f"Unexpected call: {path}")

        with patch("server.call_linnworks", side_effect=side):
            result = server.remove_order_item(GUID, "not-a-real-row-id", dry_run=False)

        assert "error" in result
        assert "not-a-real-row-id" in result["error"]
        assert "get_order" in result["error"].lower()
        assert not any("RemoveOrderItem" in p for p in calls)


# ── processed-order refusal ────────────────────────────────────────────────────

class TestProcessedOrderRefusal:

    def test_processed_order_is_refused_with_no_write(self):
        calls = []

        def side(path, payload, **kwargs):
            calls.append(path)
            if "GetOrdersById" in path:
                return [PROCESSED_ORDER]
            raise AssertionError(f"Unexpected call: {path}")

        with patch("server.call_linnworks", side_effect=side):
            result = server.remove_order_item(GUID, ROW_ID_1, dry_run=False)

        assert "error" in result
        assert "processed" in result["error"].lower()
        assert not any("RemoveOrderItem" in p for p in calls)


# ── last-line block and opt-in ─────────────────────────────────────────────────

class TestLastLineBlock:

    def test_last_line_removal_is_blocked_by_default(self):
        calls = []

        def side(path, payload, **kwargs):
            calls.append(path)
            if "GetOrdersById" in path:
                return [ONE_LINE_ORDER]
            raise AssertionError(f"Unexpected call: {path}")

        with patch("server.call_linnworks", side_effect=side):
            result = server.remove_order_item(GUID, ROW_ID_1, dry_run=False)

        assert "error" in result
        assert "last remaining line" in result["error"].lower()
        assert not any("RemoveOrderItem" in p for p in calls)

    def test_last_line_removal_proceeds_with_explicit_opt_in(self):
        after = _with_removed_line(ONE_LINE_ORDER, ROW_ID_1)
        counter = {"n": 0}

        def side(path, payload, **kwargs):
            if "GetOrdersById" in path:
                counter["n"] += 1
                return [ONE_LINE_ORDER] if counter["n"] == 1 else [after]
            if "RemoveOrderItem" in path:
                return {}
            raise AssertionError(f"Unexpected call: {path}")

        with patch("server.call_linnworks", side_effect=side):
            result = server.remove_order_item(
                GUID, ROW_ID_1, allow_empty_order=True, dry_run=False
            )

        assert "error" not in result
        assert result["outcome"] == "removed"
        assert result["remaining_line_count"] == 0


# ── composite-parent manifest ──────────────────────────────────────────────────

class TestCompositeParentManifest:

    def test_dry_run_lists_composite_children_sku_and_quantity(self):
        with patch("server.call_linnworks", side_effect=_post_side_effect(COMPOSITE_ORDER)):
            result = server.remove_order_item(GUID, ROW_ID_3, dry_run=True)

        children = result["removed_line"]["composite_children"]
        assert len(children) == 2
        by_sku = {c["sku"]: c["quantity"] for c in children}
        assert by_sku == {"CHILD-SKU-1": 2, "CHILD-SKU-2": 1}

    def test_non_composite_line_has_no_composite_children_key(self):
        with patch("server.call_linnworks", side_effect=_post_side_effect(COMPOSITE_ORDER)):
            result = server.remove_order_item(GUID, ROW_ID_2, dry_run=True)

        assert "composite_children" not in result["removed_line"]


# ── live write payload (AC8) ────────────────────────────────────────────────────

class TestLiveWritePayload:

    def test_payload_uses_orders_own_fulfilment_location_by_default(self):
        payload_sent = {}

        def side(path, payload, **kwargs):
            if "GetOrdersById" in path:
                return [TWO_LINE_ORDER]
            if "RemoveOrderItem" in path:
                payload_sent.update(payload)
                return {}
            raise AssertionError(f"Unexpected call: {path}")

        with patch("server.call_linnworks", side_effect=side):
            server.remove_order_item(GUID, ROW_ID_1, dry_run=False)

        assert payload_sent == {
            "orderId": GUID,
            "rowid": ROW_ID_1,
            "fulfilmentCenter": GUID_LOC,
        }

    def test_location_id_override_takes_precedence(self):
        payload_sent = {}
        override = "ffffffff-0000-0000-0000-000000000009"

        def side(path, payload, **kwargs):
            if "GetOrdersById" in path:
                return [TWO_LINE_ORDER]
            if "RemoveOrderItem" in path:
                payload_sent.update(payload)
                return {}
            raise AssertionError(f"Unexpected call: {path}")

        with patch("server.call_linnworks", side_effect=side):
            server.remove_order_item(GUID, ROW_ID_1, location_id=override, dry_run=False)

        assert payload_sent["fulfilmentCenter"] == override

    def test_falls_back_to_default_location_when_order_carries_none(self):
        no_loc_order = dict(TWO_LINE_ORDER)
        no_loc_order["FulfilmentLocationId"] = None
        payload_sent = {}

        def side(path, payload, **kwargs):
            if "GetOrdersById" in path:
                return [no_loc_order]
            if "RemoveOrderItem" in path:
                payload_sent.update(payload)
                return {}
            raise AssertionError(f"Unexpected call: {path}")

        with patch("server.call_linnworks", side_effect=side):
            server.remove_order_item(GUID, ROW_ID_1, dry_run=False)

        assert payload_sent["fulfilmentCenter"] == server.DEFAULT_LOCATION_ID


# ── read-back classification ───────────────────────────────────────────────────

class TestReadBackClassification:

    def test_successful_removal_is_classified_removed(self):
        after = _with_removed_line(TWO_LINE_ORDER, ROW_ID_1)
        counter = {"n": 0}

        def side(path, payload, **kwargs):
            if "GetOrdersById" in path:
                counter["n"] += 1
                return [TWO_LINE_ORDER] if counter["n"] == 1 else [after]
            if "RemoveOrderItem" in path:
                return {}
            raise AssertionError(f"Unexpected call: {path}")

        with patch("server.call_linnworks", side_effect=side):
            result = server.remove_order_item(GUID, ROW_ID_1, dry_run=False)

        assert result["outcome"] == "removed"
        assert result["remaining_line_count"] == 1
        assert "error" not in result

    def test_readback_still_showing_the_line_is_classified_still_present(self):
        """The API accepted the call (2xx) but a fresh read shows the line
        unchanged — a 2xx must never be reported as 'removed'."""
        def side(path, payload, **kwargs):
            if "GetOrdersById" in path:
                return [TWO_LINE_ORDER]
            if "RemoveOrderItem" in path:
                return {}
            raise AssertionError(f"Unexpected call: {path}")

        with patch("server.call_linnworks", side_effect=side):
            result = server.remove_order_item(GUID, ROW_ID_1, dry_run=False)

        assert result["outcome"] == "still_present"
        assert "warning" in result

    def test_readback_is_a_fresh_call_not_the_prewrite_object(self):
        counter = {"n": 0}

        def side(path, payload, **kwargs):
            if "GetOrdersById" in path:
                counter["n"] += 1
                return [TWO_LINE_ORDER]
            if "RemoveOrderItem" in path:
                return {}
            raise AssertionError(f"Unexpected call: {path}")

        with patch("server.call_linnworks", side_effect=side):
            server.remove_order_item(GUID, ROW_ID_1, dry_run=False)

        # 1 pre-write read + 1 post-write read-back = 2 GetOrdersById calls.
        assert counter["n"] == 2

    def test_numeric_order_id_readback_still_goes_by_guid(self):
        call_log = []

        def get_side(path, params=None, **kwargs):
            call_log.append(("GET", path))
            if "GetOrderDetailsByNumOrderId" in path:
                return TWO_LINE_ORDER
            raise AssertionError(f"Unexpected GET: {path}")

        after = _with_removed_line(TWO_LINE_ORDER, ROW_ID_1)

        def post_side(path, payload, **kwargs):
            call_log.append(("POST", path))
            if "GetOrdersById" in path:
                return [after]
            if "RemoveOrderItem" in path:
                return {}
            raise AssertionError(f"Unexpected POST: {path}")

        with patch("server.call_linnworks", side_effect=post_side), \
             patch("server.call_linnworks_get", side_effect=get_side):
            result = server.remove_order_item("654321", ROW_ID_1, dry_run=False)

        assert result["outcome"] == "removed"
        # Only the initial numeric resolution used the GET route; the
        # read-back must go by GUID (POST GetOrdersById), not a second
        # GetOrderDetailsByNumOrderId call.
        get_calls = [c for c in call_log if c[0] == "GET"]
        post_calls = [c for c in call_log if c[0] == "POST"]
        assert len(get_calls) == 1
        assert any("GetOrdersById" in p for _, p in post_calls)

    def test_readback_failure_is_unconfirmed_never_success_or_not_found(self):
        counter = {"n": 0}

        def side(path, payload, **kwargs):
            if "GetOrdersById" in path:
                counter["n"] += 1
                if counter["n"] == 1:
                    return [TWO_LINE_ORDER]
                raise RuntimeError("No order found for GUID")
            if "RemoveOrderItem" in path:
                return {}
            raise AssertionError(f"Unexpected call: {path}")

        with patch("server.call_linnworks", side_effect=side):
            result = server.remove_order_item(GUID, ROW_ID_1, dry_run=False)

        assert result["outcome"] == "unconfirmed"
        assert "line not found" not in result.get("unconfirmed_reason", "").lower()
        assert "removed" != result["outcome"]


# ── rate-limited path (AC11) ────────────────────────────────────────────────────

class TestRateLimiting:

    def test_rate_limit_resolving_order_reports_rate_limited_outcome(self):
        with patch("server.call_linnworks",
                   side_effect=server.RateLimitError("quota exceeded")):
            result = server.remove_order_item(GUID, ROW_ID_1, dry_run=True)

        assert result["outcome"] == "rate_limited"
        assert "error" in result

    def test_rate_limit_on_write_is_not_reported_as_removal_failure(self):
        def side(path, payload, **kwargs):
            if "GetOrdersById" in path:
                return [TWO_LINE_ORDER]
            if "RemoveOrderItem" in path:
                raise server.RateLimitError("quota exceeded")
            raise AssertionError(f"Unexpected call: {path}")

        with patch("server.call_linnworks", side_effect=side):
            result = server.remove_order_item(GUID, ROW_ID_1, dry_run=False)

        assert result["outcome"] == "rate_limited"

    def test_rate_limit_on_readback_is_reported_via_unconfirmed_with_reason(self):
        """AC10 mandates 'unconfirmed' (not silent success, not 'not found')
        for a failed-or-rate-limited read-back specifically; the reason text
        still identifies it as a rate-limit condition, satisfying AC11's
        'distinct from a not-found or failed-removal outcome' for this call
        site too."""
        counter = {"n": 0}

        def side(path, payload, **kwargs):
            if "GetOrdersById" in path:
                counter["n"] += 1
                if counter["n"] == 1:
                    return [TWO_LINE_ORDER]
                raise server.RateLimitError("quota exceeded")
            if "RemoveOrderItem" in path:
                return {}
            raise AssertionError(f"Unexpected call: {path}")

        with patch("server.call_linnworks", side_effect=side):
            result = server.remove_order_item(GUID, ROW_ID_1, dry_run=False)

        assert result["outcome"] == "unconfirmed"
        assert "rate_limited" in result["unconfirmed_reason"].lower()


# ── unproven-endpoint warning (AC12) and order totals (AC13) ───────────────────

class TestUnprovenWarningAndTotals:

    def test_live_run_carries_unproven_warning_naming_find_unlinked_order_lines(self):
        def side(path, payload, **kwargs):
            if "GetOrdersById" in path:
                return [TWO_LINE_ORDER]
            if "RemoveOrderItem" in path:
                return {}
            raise AssertionError(f"Unexpected call: {path}")

        with patch("server.call_linnworks", side_effect=side):
            result = server.remove_order_item(GUID, ROW_ID_1, dry_run=False)

        assert "unproven_warning" in result
        assert "find_unlinked_order_lines" in result["unproven_warning"]
        assert "Linnworks UI" in result["unproven_warning"]

    def test_order_total_before_and_after_are_both_reported(self):
        after = _with_removed_line(TWO_LINE_ORDER, ROW_ID_1)
        # Simulate Linnworks NOT recalculating the total after removal.
        after["TotalsInfo"] = dict(TWO_LINE_ORDER["TotalsInfo"])
        counter = {"n": 0}

        def side(path, payload, **kwargs):
            if "GetOrdersById" in path:
                counter["n"] += 1
                return [TWO_LINE_ORDER] if counter["n"] == 1 else [after]
            if "RemoveOrderItem" in path:
                return {}
            raise AssertionError(f"Unexpected call: {path}")

        with patch("server.call_linnworks", side_effect=side):
            result = server.remove_order_item(GUID, ROW_ID_1, dry_run=False)

        assert result["order_total_before"] == 25.0
        assert result["order_total_after"] == 25.0
