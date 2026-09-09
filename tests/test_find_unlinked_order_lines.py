"""
QA tests for channel line-link detection (issue #52):
  - _format_order_detail's per-item rows (consumed by get_order)
  - _flatten_order_item (consumed by get_processed_order_items and friends)
  - _classify_order_line
  - find_unlinked_order_lines

Field names in the fixtures below match the live-confirmed OrderItem shape
(ItemNumber / ItemSource / ChannelSKU / CompositeSubItems / GeneralInfo.Source)
recorded in CLAUDE.md's v1.51.0 note, not invented names.

All tests use unittest.mock — no live Linnworks API calls.
Run with: pytest tests/test_find_unlinked_order_lines.py -v
"""

import sys
import os
import pytest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server


# ── Fixtures ─────────────────────────────────────────────────────────────────

_UNSET = object()


def _raw_item(
    sku,
    item_number=_UNSET,
    item_source=_UNSET,
    channel_sku=_UNSET,
    title=None,
    row_id=None,
    sub_items=None,
):
    """
    Build a raw OrderItem dict as Orders/GetOrdersById returns it.

    Passing item_number/item_source/channel_sku as _UNSET (the default) omits
    the key entirely from the dict — simulating a field the API never
    returned, distinct from passing "" (present but blank, the live-observed
    orphaned-line shape).
    """
    d = {"SKU": sku, "Title": title or sku, "RowId": row_id or f"row-{sku}"}
    if item_number is not _UNSET:
        d["ItemNumber"] = item_number
    if item_source is not _UNSET:
        d["ItemSource"] = item_source
    if channel_sku is not _UNSET:
        d["ChannelSKU"] = channel_sku
    if sub_items:
        d["CompositeSubItems"] = sub_items
    return d


def _raw_order(guid, num_order_id, source, processed, items, reference_num=None):
    return {
        "OrderId": guid,
        "NumOrderId": num_order_id,
        "Processed": processed,
        "GeneralInfo": {
            "Source": source,
            "ReferenceNum": reference_num or f"REF-{num_order_id}",
        },
        "Items": items,
    }


# A real linked line, shaped exactly like the live SHOPIFY example in
# CLAUDE.md (order 607855): ItemNumber is the channel's own line id, distinct
# from SKU; ItemSource matches the order's channel.
LINKED_ITEM = _raw_item(
    "LOB-CLA-CAR",
    item_number="16506671005945",
    item_source="SHOPIFY",
    channel_sku="LOB-CLA-CAR",
    title="Clawskin - Cartoon",
)

# A genuinely orphaned line, shaped like the live example in CLAUDE.md (order
# 605126): ItemSource comes back blank and ItemNumber falls back to the SKU,
# but both keys ARE present.
UNLINKED_ITEM = _raw_item(
    "IND-SKT-5149",
    item_number="IND-SKT-5149",
    item_source="",
    channel_sku="IND-SKT-5149",
    title="Independent Trucks",
)


# ══════════════════════════════════════════════════════════════════════════════
# AC2 — get_order / _flatten_order_item carry the new fields verbatim
# ══════════════════════════════════════════════════════════════════════════════

class TestChannelIdentityFieldsSurfaced:
    def test_format_order_detail_carries_new_keys_verbatim(self):
        """get_order's item dict (via _format_order_detail) gains the three
        channel-identity keys, with values carried through unchanged, and
        None when the raw item never had the key at all."""
        raw_order = {
            "OrderId": "guid-1",
            "NumOrderId": 1,
            "Processed": True,
            "GeneralInfo": {"Source": "SHOPIFY"},
            "CustomerInfo": {"Address": {}, "BillingAddress": {}},
            "ShippingInfo": {},
            "TotalsInfo": {},
            "Items": [
                LINKED_ITEM,
                UNLINKED_ITEM,
                _raw_item("NO-FIELDS-AT-ALL"),  # both keys omitted entirely
            ],
        }
        fmt = server._format_order_detail(raw_order)
        items = fmt["items"]
        assert len(items) == 3

        linked, unlinked, stripped = items
        assert linked["channel_line_id"] == "16506671005945"
        assert linked["channel_line_source"] == "SHOPIFY"
        assert linked["channel_sku"] == "LOB-CLA-CAR"

        assert unlinked["channel_line_id"] == "IND-SKT-5149"
        assert unlinked["channel_line_source"] == ""
        assert unlinked["channel_sku"] == "IND-SKT-5149"

        assert stripped["channel_line_id"] is None
        assert stripped["channel_line_source"] is None
        assert stripped["channel_sku"] is None

    def test_format_order_detail_existing_keys_unchanged(self):
        """Every pre-existing item key keeps its current name, type and value."""
        raw_order = {
            "OrderId": "guid-1",
            "NumOrderId": 1,
            "GeneralInfo": {},
            "CustomerInfo": {"Address": {}, "BillingAddress": {}},
            "ShippingInfo": {},
            "TotalsInfo": {},
            "Items": [
                {
                    "StockItemId": "sid-1",
                    "SKU": "SKU-1",
                    "Title": "Title 1",
                    "Quantity": 3,
                    "RowId": "row-1",
                    "PricePerUnit": 9.99,
                    "CostIncTax": 29.97,
                    "ItemNumber": "chan-1",
                    "ItemSource": "SHOPIFY",
                    "ChannelSKU": "SKU-1",
                }
            ],
        }
        item = server._format_order_detail(raw_order)["items"][0]
        assert item["StockItemId"] == "sid-1"
        assert item["SKU"] == "SKU-1"
        assert item["Title"] == "Title 1"
        assert item["Quantity"] == 3
        assert item["row_id"] == "row-1"
        assert item["price_per_unit"] == 9.99
        assert item["cost_inc_tax"] == 29.97

    def test_flatten_order_item_carries_new_keys_verbatim(self):
        linked = server._flatten_order_item(LINKED_ITEM)
        assert linked["channel_line_id"] == "16506671005945"
        assert linked["channel_line_source"] == "SHOPIFY"
        assert linked["channel_sku"] == "LOB-CLA-CAR"

        stripped = server._flatten_order_item(_raw_item("NO-FIELDS-AT-ALL"))
        assert stripped["channel_line_id"] is None
        assert stripped["channel_line_source"] is None

    def test_flatten_order_item_existing_keys_unchanged(self):
        flat = server._flatten_order_item(LINKED_ITEM)
        assert flat["sku"] == "LOB-CLA-CAR"
        assert flat["title"] == "Clawskin - Cartoon"
        assert flat["channel_sku"] == "LOB-CLA-CAR"  # pre-existing key, untouched
        assert flat["composite_sub_items"] == []


# ══════════════════════════════════════════════════════════════════════════════
# _classify_order_line — the classification rules directly
# ══════════════════════════════════════════════════════════════════════════════

class TestClassifyOrderLine:
    def test_linked_line_on_channel_order(self):
        flat = server._flatten_order_item(LINKED_ITEM)
        rows = server._classify_order_line(flat, is_channel_order=True)
        assert len(rows) == 1
        assert rows[0]["status"] == "linked"
        assert rows[0]["reason"] is None

    def test_unlinked_line_on_channel_order(self):
        """AC1's live example: ItemSource present but blank on a channel order."""
        flat = server._flatten_order_item(UNLINKED_ITEM)
        rows = server._classify_order_line(flat, is_channel_order=True)
        assert len(rows) == 1
        assert rows[0]["status"] == "unlinked"
        assert rows[0]["reason"] is None

    def test_unknown_when_fields_never_returned_at_all(self):
        """AC4 — a fixture with the identity fields stripped entirely must
        classify as unknown, never as unlinked and never as linked."""
        flat = server._flatten_order_item(_raw_item("SOME-SKU"))
        assert flat["channel_line_id"] is None
        assert flat["channel_line_source"] is None

        rows = server._classify_order_line(flat, is_channel_order=True)
        assert len(rows) == 1
        assert rows[0]["status"] == "unknown"
        assert rows[0]["reason"] == "fields_absent"
        assert rows[0]["status"] != "unlinked"
        assert rows[0]["status"] != "linked"

    def test_manual_order_line_is_not_expected_not_unlinked(self):
        """AC5 — a line on a manually created (non-channel) order looks
        exactly like an orphaned line (blank ItemNumber/ItemSource) but must
        never count as unlinked."""
        manual_line = _raw_item("H159ORANGEL/XL", item_number="", item_source="")
        flat = server._flatten_order_item(manual_line)
        rows = server._classify_order_line(flat, is_channel_order=False)
        assert len(rows) == 1
        assert rows[0]["status"] == "not_expected"
        assert rows[0]["reason"] == "manual_order"

    def test_composite_child_is_not_expected_not_unlinked(self):
        """AC5/AC6 — a composite child never carries its own channel
        reference (live example: blackgrip-9x33 under swh_GRIP_OPTION), and
        must be excluded from the unlinked bucket even on a linked parent."""
        child = _raw_item("blackgrip-9x33", item_number="blackgrip-9x33", item_source="")
        parent = _raw_item(
            "swh_GRIP_OPTION",
            item_number="17061777080566",
            item_source="SHOPIFY",
            sub_items=[child],
        )
        flat = server._flatten_order_item(parent)
        rows = server._classify_order_line(flat, is_channel_order=True)

        assert len(rows) == 2
        parent_row, child_row = rows
        assert parent_row["status"] == "linked"
        assert parent_row["is_composite_child"] is False

        assert child_row["status"] == "not_expected"
        assert child_row["reason"] == "composite_child"
        assert child_row["is_composite_child"] is True

    def test_composite_child_excluded_even_if_its_own_fields_look_linked(self):
        """A composite child is not_expected regardless of what its own
        fields say — the classification is about the role, not the values."""
        child = _raw_item("child-sku", item_number="looks-linked", item_source="SHOPIFY")
        parent = _raw_item("parent-sku", item_number="p1", item_source="SHOPIFY", sub_items=[child])
        flat = server._flatten_order_item(parent)
        rows = server._classify_order_line(flat, is_channel_order=True)
        child_row = rows[1]
        assert child_row["status"] == "not_expected"
        assert child_row["reason"] == "composite_child"


# ══════════════════════════════════════════════════════════════════════════════
# find_unlinked_order_lines — the tool itself
# ══════════════════════════════════════════════════════════════════════════════

OPEN_ORDER_GUID = "open0000-0000-0000-0000-000000000001"
PROCESSED_ORDER_GUID = "proc0000-0000-0000-0000-000000000002"


def _open_orders_response(guids_and_refs):
    return {
        "Orders": [
            {"pkOrderID": guid, "ReferenceNum": ref, "OrderId": i + 1}
            for i, (guid, ref) in enumerate(guids_and_refs)
        ]
    }


def _processed_orders_page(rows, total_pages=1):
    return {
        "ProcessedOrders": {
            "Data": rows,
            "TotalPages": total_pages,
            "TotalEntries": len(rows),
        }
    }


def _make_dispatch(open_resp=None, processed_pages=None, order_details=None, raise_on_details=False):
    """
    Build a call_linnworks side_effect dispatching on method_path, mirroring
    the fixture style used elsewhere in this suite (e.g. test_list_duplicate_guard.py).
    """
    processed_pages = list(processed_pages or [])

    def fake_post(path, payload):
        if path == "OpenOrders/GetOrdersLowFidelity":
            return open_resp or {"Orders": []}
        if path == "ProcessedOrders/SearchProcessedOrders":
            page_num = payload["request"]["PageNumber"]
            return processed_pages[page_num - 1]
        if path == "Orders/GetOrdersById":
            if raise_on_details:
                raise server.RateLimitError("quota exceeded")
            requested = set(payload["pkOrderIds"])
            return [o for o in (order_details or []) if o["OrderId"] in requested]
        raise AssertionError(f"unexpected call_linnworks path: {path}")

    return fake_post


class TestFindUnlinkedOrderLines:
    def test_reports_per_order_per_line_and_top_level_counts(self):
        """AC3 — linked and unlinked lines are both correctly classified,
        with per-order rows, per-line rows, and top-level counts."""
        order = _raw_order(
            PROCESSED_ORDER_GUID, 605126, "SHOPIFY", True,
            [LINKED_ITEM, UNLINKED_ITEM],
        )
        dispatch = _make_dispatch(
            processed_pages=[_processed_orders_page(
                [{"pkOrderID": PROCESSED_ORDER_GUID, "nOrderId": 605126,
                  "ReferenceNum": "REF-605126", "Source": "SHOPIFY"}]
            )],
            order_details=[order],
        )
        with patch.object(server, "call_linnworks", side_effect=dispatch):
            result = server.find_unlinked_order_lines(
                include_open=False, from_date="2026-01-01", to_date="2026-01-31",
                only_problems=False,
            )

        assert result["counts"]["linked"] == 1
        assert result["counts"]["unlinked"] == 1
        assert result["counts"]["unknown"] == 0
        assert result["order_count_scanned"] == 1
        assert result["complete"] is True

        assert len(result["orders"]) == 1
        order_row = result["orders"][0]
        assert order_row["order_id"] == PROCESSED_ORDER_GUID
        assert order_row["source"] == "SHOPIFY"
        assert order_row["processed"] is True
        assert len(order_row["lines"]) == 2
        statuses = {l["sku"]: l["status"] for l in order_row["lines"]}
        assert statuses["LOB-CLA-CAR"] == "linked"
        assert statuses["IND-SKT-5149"] == "unlinked"

    def test_unknown_is_first_class_never_counted_linked_or_unlinked(self):
        """AC4."""
        order = _raw_order(
            PROCESSED_ORDER_GUID, 1, "SHOPIFY", True,
            [_raw_item("STRIPPED-SKU")],  # ItemNumber/ItemSource both absent
        )
        dispatch = _make_dispatch(
            processed_pages=[_processed_orders_page(
                [{"pkOrderID": PROCESSED_ORDER_GUID, "nOrderId": 1, "Source": "SHOPIFY"}]
            )],
            order_details=[order],
        )
        with patch.object(server, "call_linnworks", side_effect=dispatch):
            result = server.find_unlinked_order_lines(
                include_open=False, from_date="2026-01-01", to_date="2026-01-31",
                only_problems=False,
            )

        assert result["counts"]["unknown"] == 1
        assert result["counts"]["linked"] == 0
        assert result["counts"]["unlinked"] == 0

    def test_manual_order_and_composite_child_excluded_from_unlinked(self):
        """AC5 — a manually created order and a composite line are both
        reported in their own not_expected bucket, not counted as unlinked."""
        manual_order = _raw_order(
            "guid-manual", 595169, "DIRECT", True,
            [_raw_item("H159ORANGEL/XL", item_number="", item_source="")],
        )
        child = _raw_item("blackgrip-9x33", item_number="blackgrip-9x33", item_source="")
        composite_order = _raw_order(
            "guid-composite", 605126, "SHOPIFY", True,
            [_raw_item("swh_GRIP_OPTION", item_number="p1", item_source="SHOPIFY", sub_items=[child])],
        )
        dispatch = _make_dispatch(
            processed_pages=[_processed_orders_page([
                {"pkOrderID": "guid-manual", "nOrderId": 595169, "Source": "DIRECT"},
                {"pkOrderID": "guid-composite", "nOrderId": 605126, "Source": "SHOPIFY"},
            ])],
            order_details=[manual_order, composite_order],
        )
        with patch.object(server, "call_linnworks", side_effect=dispatch):
            result = server.find_unlinked_order_lines(
                include_open=False, from_date="2026-01-01", to_date="2026-01-31",
                only_problems=False,
            )

        assert result["counts"]["unlinked"] == 0
        assert result["counts"]["not_expected"] == 2  # the manual line + the composite child

        by_order = {o["order_id"]: o for o in result["orders"]}
        manual_line = by_order["guid-manual"]["lines"][0]
        assert manual_line["status"] == "not_expected"
        assert manual_line["reason"] == "manual_order"

        composite_lines = by_order["guid-composite"]["lines"]
        assert composite_lines[0]["status"] == "linked"  # the parent line IS linked
        assert composite_lines[1]["status"] == "not_expected"
        assert composite_lines[1]["reason"] == "composite_child"

    def test_composite_children_walked_on_a_currently_open_order(self):
        """AC6 — open-order shape (Processed: False), via GetOrdersById."""
        child = _raw_item("blackgrip-9x33", item_number="blackgrip-9x33", item_source="")
        order = _raw_order(
            OPEN_ORDER_GUID, 608000, "SHOPIFY", False,
            [_raw_item("swh_GRIP_OPTION", item_number="p1", item_source="SHOPIFY", sub_items=[child])],
        )
        dispatch = _make_dispatch(
            open_resp=_open_orders_response([(OPEN_ORDER_GUID, "REF-608000")]),
            order_details=[order],
        )
        with patch.object(server, "call_linnworks", side_effect=dispatch):
            result = server.find_unlinked_order_lines(include_open=True, only_problems=False)

        order_row = result["orders"][0]
        assert order_row["processed"] is False
        assert len(order_row["lines"]) == 2
        assert order_row["lines"][1]["is_composite_child"] is True
        assert order_row["lines"][1]["status"] == "not_expected"

    def test_composite_children_walked_on_a_processed_order(self):
        """AC6 — processed-order shape (Processed: True), same endpoint,
        same nesting key — proven identical to the open-order case above."""
        child = _raw_item("blackgrip-9x33", item_number="blackgrip-9x33", item_source="")
        order = _raw_order(
            PROCESSED_ORDER_GUID, 605126, "SHOPIFY", True,
            [_raw_item("swh_GRIP_OPTION", item_number="p1", item_source="SHOPIFY", sub_items=[child])],
        )
        dispatch = _make_dispatch(
            processed_pages=[_processed_orders_page(
                [{"pkOrderID": PROCESSED_ORDER_GUID, "nOrderId": 605126, "Source": "SHOPIFY"}]
            )],
            order_details=[order],
        )
        with patch.object(server, "call_linnworks", side_effect=dispatch):
            result = server.find_unlinked_order_lines(
                include_open=False, from_date="2026-01-01", to_date="2026-01-31",
                only_problems=False,
            )

        order_row = result["orders"][0]
        assert order_row["processed"] is True
        assert len(order_row["lines"]) == 2
        assert order_row["lines"][1]["is_composite_child"] is True
        assert order_row["lines"][1]["status"] == "not_expected"

    def test_rate_limited_batch_never_reads_as_complete_with_zero_findings(self):
        """AC7 — a mocked RateLimitError during the scan is bucketed
        separately, and `complete` is False. The run must never look like a
        clean "0 unlinked found" when it was actually throttled."""
        dispatch = _make_dispatch(
            processed_pages=[_processed_orders_page(
                [{"pkOrderID": PROCESSED_ORDER_GUID, "nOrderId": 1, "Source": "SHOPIFY"}]
            )],
            raise_on_details=True,
        )
        with patch.object(server, "call_linnworks", side_effect=dispatch):
            result = server.find_unlinked_order_lines(
                include_open=False, from_date="2026-01-01", to_date="2026-01-31",
            )

        assert result["complete"] is False
        assert len(result["rate_limited_orders"]) == 1
        assert result["rate_limited_orders"][0]["order_id"] == PROCESSED_ORDER_GUID
        # The throttled order contributed nothing to any bucket — it is not
        # silently present as a "clean" zero.
        assert result["counts"] == {"linked": 0, "unlinked": 0, "unknown": 0, "not_expected": 0}
        assert result["order_count_scanned"] == 0
        # And it must never be possible to mistake this for a complete,
        # problem-free run just by reading `counts` in isolation.
        assert not (result["complete"] and sum(result["counts"].values()) == 0
                    and not result["rate_limited_orders"])

    def test_only_problems_filters_orders_but_not_top_level_counts(self):
        """only_problems=True (the default) still reports every line in the
        top-level counts; it only trims which orders are echoed back."""
        clean_order = _raw_order(
            "guid-clean", 1, "SHOPIFY", True, [LINKED_ITEM],
        )
        broken_order = _raw_order(
            "guid-broken", 2, "SHOPIFY", True, [UNLINKED_ITEM],
        )
        dispatch = _make_dispatch(
            processed_pages=[_processed_orders_page([
                {"pkOrderID": "guid-clean", "nOrderId": 1, "Source": "SHOPIFY"},
                {"pkOrderID": "guid-broken", "nOrderId": 2, "Source": "SHOPIFY"},
            ])],
            order_details=[clean_order, broken_order],
        )
        with patch.object(server, "call_linnworks", side_effect=dispatch):
            result = server.find_unlinked_order_lines(
                include_open=False, from_date="2026-01-01", to_date="2026-01-31",
            )

        assert result["only_problems"] is True
        assert result["counts"]["linked"] == 1
        assert result["counts"]["unlinked"] == 1
        # Only the broken order is echoed back.
        assert len(result["orders"]) == 1
        assert result["orders"][0]["order_id"] == "guid-broken"

    def test_raises_when_nothing_to_scan(self):
        with pytest.raises(ValueError):
            server.find_unlinked_order_lines(include_open=False)


# ══════════════════════════════════════════════════════════════════════════════
# AC10 — this build ships no write tool
# ══════════════════════════════════════════════════════════════════════════════

class TestNoWriteCapabilityShipped:
    def test_no_new_write_threshold_entry(self):
        assert "find_unlinked_order_lines" not in server.WRITE_THRESHOLDS

    def test_tool_has_no_dry_run_or_confirmed_count_parameter(self):
        import inspect
        sig = inspect.signature(server.find_unlinked_order_lines)
        assert "dry_run" not in sig.parameters
        assert "confirmed_count" not in sig.parameters


# ══════════════════════════════════════════════════════════════════════════════
# AC13 — no claim about how Linnworks internally matches despatch to line
# ══════════════════════════════════════════════════════════════════════════════

class TestDocstringDoesNotAssertInternalMatchingMechanism:
    def test_docstring_disclaims_internal_matching_mechanism(self):
        doc = server.find_unlinked_order_lines.__doc__ or ""
        assert "does not assert how Linnworks internally matches" in doc

    def test_docstring_never_claims_a_fix_is_available(self):
        doc = server.find_unlinked_order_lines.__doc__ or ""
        assert "REPORT, not a fix" in doc
        assert "already despatched" in doc


# ══════════════════════════════════════════════════════════════════════════════
# AC1 / AC8 — live findings recorded verbatim in CLAUDE.md
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture(scope="module")
def claude_md():
    from pathlib import Path
    return (Path(__file__).resolve().parent.parent / "CLAUDE.md").read_text()


class TestClaudeMdRecordsTheLiveFindings:
    def test_records_the_linked_line_example(self, claude_md):
        assert "16506671005945" in claude_md
        assert "LOB-CLA-CAR" in claude_md

    def test_records_the_unlinked_line_example(self, claude_md):
        assert "IND-SKT-5149" in claude_md

    def test_records_the_manual_order_example(self, claude_md):
        assert "595169" in claude_md
        assert "H159ORANGEL/XL" in claude_md

    def test_records_the_composite_child_example(self, claude_md):
        assert "blackgrip-9x33" in claude_md

    def test_records_the_endpoint_probe_responses(self, claude_md):
        assert "Orders/UpdateOrderItem" in claude_md
        assert "The request is invalid." in claude_md
        assert "No HTTP resource was found" in claude_md

    def test_does_not_add_a_does_not_work_row_for_endpoints_that_do_exist(self, claude_md):
        """AC9 only applies when no candidate endpoint is found. One was
        found (Orders/UpdateOrderItem exists), so it must not be listed in
        the 'Endpoints that do NOT work' table."""
        do_not_work_idx = claude_md.index("## Endpoints that do NOT work")
        next_section_idx = claude_md.index("\n## ", do_not_work_idx + 1)
        do_not_work_section = claude_md[do_not_work_idx:next_section_idx]
        assert "Orders/UpdateOrderItem" not in do_not_work_section
