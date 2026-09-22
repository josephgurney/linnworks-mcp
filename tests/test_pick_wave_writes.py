"""
Tests for the pickwave detail read + write tools (issue #67):
  get_pick_wave_detail, generate_pick_waves, update_pick_wave,
  remove_orders_from_pick_waves

Response shapes match the live reads recorded 22 Sep 2026 in
docs/superpowers/specs/2026-09-22-pickwave-write-tools-design.md. The three
write endpoints were NOT live-fired when these tests were written; the live
proof is Task 7 of docs/superpowers/plans/2026-09-22-pickwave-write-tools.md.

All tests use unittest.mock — no live Linnworks API calls.
Run with: pytest tests/test_pick_wave_writes.py -v
"""
import contextlib
import inspect
import os
import sys
from unittest.mock import patch

import pytest
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server

DEFAULT = server.DEFAULT_LOCATION_ID
SID = "11111111-2222-3333-4444-555555555555"
GUID_A = "aaaaaaaa-0000-0000-0000-00000000000a"
GUID_B = "bbbbbbbb-0000-0000-0000-00000000000b"
GUID_C = "cccccccc-0000-0000-0000-00000000000c"
WRITE_PATHS = (
    "Picking/GeneratePickingWave",
    "Picking/UpdatePickingWaveHeader",
    "Picking/DeleteOrdersFromPickingWaves",
)


def _wave_order(num, guid, *, locked=False, on_hold=False, cancelled=False, processed=False):
    """One order row as Picking/GetPickingWave returns it (live shape, 22 Sep 2026)."""
    return {
        "OrderId": num,
        "OrderId_Guid": guid,
        "PickState": "Unpicked",
        "SortOrder": 0,
        "IsLocked": locked,
        "IsOnHold": on_hold,
        "IsCancelled": cancelled,
        "IsProcessed": processed,
        "IsPaid": True,
        "Items": [{
            "PickingWaveItemsRowId": 50070,
            "OrderItemRowId": f"row-{num}",
            "StockItemId": SID,
            "ToPickQuantity": 1,
            "PickedQuantity": 0,
            "ItemState": "Normal",
        }],
    }


def _wave_resp(wave_id=9001, state="Unallocated", orders=(), user_id=None, email=None, start=None,
               items_picked=None):
    """A whole GetPickingWave response. UserId/EmailAddress are ABSENT when unassigned."""
    wave = {
        "PickingWaveId": wave_id,
        "State": state,
        "LocationId": DEFAULT,
        "OrderCount": len(orders),
        "ItemCount": len(orders),
        "GroupType": "Items",
        "SortingType": "BinPriority",
        "Orders": list(orders),
    }
    if user_id is not None:
        wave["UserId"] = user_id
        wave["EmailAddress"] = email
    if start is not None:
        wave["StartTime"] = start
    if items_picked is not None:
        wave["ItemsPicked"] = items_picked
    return {
        "PickingWaves": [wave],
        "Skus": [{"SKU": "SKU-1", "StockItemId": SID, "ItemTitle": "Thing"}],
        "Bins": [{"BinRack": "10-A-03", "StockItemId": SID}],
    }


def _body(payload):
    """The write body, whether or not _picking_write wrapped it in {"request": ...}."""
    return payload["request"] if "request" in payload else payload


class FakeLinnworks:
    """
    Routes call_linnworks / call_linnworks_get / _resolve_order_guid by endpoint
    and records every call. Write endpoints are handled by `on_write`
    callables (path -> fn(body) -> response), which may mutate `waves` /
    `headers` so the tool's read-back sees the effect.
    """

    def __init__(self, *, orders=None, roster=None, fifo_ready=(), unpickable=None,
                 waves=None, headers=None, on_write=None, raise_on=None):
        self.orders = orders or {}            # input id (str) -> (guid, num)
        self.roster = ({19: "jo@thewarehousegroup.co.uk",
                        68: "warehouse+01@thewarehousegroup.co.uk"}
                       if roster is None else roster)
        self.fifo_ready = {g.lower() for g in fifo_ready}
        self.unpickable = unpickable or {}    # num -> Linnworks error list
        self.waves = waves or {}              # wave_id -> GetPickingWave response
        self.headers = headers or {}          # state -> list of header rows
        self.on_write = on_write or {}
        self.raise_on = raise_on or {}        # endpoint (or "resolve") -> exception
        self.calls = []

    def resolve(self, order_id):
        if "resolve" in self.raise_on:
            raise self.raise_on["resolve"]
        key = str(order_id).strip()
        if key not in self.orders:
            raise RuntimeError(f"No order found for '{key}'.")
        guid, num = self.orders[key]
        return guid, {"OrderId": guid, "NumOrderId": num}

    def post(self, path, payload):
        self.calls.append(("POST", path, payload))
        if path in self.raise_on:
            raise self.raise_on[path]
        if path == "Picking/CheckAllocatableToPickwave":
            return {"Results": [
                {"OrderId": n, "HasErrors": n in self.unpickable,
                 "Errors": self.unpickable.get(n, [])}
                for n in payload["request"]["OrderIds"]
            ]}
        if path == "OpenOrders/GetIdentifiersByOrderIds":
            return [
                {"fkOrderId": g, "IdentifierId": 0, "IsCustom": False, "Tag": "FIFO_READY"}
                for g in payload["OrderIds"] if g.lower() in self.fifo_ready
            ]
        if path in self.on_write:
            return self.on_write[path](_body(payload))
        raise AssertionError(f"unexpected POST {path}")

    def get(self, path, params=None):
        self.calls.append(("GET", path, params))
        if path in self.raise_on:
            raise self.raise_on[path]
        if path == "Picking/GetPickwaveUsersWithSummary":
            rows = [{"PickingWaveId": 0, "UserId": u, "EmailAddress": e}
                    for u, e in self.roster.items()]
            rows.append({"PickingWaveId": 3549, "UserId": None, "EmailAddress": "Unallocated"})
            return {"PickingWaves": rows}
        if path == "Picking/GetPickingWave":
            return self.waves.get(params["pickingWaveId"],
                                  {"PickingWaves": [], "Skus": [], "Bins": []})
        if path == "Picking/GetAllPickingWaveHeaders":
            return {"PickwaveHeaders": self.headers.get(params.get("state"), [])}
        raise AssertionError(f"unexpected GET {path}")

    @contextlib.contextmanager
    def active(self):
        with patch.object(server, "call_linnworks", side_effect=self.post), \
             patch.object(server, "call_linnworks_get", side_effect=self.get), \
             patch.object(server, "_resolve_order_guid", side_effect=self.resolve):
            yield self

    def writes(self):
        return [c for c in self.calls if c[1] in WRITE_PATHS]


# ── get_pick_wave_detail ────────────────────────────────────────────────────

class TestGetPickWaveDetail:

    def test_joins_sku_title_and_bins_onto_items(self):
        fake = FakeLinnworks(waves={9001: _wave_resp(orders=[_wave_order(611385, GUID_A)])})
        with fake.active():
            out = server.get_pick_wave_detail(9001)
        assert out["found"] is True
        item = out["orders"][0]["items"][0]
        assert item["sku"] == "SKU-1"
        assert item["title"] == "Thing"
        assert item["bins"] == ["10-A-03"]
        assert item["to_pick"] == 1
        assert item["picked"] == 0

    def test_blockers_list_every_problem_flag(self):
        orders = [
            _wave_order(1, GUID_A, locked=True),
            _wave_order(2, GUID_B),
            _wave_order(3, GUID_C, cancelled=True, processed=True),
        ]
        fake = FakeLinnworks(waves={9001: _wave_resp(orders=orders)})
        with fake.active():
            out = server.get_pick_wave_detail(9001)
        assert out["blockers"] == [
            {"order_id": 1, "order_guid": GUID_A, "reasons": ["locked"]},
            {"order_id": 3, "order_guid": GUID_C, "reasons": ["cancelled", "processed"]},
        ]

    def test_empty_response_is_not_reported_as_an_empty_wave(self):
        with FakeLinnworks().active():
            out = server.get_pick_wave_detail(4242)
        assert out["found"] is False
        assert "FINISHED" in out["note"]
        assert "orders" not in out

    def test_rate_limited_is_not_reported_as_not_found(self):
        fake = FakeLinnworks(raise_on={
            "Picking/GetPickingWave": server.RateLimitError("quota exceeded")})
        with fake.active():
            out = server.get_pick_wave_detail(9001)
        assert out["found"] is None
        assert out["rate_limited"] is True
        assert out["complete"] is False

    def test_assigned_user_is_read_from_the_detail(self):
        fake = FakeLinnworks(waves={9001: _wave_resp(
            state="InProgress", user_id=69, email="warehouse+02@thewarehousegroup.co.uk",
            orders=[_wave_order(1, GUID_A)])})
        with fake.active():
            out = server.get_pick_wave_detail(9001)
        assert out["user_id"] == 69
        assert out["state"] == "InProgress"

    def test_unassigned_wave_has_no_user(self):
        fake = FakeLinnworks(waves={9001: _wave_resp(orders=[_wave_order(1, GUID_A)])})
        with fake.active():
            out = server.get_pick_wave_detail(9001)
        assert out["user_id"] is None

    def test_uses_the_shared_state_labeller(self):
        fake = FakeLinnworks(waves={9001: _wave_resp(state="Paused")})
        with fake.active():
            out = server.get_pick_wave_detail(9001)
        assert out["state_label"] == server._pick_wave_state_label("Paused")

    def test_is_a_read_tool(self):
        assert "dry_run" not in inspect.signature(server.get_pick_wave_detail).parameters
        assert "get_pick_wave_detail" not in server.WRITE_THRESHOLDS

    # M1 — the wave is selected by id, never just "the first one".
    def test_a_response_for_a_different_wave_is_not_found(self):
        fake = FakeLinnworks(waves={9001: _wave_resp(wave_id=9999, orders=[_wave_order(1, GUID_A)])})
        with fake.active():
            out = server.get_pick_wave_detail(9001)
        assert out["found"] is False
        assert "orders" not in out

    def test_format_detail_picks_the_wave_with_the_matching_id(self):
        resp = _wave_resp(wave_id=9999, orders=[_wave_order(1, GUID_A)])
        resp["PickingWaves"].append(
            _wave_resp(wave_id=9001, orders=[_wave_order(2, GUID_B)])["PickingWaves"][0])
        detail = server._format_pick_wave_detail(resp, 9001)
        assert detail["picking_wave_id"] == 9001
        assert [o["order_id"] for o in detail["orders"]] == [2]
        assert server._format_pick_wave_detail(resp, 4242) is None
        # Without an id the first wave is still used (unchanged behaviour).
        assert server._format_pick_wave_detail(resp)["picking_wave_id"] == 9999


# ── Shared pre-write helpers ────────────────────────────────────────────────

class TestSharedHelpers:

    def test_resolve_order_numbers_splits_resolved_from_errors(self):
        fake = FakeLinnworks(orders={"611385": (GUID_A, 611385)})
        with fake.active():
            resolved, errors, limited = server._resolve_order_numbers(["611385", "999"])
        assert resolved == [("611385", 611385, GUID_A)]
        assert [e["order_id"] for e in errors] == ["999"]
        assert limited == []

    def test_resolve_order_numbers_accepts_ints(self):
        fake = FakeLinnworks(orders={"611385": (GUID_A, 611385)})
        with fake.active():
            resolved, _, _ = server._resolve_order_numbers([611385])
        assert resolved == [(611385, 611385, GUID_A)]

    def test_resolve_order_numbers_reports_rate_limit_separately(self):
        fake = FakeLinnworks(raise_on={"resolve": server.RateLimitError("quota exceeded")})
        with fake.active():
            resolved, errors, limited = server._resolve_order_numbers(["611385"])
        assert resolved == []
        assert errors == []
        assert limited == [{"order_id": "611385", "reason": "quota exceeded"}]

    def test_picker_roster_skips_unassigned_rows_and_asks_for_unallocated(self):
        with FakeLinnworks().active() as fake:
            roster = server._fetch_picker_roster()
        assert roster == {
            19: "jo@thewarehousegroup.co.uk",
            68: "warehouse+01@thewarehousegroup.co.uk",
        }
        params = [c[2] for c in fake.calls if c[1] == "Picking/GetPickwaveUsersWithSummary"][0]
        assert params["state"] == "Unallocated"

    def test_fifo_ready_read_is_unwrapped(self):
        fake = FakeLinnworks(fifo_ready=[GUID_A])
        with fake.active():
            ready = server._fetch_fifo_ready_guids([GUID_A, GUID_B])
        assert ready == {GUID_A}
        call = [c for c in fake.calls if c[1] == "OpenOrders/GetIdentifiersByOrderIds"][0]
        assert call[2] == {"OrderIds": [GUID_A, GUID_B]}

    def test_fifo_ready_read_chunks_at_100(self):
        guids = [f"{i:08d}-0000-0000-0000-000000000000" for i in range(150)]
        with FakeLinnworks().active() as fake:
            server._fetch_fifo_ready_guids(guids)
        sizes = [len(c[2]["OrderIds"]) for c in fake.calls
                 if c[1] == "OpenOrders/GetIdentifiersByOrderIds"]
        assert sizes == [100, 50]

    def test_check_pickable_numbers_maps_linnworks_errors(self):
        err = [{"Error": "Order doesn't exist", "ErrorType": "OrderDoesntExist"}]
        with FakeLinnworks(unpickable={2: err}).active():
            out = server._check_pickable_numbers([1, 2])
        assert out == {1: {"pickable": True, "errors": []}, 2: {"pickable": False, "errors": err}}

    def test_check_pickable_numbers_makes_no_call_for_an_empty_list(self):
        with FakeLinnworks().active() as fake:
            assert server._check_pickable_numbers([]) == {}
        assert fake.calls == []

    def test_find_wave_header_reads_the_state_list(self):
        hdr = {"PickingWaveId": 7, "State": "Abandoned", "LocationId": DEFAULT}
        with FakeLinnworks(headers={"Abandoned": [hdr]}).active():
            assert server._find_wave_header(7, "Abandoned")["picking_wave_id"] == 7
            assert server._find_wave_header(8, "Abandoned") is None

    def test_picking_write_follows_the_per_endpoint_wrapper_setting(self):
        fake = FakeLinnworks(on_write={"Picking/DeleteOrdersFromPickingWaves": lambda b: {}})
        with fake.active():
            server._picking_write("Picking/DeleteOrdersFromPickingWaves", {"OrderIds": [1]})
        raw = fake.writes()[0][2]
        wrapped = server._PICKING_WRITE_WRAPPED["Picking/DeleteOrdersFromPickingWaves"]
        assert ("request" in raw) is wrapped
        assert _body(raw) == {"OrderIds": [1]}

    def test_settable_states_are_exactly_the_three_agreed(self):
        assert server._PICK_WAVE_SETTABLE_STATES == ("Abandoned", "Paused", "Unallocated")

    # C1 — Generate and UpdateHeader go unwrapped first; Delete stays wrapped.
    def test_write_wrapper_flags_follow_the_c1_ruling(self):
        assert server._PICKING_WRITE_WRAPPED == {
            "Picking/GeneratePickingWave": False,
            "Picking/UpdatePickingWaveHeader": False,
            "Picking/DeleteOrdersFromPickingWaves": True,
        }

    def test_started_and_all_state_sets(self):
        assert server._PICK_WAVE_STARTED_STATES == ("InProgress", "Paused", "Complete", "Packing")
        assert set(server._PICK_WAVE_ALL_STATES) == {
            "Unallocated", "Allocated", "InProgress", "Paused",
            "Complete", "Abandoned", "Packing", "Shipped",
        }

    # M6 — registration.
    def test_write_threshold_registration(self):
        assert server.WRITE_THRESHOLDS["generate_pick_waves"] == 25
        assert server.WRITE_THRESHOLDS["remove_orders_from_pick_waves"] == 25
        assert "update_pick_wave" not in server.WRITE_THRESHOLDS
        assert "get_pick_wave_detail" not in server.WRITE_THRESHOLDS

    def test_as_int(self):
        assert server._as_int("611385") == 611385
        assert server._as_int(None) is None
        assert server._as_int("x") is None


# ── generate_pick_waves ─────────────────────────────────────────────────────

ORDERS = {
    "611385": (GUID_A, 611385),
    "611386": (GUID_B, 611386),
    "611387": (GUID_C, 611387),
    GUID_C: (GUID_C, 611387),
}


def _generate_creates(fake, wave_ids):
    """on_write handler: each call creates the next wave id and registers it for read-back."""
    ids = list(wave_ids)

    def handler(body):
        wid = ids.pop(0)
        orders = [_wave_order(o["OrderId"], f"guid-{o['OrderId']}") for o in body["Orders"]]
        fake.waves[wid] = _wave_resp(wave_id=wid, orders=orders, user_id=body.get("UserId"))
        return {"ValidationResults": [], "PickingWaves": [{"PickingWaveId": wid}],
                "Skus": [], "Bins": []}
    return handler


class TestGenerateRefusesBeforeAnyCall:

    def test_empty_waves(self):
        with FakeLinnworks().active() as fake:
            out = server.generate_pick_waves([])
        assert out["success"] is False
        assert fake.calls == []

    def test_bad_sorting_type(self):
        with FakeLinnworks(orders=ORDERS).active() as fake:
            out = server.generate_pick_waves(
                [{"order_ids": ["611385"], "sorting_type": "Alphabetical"}])
        assert out["success"] is False
        assert "sorting_type" in out["error"]
        assert fake.calls == []

    def test_non_integer_user_id(self):
        with FakeLinnworks(orders=ORDERS).active() as fake:
            out = server.generate_pick_waves([{"order_ids": ["611385"], "user_id": "19"}])
        assert out["success"] is False
        assert fake.calls == []

    def test_order_in_two_waves(self):
        with FakeLinnworks(orders=ORDERS).active() as fake:
            out = server.generate_pick_waves(
                [{"order_ids": ["611385"]}, {"order_ids": ["611385"]}])
        assert out["success"] is False
        assert "more than once" in out["error"]
        assert fake.calls == []


class TestGenerateRefusesBeforeAnyWrite:

    def test_same_order_by_guid_and_by_number(self):
        with FakeLinnworks(orders=ORDERS).active() as fake:
            out = server.generate_pick_waves(
                [{"order_ids": ["611387"]}, {"order_ids": [GUID_C]}])
        assert out["success"] is False
        assert "611387" in out["error"]
        assert fake.writes() == []

    def test_unknown_user_id(self):
        with FakeLinnworks(orders=ORDERS).active() as fake:
            out = server.generate_pick_waves([{"order_ids": ["611385"], "user_id": 999}])
        assert out["success"] is False
        assert "not a Linnworks picker" in out["error"]
        assert fake.writes() == []

    def test_preflight_rate_limit_writes_nothing(self):
        fake = FakeLinnworks(orders=ORDERS, raise_on={
            "Picking/CheckAllocatableToPickwave": server.RateLimitError("quota exceeded")})
        with fake.active():
            out = server.generate_pick_waves([{"order_ids": ["611385"]}], dry_run=False)
        assert out["success"] is False
        assert out["complete"] is False
        assert out["rate_limited"]
        assert fake.writes() == []

    def test_roster_failure_is_a_structured_refusal(self):
        fake = FakeLinnworks(orders=ORDERS, raise_on={
            "Picking/GetPickwaveUsersWithSummary": RuntimeError("HTTP 500 — boom")})
        with fake.active():
            out = server.generate_pick_waves(
                [{"order_ids": ["611385"], "user_id": 19}], dry_run=False)
        assert out["success"] is False
        assert "HTTP 500 — boom" in out["error"]
        assert "nothing was written" in out["error"]
        assert fake.writes() == []

    def test_pickability_failure_is_a_structured_refusal(self):
        fake = FakeLinnworks(orders=ORDERS, raise_on={
            "Picking/CheckAllocatableToPickwave": RuntimeError("HTTP 400 — bad")})
        with fake.active():
            out = server.generate_pick_waves([{"order_ids": ["611385"]}], dry_run=False)
        assert out["success"] is False
        assert "HTTP 400 — bad" in out["error"]
        assert "nothing was written" in out["error"]
        assert fake.writes() == []

    # M6 — a 429 at the FIFO and roster call sites.
    def test_fifo_rate_limit_writes_nothing(self):
        fake = FakeLinnworks(orders=ORDERS, raise_on={
            "OpenOrders/GetIdentifiersByOrderIds": server.RateLimitError("quota exceeded")})
        with fake.active():
            out = server.generate_pick_waves([{"order_ids": ["611385"]}], dry_run=False)
        assert out["success"] is False
        assert out["complete"] is False
        assert out["rate_limited"][0]["step"] == "FIFO_READY check"
        assert fake.writes() == []

    def test_roster_rate_limit_writes_nothing(self):
        fake = FakeLinnworks(orders=ORDERS, raise_on={
            "Picking/GetPickwaveUsersWithSummary": server.RateLimitError("quota exceeded")})
        with fake.active():
            out = server.generate_pick_waves(
                [{"order_ids": ["611385"], "user_id": 19}], dry_run=False)
        assert out["success"] is False
        assert out["complete"] is False
        assert out["rate_limited"][0]["step"] == "picker roster"
        assert fake.writes() == []


class TestGenerateDryRun:

    def test_unresolved_order_blocks_only_its_own_wave(self):
        with FakeLinnworks(orders=ORDERS).active() as fake:
            out = server.generate_pick_waves(
                [{"order_ids": ["611385"]}, {"order_ids": ["nope"]}])
        assert out["dry_run"] is True
        assert out["manifest"][0]["blocked"] is False
        assert out["manifest"][1]["blocked"] is True
        assert fake.writes() == []

    def test_flags_unpickable_and_missing_fifo_without_writing(self):
        err = [{"Error": "Order is already in a pickwave"}]
        fake = FakeLinnworks(orders=ORDERS, unpickable={611385: err}, fifo_ready=[GUID_B])
        with fake.active():
            out = server.generate_pick_waves([{"order_ids": ["611385", "611386"]}])
        wave = out["manifest"][0]
        joined = " | ".join(wave["warnings"])
        assert "611385" in joined and "not pickable" in joined
        assert "not tagged FIFO_READY" in joined
        assert wave["blocked"] is False
        assert wave["orders"][0]["pickable"] is False
        assert wave["orders"][1]["fifo_ready"] is True
        assert fake.writes() == []

    def test_fifo_check_failure_is_skipped_not_passed(self):
        fake = FakeLinnworks(orders=ORDERS, raise_on={
            "OpenOrders/GetIdentifiersByOrderIds": RuntimeError("HTTP 500")})
        with fake.active():
            out = server.generate_pick_waves([{"order_ids": ["611385"]}])
        assert any("SKIPPED" in w for w in out["warnings"])
        assert out["manifest"][0]["orders"][0]["fifo_ready"] is None

    def test_stages_above_25_orders(self):
        many = {str(700000 + i): (f"{i:08d}-0000-0000-0000-000000000000", 700000 + i)
                for i in range(26)}
        with FakeLinnworks(orders=many).active() as fake:
            out = server.generate_pick_waves(
                [{"order_ids": list(many)}], dry_run=False)
        assert out["staged"] is True
        assert out["item_count"] == 26
        assert "manifest" in out
        assert fake.writes() == []


def _many_orders(n=26):
    return {str(700000 + i): (f"{i:08d}-0000-0000-0000-000000000000", 700000 + i)
            for i in range(n)}


class TestGenerateStaging:
    """M6 — the confirmed_count echo for a batch above the 25-order threshold."""

    def test_wrong_confirmed_count_is_an_error_and_writes_nothing(self):
        many = _many_orders()
        with FakeLinnworks(orders=many).active() as fake:
            out = server.generate_pick_waves(
                [{"order_ids": list(many)}], confirmed_count=25, dry_run=False)
        assert out["success"] is False
        assert out["staged"] is False
        assert "does not match" in out["message"]
        assert fake.writes() == []

    def test_matching_confirmed_count_writes(self):
        many = _many_orders()
        fake = FakeLinnworks(orders=many)
        fake.on_write["Picking/GeneratePickingWave"] = _generate_creates(fake, [9001])
        with fake.active():
            out = server.generate_pick_waves(
                [{"order_ids": list(many)}], confirmed_count=26, dry_run=False)
        assert len(fake.writes()) == 1
        assert len(_body(fake.writes()[0][2])["Orders"]) == 26
        assert out["results"][0]["outcome"] == "created"
        assert out["results"][0]["readback_matches"] is True


class TestGenerateLive:

    def test_sends_the_documented_body_and_reads_back(self):
        fake = FakeLinnworks(orders=ORDERS, fifo_ready=[GUID_A, GUID_B])
        fake.on_write["Picking/GeneratePickingWave"] = _generate_creates(fake, [9001])
        with fake.active():
            out = server.generate_pick_waves(
                [{"order_ids": ["611385", "611386"], "user_id": 19}], dry_run=False)
        result = out["results"][0]
        assert result["outcome"] == "created"
        assert result["picking_wave_ids"] == [9001]
        assert result["readback_matches"] is True
        assert out["complete"] is True
        assert _body(fake.writes()[0][2]) == {
            "LocationId": DEFAULT,
            "SortingType": "BinPriority",
            "GroupType": "Items",
            "UserId": 19,
            "Orders": [{"OrderId": 611385, "SortOrder": 0}, {"OrderId": 611386, "SortOrder": 1}],
        }

    def test_unassigned_wave_omits_user_id(self):
        fake = FakeLinnworks(orders=ORDERS)
        fake.on_write["Picking/GeneratePickingWave"] = _generate_creates(fake, [9001])
        with fake.active():
            server.generate_pick_waves([{"order_ids": ["611385"]}], dry_run=False)
        assert "UserId" not in _body(fake.writes()[0][2])

    def test_partial_run_says_not_to_rerun_the_batch(self):
        fake = FakeLinnworks(orders=ORDERS)
        creates = _generate_creates(fake, [9001])
        responses = iter([
            creates,
            lambda body: {"ValidationResults": [{"OrderId": 611386, "HasErrors": True,
                          "Errors": [{"Error": "Order is already in a pickwave"}]}],
                          "PickingWaves": []},
        ])
        fake.on_write["Picking/GeneratePickingWave"] = lambda body: next(responses)(body)
        with fake.active():
            out = server.generate_pick_waves(
                [{"order_ids": ["611385"]}, {"order_ids": ["611386"]}], dry_run=False)
        assert out["results"][0]["outcome"] == "created"
        assert out["results"][1]["outcome"] == "refused"
        assert out["results"][1]["validation_results"][0]["OrderId"] == 611386
        assert out["created_wave_ids"] == [9001]
        assert "Do NOT re-run" in out["message"]
        assert out["complete"] is False

    def test_rate_limited_write_is_its_own_outcome(self):
        fake = FakeLinnworks(orders=ORDERS, raise_on={
            "Picking/GeneratePickingWave": server.RateLimitError("quota exceeded")})
        with fake.active():
            out = server.generate_pick_waves([{"order_ids": ["611385"]}], dry_run=False)
        assert out["results"][0]["outcome"] == "rate_limited"
        assert out["complete"] is False

    def test_readback_failure_is_unconfirmed_not_created(self):
        fake = FakeLinnworks(orders=ORDERS)
        # Linnworks reports a new wave, but it never reads back.
        fake.on_write["Picking/GeneratePickingWave"] = lambda body: {
            "ValidationResults": [], "PickingWaves": [{"PickingWaveId": 9001}]}
        with fake.active():
            out = server.generate_pick_waves([{"order_ids": ["611385"]}], dry_run=False)
        assert out["results"][0]["outcome"] == "unconfirmed"
        assert out["created_wave_ids"] == [9001]
        assert out["complete"] is False

    def test_body_wrapping_follows_the_flag(self):
        fake = FakeLinnworks(orders=ORDERS)
        fake.on_write["Picking/GeneratePickingWave"] = _generate_creates(fake, [9001])
        with fake.active():
            server.generate_pick_waves([{"order_ids": ["611385"]}], dry_run=False)
        raw = fake.writes()[0][2]
        assert ("request" in raw) is server._PICKING_WRITE_WRAPPED["Picking/GeneratePickingWave"]

    # I1 — an ambiguous response is never reported as a refusal.
    def test_empty_response_is_unconfirmed_not_refused(self):
        fake = FakeLinnworks(orders=ORDERS)
        fake.on_write["Picking/GeneratePickingWave"] = lambda body: {}
        with fake.active():
            out = server.generate_pick_waves([{"order_ids": ["611385"]}], dry_run=False)
        result = out["results"][0]
        assert result["outcome"] == "unconfirmed"
        assert result["validation_results"] == []
        assert result["raw_response"] == {}
        assert "get_pick_waves(state='Unallocated')" in result["note"]
        assert out["complete"] is False
        assert "[0] may exist" in out["message"]

    def test_list_response_is_unconfirmed_without_crashing(self):
        fake = FakeLinnworks(orders=ORDERS)
        fake.on_write["Picking/GeneratePickingWave"] = lambda body: [{"PickingWaveId": n}
                                                                     for n in range(300)]
        with fake.active():
            out = server.generate_pick_waves([{"order_ids": ["611385"]}], dry_run=False)
        result = out["results"][0]
        assert result["outcome"] == "unconfirmed"
        assert result["validation_results"] == []
        assert isinstance(result["raw_response"], str)
        assert result["raw_response"].startswith("[{'PickingWaveId': 0}")
        assert len(result["raw_response"]) == 500
        assert out["created_wave_ids"] == []

    def test_raw_text_response_is_unconfirmed_and_kept_verbatim(self):
        fake = FakeLinnworks(orders=ORDERS)
        fake.on_write["Picking/GeneratePickingWave"] = lambda body: {"raw": "OK"}
        with fake.active():
            out = server.generate_pick_waves([{"order_ids": ["611385"]}], dry_run=False)
        assert out["results"][0]["outcome"] == "unconfirmed"
        assert out["results"][0]["raw_response"] == {"raw": "OK"}

    # I2 — a network error after wave 1 must not hide wave 1.
    def test_network_error_on_a_later_wave_is_unconfirmed_and_named(self):
        fake = FakeLinnworks(orders=ORDERS)
        creates = _generate_creates(fake, [9001])
        calls = {"n": 0}

        def handler(body):
            calls["n"] += 1
            if calls["n"] == 2:
                raise requests.exceptions.Timeout("read timed out")
            return creates(body)
        fake.on_write["Picking/GeneratePickingWave"] = handler
        with fake.active():
            out = server.generate_pick_waves(
                [{"order_ids": ["611385"]}, {"order_ids": ["611386"]}], dry_run=False)
        assert out["results"][0]["outcome"] == "created"
        second = out["results"][1]
        assert second["outcome"] == "unconfirmed"
        assert second["error"] == "Timeout: read timed out"
        assert "may have reached Linnworks" in second["note"]
        assert out["created_wave_ids"] == [9001]
        assert "[9001]" in out["message"]
        assert "wave index(es) [1] may exist" in out["message"]
        assert "check get_pick_waves before re-sending" in out["message"]
        assert "re-send only" not in out["message"]
        assert out["complete"] is False

    def test_os_error_is_caught_too(self):
        fake = FakeLinnworks(orders=ORDERS, raise_on={
            "Picking/GeneratePickingWave": OSError("connection reset")})
        with fake.active():
            out = server.generate_pick_waves([{"order_ids": ["611385"]}], dry_run=False)
        assert out["results"][0]["outcome"] == "unconfirmed"
        assert out["results"][0]["error"] == "OSError: connection reset"

    def test_unconfirmed_waves_are_not_in_the_resend_list(self):
        fake = FakeLinnworks(orders=ORDERS)
        creates = _generate_creates(fake, [9001])
        refusal = {"ValidationResults": [{"OrderId": 611386, "HasErrors": True,
                                          "Errors": [{"Error": "Order is already in a pickwave"}]}],
                   "PickingWaves": []}
        calls = {"n": 0}

        def handler(body):
            calls["n"] += 1
            if calls["n"] == 1:
                return creates(body)
            if calls["n"] == 2:
                return refusal
            raise requests.exceptions.ConnectionError("dropped")
        fake.on_write["Picking/GeneratePickingWave"] = handler
        with fake.active():
            out = server.generate_pick_waves(
                [{"order_ids": ["611385"]}, {"order_ids": ["611386"]}, {"order_ids": ["611387"]}],
                dry_run=False)
        assert [r["outcome"] for r in out["results"]] == ["created", "refused", "unconfirmed"]
        assert "re-send only wave index(es) [1]." in out["message"]
        assert "wave index(es) [2] may exist" in out["message"]
        assert "Do NOT re-run" in out["message"]

    # I3 — ValidationResults are never dropped, and a mismatched create is named.
    def test_created_wave_with_none_of_the_requested_orders_is_named(self):
        fake = FakeLinnworks(orders=ORDERS)

        def handler(body):
            fake.waves[9001] = _wave_resp(wave_id=9001, orders=[_wave_order(999999, GUID_C)])
            return {"ValidationResults": [], "PickingWaves": [{"PickingWaveId": 9001}]}
        fake.on_write["Picking/GeneratePickingWave"] = handler
        with fake.active():
            out = server.generate_pick_waves([{"order_ids": ["611385"]}], dry_run=False)
        result = out["results"][0]
        assert result["outcome"] == "created"
        assert result["readback_matches"] is False
        assert result["missing_order_ids"] == [611385]
        assert result["extra_order_ids"] == [999999]
        assert result["validation_results"] == []
        assert "wave 9001 does not match the request (missing [611385], extra [999999])" \
            in out["message"]
        assert out["complete"] is False

    def test_partial_create_keeps_its_validation_results(self):
        fake = FakeLinnworks(orders=ORDERS)
        reasons = [{"OrderId": 611386, "HasErrors": True,
                    "Errors": [{"Error": "Order is already in a pickwave"}]}]

        def handler(body):
            fake.waves[9001] = _wave_resp(wave_id=9001, orders=[_wave_order(611385, GUID_A)])
            return {"ValidationResults": reasons, "PickingWaves": [{"PickingWaveId": 9001}]}
        fake.on_write["Picking/GeneratePickingWave"] = handler
        with fake.active():
            out = server.generate_pick_waves([{"order_ids": ["611385", "611386"]}], dry_run=False)
        result = out["results"][0]
        assert result["outcome"] == "created"
        assert result["validation_results"] == reasons
        assert result["missing_order_ids"] == [611386]
        assert result["extra_order_ids"] == []
        assert "wave 9001 does not match the request (missing [611386], extra [])" in out["message"]

    def test_matching_create_reports_empty_differences(self):
        fake = FakeLinnworks(orders=ORDERS)
        fake.on_write["Picking/GeneratePickingWave"] = _generate_creates(fake, [9001])
        with fake.active():
            out = server.generate_pick_waves([{"order_ids": ["611385"]}], dry_run=False)
        result = out["results"][0]
        assert result["missing_order_ids"] == []
        assert result["extra_order_ids"] == []
        assert result["validation_results"] == []
        assert "does not match" not in out["message"]

    def test_every_live_result_carries_validation_results(self):
        fake = FakeLinnworks(orders=ORDERS, raise_on={
            "Picking/GeneratePickingWave": server.RateLimitError("quota exceeded")})
        with fake.active():
            out = server.generate_pick_waves(
                [{"order_ids": ["611385"]}, {"order_ids": ["nope"]}], dry_run=False)
        assert [r["outcome"] for r in out["results"]] == ["rate_limited", "blocked"]
        assert all(r["validation_results"] == [] for r in out["results"])

    # Deferred minor 5 — _read_back_generated_wave's failure branches.
    def test_generated_wave_readback_rate_limit_is_unconfirmed(self):
        fake = FakeLinnworks(raise_on={
            "Picking/GetPickingWave": server.RateLimitError("quota exceeded")})
        with fake.active():
            out = server._read_back_generated_wave([9001], [611385])
        assert out["outcome"] == "unconfirmed"
        assert out["picking_wave_ids"] == [9001]
        assert out["readback_error"].startswith("rate limited:")
        assert "quota exceeded" in out["readback_error"]

    def test_generated_wave_readback_error_is_unconfirmed(self):
        fake = FakeLinnworks(raise_on={"Picking/GetPickingWave": RuntimeError("HTTP 500 — boom")})
        with fake.active():
            out = server._read_back_generated_wave([9001], [611385])
        assert out["outcome"] == "unconfirmed"
        assert out["readback_error"] == "HTTP 500 — boom"

    # F — a plain exception (timeout, dropped connection) at the read-back
    # must not escape; it is unconfirmed like every other read-back failure.
    def test_generated_wave_readback_timeout_is_unconfirmed(self):
        fake = FakeLinnworks(raise_on={
            "Picking/GetPickingWave": requests.exceptions.Timeout("read timed out")})
        with fake.active():
            out = server._read_back_generated_wave([9001], [611385])
        assert out["outcome"] == "unconfirmed"
        assert out["readback_error"] == "Timeout: read timed out"


# ── update_pick_wave ────────────────────────────────────────────────────────

JO = "jo@thewarehousegroup.co.uk"


def _assigned_wave(state="Unallocated", start=None, items_picked=None):
    return _wave_resp(wave_id=9001, state=state, user_id=19, email=JO,
                      orders=[_wave_order(611385, GUID_A)], start=start,
                      items_picked=items_picked)


def _update_applies(fake):
    """on_write handler: apply the header update so the read-back sees it."""
    def handler(body):
        wave_id = body["PickingWaveId"]
        wave = fake.waves[wave_id]["PickingWaves"][0]
        wave["State"] = body["State"]
        if body.get("UserId") == -1:
            wave.pop("UserId", None)
            wave.pop("EmailAddress", None)
        elif "UserId" in body:
            wave["UserId"] = body["UserId"]
            wave["EmailAddress"] = fake.roster.get(body["UserId"])
        if body["State"] == "Abandoned":
            fake.headers.setdefault("Abandoned", []).append(dict(wave))
            del fake.waves[wave_id]
        return {"PickingWaves": []}
    return handler


class TestUpdateRefusesBeforeAnyCall:

    def test_user_id_and_unassign_together(self):
        with FakeLinnworks().active() as fake:
            out = server.update_pick_wave(9001, user_id=19, unassign=True)
        assert out["success"] is False
        assert fake.calls == []

    def test_nothing_to_change(self):
        with FakeLinnworks().active() as fake:
            out = server.update_pick_wave(9001)
        assert out["success"] is False
        assert fake.calls == []

    def test_progress_state_is_not_settable(self):
        with FakeLinnworks().active() as fake:
            out = server.update_pick_wave(9001, state="Shipped")
        assert out["success"] is False
        assert "physical work" in out["error"]
        assert fake.calls == []

    # Fix round 1 — the retracted "Allocated is set by assigning a user_id"
    # phrase (live fact 4: reassigning never moves the state) must not
    # reappear in this refusal message.
    def test_progress_state_refusal_does_not_claim_reassign_sets_allocated(self):
        with FakeLinnworks().active() as fake:
            out = server.update_pick_wave(9001, state="Shipped")
        assert "Allocated is set by" not in out["error"]
        assert "generate_pick_waves with a user_id creates the wave Allocated" in out["error"]
        assert "update_pick_wave never changes the state when you reassign" in out["error"]

    def test_non_positive_user_id(self):
        with FakeLinnworks().active() as fake:
            out = server.update_pick_wave(9001, user_id=0)
        assert out["success"] is False
        assert fake.calls == []


class TestUpdateChecks:

    def test_unknown_or_finished_wave_is_refused(self):
        with FakeLinnworks().active() as fake:
            out = server.update_pick_wave(4242, state="Paused")
        assert out["success"] is False
        assert "FINISHED" in out["error"]
        assert fake.writes() == []

    # I5 / deferred minor 9 — any started wave, not just InProgress.
    @pytest.mark.parametrize("target", ["Abandoned", "Unallocated"])
    @pytest.mark.parametrize("current,items_picked", [
        ("InProgress", None),
        ("Paused", None),
        ("Complete", None),
        ("Packing", None),
        ("Unallocated", 1),
    ])
    def test_resetting_a_started_wave_needs_the_flag(self, current, items_picked, target):
        fake = FakeLinnworks(waves={9001: _assigned_wave(state=current, items_picked=items_picked)})
        fake.on_write["Picking/UpdatePickingWaveHeader"] = _update_applies(fake)
        with fake.active():
            refused = server.update_pick_wave(9001, state=target, dry_run=False)
            assert fake.writes() == []
            allowed = server.update_pick_wave(9001, state=target, allow_in_progress=True)
        assert refused["success"] is False
        assert refused["current_state"] == current
        assert "allow_in_progress" in refused["error"]
        assert "trolley" in refused["error"]
        assert allowed["dry_run"] is True
        assert allowed["plan"]["new_state"] == target

    def test_abandoning_an_unstarted_wave_needs_no_flag(self):
        fake = FakeLinnworks(waves={9001: _assigned_wave(state="Unallocated", items_picked=0)})
        with fake.active():
            out = server.update_pick_wave(9001, state="Abandoned")
        assert out["dry_run"] is True
        assert out["plan"]["new_state"] == "Abandoned"

    def test_pausing_an_in_progress_wave_is_allowed(self):
        fake = FakeLinnworks(waves={9001: _assigned_wave(state="InProgress")})
        with fake.active():
            out = server.update_pick_wave(9001, state="Paused")
        assert out["dry_run"] is True
        assert out["plan"]["new_state"] == "Paused"

    def test_unknown_user_is_refused(self):
        fake = FakeLinnworks(waves={9001: _assigned_wave()})
        with fake.active():
            out = server.update_pick_wave(9001, user_id=999)
        assert out["success"] is False
        assert "not a Linnworks picker" in out["error"]
        assert fake.writes() == []

    def test_dry_run_shows_the_plan_and_writes_nothing(self):
        fake = FakeLinnworks(waves={9001: _assigned_wave()})
        with fake.active():
            out = server.update_pick_wave(9001, user_id=68)
        assert out["dry_run"] is True
        assert out["plan"]["current_user_id"] == 19
        assert out["plan"]["new_user_id"] == 68
        assert fake.writes() == []

    def test_wave_read_failure_is_a_structured_refusal(self):
        fake = FakeLinnworks(raise_on={"Picking/GetPickingWave": RuntimeError("HTTP 500 — boom")})
        with fake.active():
            out = server.update_pick_wave(9001, state="Paused", dry_run=False)
        assert out["success"] is False
        assert "HTTP 500 — boom" in out["error"]
        assert "nothing was written" in out["error"]
        assert fake.writes() == []

    def test_roster_failure_is_a_structured_refusal(self):
        fake = FakeLinnworks(waves={9001: _assigned_wave()},
                              raise_on={"Picking/GetPickwaveUsersWithSummary": RuntimeError("HTTP 500 — boom")})
        with fake.active():
            out = server.update_pick_wave(9001, user_id=68, dry_run=False)
        assert out["success"] is False
        assert "HTTP 500 — boom" in out["error"]
        assert "nothing was written" in out["error"]
        assert fake.writes() == []

    # M1 — a response for another wave id is not this wave.
    def test_response_for_another_wave_is_refused(self):
        resp = _wave_resp(wave_id=9999, orders=[_wave_order(611385, GUID_A)])
        fake = FakeLinnworks(waves={9001: resp})
        fake.on_write["Picking/UpdatePickingWaveHeader"] = lambda body: {}
        with fake.active():
            out = server.update_pick_wave(9001, state="Paused", dry_run=False)
        assert out["success"] is False
        assert "No live wave with that id" in out["error"]
        assert fake.writes() == []

    # M2 — an unknown current state is refused before any write.
    @pytest.mark.parametrize("raw_state", [None, "Weird"])
    def test_unknown_current_state_is_refused(self, raw_state):
        resp = _assigned_wave()
        wave = resp["PickingWaves"][0]
        if raw_state is None:
            del wave["State"]
        else:
            wave["State"] = raw_state
        fake = FakeLinnworks(waves={9001: resp})
        fake.on_write["Picking/UpdatePickingWaveHeader"] = lambda body: {}
        with fake.active():
            out = server.update_pick_wave(9001, user_id=68, dry_run=False)
        assert out["success"] is False
        assert out["current_state"] == raw_state
        assert "not a documented pickwave state" in out["error"]
        assert fake.writes() == []


class TestUpdateLive:

    def test_state_is_always_sent_even_for_a_reassign(self):
        fake = FakeLinnworks(waves={9001: _assigned_wave()})
        fake.on_write["Picking/UpdatePickingWaveHeader"] = _update_applies(fake)
        with fake.active():
            out = server.update_pick_wave(9001, user_id=68, dry_run=False)
        body = _body(fake.writes()[0][2])
        assert body["State"] == "Unallocated"
        assert body["UserId"] == 68
        assert out["outcome"] == "updated"
        assert out["after_user_id"] == 68

    def test_state_change_omits_user_id_so_the_user_is_kept(self):
        fake = FakeLinnworks(waves={9001: _assigned_wave()})
        fake.on_write["Picking/UpdatePickingWaveHeader"] = _update_applies(fake)
        with fake.active():
            out = server.update_pick_wave(9001, state="Paused", dry_run=False)
        body = _body(fake.writes()[0][2])
        assert "UserId" not in body
        assert body["State"] == "Paused"
        assert out["outcome"] == "updated"
        assert out["after_user_id"] == 19

    def test_unassign_sends_minus_one(self):
        fake = FakeLinnworks(waves={9001: _assigned_wave()})
        fake.on_write["Picking/UpdatePickingWaveHeader"] = _update_applies(fake)
        with fake.active():
            out = server.update_pick_wave(9001, unassign=True, dry_run=False)
        assert _body(fake.writes()[0][2])["UserId"] == -1
        assert out["outcome"] == "updated"
        assert out["after_user_id"] is None

    def test_carries_start_time_through(self):
        fake = FakeLinnworks(waves={9001: _assigned_wave(state="InProgress",
                                                         start="2026-09-22T08:00:00Z")})
        fake.on_write["Picking/UpdatePickingWaveHeader"] = _update_applies(fake)
        with fake.active():
            server.update_pick_wave(9001, state="Paused", dry_run=False)
        assert _body(fake.writes()[0][2])["StartTime"] == "2026-09-22T08:00:00Z"

    def test_abandon_is_read_back_from_the_abandoned_header_list(self):
        fake = FakeLinnworks(waves={9001: _assigned_wave()})
        fake.on_write["Picking/UpdatePickingWaveHeader"] = _update_applies(fake)
        with fake.active():
            out = server.update_pick_wave(9001, state="Abandoned", dry_run=False)
        assert out["outcome"] == "updated"
        assert out["after_state"] == "Abandoned"

    def test_not_applied_when_the_read_back_disagrees(self):
        fake = FakeLinnworks(waves={9001: _assigned_wave()})
        fake.on_write["Picking/UpdatePickingWaveHeader"] = lambda body: {}  # ignored
        with fake.active():
            out = server.update_pick_wave(9001, state="Paused", dry_run=False)
        assert out["outcome"] == "not_applied"
        assert out["complete"] is False

    def test_rate_limited_write(self):
        fake = FakeLinnworks(waves={9001: _assigned_wave()}, raise_on={
            "Picking/UpdatePickingWaveHeader": server.RateLimitError("quota exceeded")})
        with fake.active():
            out = server.update_pick_wave(9001, state="Paused", dry_run=False)
        assert out["outcome"] == "rate_limited"
        assert out["complete"] is False

    def test_readback_failure_is_unconfirmed(self):
        fake = FakeLinnworks(waves={9001: _assigned_wave()})

        def handler(body):
            fake.raise_on["Picking/GetPickingWave"] = RuntimeError("HTTP 500")
            return {}
        fake.on_write["Picking/UpdatePickingWaveHeader"] = handler
        with fake.active():
            out = server.update_pick_wave(9001, state="Paused", dry_run=False)
        assert out["outcome"] == "unconfirmed"

    # F — a timeout at the read-back must not escape either.
    def test_readback_timeout_is_unconfirmed(self):
        fake = FakeLinnworks(waves={9001: _assigned_wave()})

        def handler(body):
            fake.raise_on["Picking/GetPickingWave"] = requests.exceptions.Timeout("read timed out")
            return {}
        fake.on_write["Picking/UpdatePickingWaveHeader"] = handler
        with fake.active():
            out = server.update_pick_wave(9001, state="Paused", dry_run=False)
        assert out["outcome"] == "unconfirmed"
        assert out["readback_error"] == "Timeout: read timed out"

    def test_body_wrapping_follows_the_flag(self):
        fake = FakeLinnworks(waves={9001: _assigned_wave()})
        fake.on_write["Picking/UpdatePickingWaveHeader"] = _update_applies(fake)
        with fake.active():
            server.update_pick_wave(9001, state="Paused", dry_run=False)
        raw = fake.writes()[0][2]
        assert ("request" in raw) is server._PICKING_WRITE_WRAPPED["Picking/UpdatePickingWaveHeader"]

    # Deferred minor 8 — a plain Linnworks error from the write itself.
    def test_write_error_is_outcome_error(self):
        fake = FakeLinnworks(waves={9001: _assigned_wave()}, raise_on={
            "Picking/UpdatePickingWaveHeader": RuntimeError("HTTP 500 — boom")})
        with fake.active():
            out = server.update_pick_wave(9001, state="Paused", dry_run=False)
        assert out["outcome"] == "error"
        assert out["complete"] is False
        assert "HTTP 500 — boom" in out["error"]

    # F — a timeout on the write itself must not escape; it is unconfirmed
    # (the change may have gone through), not a plain error.
    def test_write_timeout_is_unconfirmed(self):
        fake = FakeLinnworks(waves={9001: _assigned_wave()}, raise_on={
            "Picking/UpdatePickingWaveHeader": OSError("connection reset")})
        with fake.active():
            out = server.update_pick_wave(9001, state="Paused", dry_run=False)
        assert out["outcome"] == "unconfirmed"
        assert out["complete"] is False
        assert out["error"] == "OSError: connection reset"
        assert "get_pick_wave_detail" in out["note"]

    # I4 — judge only what the caller asked to change; report the rest.
    def test_reassign_where_the_server_also_moves_the_state(self):
        fake = FakeLinnworks(waves={9001: _assigned_wave()})
        apply = _update_applies(fake)

        def handler(body):
            apply(body)
            fake.waves[9001]["PickingWaves"][0]["State"] = "Allocated"
            return {}
        fake.on_write["Picking/UpdatePickingWaveHeader"] = handler
        with fake.active():
            out = server.update_pick_wave(9001, user_id=68, dry_run=False)
        assert out["outcome"] == "updated"
        assert out["complete"] is True
        assert out["after_state"] == "Allocated"
        assert out["after_user_id"] == 68
        assert out["state_changed_by_server"] is True
        assert "user_changed_by_server" not in out

    def test_reassign_where_the_state_is_kept_reports_no_server_change(self):
        fake = FakeLinnworks(waves={9001: _assigned_wave()})
        fake.on_write["Picking/UpdatePickingWaveHeader"] = _update_applies(fake)
        with fake.active():
            out = server.update_pick_wave(9001, user_id=68, dry_run=False)
        assert out["outcome"] == "updated"
        assert out["state_changed_by_server"] is False

    def test_state_change_where_the_server_clears_the_user(self):
        fake = FakeLinnworks(waves={9001: _assigned_wave()})
        apply = _update_applies(fake)

        def handler(body):
            apply(body)
            fake.waves[9001]["PickingWaves"][0].pop("UserId", None)
            return {}
        fake.on_write["Picking/UpdatePickingWaveHeader"] = handler
        with fake.active():
            out = server.update_pick_wave(9001, state="Paused", dry_run=False)
        assert out["outcome"] == "updated"
        assert out["complete"] is True
        assert out["after_state"] == "Paused"
        assert out["after_user_id"] is None
        assert out["user_changed_by_server"] is True
        assert "state_changed_by_server" not in out

    def test_state_change_that_keeps_the_user_reports_no_server_change(self):
        fake = FakeLinnworks(waves={9001: _assigned_wave()})
        fake.on_write["Picking/UpdatePickingWaveHeader"] = _update_applies(fake)
        with fake.active():
            out = server.update_pick_wave(9001, state="Paused", dry_run=False)
        assert out["user_changed_by_server"] is False

    @pytest.mark.parametrize("read_back_user", [0, -1])
    def test_unassign_reading_back_zero_or_minus_one_counts_as_no_user(self, read_back_user):
        fake = FakeLinnworks(waves={9001: _assigned_wave()})
        apply = _update_applies(fake)

        def handler(body):
            apply(body)
            fake.waves[9001]["PickingWaves"][0]["UserId"] = read_back_user
            return {}
        fake.on_write["Picking/UpdatePickingWaveHeader"] = handler
        with fake.active():
            out = server.update_pick_wave(9001, unassign=True, dry_run=False)
        assert out["outcome"] == "updated"
        assert out["after_user_id"] == read_back_user

    def test_both_changed_and_state_ignored_is_not_applied(self):
        fake = FakeLinnworks(waves={9001: _assigned_wave()})
        apply = _update_applies(fake)

        def handler(body):
            apply({**body, "State": "Unallocated"})  # user applied, state not
            return {}
        fake.on_write["Picking/UpdatePickingWaveHeader"] = handler
        with fake.active():
            out = server.update_pick_wave(9001, user_id=68, state="Paused", dry_run=False)
        assert out["outcome"] == "not_applied"
        assert out["after_user_id"] == 68
        assert out["after_state"] == "Unallocated"

    def test_abandon_reports_user_change_only_when_the_header_carries_a_user(self):
        fake = FakeLinnworks(waves={9001: _assigned_wave()})
        fake.on_write["Picking/UpdatePickingWaveHeader"] = _update_applies(fake)
        with fake.active():
            out = server.update_pick_wave(9001, state="Abandoned", dry_run=False)
        assert out["outcome"] == "updated"
        assert out["user_changed_by_server"] is False

    def test_abandon_without_a_user_key_on_the_header_reports_nothing(self):
        fake = FakeLinnworks(waves={9001: _assigned_wave()})
        apply = _update_applies(fake)

        def handler(body):
            apply(body)
            for row in fake.headers["Abandoned"]:
                row.pop("UserId", None)
                row.pop("EmailAddress", None)
            return {}
        fake.on_write["Picking/UpdatePickingWaveHeader"] = handler
        with fake.active():
            out = server.update_pick_wave(9001, state="Abandoned", dry_run=False)
        assert out["outcome"] == "updated"
        assert "user_changed_by_server" not in out


# ── update_pick_wave: abandoning does NOT release the wave's orders ────────
# Live-confirmed 22 Sep 2026 (#67 contained test) — see CLAUDE.md.

class TestUpdateAbandonWarning:

    def test_abandoning_a_wave_with_orders_warns_on_dry_run(self):
        fake = FakeLinnworks(waves={9001: _assigned_wave()})
        with fake.active():
            out = server.update_pick_wave(9001, state="Abandoned")
        assert out["dry_run"] is True
        assert out["warning"] == server._PICK_WAVE_ABANDON_WARNING

    def test_abandoning_a_wave_with_orders_warns_live(self):
        fake = FakeLinnworks(waves={9001: _assigned_wave()})
        fake.on_write["Picking/UpdatePickingWaveHeader"] = _update_applies(fake)
        with fake.active():
            out = server.update_pick_wave(9001, state="Abandoned", dry_run=False)
        assert out["outcome"] == "updated"
        assert out["warning"] == server._PICK_WAVE_ABANDON_WARNING

    def test_abandoning_an_empty_wave_carries_no_warning_on_dry_run(self):
        fake = FakeLinnworks(waves={9001: _wave_resp(wave_id=9001, user_id=19, email=JO)})
        with fake.active():
            out = server.update_pick_wave(9001, state="Abandoned")
        assert out["dry_run"] is True
        assert "warning" not in out

    def test_abandoning_an_empty_wave_carries_no_warning_live(self):
        fake = FakeLinnworks(waves={9001: _wave_resp(wave_id=9001, user_id=19, email=JO)})
        fake.on_write["Picking/UpdatePickingWaveHeader"] = _update_applies(fake)
        with fake.active():
            out = server.update_pick_wave(9001, state="Abandoned", dry_run=False)
        assert out["outcome"] == "updated"
        assert "warning" not in out

    def test_non_abandon_change_carries_no_warning(self):
        fake = FakeLinnworks(waves={9001: _assigned_wave()})
        with fake.active():
            out = server.update_pick_wave(9001, user_id=68)
        assert "warning" not in out


# ── remove_orders_from_pick_waves ───────────────────────────────────────────

def _two_open_waves():
    return {
        "waves": {
            9001: _wave_resp(wave_id=9001, state="Unallocated",
                             orders=[_wave_order(611385, GUID_A)]),
            9002: _wave_resp(wave_id=9002, state="InProgress", user_id=69,
                             email="warehouse+02@thewarehousegroup.co.uk",
                             orders=[_wave_order(611386, GUID_B)]),
        },
        "headers": {
            "Unallocated": [{"PickingWaveId": 9001, "State": "Unallocated"}],
            "InProgress": [{"PickingWaveId": 9002, "State": "InProgress"}],
        },
    }


def _delete_removes(fake):
    """on_write handler: remove the orders from their waves, reply like Linnworks."""
    def handler(body):
        processed, none = [], []
        for num in body["OrderIds"]:
            hit = False
            for resp in fake.waves.values():
                wave = resp["PickingWaves"][0]
                before = len(wave["Orders"])
                wave["Orders"] = [o for o in wave["Orders"] if o["OrderId"] != num]
                hit = hit or len(wave["Orders"]) < before
            (processed if hit else none).append(num)
        return {"ProcessedOrderIds": processed, "NoPickwaves": none}
    return handler


class TestRemoveOrders:

    def test_empty_list_is_refused(self):
        with FakeLinnworks().active() as fake:
            out = server.remove_orders_from_pick_waves([])
        assert out["success"] is False
        assert fake.calls == []

    def test_dry_run_maps_orders_to_waves_and_warns_on_in_progress(self):
        fake = FakeLinnworks(orders=ORDERS, **_two_open_waves())
        with fake.active():
            out = server.remove_orders_from_pick_waves(["611385", "611386", "611387"])
        rows = {r["num_order_id"]: r for r in out["manifest"]}
        assert rows[611385]["picking_wave_id"] == 9001
        assert "warning" not in rows[611385]
        assert rows[611386]["picking_wave_id"] == 9002
        assert "InProgress" in rows[611386]["warning"]
        assert rows[611387]["picking_wave_id"] is None
        assert "Not found" in rows[611387]["note"]
        assert fake.writes() == []

    def test_live_removal_sends_integer_ids_and_reads_back(self):
        fake = FakeLinnworks(orders=ORDERS, **_two_open_waves())
        fake.on_write["Picking/DeleteOrdersFromPickingWaves"] = _delete_removes(fake)
        with fake.active():
            out = server.remove_orders_from_pick_waves(["611385"], dry_run=False)
        assert _body(fake.writes()[0][2]) == {"OrderIds": [611385]}
        assert out["results"][0]["outcome"] == "removed"
        assert out["complete"] is True

    def test_still_present_when_the_read_back_finds_the_order(self):
        fake = FakeLinnworks(orders=ORDERS, **_two_open_waves())
        fake.on_write["Picking/DeleteOrdersFromPickingWaves"] = lambda body: {
            "ProcessedOrderIds": body["OrderIds"], "NoPickwaves": []}
        with fake.active():
            out = server.remove_orders_from_pick_waves(["611385"], dry_run=False)
        assert out["results"][0]["outcome"] == "still_present"
        assert out["complete"] is False

    def test_order_in_no_wave_is_reported_as_such(self):
        fake = FakeLinnworks(orders=ORDERS, **_two_open_waves())
        fake.on_write["Picking/DeleteOrdersFromPickingWaves"] = _delete_removes(fake)
        with fake.active():
            out = server.remove_orders_from_pick_waves(["611387"], dry_run=False)
        assert out["results"][0]["outcome"] == "not_in_a_wave"

    def test_permission_error_is_surfaced_verbatim(self):
        msg = "HTTP 401 — missing DeletePickingWavesNode"
        fake = FakeLinnworks(orders=ORDERS, raise_on={
            "Picking/DeleteOrdersFromPickingWaves": RuntimeError(msg)}, **_two_open_waves())
        with fake.active():
            out = server.remove_orders_from_pick_waves(["611385"], dry_run=False)
        assert out["outcome"] == "error"
        assert msg in out["error"]

    # F — a timeout on the delete itself must not escape; it is unconfirmed
    # (the removal may have gone through), not a plain error.
    def test_write_timeout_is_unconfirmed(self):
        fake = FakeLinnworks(orders=ORDERS, raise_on={
            "Picking/DeleteOrdersFromPickingWaves": requests.exceptions.Timeout("read timed out")},
            **_two_open_waves())
        with fake.active():
            out = server.remove_orders_from_pick_waves(["611385"], dry_run=False)
        assert out["outcome"] == "unconfirmed"
        assert out["complete"] is False
        assert out["error"] == "Timeout: read timed out"
        assert "get_pick_wave_detail" in out["note"]

    def test_rate_limit_while_locating_writes_nothing(self):
        fake = FakeLinnworks(orders=ORDERS, raise_on={
            "Picking/GetAllPickingWaveHeaders": server.RateLimitError("quota exceeded")})
        with fake.active():
            out = server.remove_orders_from_pick_waves(["611385"], dry_run=False)
        assert out["success"] is False
        assert out["complete"] is False
        assert fake.writes() == []

    def test_locate_failure_is_a_structured_refusal(self):
        fake = FakeLinnworks(orders=ORDERS, raise_on={
            "Picking/GetAllPickingWaveHeaders": RuntimeError("HTTP 500 — boom")})
        with fake.active():
            out = server.remove_orders_from_pick_waves(["611385"], dry_run=False)
        assert out["success"] is False
        assert "HTTP 500 — boom" in out["error"]
        assert "nothing was written" in out["error"]
        assert fake.writes() == []

    def test_stages_above_25_orders(self):
        many = {str(700000 + i): (f"{i:08d}-0000-0000-0000-000000000000", 700000 + i)
                for i in range(26)}
        with FakeLinnworks(orders=many).active() as fake:
            out = server.remove_orders_from_pick_waves(list(many), dry_run=False)
        assert out["staged"] is True
        assert fake.writes() == []

    # M6 — the confirmed_count echo.
    def test_wrong_confirmed_count_is_an_error_and_writes_nothing(self):
        many = _many_orders()
        with FakeLinnworks(orders=many).active() as fake:
            out = server.remove_orders_from_pick_waves(list(many), confirmed_count=27, dry_run=False)
        assert out["success"] is False
        assert out["staged"] is False
        assert "does not match" in out["message"]
        assert fake.writes() == []

    def test_matching_confirmed_count_writes(self):
        many = _many_orders()
        wave = _wave_resp(wave_id=9001, orders=[_wave_order(num, guid) for guid, num in many.values()])
        fake = FakeLinnworks(orders=many, waves={9001: wave},
                             headers={"Unallocated": [{"PickingWaveId": 9001, "State": "Unallocated"}]})
        fake.on_write["Picking/DeleteOrdersFromPickingWaves"] = _delete_removes(fake)
        with fake.active():
            out = server.remove_orders_from_pick_waves(list(many), confirmed_count=26, dry_run=False)
        assert len(fake.writes()) == 1
        assert len(_body(fake.writes()[0][2])["OrderIds"]) == 26
        assert {r["outcome"] for r in out["results"]} == {"removed"}
        assert out["complete"] is True

    # M5 — Linnworks' delete is neither state- nor location-scoped; live-
    # confirmed 22 Sep 2026 for an order held by an ABANDONED wave.
    def test_not_found_note_says_the_delete_is_not_location_scoped(self):
        fake = FakeLinnworks(orders=ORDERS, **_two_open_waves())
        with fake.active():
            out = server.remove_orders_from_pick_waves(["611387"])
        note = out["manifest"][0]["note"]
        assert note.startswith("Not found in any open wave at this location.")
        assert "neither state- nor location-scoped" in note
        assert "ABANDONED" in note

    # I5 — the warning covers any started wave.
    @pytest.mark.parametrize("state", ["InProgress", "Paused", "Complete", "Packing"])
    def test_warns_when_the_holding_wave_has_started(self, state):
        waves = {9001: _wave_resp(wave_id=9001, state=state, orders=[_wave_order(611385, GUID_A)])}
        fake = FakeLinnworks(orders=ORDERS, waves=waves,
                             headers={state: [{"PickingWaveId": 9001, "State": state}]})
        with fake.active():
            out = server.remove_orders_from_pick_waves(["611385"])
        warning = out["manifest"][0]["warning"]
        assert state in warning
        assert "trolley" in warning

    # I1 — a non-dict delete response never crashes; the read-back decides.
    def test_list_response_falls_through_to_the_read_back(self):
        fake = FakeLinnworks(orders=ORDERS, **_two_open_waves())
        remove = _delete_removes(fake)

        def handler(body):
            remove({"OrderIds": [611385]})  # only 611385 actually leaves its wave
            return ["ok"]
        fake.on_write["Picking/DeleteOrdersFromPickingWaves"] = handler
        with fake.active():
            out = server.remove_orders_from_pick_waves(["611385", "611386"], dry_run=False)
        outcomes = {r["num_order_id"]: r["outcome"] for r in out["results"]}
        assert outcomes == {611385: "unconfirmed", 611386: "still_present"}
        assert out["complete"] is False

    # Deferred minor 11 — the read-back error is kept.
    def test_readback_rate_limit_is_unconfirmed_with_the_reason(self):
        fake = FakeLinnworks(orders=ORDERS, **_two_open_waves())
        remove = _delete_removes(fake)

        def handler(body):
            reply = remove(body)
            fake.raise_on["Picking/GetPickingWave"] = server.RateLimitError("quota exceeded")
            return reply
        fake.on_write["Picking/DeleteOrdersFromPickingWaves"] = handler
        with fake.active():
            out = server.remove_orders_from_pick_waves(["611385"], dry_run=False)
        result = out["results"][0]
        assert result["outcome"] == "unconfirmed"
        assert result["readback_error"].startswith("rate limited:")
        assert "quota exceeded" in result["readback_error"]
        assert out["complete"] is False

    def test_readback_error_is_kept_verbatim(self):
        fake = FakeLinnworks(orders=ORDERS, **_two_open_waves())
        remove = _delete_removes(fake)

        def handler(body):
            reply = remove(body)
            fake.raise_on["Picking/GetPickingWave"] = RuntimeError("HTTP 500 — boom")
            return reply
        fake.on_write["Picking/DeleteOrdersFromPickingWaves"] = handler
        with fake.active():
            out = server.remove_orders_from_pick_waves(["611385"], dry_run=False)
        assert out["results"][0]["outcome"] == "unconfirmed"
        assert out["results"][0]["readback_error"] == "HTTP 500 — boom"

    # F — a timeout at the per-wave read-back is broadened to Exception, not
    # just RuntimeError, and must not escape.
    def test_readback_timeout_is_kept_verbatim(self):
        fake = FakeLinnworks(orders=ORDERS, **_two_open_waves())
        remove = _delete_removes(fake)

        def handler(body):
            reply = remove(body)
            fake.raise_on["Picking/GetPickingWave"] = requests.exceptions.Timeout("read timed out")
            return reply
        fake.on_write["Picking/DeleteOrdersFromPickingWaves"] = handler
        with fake.active():
            out = server.remove_orders_from_pick_waves(["611385"], dry_run=False)
        assert out["results"][0]["outcome"] == "unconfirmed"
        assert out["results"][0]["readback_error"] == "read timed out"

    def test_removed_order_carries_no_readback_error(self):
        fake = FakeLinnworks(orders=ORDERS, **_two_open_waves())
        fake.on_write["Picking/DeleteOrdersFromPickingWaves"] = _delete_removes(fake)
        with fake.active():
            out = server.remove_orders_from_pick_waves(["611385"], dry_run=False)
        assert "readback_error" not in out["results"][0]
