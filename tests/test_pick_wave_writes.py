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
