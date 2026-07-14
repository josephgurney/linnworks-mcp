"""
QA tests for find_orders_by_reference.

All tests use unittest.mock — no live Linnworks API calls.
Run with: pytest tests/test_find_orders_by_reference.py -v

Note: OpenOrders/SearchOrders is confirmed in the public Linnworks OpenAPI
spec but had not been live-tested on this tenant as of May 2026.
"""

import sys
import os
import pytest
from unittest.mock import patch, call as mock_call_obj

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ── Fixtures ─────────────────────────────────────────────────────────────────

GUID_1 = "aaaaaaaa-0001-0001-0001-000000000001"
GUID_2 = "bbbbbbbb-0002-0002-0002-000000000002"

# Shape returned by OpenOrders/SearchOrders
SEARCH_RESPONSE_ONE_OPEN = {
    "OpenOrders": [
        {"ViewId": 1, "LocationId": "00000000-0000-0000-0000-000000000000",
         "TotalOrders": 1, "OrderIds": [GUID_1], "Count": 1}
    ],
    "ProcessedOrders": [],
}

SEARCH_RESPONSE_WITH_PROCESSED = {
    "OpenOrders": [
        {"ViewId": 1, "LocationId": "00000000-0000-0000-0000-000000000000",
         "TotalOrders": 1, "OrderIds": [GUID_1], "Count": 1}
    ],
    "ProcessedOrders": [GUID_2],
}

SEARCH_RESPONSE_EMPTY = {
    "OpenOrders": [],
    "ProcessedOrders": [],
}

SEARCH_RESPONSE_DUPLICATE_GUIDS = {
    "OpenOrders": [
        {"ViewId": 1, "OrderIds": [GUID_1]},
        {"ViewId": 2, "OrderIds": [GUID_1]},  # same GUID in two views
    ],
    "ProcessedOrders": [],
}

# Minimal order detail returned by GetOrdersById
def _make_order_detail(guid, num_id=596475, ref="11177274", ext_ref="11177274",
                       source="Shopify", processed=False, customer="Jane Smith",
                       email="jane@example.com"):
    return {
        "OrderId": guid,
        "NumOrderId": num_id,
        "Processed": processed,
        "GeneralInfo": {
            "Status": 1,
            "ReferenceNum": ref,
            "ExternalReference": ext_ref,
            "Source": source,
            "SubSource": source,
            "IsParked": False,
            "Marker": 0,
            "ReceivedDate": "2026-05-01T09:00:00",
        },
        "ShippingInfo": {},
        "CustomerInfo": {
            "ChannelBuyerName": customer,
            "Address": {
                "FullName": customer,
                "EmailAddress": email,
            },
            "BillingAddress": {},
        },
        "Items": [],
        "Notes": [],
    }


DETAIL_ORDER_1 = _make_order_detail(GUID_1)
DETAIL_ORDER_2 = _make_order_detail(GUID_2, num_id=596476, processed=True)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _post_side_effect(search_resp, detail_resp):
    """Returns a side_effect function for call_linnworks that routes by path."""
    def side_effect(path, *args, **kwargs):
        if "SearchOrders" in path:
            return search_resp
        if "GetOrdersById" in path:
            return detail_resp
        raise AssertionError(f"Unexpected call_linnworks path: {path}")
    return side_effect


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestFindOrdersByReference:

    def test_strips_leading_hash(self):
        """#11177274 and 11177274 should produce the same search term."""
        import server

        captured = {}

        def side_effect(path, payload, **kwargs):
            if "SearchOrders" in path:
                captured["term"] = payload.get("SearchTerm")
                return SEARCH_RESPONSE_EMPTY
            return []

        with patch("server.call_linnworks", side_effect=side_effect):
            server.find_orders_by_reference("#11177274")

        assert captured["term"] == "11177274"

    def test_empty_reference_returns_error(self):
        import server
        result = server.find_orders_by_reference("#")
        assert "error" in result

    def test_returns_zero_when_no_match(self):
        import server
        with patch("server.call_linnworks",
                   side_effect=_post_side_effect(SEARCH_RESPONSE_EMPTY, [])):
            result = server.find_orders_by_reference("NOTFOUND")

        assert result["match_count"] == 0
        assert result["orders"] == []
        assert result["reference"] == "NOTFOUND"

    def test_returns_matching_open_order(self):
        import server
        with patch("server.call_linnworks",
                   side_effect=_post_side_effect(SEARCH_RESPONSE_ONE_OPEN, [DETAIL_ORDER_1])):
            result = server.find_orders_by_reference("11177274")

        assert result["match_count"] == 1
        assert result["orders"][0]["order_id"] == GUID_1
        assert result["orders"][0]["customer_name"] == "Jane Smith"
        assert result["orders"][0]["customer_email"] == "jane@example.com"
        assert result["orders"][0]["external_reference"] == "11177274"

    def test_processed_orders_excluded_by_default(self):
        """ProcessedOrders GUIDs must not be fetched when include_processed=False."""
        import server

        def side_effect(path, payload, **kwargs):
            if "SearchOrders" in path:
                return SEARCH_RESPONSE_WITH_PROCESSED
            if "GetOrdersById" in path:
                guids = payload.get("pkOrderIds", [])
                # Processed GUID must not be in the request
                assert GUID_2 not in guids, "Processed order GUID should not be fetched"
                return [DETAIL_ORDER_1]
            return []

        with patch("server.call_linnworks", side_effect=side_effect):
            result = server.find_orders_by_reference("11177274", include_processed=False)

        # Only the open order should come back
        assert result["match_count"] == 1
        assert result["orders"][0]["order_id"] == GUID_1

    def test_processed_orders_included_when_flag_set(self):
        """With include_processed=True both open and processed GUIDs are fetched."""
        import server

        def side_effect(path, payload, **kwargs):
            if "SearchOrders" in path:
                return SEARCH_RESPONSE_WITH_PROCESSED
            if "GetOrdersById" in path:
                return [DETAIL_ORDER_1, DETAIL_ORDER_2]
            return []

        with patch("server.call_linnworks", side_effect=side_effect):
            result = server.find_orders_by_reference("11177274", include_processed=True)

        assert result["match_count"] == 2
        order_ids = {o["order_id"] for o in result["orders"]}
        assert GUID_1 in order_ids
        assert GUID_2 in order_ids

    def test_include_processed_sent_in_payload(self):
        """The IncludeProcessed flag must be forwarded to SearchOrders."""
        import server

        captured = {}

        def side_effect(path, payload, **kwargs):
            if "SearchOrders" in path:
                captured["inc"] = payload.get("IncludeProcessed")
                return SEARCH_RESPONSE_EMPTY
            return []

        with patch("server.call_linnworks", side_effect=side_effect):
            server.find_orders_by_reference("ref", include_processed=True)

        assert captured["inc"] is True

    def test_duplicate_guids_across_views_deduped(self):
        """The same GUID appearing in multiple OpenOrders views must only be fetched once."""
        import server

        batch_guids = []

        def side_effect(path, payload, **kwargs):
            if "SearchOrders" in path:
                return SEARCH_RESPONSE_DUPLICATE_GUIDS
            if "GetOrdersById" in path:
                batch_guids.extend(payload.get("pkOrderIds", []))
                return [DETAIL_ORDER_1]
            return []

        with patch("server.call_linnworks", side_effect=side_effect):
            server.find_orders_by_reference("11177274")

        assert batch_guids.count(GUID_1) == 1, "GUID_1 should only be fetched once"

    def test_search_orders_payload_is_unwrapped(self):
        """SearchOrders must be sent UNWRAPPED — the {'request': {...}} wrapper
        returns HTTP 400 'Must provide a search term.' (live-tested 15 Jun 2026,
        see CLAUDE.md confirmed-endpoints table)."""
        import server

        captured = {}

        def side_effect(path, payload, **kwargs):
            if "SearchOrders" in path:
                captured["payload"] = payload
                return SEARCH_RESPONSE_EMPTY
            return []

        with patch("server.call_linnworks", side_effect=side_effect):
            server.find_orders_by_reference("123")

        assert "request" not in captured["payload"], (
            "SearchOrders payload must be UNWRAPPED — the wrapper 400s "
            "'Must provide a search term.'"
        )
        assert captured["payload"]["SearchTerm"] == "123"
        assert "LocationId" in captured["payload"]
        assert "IncludeProcessed" in captured["payload"]

    def test_result_includes_expected_fields(self):
        """Each order in results must include the documented fields."""
        import server
        with patch("server.call_linnworks",
                   side_effect=_post_side_effect(SEARCH_RESPONSE_ONE_OPEN, [DETAIL_ORDER_1])):
            result = server.find_orders_by_reference("11177274")

        order = result["orders"][0]
        for field in [
            "order_id", "num_order_id", "reference_num", "external_reference",
            "source", "sub_source", "status", "processed", "received_date",
            "customer_name", "customer_email",
        ]:
            assert field in order, f"Missing field: {field}"

    def test_reference_echoed_in_result(self):
        import server
        with patch("server.call_linnworks",
                   side_effect=_post_side_effect(SEARCH_RESPONSE_EMPTY, [])):
            result = server.find_orders_by_reference("#SHOPIFY-001")

        assert result["reference"] == "SHOPIFY-001"  # # stripped

    def test_batches_large_guid_lists(self):
        """More than 50 GUIDs must be sent in batches of 50 to GetOrdersById."""
        import server

        many_guids = [f"guid{i:04d}-0000-0000-0000-000000000000" for i in range(75)]
        search_resp = {
            "OpenOrders": [{"OrderIds": many_guids}],
            "ProcessedOrders": [],
        }

        batches_seen = []

        def side_effect(path, payload, **kwargs):
            if "SearchOrders" in path:
                return search_resp
            if "GetOrdersById" in path:
                batches_seen.append(len(payload.get("pkOrderIds", [])))
                return []
            return []

        with patch("server.call_linnworks", side_effect=side_effect):
            server.find_orders_by_reference("test")

        assert len(batches_seen) == 2, "Expected 2 batches for 75 GUIDs"
        assert batches_seen[0] == 50
        assert batches_seen[1] == 25


# ── _format_order_detail external_reference field ────────────────────────────

def test_format_order_detail_includes_external_reference():
    """_format_order_detail must surface external_reference from GeneralInfo."""
    import server

    raw = {
        "OrderId": GUID_1,
        "NumOrderId": 1,
        "Processed": False,
        "GeneralInfo": {
            "ReferenceNum": "LW-001",
            "ExternalReference": "SHOPIFY-11177274",
        },
        "ShippingInfo": {},
        "CustomerInfo": {"Address": {}, "BillingAddress": {}},
        "Items": [],
    }
    result = server._format_order_detail(raw)
    assert result["external_reference"] == "SHOPIFY-11177274"


def test_format_order_detail_external_reference_absent():
    """external_reference should be None when not present."""
    import server

    raw = {
        "OrderId": GUID_1,
        "NumOrderId": 1,
        "Processed": False,
        "GeneralInfo": {},
        "ShippingInfo": {},
        "CustomerInfo": {"Address": {}, "BillingAddress": {}},
        "Items": [],
    }
    result = server._format_order_detail(raw)
    assert result["external_reference"] is None


# ── version ───────────────────────────────────────────────────────────────────

def test_version_is_at_least_1_8_0():
    import server
    major, minor, patch = (int(x) for x in server.__version__.split("."))
    assert (major, minor, patch) >= (1, 8, 0), f"Expected >= 1.8.0, got {server.__version__}"
