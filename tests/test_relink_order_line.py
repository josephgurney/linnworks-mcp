"""
QA tests for relink_order_line (issue #56).

All tests use unittest.mock — no live Linnworks API calls.
Run with: pytest tests/test_relink_order_line.py -v

NOTE: Orders/UpdateOrderItem has only ever been probed for existence with a
deliberately invalid payload against the zero GUID (issue #52). It has never
been called against a real order. These tests prove the tool's own logic —
resolution, refusals, payload construction and integrity, and read-back
classification — and nothing about whether Linnworks actually restores the
channel link. See CLAUDE.md's post_merge_verification checklist for the
owner-run live proof.
"""
import sys
import os
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server


# ── Fixtures ─────────────────────────────────────────────────────────────────

GUID = "aaaaaaaa-1234-1234-1234-000000000056"
GUID_LOC = "bbbbbbbb-0000-0000-0000-000000000056"
ROW_ID_1 = "cccccccc-1111-1111-1111-000000000056"   # orphaned/unlinked line
ROW_ID_2 = "dddddddd-2222-2222-2222-000000000056"   # already-linked line

NEW_CHANNEL_LINE_ID = "16506671005945"


def _unlinked_item(row_id=ROW_ID_1, extra=None):
    item = {
        "StockItemId": "item-guid-a",
        "SKU": "TEST-SKU-A",
        "Title": "Test Product A",
        "Quantity": 2,
        "RowId": row_id,
        "PricePerUnit": 5.0,
        "CostIncTax": 10.0,
        "ItemNumber": "TEST-SKU-A",   # orphaned: falls back to the bare SKU
        "ItemSource": "",             # orphaned: present, blank
        "ChannelSKU": "TEST-SKU-A",
    }
    if extra:
        item.update(extra)
    return item


def _linked_item(row_id=ROW_ID_2):
    return {
        "StockItemId": "item-guid-b",
        "SKU": "TEST-SKU-B",
        "Title": "Test Product B",
        "Quantity": 1,
        "RowId": row_id,
        "PricePerUnit": 10.0,
        "CostIncTax": 10.0,
        "ItemNumber": "16506671005999",
        "ItemSource": "SHOPIFY",      # already linked
        "ChannelSKU": "TEST-SKU-B",
    }


def _order(processed=False, items=None):
    return {
        "OrderId": GUID,
        "NumOrderId": 656056,
        "Processed": processed,
        "FulfilmentLocationId": GUID_LOC,
        "GeneralInfo": {
            "ReferenceNum": "REF056",
            "ExternalReference": "EXT056",
            "Source": "SHOPIFY",
            "SubSource": "SWH Shopify",
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
            "TotalCharge": 25.0,
            "Currency": "GBP",
            "TotalDiscount": 0.0,
        },
        "Items": items if items is not None else [_unlinked_item(), _linked_item()],
        "Notes": [],
    }


BASE_ORDER = _order()
PROCESSED_ORDER = _order(processed=True)


def _post_side_effect(order):
    def _side(path, payload, **kwargs):
        if "GetOrdersById" in path:
            return [order]
        raise AssertionError(f"Unexpected call_linnworks path: {path}")
    return _side


def _with_relinked_line(order, row_id, item_number, item_source):
    """A fresh copy of `order` with the given row_id's identity fields updated."""
    after = dict(order)
    after["Items"] = [
        {**i, "ItemNumber": item_number, "ItemSource": item_source}
        if i["RowId"] == row_id else i
        for i in order["Items"]
    ]
    return after


# ── order resolution and row lookup (AC2) ────────────────────────────────────

class TestOrderResolutionAndRowLookup:

    def test_guid_order_id_resolves_via_get_orders_by_id(self):
        with patch("server.call_linnworks", side_effect=_post_side_effect(BASE_ORDER)):
            result = server.relink_order_line(
                GUID, ROW_ID_1, channel_line_id=NEW_CHANNEL_LINE_ID, dry_run=True
            )

        assert result["order_id"] == GUID

    def test_numeric_order_id_resolves_via_get_order_details(self):
        def get_side(path, params=None, **kwargs):
            if "GetOrderDetailsByNumOrderId" in path:
                return BASE_ORDER
            raise AssertionError(f"Unexpected GET: {path}")

        with patch("server.call_linnworks_get", side_effect=get_side):
            result = server.relink_order_line(
                "656056", ROW_ID_1, channel_line_id=NEW_CHANNEL_LINE_ID, dry_run=True
            )

        assert result["order_id"] == GUID
        assert result["num_order_id"] == 656056

    def test_unresolvable_order_id_returns_error_dict_not_raise(self):
        with patch("server.call_linnworks", return_value=[]):
            result = server.relink_order_line(
                GUID, ROW_ID_1, channel_line_id=NEW_CHANNEL_LINE_ID, dry_run=True
            )

        assert "error" in result

    def test_unknown_row_id_returns_error_naming_it_and_no_write(self):
        calls = []

        def side(path, payload, **kwargs):
            calls.append(path)
            if "GetOrdersById" in path:
                return [BASE_ORDER]
            raise AssertionError(f"Unexpected call: {path}")

        with patch("server.call_linnworks", side_effect=side):
            result = server.relink_order_line(
                GUID, "not-a-real-row-id", channel_line_id=NEW_CHANNEL_LINE_ID, dry_run=False
            )

        assert "error" in result
        assert "not-a-real-row-id" in result["error"]
        assert "get_order" in result["error"].lower()
        assert not any("UpdateOrderItem" in p for p in calls)

    def test_row_is_located_from_raw_items_not_flattened_shape(self):
        """The line is found by scanning raw.Items for RowId — not via
        _format_order_detail's flattened "items" list, which renames RowId
        to "row_id" and drops most fields."""
        with patch("server.call_linnworks", side_effect=_post_side_effect(BASE_ORDER)):
            result = server.relink_order_line(
                GUID, ROW_ID_1, channel_line_id=NEW_CHANNEL_LINE_ID, dry_run=True
            )

        assert result["line"]["sku"] == "TEST-SKU-A"
        assert result["line"]["title"] == "Test Product A"
        assert result["line"]["quantity"] == 2


# ── processed-order refusal (AC3) ─────────────────────────────────────────────

class TestProcessedOrderRefusal:

    def test_processed_order_is_refused_before_any_write(self):
        calls = []

        def side(path, payload, **kwargs):
            calls.append(path)
            if "GetOrdersById" in path:
                return [PROCESSED_ORDER]
            raise AssertionError(f"Unexpected call: {path}")

        with patch("server.call_linnworks", side_effect=side):
            result = server.relink_order_line(
                GUID, ROW_ID_1, channel_line_id=NEW_CHANNEL_LINE_ID, dry_run=False
            )

        assert "error" in result
        assert "despatch" in result["error"].lower()
        assert "cannot be repaired this way" in result["error"].lower()
        assert not any("UpdateOrderItem" in p for p in calls)


# ── payload construction preserves every other key (AC4) ────────────────────

class TestPayloadPreservesUnrecognisedKeys:

    def test_unrecognised_key_survives_untouched_in_outgoing_payload(self):
        extra_value = {"nested": ["arbitrary", "structure", 123]}
        order_with_extra = _order(items=[
            _unlinked_item(extra={"SomeUnrecognisedField": extra_value}),
            _linked_item(),
        ])
        payload_sent = {}

        def side(path, payload, **kwargs):
            if "GetOrdersById" in path:
                return [order_with_extra]
            if "UpdateOrderItem" in path:
                payload_sent.update(payload)
                return {}
            raise AssertionError(f"Unexpected call: {path}")

        with patch("server.call_linnworks", side_effect=side):
            server.relink_order_line(
                GUID, ROW_ID_1, channel_line_id=NEW_CHANNEL_LINE_ID, dry_run=False
            )

        assert payload_sent["orderItem"]["SomeUnrecognisedField"] == extra_value
        # And every other pre-existing field survives untouched too.
        assert payload_sent["orderItem"]["PricePerUnit"] == 5.0
        assert payload_sent["orderItem"]["CostIncTax"] == 10.0
        assert payload_sent["orderItem"]["SKU"] == "TEST-SKU-A"
        # Only the two identity fields changed.
        assert payload_sent["orderItem"]["ItemNumber"] == NEW_CHANNEL_LINE_ID
        assert payload_sent["orderItem"]["ItemSource"] == "SHOPIFY"


# ── pre-write integrity check (AC5) ───────────────────────────────────────────

class TestPayloadIntegrityCheck:

    def test_forced_mismatch_aborts_without_writing(self):
        """Force _build_relink_order_item to return a payload that drifts a
        key other than ItemNumber/ItemSource, and prove the tool refuses to
        send it."""
        calls = []

        def side(path, payload, **kwargs):
            calls.append(path)
            if "GetOrdersById" in path:
                return [BASE_ORDER]
            raise AssertionError(f"Unexpected call: {path}")

        def bad_builder(raw_item, channel_line_id, channel_line_source):
            payload = dict(raw_item)
            payload["ItemNumber"] = channel_line_id
            payload["ItemSource"] = channel_line_source
            payload["PricePerUnit"] = 999.99  # drifted — must be caught
            return payload

        with patch("server.call_linnworks", side_effect=side), \
             patch("server._build_relink_order_item", side_effect=bad_builder):
            result = server.relink_order_line(
                GUID, ROW_ID_1, channel_line_id=NEW_CHANNEL_LINE_ID, dry_run=False
            )

        assert "error" in result
        assert not any("UpdateOrderItem" in p for p in calls)
        assert "PricePerUnit" in result["unexpected_diff"]

    def test_forced_missing_key_aborts_without_writing(self):
        calls = []

        def side(path, payload, **kwargs):
            calls.append(path)
            if "GetOrdersById" in path:
                return [BASE_ORDER]
            raise AssertionError(f"Unexpected call: {path}")

        def bad_builder(raw_item, channel_line_id, channel_line_source):
            payload = dict(raw_item)
            payload["ItemNumber"] = channel_line_id
            payload["ItemSource"] = channel_line_source
            del payload["CostIncTax"]  # dropped — must be caught
            return payload

        with patch("server.call_linnworks", side_effect=side), \
             patch("server._build_relink_order_item", side_effect=bad_builder):
            result = server.relink_order_line(
                GUID, ROW_ID_1, channel_line_id=NEW_CHANNEL_LINE_ID, dry_run=False
            )

        assert "error" in result
        assert not any("UpdateOrderItem" in p for p in calls)
        assert "CostIncTax" in result["unexpected_diff"]

    def test_diff_helper_ignores_the_two_identity_fields(self):
        original = {"SKU": "X", "ItemNumber": "old", "ItemSource": ""}
        payload = {"SKU": "X", "ItemNumber": "new", "ItemSource": "SHOPIFY"}
        assert server._relink_payload_unexpected_diff(original, payload) == {}


# ── channel_line_id is required, never derived (AC6) ─────────────────────────

class TestChannelLineIdRequired:

    def test_omitted_channel_line_id_is_refused_with_no_write(self):
        calls = []

        def side(path, payload, **kwargs):
            calls.append(path)
            if "GetOrdersById" in path:
                return [BASE_ORDER]
            raise AssertionError(f"Unexpected call: {path}")

        with patch("server.call_linnworks", side_effect=side):
            result = server.relink_order_line(GUID, ROW_ID_1, dry_run=False)

        assert "error" in result
        assert result["reason"] == "channel_line_id_required"
        assert not any("UpdateOrderItem" in p for p in calls)

    def test_blank_channel_line_id_is_refused_with_no_write(self):
        calls = []

        def side(path, payload, **kwargs):
            calls.append(path)
            if "GetOrdersById" in path:
                return [BASE_ORDER]
            raise AssertionError(f"Unexpected call: {path}")

        with patch("server.call_linnworks", side_effect=side):
            result = server.relink_order_line(
                GUID, ROW_ID_1, channel_line_id="   ", dry_run=False
            )

        assert "error" in result
        assert result["reason"] == "channel_line_id_required"
        assert not any("UpdateOrderItem" in p for p in calls)

    def test_channel_line_id_is_never_defaulted_from_sku_or_anything_else(self):
        """Even though the target line's SKU/ChannelSKU is known, the tool
        must never fall back to it as a guessed channel_line_id."""
        with patch("server.call_linnworks", side_effect=_post_side_effect(BASE_ORDER)):
            result = server.relink_order_line(GUID, ROW_ID_1, dry_run=False)

        assert "error" in result
        assert "never derived, defaulted, or guessed" in result["error"]


# ── channel_line_source: derived default vs override (AC7) ──────────────────

class TestChannelLineSourceDefaultAndOverride:

    def test_source_defaults_to_order_source_and_is_labelled_derived(self):
        with patch("server.call_linnworks", side_effect=_post_side_effect(BASE_ORDER)):
            result = server.relink_order_line(
                GUID, ROW_ID_1, channel_line_id=NEW_CHANNEL_LINE_ID, dry_run=True
            )

        assert result["channel_line_source_after"] == "SHOPIFY"
        assert result["channel_line_source_origin"] == "derived_from_order_source"

    def test_explicit_source_overrides_the_default_and_is_labelled_supplied(self):
        with patch("server.call_linnworks", side_effect=_post_side_effect(BASE_ORDER)):
            result = server.relink_order_line(
                GUID, ROW_ID_1,
                channel_line_id=NEW_CHANNEL_LINE_ID,
                channel_line_source="EBAY0",
                dry_run=True,
            )

        assert result["channel_line_source_after"] == "EBAY0"
        assert result["channel_line_source_origin"] == "supplied"


# ── already-linked line refusal (AC8) ─────────────────────────────────────────

class TestAlreadyLinkedRefusal:

    def test_already_linked_line_is_refused_by_default(self):
        calls = []

        def side(path, payload, **kwargs):
            calls.append(path)
            if "GetOrdersById" in path:
                return [BASE_ORDER]
            raise AssertionError(f"Unexpected call: {path}")

        with patch("server.call_linnworks", side_effect=side):
            result = server.relink_order_line(
                GUID, ROW_ID_2, channel_line_id=NEW_CHANNEL_LINE_ID, dry_run=False
            )

        assert "error" in result
        assert result["reason"] == "already_linked"
        assert not any("UpdateOrderItem" in p for p in calls)

    def test_already_linked_line_is_also_refused_on_a_dry_run(self):
        with patch("server.call_linnworks", side_effect=_post_side_effect(BASE_ORDER)):
            result = server.relink_order_line(
                GUID, ROW_ID_2, channel_line_id=NEW_CHANNEL_LINE_ID, dry_run=True
            )

        assert "error" in result
        assert result["reason"] == "already_linked"

    def test_already_linked_line_proceeds_with_explicit_override(self):
        with patch("server.call_linnworks", side_effect=_post_side_effect(BASE_ORDER)):
            result = server.relink_order_line(
                GUID, ROW_ID_2,
                channel_line_id=NEW_CHANNEL_LINE_ID,
                allow_relink_of_linked_line=True,
                dry_run=True,
            )

        assert "error" not in result
        assert result["dry_run"] is True


# ── dry run (AC9) ──────────────────────────────────────────────────────────────

class TestDryRun:

    def test_dry_run_defaults_to_true(self):
        with patch("server.call_linnworks", side_effect=_post_side_effect(BASE_ORDER)):
            result = server.relink_order_line(
                GUID, ROW_ID_1, channel_line_id=NEW_CHANNEL_LINE_ID
            )

        assert result["dry_run"] is True

    def test_dry_run_makes_zero_calls_to_update_order_item(self):
        calls = []

        def side(path, payload, **kwargs):
            calls.append(path)
            if "GetOrdersById" in path:
                return [BASE_ORDER]
            raise AssertionError(f"Unexpected call: {path}")

        with patch("server.call_linnworks", side_effect=side):
            server.relink_order_line(
                GUID, ROW_ID_1, channel_line_id=NEW_CHANNEL_LINE_ID, dry_run=True
            )

        assert not any("UpdateOrderItem" in p for p in calls)

    def test_dry_run_manifest_shows_order_line_and_identity_values(self):
        with patch("server.call_linnworks", side_effect=_post_side_effect(BASE_ORDER)):
            result = server.relink_order_line(
                GUID, ROW_ID_1, channel_line_id=NEW_CHANNEL_LINE_ID, dry_run=True
            )

        assert result["order_id"] == GUID
        assert result["num_order_id"] == 656056
        assert result["row_id"] == ROW_ID_1
        assert result["line"] == {"sku": "TEST-SKU-A", "title": "Test Product A", "quantity": 2}
        assert result["channel_line_id_before"] == "TEST-SKU-A"
        assert result["channel_line_id_after"] == NEW_CHANNEL_LINE_ID
        assert result["channel_line_source_before"] == ""
        assert result["channel_line_source_after"] == "SHOPIFY"


# ── read-back classification (AC10) ───────────────────────────────────────────

class TestReadBackClassification:

    def test_successful_relink_is_classified_relinked(self):
        after = _with_relinked_line(BASE_ORDER, ROW_ID_1, NEW_CHANNEL_LINE_ID, "SHOPIFY")
        counter = {"n": 0}

        def side(path, payload, **kwargs):
            if "GetOrdersById" in path:
                counter["n"] += 1
                return [BASE_ORDER] if counter["n"] == 1 else [after]
            if "UpdateOrderItem" in path:
                return {}
            raise AssertionError(f"Unexpected call: {path}")

        with patch("server.call_linnworks", side_effect=side):
            result = server.relink_order_line(
                GUID, ROW_ID_1, channel_line_id=NEW_CHANNEL_LINE_ID, dry_run=False
            )

        assert result["outcome"] == "relinked"
        assert "error" not in result

    def test_write_accepted_but_unchanged_readback_is_not_persisted(self):
        """A 2xx must never be reported as 'relinked' — the fresh read-back
        still shows the pre-write (orphaned) identity values."""
        def side(path, payload, **kwargs):
            if "GetOrdersById" in path:
                return [BASE_ORDER]
            if "UpdateOrderItem" in path:
                return {}
            raise AssertionError(f"Unexpected call: {path}")

        with patch("server.call_linnworks", side_effect=side):
            result = server.relink_order_line(
                GUID, ROW_ID_1, channel_line_id=NEW_CHANNEL_LINE_ID, dry_run=False
            )

        assert result["outcome"] == "not_persisted"
        assert "warning" in result

    def test_readback_is_a_fresh_call_not_the_prewrite_object(self):
        counter = {"n": 0}

        def side(path, payload, **kwargs):
            if "GetOrdersById" in path:
                counter["n"] += 1
                return [BASE_ORDER]
            if "UpdateOrderItem" in path:
                return {}
            raise AssertionError(f"Unexpected call: {path}")

        with patch("server.call_linnworks", side_effect=side):
            server.relink_order_line(
                GUID, ROW_ID_1, channel_line_id=NEW_CHANNEL_LINE_ID, dry_run=False
            )

        # 1 pre-write read + 1 post-write read-back = 2 GetOrdersById calls.
        assert counter["n"] == 2

    def test_numeric_order_id_readback_still_goes_by_guid(self):
        call_log = []

        def get_side(path, params=None, **kwargs):
            call_log.append(("GET", path))
            if "GetOrderDetailsByNumOrderId" in path:
                return BASE_ORDER
            raise AssertionError(f"Unexpected GET: {path}")

        after = _with_relinked_line(BASE_ORDER, ROW_ID_1, NEW_CHANNEL_LINE_ID, "SHOPIFY")

        def post_side(path, payload, **kwargs):
            call_log.append(("POST", path))
            if "GetOrdersById" in path:
                return [after]
            if "UpdateOrderItem" in path:
                return {}
            raise AssertionError(f"Unexpected POST: {path}")

        with patch("server.call_linnworks", side_effect=post_side), \
             patch("server.call_linnworks_get", side_effect=get_side):
            result = server.relink_order_line(
                "656056", ROW_ID_1, channel_line_id=NEW_CHANNEL_LINE_ID, dry_run=False
            )

        assert result["outcome"] == "relinked"
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
                    return [BASE_ORDER]
                raise RuntimeError("No order found for GUID")
            if "UpdateOrderItem" in path:
                return {}
            raise AssertionError(f"Unexpected call: {path}")

        with patch("server.call_linnworks", side_effect=side):
            result = server.relink_order_line(
                GUID, ROW_ID_1, channel_line_id=NEW_CHANNEL_LINE_ID, dry_run=False
            )

        assert result["outcome"] == "unconfirmed"
        reason = result.get("unconfirmed_reason", "").lower()
        assert "line not found" not in reason
        assert result["outcome"] != "relinked"

    def test_readback_missing_the_line_entirely_is_unconfirmed(self):
        after_without_line = dict(BASE_ORDER)
        after_without_line["Items"] = [_linked_item()]  # ROW_ID_1 gone
        counter = {"n": 0}

        def side(path, payload, **kwargs):
            if "GetOrdersById" in path:
                counter["n"] += 1
                return [BASE_ORDER] if counter["n"] == 1 else [after_without_line]
            if "UpdateOrderItem" in path:
                return {}
            raise AssertionError(f"Unexpected call: {path}")

        with patch("server.call_linnworks", side_effect=side):
            result = server.relink_order_line(
                GUID, ROW_ID_1, channel_line_id=NEW_CHANNEL_LINE_ID, dry_run=False
            )

        assert result["outcome"] == "unconfirmed"
        assert "line not found" not in result.get("unconfirmed_reason", "").lower()


# ── unexpected field changes are surfaced (AC11) ──────────────────────────────

class TestUnexpectedFieldChanges:

    def test_altered_price_on_readback_is_surfaced_not_swallowed(self):
        after = _with_relinked_line(BASE_ORDER, ROW_ID_1, NEW_CHANNEL_LINE_ID, "SHOPIFY")
        after = dict(after)
        after["Items"] = [
            {**i, "PricePerUnit": 999.0} if i["RowId"] == ROW_ID_1 else i
            for i in after["Items"]
        ]
        counter = {"n": 0}

        def side(path, payload, **kwargs):
            if "GetOrdersById" in path:
                counter["n"] += 1
                return [BASE_ORDER] if counter["n"] == 1 else [after]
            if "UpdateOrderItem" in path:
                return {}
            raise AssertionError(f"Unexpected call: {path}")

        with patch("server.call_linnworks", side_effect=side):
            result = server.relink_order_line(
                GUID, ROW_ID_1, channel_line_id=NEW_CHANNEL_LINE_ID, dry_run=False
            )

        assert "PricePerUnit" in result["unexpected_field_changes"]
        assert result["unexpected_field_changes"]["PricePerUnit"] == {
            "before": 5.0, "after": 999.0,
        }

    def test_unchanged_other_fields_produce_no_unexpected_changes(self):
        after = _with_relinked_line(BASE_ORDER, ROW_ID_1, NEW_CHANNEL_LINE_ID, "SHOPIFY")
        counter = {"n": 0}

        def side(path, payload, **kwargs):
            if "GetOrdersById" in path:
                counter["n"] += 1
                return [BASE_ORDER] if counter["n"] == 1 else [after]
            if "UpdateOrderItem" in path:
                return {}
            raise AssertionError(f"Unexpected call: {path}")

        with patch("server.call_linnworks", side_effect=side):
            result = server.relink_order_line(
                GUID, ROW_ID_1, channel_line_id=NEW_CHANNEL_LINE_ID, dry_run=False
            )

        assert result["unexpected_field_changes"] == {}


# ── rate-limited path, distinct at all three points (AC12) ───────────────────

class TestRateLimiting:

    def test_rate_limit_resolving_order_reports_rate_limited_outcome(self):
        with patch("server.call_linnworks",
                   side_effect=server.RateLimitError("quota exceeded")):
            result = server.relink_order_line(
                GUID, ROW_ID_1, channel_line_id=NEW_CHANNEL_LINE_ID, dry_run=True
            )

        assert result["outcome"] == "rate_limited"
        assert "error" in result

    def test_rate_limit_on_write_is_reported_as_rate_limited_not_not_persisted(self):
        def side(path, payload, **kwargs):
            if "GetOrdersById" in path:
                return [BASE_ORDER]
            if "UpdateOrderItem" in path:
                raise server.RateLimitError("quota exceeded")
            raise AssertionError(f"Unexpected call: {path}")

        with patch("server.call_linnworks", side_effect=side):
            result = server.relink_order_line(
                GUID, ROW_ID_1, channel_line_id=NEW_CHANNEL_LINE_ID, dry_run=False
            )

        assert result["outcome"] == "rate_limited"
        assert result["outcome"] != "not_persisted"

    def test_rate_limit_on_readback_is_reported_as_rate_limited_not_unconfirmed(self):
        """This tool's own brief distinguishes 'rate_limited' from
        'unconfirmed' at every call site, including the read-back — unlike
        remove_order_item, which folds a rate-limited read-back into
        'unconfirmed'."""
        counter = {"n": 0}

        def side(path, payload, **kwargs):
            if "GetOrdersById" in path:
                counter["n"] += 1
                if counter["n"] == 1:
                    return [BASE_ORDER]
                raise server.RateLimitError("quota exceeded")
            if "UpdateOrderItem" in path:
                return {}
            raise AssertionError(f"Unexpected call: {path}")

        with patch("server.call_linnworks", side_effect=side):
            result = server.relink_order_line(
                GUID, ROW_ID_1, channel_line_id=NEW_CHANNEL_LINE_ID, dry_run=False
            )

        assert result["outcome"] == "rate_limited"
        assert result["outcome"] != "unconfirmed"


# ── unproven-endpoint warning (AC13) ───────────────────────────────────────────

class TestUnprovenWarning:

    def test_warning_present_on_successful_relink(self):
        after = _with_relinked_line(BASE_ORDER, ROW_ID_1, NEW_CHANNEL_LINE_ID, "SHOPIFY")
        counter = {"n": 0}

        def side(path, payload, **kwargs):
            if "GetOrdersById" in path:
                counter["n"] += 1
                return [BASE_ORDER] if counter["n"] == 1 else [after]
            if "UpdateOrderItem" in path:
                return {}
            raise AssertionError(f"Unexpected call: {path}")

        with patch("server.call_linnworks", side_effect=side):
            result = server.relink_order_line(
                GUID, ROW_ID_1, channel_line_id=NEW_CHANNEL_LINE_ID, dry_run=False
            )

        assert "unproven_warning" in result
        assert "find_unlinked_order_lines" in result["unproven_warning"]
        assert "not proof" in result["unproven_warning"].lower()

    def test_warning_present_on_rate_limited_write(self):
        def side(path, payload, **kwargs):
            if "GetOrdersById" in path:
                return [BASE_ORDER]
            if "UpdateOrderItem" in path:
                raise server.RateLimitError("quota exceeded")
            raise AssertionError(f"Unexpected call: {path}")

        with patch("server.call_linnworks", side_effect=side):
            result = server.relink_order_line(
                GUID, ROW_ID_1, channel_line_id=NEW_CHANNEL_LINE_ID, dry_run=False
            )

        assert "unproven_warning" in result

    def test_warning_present_on_rate_limited_readback(self):
        counter = {"n": 0}

        def side(path, payload, **kwargs):
            if "GetOrdersById" in path:
                counter["n"] += 1
                if counter["n"] == 1:
                    return [BASE_ORDER]
                raise server.RateLimitError("quota exceeded")
            if "UpdateOrderItem" in path:
                return {}
            raise AssertionError(f"Unexpected call: {path}")

        with patch("server.call_linnworks", side_effect=side):
            result = server.relink_order_line(
                GUID, ROW_ID_1, channel_line_id=NEW_CHANNEL_LINE_ID, dry_run=False
            )

        assert "unproven_warning" in result

    def test_warning_present_on_unconfirmed_readback_failure(self):
        counter = {"n": 0}

        def side(path, payload, **kwargs):
            if "GetOrdersById" in path:
                counter["n"] += 1
                if counter["n"] == 1:
                    return [BASE_ORDER]
                raise RuntimeError("boom")
            if "UpdateOrderItem" in path:
                return {}
            raise AssertionError(f"Unexpected call: {path}")

        with patch("server.call_linnworks", side_effect=side):
            result = server.relink_order_line(
                GUID, ROW_ID_1, channel_line_id=NEW_CHANNEL_LINE_ID, dry_run=False
            )

        assert result["outcome"] == "unconfirmed"
        assert "unproven_warning" in result

    def test_warning_present_on_not_persisted(self):
        def side(path, payload, **kwargs):
            if "GetOrdersById" in path:
                return [BASE_ORDER]
            if "UpdateOrderItem" in path:
                return {}
            raise AssertionError(f"Unexpected call: {path}")

        with patch("server.call_linnworks", side_effect=side):
            result = server.relink_order_line(
                GUID, ROW_ID_1, channel_line_id=NEW_CHANNEL_LINE_ID, dry_run=False
            )

        assert result["outcome"] == "not_persisted"
        assert "unproven_warning" in result

    def test_warning_present_on_forced_integrity_abort(self):
        def side(path, payload, **kwargs):
            if "GetOrdersById" in path:
                return [BASE_ORDER]
            raise AssertionError(f"Unexpected call: {path}")

        def bad_builder(raw_item, channel_line_id, channel_line_source):
            payload = dict(raw_item)
            payload["ItemNumber"] = channel_line_id
            payload["ItemSource"] = channel_line_source
            payload["Quantity"] = 999
            return payload

        with patch("server.call_linnworks", side_effect=side), \
             patch("server._build_relink_order_item", side_effect=bad_builder):
            result = server.relink_order_line(
                GUID, ROW_ID_1, channel_line_id=NEW_CHANNEL_LINE_ID, dry_run=False
            )

        assert "unproven_warning" in result

    def test_warning_absent_on_dry_run(self):
        with patch("server.call_linnworks", side_effect=_post_side_effect(BASE_ORDER)):
            result = server.relink_order_line(
                GUID, ROW_ID_1, channel_line_id=NEW_CHANNEL_LINE_ID, dry_run=True
            )

        assert "unproven_warning" not in result

    def test_warning_absent_on_early_static_refusals(self):
        """Refusals that occur identically regardless of dry_run (unknown
        row_id, processed order, missing channel_line_id, already-linked)
        never carry the live-run warning — they happen before any write is
        even attempted, matching remove_order_item's own precedent."""
        with patch("server.call_linnworks", side_effect=_post_side_effect(PROCESSED_ORDER)):
            result = server.relink_order_line(
                GUID, ROW_ID_1, channel_line_id=NEW_CHANNEL_LINE_ID, dry_run=False
            )
        assert "unproven_warning" not in result

        with patch("server.call_linnworks", side_effect=_post_side_effect(BASE_ORDER)):
            result = server.relink_order_line(GUID, ROW_ID_1, dry_run=False)
        assert "unproven_warning" not in result


# ── live write payload shape ───────────────────────────────────────────────────

class TestLiveWritePayloadShape:

    def test_payload_uses_orders_own_fulfilment_location_source_and_subsource(self):
        payload_sent = {}

        def side(path, payload, **kwargs):
            if "GetOrdersById" in path:
                return [BASE_ORDER]
            if "UpdateOrderItem" in path:
                payload_sent.update(payload)
                return {}
            raise AssertionError(f"Unexpected call: {path}")

        with patch("server.call_linnworks", side_effect=side):
            server.relink_order_line(
                GUID, ROW_ID_1, channel_line_id=NEW_CHANNEL_LINE_ID, dry_run=False
            )

        assert payload_sent["orderId"] == GUID
        assert payload_sent["fulfilmentCenter"] == GUID_LOC
        assert payload_sent["source"] == "SHOPIFY"
        assert payload_sent["subSource"] == "SWH Shopify"

    def test_location_id_override_takes_precedence(self):
        override = "ffffffff-0000-0000-0000-000000000056"
        payload_sent = {}

        def side(path, payload, **kwargs):
            if "GetOrdersById" in path:
                return [BASE_ORDER]
            if "UpdateOrderItem" in path:
                payload_sent.update(payload)
                return {}
            raise AssertionError(f"Unexpected call: {path}")

        with patch("server.call_linnworks", side_effect=side):
            server.relink_order_line(
                GUID, ROW_ID_1, channel_line_id=NEW_CHANNEL_LINE_ID,
                location_id=override, dry_run=False,
            )

        assert payload_sent["fulfilmentCenter"] == override

    def test_falls_back_to_default_location_when_order_carries_none(self):
        no_loc_order = dict(BASE_ORDER)
        no_loc_order["FulfilmentLocationId"] = None
        payload_sent = {}

        def side(path, payload, **kwargs):
            if "GetOrdersById" in path:
                return [no_loc_order]
            if "UpdateOrderItem" in path:
                payload_sent.update(payload)
                return {}
            raise AssertionError(f"Unexpected call: {path}")

        with patch("server.call_linnworks", side_effect=side):
            server.relink_order_line(
                GUID, ROW_ID_1, channel_line_id=NEW_CHANNEL_LINE_ID, dry_run=False
            )

        assert payload_sent["fulfilmentCenter"] == server.DEFAULT_LOCATION_ID
