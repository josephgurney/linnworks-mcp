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


def _wave_resp(wave_id=9001, state="Unallocated", orders=(), user_id=None, email=None, start=None):
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
