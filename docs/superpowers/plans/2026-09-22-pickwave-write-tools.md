# Pickwave Write Tools Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add four pickwave tools to the Linnworks MCP server: `get_pick_wave_detail` (read), `generate_pick_waves`, `update_pick_wave` and `remove_orders_from_pick_waves`. Prove them live with a contained test, then ship as v1.56.0.

**Architecture:** All code goes in `server.py`, in a new "Pickwave detail + writes (issue #67)" block placed right after `check_orders_pickable`. The block reuses the #64 read helpers (`_format_pick_wave`, `_pick_wave_state_label`, `_pick_wave_query_params`) and the existing write-safety framework (`_write_guard`, `WRITE_THRESHOLDS`, `dry_run=True`). Every write is read back through a fresh `Picking/GetPickingWave` call (or the header list, for an abandoned wave). `RateLimitError` is caught explicitly at every call site.

**Tech Stack:** Python 3.10+, FastMCP (`mcp>=1.29.0,<2`), `requests`, pytest with `unittest.mock`. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-09-22-pickwave-write-tools-design.md` (approved 22 Sep 2026)

## Global Constraints

- **Branch:** work on `feature/issue-67-pickwave-writes`. **Never push to `main`.** Every change goes through a PR.
- **Version:** v1.56.0 at the end. The tool count goes from 94 to 98, one per tool task (95, 96, 97, 98).
- **Write defaults:** every write tool defaults to `dry_run=True`.
- **Staging thresholds:** `generate_pick_waves` = 25 (counted by orders across all waves); `remove_orders_from_pick_waves` = 25 (orders); `update_pick_wave` has none (single wave).
- **Settable wave states:** exactly `Abandoned`, `Paused`, `Unallocated`. Abandoning an InProgress wave, or moving it back to Unallocated, needs `allow_in_progress=True`. Pausing an InProgress wave doesn't.
- **Generate defaults:** `sorting_type="BinPriority"`, `group_type="Items"`. One `Picking/GeneratePickingWave` call per wave.
- **FIFO_READY:** warn in the manifest; never block.
- **Weight and dimensions:** no weight or dimension field in any output (deferred to the planning piece).
- **Rate limits:** `RateLimitError` is never reported as refused, not found, empty or "not in a wave". It gets its own `rate_limited` outcome or bucket, with `complete: False`.
- **Tests:** offline only. `tests/conftest.py` already sets dummy credentials. Run them with `.venv/bin/python3 -m pytest`.
- **Live writes:** each live write needs explicit owner approval in chat (Task 7).
- **Commits:** end every commit message with `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.

---

## File Structure

| File | Change | Responsibility |
|---|---|---|
| `server.py` | Modify | New helpers + 4 tools after `check_orders_pickable` (currently ends just before `def get_category_report`); `check_orders_pickable` refactored to use the shared resolver; 2 `WRITE_THRESHOLDS` rows; version |
| `tests/test_pick_wave_writes.py` | Create | All tests for the four new tools and their helpers, built on one `FakeLinnworks` router |
| `tests/test_pick_waves.py` | Modify | Tool-count assertion 94 → 95 → 96 → 97 → 98; state-label set in Task 7 |
| `CLAUDE.md` | Modify | Tools-table rows, threshold rows, confirmed-endpoint rows, counts, version note |
| `README.md` | Modify | Tool rows, threshold rows, tools badge, version badge |

Anchors used throughout:
- **server.py insertion point:** after the closing `    }` of `check_orders_pickable`'s return dict, and before `@mcp.tool()` / `def get_category_report(`. Find it with `grep -n "^def get_category_report" server.py`.
- **CLAUDE.md:** `### Picking (read) — issue #64` (tools table) and `### Reporting (read, autopaginating)` (the next section).
- **README.md:** the `**Picking (read)**` table and the threshold table row `| \`create_order\` | 10 lines |`.

---

### Task 1: `get_pick_wave_detail` read tool + test harness

**Files:**
- Modify: `server.py` (insert the new block after `check_orders_pickable`)
- Create: `tests/test_pick_wave_writes.py`
- Modify: `tests/test_pick_waves.py` (tool count)
- Modify: `CLAUDE.md`, `README.md` (row + counts)

**Interfaces:**
- Consumes: `_format_pick_wave(row) -> dict`, `_pick_wave_state_label(raw) -> str`, `call_linnworks_get(path, params)`, `RateLimitError` (all existing).
- Produces:
  - `_PICK_WAVE_EMPTY_DETAIL_NOTE: str`
  - `_PICK_WAVE_BLOCKER_FLAGS: tuple[tuple[str, str], ...]`
  - `_fetch_pick_wave(picking_wave_id: int) -> dict` (raw response; lets `RateLimitError` propagate)
  - `_format_pick_wave_detail(resp: dict) -> dict | None`: the `_format_pick_wave` header fields plus `orders: list[dict]` and `blockers: list[dict]`; `None` when no wave is returned. Order row keys: `order_id, order_guid, pick_state, sort_order, is_locked, is_on_hold, is_cancelled, is_processed, is_paid, items`. Item keys: `picking_wave_item_row_id, order_item_row_id, stock_item_id, sku, title, to_pick, picked, item_state, bins`.
  - Tool `get_pick_wave_detail(picking_wave_id: int) -> dict`
  - Test harness in `tests/test_pick_wave_writes.py`: `FakeLinnworks`, `_wave_order`, `_wave_resp`, `_body`, constants `DEFAULT`, `SID`, `GUID_A`, `GUID_B`, `GUID_C`, `WRITE_PATHS`.

- [ ] **Step 1: Create the test file with the harness and the failing detail tests**

Create `tests/test_pick_wave_writes.py`:

```python
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
```

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `.venv/bin/python3 -m pytest tests/test_pick_wave_writes.py -v`
Expected: every `TestGetPickWaveDetail` test FAILS with `AttributeError: module 'server' has no attribute 'get_pick_wave_detail'`.

- [ ] **Step 3: Add the detail helpers and the tool to `server.py`**

Insert after `check_orders_pickable`, before `@mcp.tool()` / `def get_category_report(`:

```python
# ── Pickwave detail + writes (issue #67) ────────────────────────────────────
#
# Live facts this section relies on (confirmed read-only, 22 Sep 2026 — see
# docs/superpowers/specs/2026-09-22-pickwave-write-tools-design.md):
#   - Picking/GetPickingWave returns full order + item detail for a LIVE wave,
#     and NOTHING for a finished (Shipped/Abandoned) wave. The v1.54.0 note
#     that it "returns zero waves" was only ever tested on finished waves.
#   - The wave row carries UserId/EmailAddress only while the wave is assigned
#     (the keys are ABSENT, not null, when unassigned).
#   - Bins[] on that response carries real bin codes (e.g. "10-A-03"), even
#     though GetItemBinracks errors on every item here (non-WMS locations).

_PICK_WAVE_EMPTY_DETAIL_NOTE = (
    "Linnworks returned no wave for this id. It does this for a FINISHED wave "
    "(Shipped or Abandoned) — confirmed live 22 Sep 2026 — and for an id that "
    "doesn't exist. It does NOT mean the wave has no orders. Use "
    "get_pick_waves(state='Shipped' or 'Abandoned') to see a finished wave's "
    "header counts."
)

# (output name, Linnworks order flag) — an order with any of these set can't
# be picked as planned, so get_pick_wave_detail lists it under `blockers`.
_PICK_WAVE_BLOCKER_FLAGS = (
    ("locked", "IsLocked"),
    ("on_hold", "IsOnHold"),
    ("cancelled", "IsCancelled"),
    ("processed", "IsProcessed"),
)


def _fetch_pick_wave(picking_wave_id: int) -> dict:
    """
    Raw Picking/GetPickingWave response for one wave (GET ?pickingWaveId=).
    Returns {} if the body isn't a dict. Lets RateLimitError propagate: the
    caller decides how to report a throttle, and it is never "no such wave".
    """
    resp = call_linnworks_get("Picking/GetPickingWave", {"pickingWaveId": picking_wave_id})
    return resp if isinstance(resp, dict) else {}


def _format_pick_wave_detail(resp: dict) -> dict | None:
    """
    Normalise a GetPickingWave response into header + orders + blockers.

    Returns None when the response holds no wave (a finished wave, or an
    unknown id — see _PICK_WAVE_EMPTY_DETAIL_NOTE). The header comes from
    _format_pick_wave, so there is exactly one pickwave state map (#64 AC7).
    No weight or volume figure is produced (#67 defers those).
    """
    waves = resp.get("PickingWaves") or []
    if not waves:
        return None
    wave = waves[0]

    skus = {str(s.get("StockItemId") or "").lower(): s for s in (resp.get("Skus") or [])}
    bins: dict[str, list[str]] = {}
    for b in resp.get("Bins") or []:
        sid = str(b.get("StockItemId") or "").lower()
        rack = b.get("BinRack")
        if rack and rack not in bins.setdefault(sid, []):
            bins[sid].append(rack)

    orders: list[dict] = []
    blockers: list[dict] = []
    for o in wave.get("Orders") or []:
        items = []
        for it in o.get("Items") or []:
            sid = str(it.get("StockItemId") or "").lower()
            sku = skus.get(sid, {})
            items.append({
                "picking_wave_item_row_id": it.get("PickingWaveItemsRowId"),
                "order_item_row_id": it.get("OrderItemRowId"),
                "stock_item_id": it.get("StockItemId"),
                "sku": sku.get("SKU"),
                "title": sku.get("ItemTitle"),
                "to_pick": it.get("ToPickQuantity"),
                "picked": it.get("PickedQuantity"),
                "item_state": it.get("ItemState"),
                "bins": bins.get(sid, []),
            })
        flags = {name: bool(o.get(key)) for name, key in _PICK_WAVE_BLOCKER_FLAGS}
        row = {
            "order_id": o.get("OrderId"),
            "order_guid": o.get("OrderId_Guid"),
            "pick_state": o.get("PickState"),
            "sort_order": o.get("SortOrder"),
            **{f"is_{name}": value for name, value in flags.items()},
            "is_paid": bool(o.get("IsPaid")),
            "items": items,
        }
        orders.append(row)
        reasons = [name for name, value in flags.items() if value]
        if reasons:
            blockers.append({
                "order_id": row["order_id"],
                "order_guid": row["order_guid"],
                "reasons": reasons,
            })

    return {**_format_pick_wave(wave), "orders": orders, "blockers": blockers}


@mcp.tool()
def get_pick_wave_detail(picking_wave_id: int) -> dict:
    """
    Read one pickwave in full (Picking/GetPickingWave): its header, every
    order with its pick state and locked / on-hold / cancelled / processed /
    paid flags, and every item with SKU, title, quantity to pick, quantity
    picked and bin codes.

    Read-only. A point-in-time snapshot: pickers change waves while they work.

    `blockers` lists orders in the wave that are now locked, on hold,
    cancelled or processed — the candidates for remove_orders_from_pick_waves.

    ⚠️  Linnworks returns NOTHING for a finished wave (Shipped or Abandoned),
    or for an unknown id. That comes back as found=False with a note, never as
    "this wave has no orders". An unassigned wave has user_id None.

    Bin codes come from the wave's own Bins data. That works on this tenant
    even though get_item_bins can't (there are no WMS-managed locations).

    Args:
        picking_wave_id: The wave's id, from get_pick_waves or generate_pick_waves.

    Returns:
        found (True / False, or None when rate limited). For a found wave:
        the same header fields as get_pick_waves (picking_wave_id, state,
        state_label, user_id, email_address, counts, group_type, sort_type)
        plus orders[] and blockers[]. Always: rate_limited, complete.
    """
    try:
        resp = _fetch_pick_wave(picking_wave_id)
    except RateLimitError as exc:
        return {
            "picking_wave_id": picking_wave_id,
            "found": None,
            "rate_limited": True,
            "complete": False,
            "error": str(exc),
        }
    detail = _format_pick_wave_detail(resp)
    if detail is None:
        return {
            "picking_wave_id": picking_wave_id,
            "found": False,
            "note": _PICK_WAVE_EMPTY_DETAIL_NOTE,
            "rate_limited": False,
            "complete": True,
        }
    return {**detail, "found": True, "rate_limited": False, "complete": True}
```

- [ ] **Step 4: Run the tests and confirm they pass**

Run: `.venv/bin/python3 -m pytest tests/test_pick_wave_writes.py -v`
Expected: all 8 PASS.

- [ ] **Step 5: Update the tool count, the docs rows, and run the full suite**

In `tests/test_pick_waves.py`, replace:

```python
    def test_total_tool_count_is_94(self):
        registered = asyncio.run(server.mcp.list_tools())
        assert len(registered) == 94
```

with:

```python
    def test_total_tool_count(self):
        registered = asyncio.run(server.mcp.list_tools())
        assert len(registered) == 95
```

Bump the doc counts:

```bash
sed -i '' 's/— 94 tools/— 95 tools/; s/^94 tools\. /95 tools. /' CLAUDE.md
sed -i '' 's/badge\/tools-94-blue/badge\/tools-95-blue/' README.md
```

In `CLAUDE.md`, add this row as the last row of the `### Picking (read) — issue #64` table, directly after the `check_orders_pickable(order_ids)` row:

```markdown
| `get_pick_wave_detail(picking_wave_id)` | `Picking/GetPickingWave` (GET `?pickingWaveId=`) | Full detail for ONE wave, added for #67 (v1.56.0): header plus every order (pick state, sort order, `is_locked`/`is_on_hold`/`is_cancelled`/`is_processed`/`is_paid`) and every item (SKU and title joined from `Skus[]`, to_pick, picked, item_state, bin codes from `Bins[]`). `blockers[]` lists orders now locked, on hold, cancelled or processed. **Live-confirmed 22 Sep 2026: returns full detail for a LIVE wave (wave 3549, 8 orders) but NOTHING for a finished one** — the v1.54.0 "returns zero waves" note only ever tested finished waves. An empty response is reported as `found: False` with a note, never as an empty wave. `UserId`/`EmailAddress` are absent (not null) on an unassigned wave. |
```

In `README.md`, add this row as the last row of the `**Picking (read)**` table, directly after the `check_orders_pickable` row:

```markdown
| `get_pick_wave_detail` | Full detail for one wave: every order with its pick state and locked/on-hold/cancelled/processed flags, every item with SKU, quantities and bin codes, plus a `blockers` list. Returns nothing for a finished (Shipped/Abandoned) wave — reported as `found: False`, never as an empty wave |
```

Run: `.venv/bin/python3 -m pytest -q && .venv/bin/python3 server.py --list-tools | tail -1`
Expected: all tests pass; `95 tools registered`.

- [ ] **Step 6: Commit**

```bash
git add server.py tests/test_pick_wave_writes.py tests/test_pick_waves.py CLAUDE.md README.md
git commit -m "Add get_pick_wave_detail read tool (#67)

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: Shared pre-write helpers (no new tool)

**Files:**
- Modify: `server.py` (add helpers to the #67 block; refactor `check_orders_pickable` onto the shared resolver)
- Test: `tests/test_pick_wave_writes.py`

**Interfaces:**
- Consumes: `_resolve_order_guid(order_id: str) -> tuple[str, dict]` (existing; the raw dict carries `NumOrderId`), `_pick_wave_query_params(state, location_id, detail_level)`, `_format_pick_wave`, `call_linnworks`, `call_linnworks_get`.
- Produces:
  - Constants: `_PICK_WAVE_SETTABLE_STATES = ("Abandoned", "Paused", "Unallocated")`, `_PICK_WAVE_OPEN_STATES = ("Unallocated", "Allocated", "InProgress", "Paused", "Complete", "Packing")`, `_PICK_WAVE_SORTING_TYPES = ("BinPriority", "OrderView")`, `_PICK_WAVE_GROUP_TYPES = ("Items", "Orders")`, `_FIFO_READY_TAG = "FIFO_READY"`, `_PICKING_WRITE_WRAPPED: dict[str, bool]`
  - `_picking_write(path: str, body: dict) -> dict`
  - `_resolve_order_numbers(order_ids: list) -> tuple[list[tuple], list[dict], list[dict]]`, returning `(resolved, resolve_errors, rate_limited)`; `resolved` items are `(input_id, num_order_id: int, order_guid: str)`
  - `_fetch_picker_roster() -> dict[int, str | None]` (user_id → email)
  - `_fetch_fifo_ready_guids(order_guids: list[str]) -> set[str]` (lower-cased GUIDs)
  - `_check_pickable_numbers(num_ids: list[int]) -> dict[int, dict]` (num → `{"pickable": bool, "errors": list}`)
  - `_find_wave_header(picking_wave_id: int, state: str, location_id: str = DEFAULT_LOCATION_ID) -> dict | None`
  - `_pick_wave_preflight_throttled(rate_limited: list[dict]) -> dict`
  - `_pick_wave_unknown_user(user_id: int, roster: dict) -> dict`
  - `_as_int(value) -> int | None`

- [ ] **Step 1: Write the failing helper tests**

Append to `tests/test_pick_wave_writes.py`:

```python
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
```

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `.venv/bin/python3 -m pytest tests/test_pick_wave_writes.py::TestSharedHelpers -v`
Expected: FAIL with `AttributeError: module 'server' has no attribute '_resolve_order_numbers'` (and similar).

- [ ] **Step 3: Add the helpers to the #67 block in `server.py`**

Append right after `get_pick_wave_detail` (still before `def get_category_report`):

```python
_PICK_WAVE_SETTABLE_STATES = ("Abandoned", "Paused", "Unallocated")
_PICK_WAVE_OPEN_STATES = ("Unallocated", "Allocated", "InProgress", "Paused", "Complete", "Packing")
_PICK_WAVE_SORTING_TYPES = ("BinPriority", "OrderView")
_PICK_WAVE_GROUP_TYPES = ("Items", "Orders")
_FIFO_READY_TAG = "FIFO_READY"

# Whether each Picking write endpoint takes the {"request": {...}} wrapper.
# DeleteOrdersFromPickingWaves' spec schema has an explicit `request`
# property, so it is wrapped. GeneratePickingWave and UpdatePickingWaveHeader
# are UNVERIFIED until their first live call (#67 live proof); the sibling
# CheckAllocatableToPickwave needs the wrapper, so that is the starting guess.
# Set each entry from live evidence and record it in CLAUDE.md.
_PICKING_WRITE_WRAPPED: dict[str, bool] = {
    "Picking/GeneratePickingWave": True,
    "Picking/UpdatePickingWaveHeader": True,
    "Picking/DeleteOrdersFromPickingWaves": True,
}


def _picking_write(path: str, body: dict) -> dict:
    """POST a Picking write body, wrapped or not per _PICKING_WRITE_WRAPPED."""
    payload = {"request": body} if _PICKING_WRITE_WRAPPED[path] else body
    return call_linnworks(path, payload)


def _as_int(value) -> int | None:
    """int(value), or None if it isn't one."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _resolve_order_numbers(order_ids: list) -> tuple[list[tuple], list[dict], list[dict]]:
    """
    Resolve GUID-or-numeric order ids to (input_id, NumOrderId, order GUID).

    Returns (resolved, resolve_errors, rate_limited). Never raises for a
    per-id failure: an unknown id goes to resolve_errors, and a throttle goes
    to rate_limited — never folded into "not found" (#34/#37). Shared by
    check_orders_pickable and the #67 write tools.
    """
    resolved: list[tuple] = []
    resolve_errors: list[dict] = []
    rate_limited: list[dict] = []
    for order_id in order_ids:
        try:
            order_guid, raw = _resolve_order_guid(str(order_id))
        except RateLimitError as exc:
            rate_limited.append({"order_id": order_id, "reason": str(exc)})
            continue
        except RuntimeError as exc:
            resolve_errors.append({"order_id": order_id, "reason": str(exc)})
            continue
        num_order_id = raw.get("NumOrderId")
        if num_order_id is None:
            resolve_errors.append({
                "order_id": order_id,
                "reason": f"Order '{order_id}' resolved but carries no NumOrderId.",
            })
            continue
        resolved.append((order_id, num_order_id, order_guid))
    return resolved, resolve_errors, rate_limited


def _fetch_picker_roster() -> dict[int, str | None]:
    """
    user_id -> email for every warehouse user a wave can be assigned to.

    Source: Picking/GetPickwaveUsersWithSummary with state=Unallocated, which
    returns one row per registered user plus any unassigned wave rows
    (UserId null) — live 22 Sep 2026: 15 users, e.g. warehouse+01 = 68. Lets
    RateLimitError propagate.
    """
    resp = call_linnworks_get(
        "Picking/GetPickwaveUsersWithSummary",
        _pick_wave_query_params("Unallocated", None, None),
    )
    roster: dict[int, str | None] = {}
    for row in (resp.get("PickingWaves") or []) if isinstance(resp, dict) else []:
        user_id = row.get("UserId")
        if isinstance(user_id, int) and not isinstance(user_id, bool) and user_id > 0:
            roster.setdefault(user_id, row.get("EmailAddress"))
    return roster


def _fetch_fifo_ready_guids(order_guids: list[str]) -> set[str]:
    """
    Lower-cased GUIDs of the orders carrying the FIFO_READY identifier.

    OpenOrders/GetIdentifiersByOrderIds, sent UNWRAPPED as {"OrderIds": [guid]}
    — the {"request": ...} form 400s "OrderIds not provided in request" (live,
    22 Sep 2026). Returns a flat list of {fkOrderId, IdentifierId, IsCustom,
    Tag}. Chunked at 100, like the FIFO worker. Lets RateLimitError and
    RuntimeError propagate.
    """
    ready: set[str] = set()
    for start in range(0, len(order_guids), 100):
        chunk = order_guids[start:start + 100]
        resp = call_linnworks("OpenOrders/GetIdentifiersByOrderIds", {"OrderIds": chunk})
        for row in resp if isinstance(resp, list) else []:
            if str(row.get("Tag", "")).strip().upper() == _FIFO_READY_TAG:
                ready.add(str(row.get("fkOrderId", "")).lower())
    return ready


def _check_pickable_numbers(num_ids: list[int]) -> dict[int, dict]:
    """
    NumOrderId -> {"pickable": bool, "errors": [...]} via
    Picking/CheckAllocatableToPickwave (wrapped; proven side-effect free in
    v1.54.0). Lets RateLimitError propagate.
    """
    if not num_ids:
        return {}
    resp = call_linnworks(
        "Picking/CheckAllocatableToPickwave", {"request": {"OrderIds": num_ids}}
    )
    return {
        r.get("OrderId"): {"pickable": not r.get("HasErrors"), "errors": r.get("Errors") or []}
        for r in (resp.get("Results") or [])
    }


def _find_wave_header(
    picking_wave_id: int, state: str, location_id: str = DEFAULT_LOCATION_ID
) -> dict | None:
    """
    The formatted header for one wave, from GetAllPickingWaveHeaders(state=).
    This is how a wave that GetPickingWave no longer returns (an Abandoned one)
    gets read back. None if the wave isn't in that state's list. Lets
    RateLimitError propagate.
    """
    resp = call_linnworks_get(
        "Picking/GetAllPickingWaveHeaders", {"state": state, "locationId": location_id}
    )
    for row in (resp.get("PickwaveHeaders") or []) if isinstance(resp, dict) else []:
        if row.get("PickingWaveId") == picking_wave_id:
            return _format_pick_wave(row)
    return None


def _pick_wave_preflight_throttled(rate_limited: list[dict]) -> dict:
    """The response when a pre-write read is rate limited: nothing is written."""
    return {
        "success": False,
        "rate_limited": rate_limited,
        "complete": False,
        "message": (
            "Linnworks' rate limit was hit during the checks before writing. "
            "Nothing was written. Wait a minute and call again."
        ),
    }


def _pick_wave_unknown_user(user_id, roster: dict) -> dict:
    """The refusal for a user_id that isn't on the live picker roster."""
    known = ", ".join(f"{uid} ({email})" for uid, email in sorted(roster.items()))
    return {
        "success": False,
        "error": f"user_id {user_id} is not a Linnworks picker. Known pickers: {known}",
    }
```

Then replace the resolution loop at the top of `check_orders_pickable`, which starts:

```python
    resolved: list[tuple[str, int, str]] = []  # (input_order_id, num_order_id, order_guid)
    resolve_errors: list[dict] = []
    rate_limited: list[dict] = []

    for order_id in order_ids:
```

and runs down to `        resolved.append((order_id, num_order_id, order_guid))`. Replace that whole block with:

```python
    resolved, resolve_errors, rate_limited = _resolve_order_numbers(order_ids)
```

Everything from `    results: list[dict] = []` onwards stays unchanged.

- [ ] **Step 4: Run the new tests and the existing pickability tests**

Run: `.venv/bin/python3 -m pytest tests/test_pick_wave_writes.py tests/test_pick_waves.py -v`
Expected: all PASS. `TestCheckOrdersPickable` in `test_pick_waves.py` is the proof that the refactor kept its behaviour.

- [ ] **Step 5: Commit**

```bash
git add server.py tests/test_pick_wave_writes.py
git commit -m "Add shared pre-write helpers for pickwave writes (#67)

check_orders_pickable now uses the shared _resolve_order_numbers.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: `generate_pick_waves`

**Files:**
- Modify: `server.py` (tool + `_read_back_generated_wave`, and the `WRITE_THRESHOLDS` row)
- Test: `tests/test_pick_wave_writes.py`, `tests/test_pick_waves.py` (count 96)
- Modify: `CLAUDE.md`, `README.md` (new write section, threshold rows, counts)

**Interfaces:**
- Consumes: everything Task 2 produces, plus `_fetch_pick_wave`, `_format_pick_wave_detail`, `_write_guard`.
- Produces:
  - `_read_back_generated_wave(wave_ids: list[int], requested: list[int]) -> dict`: `{"outcome": "created"|"unconfirmed", "picking_wave_ids", "readback_order_ids"?, "readback_matches"?, "readback_error"?}`
  - Tool `generate_pick_waves(waves: list[dict], location_id: str = DEFAULT_LOCATION_ID, confirmed_count: int | None = None, dry_run: bool = True) -> dict`
  - Per-wave result keys: `wave_index, user_id, outcome` (`created`, `refused`, `rate_limited`, `error`, `unconfirmed` or `blocked`), plus `picking_wave_ids`, `readback_order_ids`, `readback_matches`, `validation_results`, `reasons` and `error` where they apply.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_pick_wave_writes.py`:

```python
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
```

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `.venv/bin/python3 -m pytest tests/test_pick_wave_writes.py -k Generate -v`
Expected: FAIL with `AttributeError: module 'server' has no attribute 'generate_pick_waves'`.

- [ ] **Step 3: Add the threshold row**

In `WRITE_THRESHOLDS` in `server.py`, directly after the `"create_order": 10,` line, add:

```python
    "generate_pick_waves":            25,   # creates live pickwaves the warehouse will pick from
```

- [ ] **Step 4: Implement the tool**

Append to the #67 block in `server.py` (after `_pick_wave_unknown_user`):

```python
def _read_back_generated_wave(wave_ids: list[int], requested: list[int]) -> dict:
    """
    Read each newly created wave back and compare its order set with the
    request. A read-back that fails or comes back empty is `unconfirmed` —
    the wave may well exist, so it is never reported as not created.
    """
    got: list[int] = []
    try:
        for wave_id in wave_ids:
            detail = _format_pick_wave_detail(_fetch_pick_wave(wave_id))
            if detail is None:
                return {
                    "outcome": "unconfirmed",
                    "picking_wave_ids": wave_ids,
                    "readback_error": f"wave {wave_id} was reported created but reads back empty",
                }
            got.extend(o["order_id"] for o in detail["orders"])
    except RateLimitError as exc:
        return {"outcome": "unconfirmed", "picking_wave_ids": wave_ids,
                "readback_error": f"rate limited: {exc}"}
    except RuntimeError as exc:
        return {"outcome": "unconfirmed", "picking_wave_ids": wave_ids,
                "readback_error": str(exc)}
    return {
        "outcome": "created",
        "picking_wave_ids": wave_ids,
        "readback_order_ids": sorted(got),
        "readback_matches": sorted(got) == sorted(requested),
    }


@mcp.tool()
def generate_pick_waves(
    waves: list[dict],
    location_id: str = DEFAULT_LOCATION_ID,
    confirmed_count: int | None = None,
    dry_run: bool = True,
) -> dict:
    """
    Create one or more pickwaves (Picking/GeneratePickingWave), one Linnworks
    call per wave.

    Each wave is a dict:
        {"order_ids": [...],            # GUIDs or order numbers, in pick order
         "user_id": 19,                 # optional — see get_pick_wave_users; omit for unassigned
         "sorting_type": "BinPriority", # or "OrderView" (default BinPriority)
         "group_type": "Items"}         # or "Orders" (default Items)

    Before anything is written — on a dry run too — every order is resolved;
    an order may appear only once per call; every user_id must be on the live
    picker roster; Linnworks' own pickability check runs for every order (an
    order already in a wave, locked, parked etc. is flagged with Linnworks'
    reason); and orders without the FIFO_READY identifier get a WARNING (not a
    block). An unresolved order blocks its own wave only. A rate limit during
    these checks stops the call with nothing written.

    Staged above 25 orders across all waves (confirmed_count). dry_run=True
    by default.

    Every created wave is read back with a fresh GetPickingWave call. A wave
    whose read-back fails is `unconfirmed` — it may exist; check with
    get_pick_waves before trying again.

    ⚠️  Not atomic across waves. If a multi-wave run partly fails, the result
    lists exactly which waves were created. Re-send only the failed waves —
    re-running the whole batch would put the created waves' orders through
    generate again.

    There is no endpoint to add an order to an existing wave: remove it and
    generate again. A wave is a point-in-time snapshot while pickers work.

    ⚠️  The request body shape (wrapped or not) is set by
    _PICKING_WRITE_WRAPPED; see CLAUDE.md for what the live proof established.

    Returns:
        dry run: manifest (per wave: orders with num_order_id, order_guid,
        pickable, pickable_errors, fifo_ready; blocked, blocked_reasons,
        warnings), warnings. Live: results (per wave: outcome of created /
        refused / rate_limited / error / unconfirmed / blocked, picking_wave_ids,
        readback_order_ids, readback_matches, validation_results), created_wave_ids,
        rate_limited, warnings, complete, message.
    """
    # 1. Shape checks — no API calls.
    if not waves:
        return {"success": False, "error": "waves is empty — pass at least one wave."}
    problems: list[str] = []
    for i, wave in enumerate(waves):
        if not isinstance(wave, dict) or not wave.get("order_ids"):
            problems.append(f"wave {i}: needs a non-empty order_ids list")
            continue
        if wave.get("sorting_type", "BinPriority") not in _PICK_WAVE_SORTING_TYPES:
            problems.append(f"wave {i}: sorting_type must be one of {list(_PICK_WAVE_SORTING_TYPES)}")
        if wave.get("group_type", "Items") not in _PICK_WAVE_GROUP_TYPES:
            problems.append(f"wave {i}: group_type must be one of {list(_PICK_WAVE_GROUP_TYPES)}")
        user_id = wave.get("user_id")
        if user_id is not None and (
            isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0
        ):
            problems.append(f"wave {i}: user_id must be a positive integer (see get_pick_wave_users)")
    if problems:
        return {"success": False, "error": "Invalid waves: " + "; ".join(problems)}

    seen_inputs: dict[str, int] = {}
    for i, wave in enumerate(waves):
        for order_id in wave["order_ids"]:
            key = str(order_id).strip().lower()
            if key in seen_inputs:
                return {"success": False, "error": (
                    f"Order {order_id!r} appears more than once (wave {seen_inputs[key]} "
                    f"and wave {i}). An order can only go in one wave per call.")}
            seen_inputs[key] = i

    all_ids = [order_id for wave in waves for order_id in wave["order_ids"]]

    # 2. Reads only: resolve, duplicate-by-number, roster, pickability, FIFO.
    resolved, resolve_errors, rate_limited = _resolve_order_numbers(all_ids)
    if rate_limited:
        return _pick_wave_preflight_throttled(rate_limited)
    by_input = {str(r[0]).strip().lower(): (r[1], r[2]) for r in resolved}

    seen_nums: dict[int, int] = {}
    for i, wave in enumerate(waves):
        for order_id in wave["order_ids"]:
            hit = by_input.get(str(order_id).strip().lower())
            if hit is None:
                continue
            num = hit[0]
            if num in seen_nums:
                return {"success": False, "error": (
                    f"Order {num} is listed twice (wave {seen_nums[num]} and wave {i}), "
                    "once by GUID and once by number. An order can only go in one wave per call.")}
            seen_nums[num] = i

    wanted_users = {w["user_id"] for w in waves if w.get("user_id") is not None}
    roster: dict[int, str | None] = {}
    if wanted_users:
        try:
            roster = _fetch_picker_roster()
        except RateLimitError as exc:
            return _pick_wave_preflight_throttled([{"step": "picker roster", "reason": str(exc)}])
        unknown = sorted(u for u in wanted_users if u not in roster)
        if unknown:
            return _pick_wave_unknown_user(unknown[0] if len(unknown) == 1 else unknown, roster)

    warnings: list[str] = []
    try:
        pickability = _check_pickable_numbers([r[1] for r in resolved])
    except RateLimitError as exc:
        return _pick_wave_preflight_throttled([{"step": "pickability check", "reason": str(exc)}])
    fifo_ready: set[str] | None
    try:
        guids = [r[2] for r in resolved]
        fifo_ready = _fetch_fifo_ready_guids(guids) if guids else set()
    except RateLimitError as exc:
        return _pick_wave_preflight_throttled([{"step": "FIFO_READY check", "reason": str(exc)}])
    except RuntimeError as exc:
        fifo_ready = None
        warnings.append(f"{_FIFO_READY_TAG} check was SKIPPED, not passed: {exc}")

    # 3. Manifest.
    errors_by_input = {str(e["order_id"]).strip().lower(): e["reason"] for e in resolve_errors}
    manifest: list[dict] = []
    for i, wave in enumerate(waves):
        user_id = wave.get("user_id")
        orders: list[dict] = []
        blocked_reasons: list[str] = []
        wave_warnings: list[str] = []
        for order_id in wave["order_ids"]:
            key = str(order_id).strip().lower()
            if key in errors_by_input:
                blocked_reasons.append(f"order {order_id!r} could not be resolved: {errors_by_input[key]}")
                orders.append({"order_id": order_id, "resolved": False})
                continue
            num, guid = by_input[key]
            check = pickability.get(num)
            is_fifo = None if fifo_ready is None else guid.lower() in fifo_ready
            if check is not None and not check["pickable"]:
                wave_warnings.append(f"order {num}: Linnworks says it is not pickable: {check['errors']}")
            if is_fifo is False:
                wave_warnings.append(f"order {num} is not tagged {_FIFO_READY_TAG}")
            orders.append({
                "order_id": order_id,
                "resolved": True,
                "num_order_id": num,
                "order_guid": guid,
                "pickable": None if check is None else check["pickable"],
                "pickable_errors": [] if check is None else check["errors"],
                "fifo_ready": is_fifo,
            })
        manifest.append({
            "wave_index": i,
            "user_id": user_id,
            "user_email": roster.get(user_id) if user_id is not None else None,
            "sorting_type": wave.get("sorting_type", "BinPriority"),
            "group_type": wave.get("group_type", "Items"),
            "orders": orders,
            "blocked": bool(blocked_reasons),
            "blocked_reasons": blocked_reasons,
            "warnings": wave_warnings,
        })

    guard = _write_guard("generate_pick_waves", all_ids, confirmed_count, dry_run)
    if guard is not None:
        return {**guard, "manifest": manifest, "warnings": warnings}
    if dry_run:
        sendable = sum(not m["blocked"] for m in manifest)
        return {
            "dry_run": True,
            "wave_count": len(waves),
            "order_count": len(all_ids),
            "manifest": manifest,
            "warnings": warnings,
            "message": f"Dry run — {sendable} of {len(waves)} wave(s) would be sent. Nothing was created.",
        }

    # 4. Live: one GeneratePickingWave per unblocked wave, each read back.
    results: list[dict] = []
    for m in manifest:
        base = {"wave_index": m["wave_index"], "user_id": m["user_id"]}
        if m["blocked"]:
            results.append({**base, "outcome": "blocked", "reasons": m["blocked_reasons"]})
            continue
        nums = [o["num_order_id"] for o in m["orders"]]
        body = {
            "LocationId": location_id,
            "SortingType": m["sorting_type"],
            "GroupType": m["group_type"],
            "Orders": [{"OrderId": n, "SortOrder": pos} for pos, n in enumerate(nums)],
        }
        if m["user_id"] is not None:
            body["UserId"] = m["user_id"]
        try:
            resp = _picking_write("Picking/GeneratePickingWave", body)
        except RateLimitError as exc:
            results.append({**base, "outcome": "rate_limited", "error": str(exc)})
            continue
        except RuntimeError as exc:
            results.append({**base, "outcome": "error", "error": str(exc)})
            continue
        created = [w.get("PickingWaveId") for w in (resp.get("PickingWaves") or [])
                   if w.get("PickingWaveId")]
        if not created:
            results.append({**base, "outcome": "refused",
                            "validation_results": resp.get("ValidationResults") or []})
            continue
        results.append({**base, **_read_back_generated_wave(created, nums)})

    created_ids = [wid for r in results for wid in r.get("picking_wave_ids", [])]
    failed = [r["wave_index"] for r in results if r["outcome"] not in ("created", "unconfirmed")]
    complete = all(r["outcome"] == "created" and r.get("readback_matches") for r in results)
    message = f"{len(created_ids)} wave(s) created: {created_ids}."
    if created_ids and failed:
        message += (
            f" PARTIAL RUN: waves {created_ids} already exist. Do NOT re-run the whole "
            f"batch — re-send only wave index(es) {failed}."
        )
    return {
        "dry_run": False,
        "results": results,
        "created_wave_ids": created_ids,
        "rate_limited": [r for r in results if r["outcome"] == "rate_limited"],
        "warnings": warnings,
        "complete": complete,
        "message": message,
    }
```

- [ ] **Step 5: Run the tests and confirm they pass**

Run: `.venv/bin/python3 -m pytest tests/test_pick_wave_writes.py -v`
Expected: all PASS.

- [ ] **Step 6: Docs, threshold rows and count**

In `tests/test_pick_waves.py`, change `assert len(registered) == 95` to `assert len(registered) == 96`.

```bash
sed -i '' 's/— 95 tools/— 96 tools/; s/^95 tools\. /96 tools. /' CLAUDE.md
sed -i '' 's/badge\/tools-95-blue/badge\/tools-96-blue/' README.md
```

In `CLAUDE.md`, insert a new section directly before `### Reporting (read, autopaginating)`:

```markdown
### Picking (write) — issue #67

Built in v1.56.0 from `docs/superpowers/specs/2026-09-22-pickwave-write-tools-design.md`. There is no endpoint to delete a wave or to add an order to an existing one; cleanup means removing orders and/or abandoning the wave. Every write is read back through a fresh `get_pick_wave_detail`, or through the header list for an abandoned wave, since the detail endpoint no longer returns one.

| Tool | Endpoint(s) | Threshold | Key notes |
|---|---|---|---|
| `generate_pick_waves(waves, location_id, confirmed_count, dry_run=True)` | `Picking/GeneratePickingWave` (one POST per wave) + `CheckAllocatableToPickwave` + `OpenOrders/GetIdentifiersByOrderIds` + `GetPickwaveUsersWithSummary` | 25 orders | Each wave `{order_ids, user_id?, sorting_type=BinPriority, group_type=Items}`. Refused before any write: an order appearing twice (including once by GUID and once by number), or a `user_id` not on the live picker roster. An unresolved order blocks its own wave only. Linnworks' pickability check and a FIFO_READY check feed the manifest; missing FIFO_READY is a **warning, not a block**. A throttle during the checks stops with nothing written. Per-wave outcome: `created` / `refused` (ValidationResults verbatim) / `rate_limited` / `error` / `unconfirmed` / `blocked`. **Not atomic across waves**: a partial run names the created waves and says not to re-run the batch. Whether the body is wrapped is set by `_PICKING_WRITE_WRAPPED`. |
```

In the `CLAUDE.md` Write-safety threshold table, add this row directly after the `create_order` row:

```markdown
| `generate_pick_waves` | 25 | Creates live pickwaves the warehouse will pick from |
```

In `README.md`, add a new table directly after the `**Picking (read)**` table (after its last row, `get_pick_wave_detail`):

```markdown

**Picking (write) — writes default to dry_run=True**

| Tool | What it does |
|---|---|
| `generate_pick_waves` | Create one or more pickwaves (one Linnworks call per wave). Checks every order first — resolves it, runs Linnworks' pickability check, warns if it isn't FIFO_READY — and validates the picker. Staged above 25 orders. Each new wave is read back; a partial multi-wave run says which waves exist and not to re-run the batch |
```

In the `README.md` threshold table, add directly after `| \`create_order\` | 10 lines |`:

```markdown
| `generate_pick_waves` | 25 orders |
```

Run: `.venv/bin/python3 -m pytest -q && .venv/bin/python3 server.py --list-tools | tail -1`
Expected: all pass; `96 tools registered`.

- [ ] **Step 7: Commit**

```bash
git add server.py tests/test_pick_wave_writes.py tests/test_pick_waves.py CLAUDE.md README.md
git commit -m "Add generate_pick_waves (#67)

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 4: `update_pick_wave`

**Files:**
- Modify: `server.py` (tool + `_read_back_updated_wave`)
- Test: `tests/test_pick_wave_writes.py`, `tests/test_pick_waves.py` (count 97)
- Modify: `CLAUDE.md`, `README.md`

**Interfaces:**
- Consumes: `_fetch_pick_wave`, `_format_pick_wave_detail`, `_find_wave_header`, `_fetch_picker_roster`, `_pick_wave_unknown_user`, `_pick_wave_preflight_throttled`, `_picking_write`, `_PICK_WAVE_SETTABLE_STATES`, `_PICK_WAVE_EMPTY_DETAIL_NOTE`.
- Produces:
  - `_read_back_updated_wave(picking_wave_id: int, expected_state: str, expected_user: int | None, check_user: bool, location_id: str) -> dict`: `{"outcome": "updated"|"not_applied"|"unconfirmed", "complete", "after_state"?, "after_user_id"?, "after_email"?, "readback_error"?}`
  - Tool `update_pick_wave(picking_wave_id: int, user_id: int | None = None, unassign: bool = False, state: str | None = None, allow_in_progress: bool = False, dry_run: bool = True) -> dict`

**Design note (why `State` is always sent):** the spec says a missing `UserId` means "keep the current user", but says nothing about a missing `State`. If `State` is a non-nullable enum on the server, leaving it out could reset the wave to the enum default, which is `Unallocated`. So the tool always sends `State` (the current state when it isn't changing) and carries `StartTime` / `EndTime` through when they're set (assumption #3 in CLAUDE.md). The live proof checks that a state-only change keeps the assigned user.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_pick_wave_writes.py`:

```python
# ── update_pick_wave ────────────────────────────────────────────────────────

JO = "jo@thewarehousegroup.co.uk"


def _assigned_wave(state="Unallocated", start=None):
    return _wave_resp(wave_id=9001, state=state, user_id=19, email=JO,
                      orders=[_wave_order(611385, GUID_A)], start=start)


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

    def test_abandoning_an_in_progress_wave_needs_the_flag(self):
        fake = FakeLinnworks(waves={9001: _assigned_wave(state="InProgress")})
        with fake.active():
            refused = server.update_pick_wave(9001, state="Abandoned")
            allowed = server.update_pick_wave(9001, state="Abandoned", allow_in_progress=True)
        assert refused["success"] is False
        assert "allow_in_progress" in refused["error"]
        assert allowed["dry_run"] is True
        assert allowed["plan"]["new_state"] == "Abandoned"

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
```

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `.venv/bin/python3 -m pytest tests/test_pick_wave_writes.py -k Update -v`
Expected: FAIL with `AttributeError: module 'server' has no attribute 'update_pick_wave'`.

- [ ] **Step 3: Implement the tool**

Append to the #67 block in `server.py`:

```python
def _read_back_updated_wave(
    picking_wave_id: int,
    expected_state: str,
    expected_user: int | None,
    check_user: bool,
    location_id: str,
) -> dict:
    """
    Re-read a wave after UpdatePickingWaveHeader. An Abandoned wave drops out
    of GetPickingWave, so it is looked up in the Abandoned header list instead.
    `check_user` is False only for an abandon that didn't touch the user,
    since the header list isn't relied on to carry the assignee.
    """
    try:
        if expected_state == "Abandoned":
            after = _find_wave_header(picking_wave_id, "Abandoned", location_id)
        else:
            after = _format_pick_wave_detail(_fetch_pick_wave(picking_wave_id))
    except RateLimitError as exc:
        return {"outcome": "unconfirmed", "complete": False, "readback_error": f"rate limited: {exc}"}
    except RuntimeError as exc:
        return {"outcome": "unconfirmed", "complete": False, "readback_error": str(exc)}
    if after is None:
        return {"outcome": "unconfirmed", "complete": False,
                "readback_error": f"wave {picking_wave_id} could not be found after the update"}
    state_ok = after["state"] == expected_state
    user_ok = (after["user_id"] == expected_user) if check_user else True
    applied = state_ok and user_ok
    return {
        "outcome": "updated" if applied else "not_applied",
        "complete": applied,
        "after_state": after["state"],
        "after_user_id": after["user_id"],
        "after_email": after["email_address"],
    }


@mcp.tool()
def update_pick_wave(
    picking_wave_id: int,
    user_id: int | None = None,
    unassign: bool = False,
    state: str | None = None,
    allow_in_progress: bool = False,
    dry_run: bool = True,
) -> dict:
    """
    Reassign, unassign, pause, reset or abandon one pickwave
    (Picking/UpdatePickingWaveHeader).

    - user_id: assign the wave to this picker (see get_pick_wave_users).
    - unassign=True: remove the picker (sends UserId -1). Not with user_id.
    - state: "Paused", "Unallocated" or "Abandoned" only. Allocated is set by
      assigning a user; InProgress, Complete, Packing and Shipped describe
      physical work on the warehouse floor and are deliberately not settable.

    Abandoning an InProgress wave, or setting it back to Unallocated, needs
    allow_in_progress=True — a picker may be working it. Pausing doesn't.

    The wave's current state is always sent, even when only the picker
    changes, and its start/end times are carried through: leaving them out
    could reset them on the server. Omitting UserId keeps the current picker
    (per the API spec — verified by the #67 live proof).

    Reads the wave back afterwards (from the Abandoned header list for an
    abandon, because the detail endpoint drops finished waves) and reports
    outcome updated / not_applied / unconfirmed / rate_limited / error.
    dry_run=True by default.
    """
    if user_id is not None and unassign:
        return {"success": False, "error": "Pass user_id OR unassign=True, not both."}
    if user_id is None and not unassign and state is None:
        return {"success": False, "error": "Nothing to change — pass user_id, unassign=True, or state."}
    if state is not None and state not in _PICK_WAVE_SETTABLE_STATES:
        return {"success": False, "error": (
            f"state must be one of {list(_PICK_WAVE_SETTABLE_STATES)}. Allocated is set by "
            "assigning a user_id; InProgress, Complete, Packing and Shipped describe physical "
            "work on the warehouse floor and are deliberately not settable here.")}
    if user_id is not None and (isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0):
        return {"success": False, "error": "user_id must be a positive integer (see get_pick_wave_users)."}

    try:
        current = _format_pick_wave_detail(_fetch_pick_wave(picking_wave_id))
    except RateLimitError as exc:
        return {"success": False, "picking_wave_id": picking_wave_id,
                "outcome": "rate_limited", "complete": False, "error": str(exc)}
    if current is None:
        return {"success": False, "picking_wave_id": picking_wave_id,
                "error": "No live wave with that id. " + _PICK_WAVE_EMPTY_DETAIL_NOTE}

    current_state = current["state"]
    if state in ("Abandoned", "Unallocated") and current_state == "InProgress" and not allow_in_progress:
        return {"success": False, "picking_wave_id": picking_wave_id, "current_state": current_state,
                "error": (f"Wave {picking_wave_id} is InProgress — a picker may be working it. "
                          f"Setting it to {state} needs allow_in_progress=True. Pausing is allowed "
                          "without it.")}

    if user_id is not None:
        try:
            roster = _fetch_picker_roster()
        except RateLimitError as exc:
            return _pick_wave_preflight_throttled([{"step": "picker roster", "reason": str(exc)}])
        if user_id not in roster:
            return _pick_wave_unknown_user(user_id, roster)

    target_state = state or current_state
    expected_user = None if unassign else (user_id if user_id is not None else current["user_id"])
    location = current.get("location_id") or DEFAULT_LOCATION_ID

    body: dict = {"PickingWaveId": picking_wave_id, "State": target_state}
    if unassign:
        body["UserId"] = -1
    elif user_id is not None:
        body["UserId"] = user_id
    for field, key in (("start_time", "StartTime"), ("end_time", "EndTime")):
        if current.get(field):
            body[key] = current[field]

    plan = {
        "picking_wave_id": picking_wave_id,
        "current_state": current_state,
        "current_user_id": current["user_id"],
        "current_email": current["email_address"],
        "new_state": target_state,
        "new_user_id": expected_user,
        "order_count": len(current["orders"]),
    }
    if dry_run:
        return {"dry_run": True, "plan": plan, "message": "Dry run — nothing was changed."}

    try:
        _picking_write("Picking/UpdatePickingWaveHeader", body)
    except RateLimitError as exc:
        return {"dry_run": False, "plan": plan, "outcome": "rate_limited",
                "complete": False, "error": str(exc)}
    except RuntimeError as exc:
        return {"dry_run": False, "plan": plan, "outcome": "error",
                "complete": False, "error": str(exc)}

    check_user = not (target_state == "Abandoned" and user_id is None and not unassign)
    return {"dry_run": False, "plan": plan,
            **_read_back_updated_wave(picking_wave_id, target_state, expected_user, check_user, location)}
```

- [ ] **Step 4: Run the tests and confirm they pass**

Run: `.venv/bin/python3 -m pytest tests/test_pick_wave_writes.py -v`
Expected: all PASS.

- [ ] **Step 5: Docs and count**

In `tests/test_pick_waves.py`, change `== 96` to `== 97`.

```bash
sed -i '' 's/— 96 tools/— 97 tools/; s/^96 tools\. /97 tools. /' CLAUDE.md
sed -i '' 's/badge\/tools-96-blue/badge\/tools-97-blue/' README.md
```

Add to the `CLAUDE.md` `### Picking (write) — issue #67` table, after the `generate_pick_waves` row:

```markdown
| `update_pick_wave(picking_wave_id, user_id, unassign, state, allow_in_progress, dry_run=True)` | `Picking/UpdatePickingWaveHeader` (POST) | — (single wave) | Reassign (`user_id`, validated against the roster), unassign (`UserId: -1`), or set `state` to **Abandoned / Paused / Unallocated only**. Abandoning an InProgress wave, or setting it back to Unallocated, needs `allow_in_progress=True`; pausing it doesn't. **`State` is always sent**, even for a reassign, and StartTime/EndTime are carried through — the spec says only that a null `UserId` keeps the user, so a missing `State` could reset to the enum default. The read-back uses the **Abandoned header list** for an abandon, because GetPickingWave drops finished waves. Outcome `updated` / `not_applied` / `unconfirmed` / `rate_limited` / `error`. |
```

Add to the `README.md` `**Picking (write)**` table:

```markdown
| `update_pick_wave` | Reassign or unassign a wave's picker, or set it to Paused, Unallocated or Abandoned (progress states aren't settable). Abandoning or unallocating an InProgress wave needs `allow_in_progress=True`. Read back after every change |
```

Run: `.venv/bin/python3 -m pytest -q && .venv/bin/python3 server.py --list-tools | tail -1`
Expected: all pass; `97 tools registered`.

- [ ] **Step 6: Commit**

```bash
git add server.py tests/test_pick_wave_writes.py tests/test_pick_waves.py CLAUDE.md README.md
git commit -m "Add update_pick_wave (#67)

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 5: `remove_orders_from_pick_waves`

**Files:**
- Modify: `server.py` (tool + `_locate_orders_in_open_waves`, and the `WRITE_THRESHOLDS` row)
- Test: `tests/test_pick_wave_writes.py`, `tests/test_pick_waves.py` (count 98)
- Modify: `CLAUDE.md`, `README.md`

**Interfaces:**
- Consumes: `_resolve_order_numbers`, `_fetch_pick_wave`, `_format_pick_wave_detail`, `_picking_write`, `_write_guard`, `_as_int`, `_pick_wave_preflight_throttled`, `_PICK_WAVE_OPEN_STATES`.
- Produces:
  - `_locate_orders_in_open_waves(num_ids: list[int], location_id: str) -> dict[int, dict]`, mapping num → `{"picking_wave_id": int, "wave_state": str}`
  - Tool `remove_orders_from_pick_waves(order_ids: list, location_id: str = DEFAULT_LOCATION_ID, confirmed_count: int | None = None, dry_run: bool = True) -> dict`
  - Per-order outcomes: `removed`, `not_in_a_wave`, `still_present`, `unconfirmed`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_pick_wave_writes.py`:

```python
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

    def test_rate_limit_while_locating_writes_nothing(self):
        fake = FakeLinnworks(orders=ORDERS, raise_on={
            "Picking/GetAllPickingWaveHeaders": server.RateLimitError("quota exceeded")})
        with fake.active():
            out = server.remove_orders_from_pick_waves(["611385"], dry_run=False)
        assert out["success"] is False
        assert out["complete"] is False
        assert fake.writes() == []

    def test_stages_above_25_orders(self):
        many = {str(700000 + i): (f"{i:08d}-0000-0000-0000-000000000000", 700000 + i)
                for i in range(26)}
        with FakeLinnworks(orders=many).active() as fake:
            out = server.remove_orders_from_pick_waves(list(many), dry_run=False)
        assert out["staged"] is True
        assert fake.writes() == []
```

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `.venv/bin/python3 -m pytest tests/test_pick_wave_writes.py -k RemoveOrders -v`
Expected: FAIL with `AttributeError: module 'server' has no attribute 'remove_orders_from_pick_waves'`.

- [ ] **Step 3: Add the threshold row**

In `WRITE_THRESHOLDS`, directly after the `"generate_pick_waves"` line:

```python
    "remove_orders_from_pick_waves":  25,   # pulls orders out of waves — a picker may already hold the items
```

- [ ] **Step 4: Implement the tool**

Append to the #67 block in `server.py`:

```python
def _locate_orders_in_open_waves(num_ids: list[int], location_id: str) -> dict[int, dict]:
    """
    NumOrderId -> {"picking_wave_id", "wave_state"} for each order found in a
    non-finished wave at this location. One header call per open state plus
    one detail call per open wave — a handful, since only a few waves are live
    at once. Lets RateLimitError propagate.
    """
    wanted = set(num_ids)
    found: dict[int, dict] = {}
    for state in _PICK_WAVE_OPEN_STATES:
        resp = call_linnworks_get(
            "Picking/GetAllPickingWaveHeaders", {"state": state, "locationId": location_id}
        )
        for header in (resp.get("PickwaveHeaders") or []) if isinstance(resp, dict) else []:
            wave_id = header.get("PickingWaveId")
            detail = _format_pick_wave_detail(_fetch_pick_wave(wave_id))
            if detail is None:
                continue
            for o in detail["orders"]:
                if o["order_id"] in wanted and o["order_id"] not in found:
                    found[o["order_id"]] = {"picking_wave_id": wave_id, "wave_state": detail["state"]}
    return found


@mcp.tool()
def remove_orders_from_pick_waves(
    order_ids: list,
    location_id: str = DEFAULT_LOCATION_ID,
    confirmed_count: int | None = None,
    dry_run: bool = True,
) -> dict:
    """
    Take orders out of whatever pickwave holds them
    (Picking/DeleteOrdersFromPickingWaves). The orders themselves are not
    changed — they just stop being in a wave, and can go into a new one.

    The manifest shows which wave holds each order, and warns when that wave
    is InProgress (a picker may already have the items on the trolley).
    Orders can be GUIDs or order numbers; an unresolvable id is reported, not
    raised. Staged above 25 orders. dry_run=True by default.

    Needs the DeletePickingWavesNode permission, which no read tool exercises
    — a missing permission comes back as outcome "error" with Linnworks'
    message verbatim.

    Each affected wave is re-read afterwards. Per-order outcome: removed,
    not_in_a_wave (Linnworks' NoPickwaves list), still_present (the read-back
    still finds it), or unconfirmed (the read-back failed).

    There is no endpoint to delete a wave itself: to retire an emptied wave,
    use update_pick_wave(state="Abandoned").
    """
    if not order_ids:
        return {"success": False, "error": "order_ids is empty."}

    resolved, resolve_errors, rate_limited = _resolve_order_numbers(order_ids)
    if rate_limited:
        return _pick_wave_preflight_throttled(rate_limited)
    nums = [r[1] for r in resolved]
    try:
        holding = _locate_orders_in_open_waves(nums, location_id) if nums else {}
    except RateLimitError as exc:
        return _pick_wave_preflight_throttled([{"step": "locating waves", "reason": str(exc)}])

    manifest: list[dict] = []
    for input_id, num, guid in resolved:
        where = holding.get(num)
        row = {
            "order_id": input_id,
            "num_order_id": num,
            "order_guid": guid,
            "picking_wave_id": where["picking_wave_id"] if where else None,
            "wave_state": where["wave_state"] if where else None,
        }
        if where and where["wave_state"] == "InProgress":
            row["warning"] = (f"Wave {where['picking_wave_id']} is InProgress — a picker may "
                              "already have this order's items on the trolley.")
        if not where:
            row["note"] = "Not found in any open wave at this location."
        manifest.append(row)

    guard = _write_guard("remove_orders_from_pick_waves", resolved, confirmed_count, dry_run)
    if guard is not None:
        return {**guard, "manifest": manifest, "resolve_errors": resolve_errors}
    if dry_run:
        in_waves = sum(1 for m in manifest if m["picking_wave_id"])
        return {
            "dry_run": True,
            "order_count": len(order_ids),
            "manifest": manifest,
            "resolve_errors": resolve_errors,
            "message": f"Dry run — {in_waves} order(s) found in open waves. Nothing was removed.",
        }
    if not nums:
        return {"dry_run": False, "results": [], "resolve_errors": resolve_errors,
                "complete": False, "message": "No order resolved; nothing was sent."}

    try:
        resp = _picking_write("Picking/DeleteOrdersFromPickingWaves", {"OrderIds": nums})
    except RateLimitError as exc:
        return {"dry_run": False, "manifest": manifest, "outcome": "rate_limited",
                "complete": False, "error": str(exc)}
    except RuntimeError as exc:
        return {"dry_run": False, "manifest": manifest, "outcome": "error",
                "complete": False, "error": str(exc)}

    processed = {_as_int(x) for x in (resp.get("ProcessedOrderIds") or [])}
    no_wave = {_as_int(x) for x in (resp.get("NoPickwaves") or [])}

    still_in: set[int] = set()
    readback_failed: set[int] = set()
    for wave_id in sorted({m["picking_wave_id"] for m in manifest if m["picking_wave_id"]}):
        try:
            detail = _format_pick_wave_detail(_fetch_pick_wave(wave_id))
        except (RateLimitError, RuntimeError):
            readback_failed.add(wave_id)
            continue
        if detail is not None:
            still_in.update(o["order_id"] for o in detail["orders"])

    results: list[dict] = []
    for m in manifest:
        num = m["num_order_id"]
        if num in still_in:
            outcome = "still_present"
        elif m["picking_wave_id"] in readback_failed:
            outcome = "unconfirmed"
        elif num in processed:
            outcome = "removed"
        elif num in no_wave:
            outcome = "not_in_a_wave"
        else:
            outcome = "unconfirmed"
        results.append({**m, "outcome": outcome})

    complete = not resolve_errors and all(r["outcome"] in ("removed", "not_in_a_wave") for r in results)
    return {
        "dry_run": False,
        "results": results,
        "resolve_errors": resolve_errors,
        "complete": complete,
        "message": f"{sum(r['outcome'] == 'removed' for r in results)} order(s) removed from waves.",
    }
```

- [ ] **Step 5: Run the tests and confirm they pass**

Run: `.venv/bin/python3 -m pytest tests/test_pick_wave_writes.py -v`
Expected: all PASS.

- [ ] **Step 6: Docs, threshold rows and count**

In `tests/test_pick_waves.py`, change `== 97` to `== 98`.

```bash
sed -i '' 's/— 97 tools/— 98 tools/; s/^97 tools\. /98 tools. /' CLAUDE.md
sed -i '' 's/badge\/tools-97-blue/badge\/tools-98-blue/' README.md
```

Add to the `CLAUDE.md` `### Picking (write) — issue #67` table:

```markdown
| `remove_orders_from_pick_waves(order_ids, location_id, confirmed_count, dry_run=True)` | `Picking/DeleteOrdersFromPickingWaves` (POST `{"OrderIds":[int]}`) + header/detail reads to locate each order | 25 orders | Takes orders out of their waves; the orders themselves are unchanged. The manifest names each order's wave and warns if it's InProgress. Needs **`DeletePickingWavesNode`**, which no read tool exercises. Per-order outcome: `removed` / `not_in_a_wave` (Linnworks' `NoPickwaves`) / `still_present` (the read-back still finds it) / `unconfirmed`. There is no delete-wave endpoint: abandon an emptied wave with `update_pick_wave`. |
```

Add to the `CLAUDE.md` threshold table, after the `generate_pick_waves` row:

```markdown
| `remove_orders_from_pick_waves` | 25 | Pulls orders out of waves — a picker may already hold the items |
```

Add to the `README.md` `**Picking (write)**` table:

```markdown
| `remove_orders_from_pick_waves` | Take orders out of their pickwaves (the orders themselves are unchanged). The preview names each order's wave and warns if a picker is working it. Staged above 25. Each wave is re-read to confirm the orders are gone |
```

Add to the `README.md` threshold table, after the `generate_pick_waves` row:

```markdown
| `remove_orders_from_pick_waves` | 25 orders |
```

Run: `.venv/bin/python3 -m pytest -q && .venv/bin/python3 server.py --list-tools | tail -1`
Expected: all pass; `98 tools registered`.

- [ ] **Step 7: Commit**

```bash
git add server.py tests/test_pick_wave_writes.py tests/test_pick_waves.py CLAUDE.md README.md
git commit -m "Add remove_orders_from_pick_waves (#67)

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 6: Version 1.56.0 and the release note

**Files:**
- Modify: `pyproject.toml`, `server.py` (`__version__`), `README.md` (version badge), `CLAUDE.md` (header + version note)

- [ ] **Step 1: Bump the version in all four places**

```bash
sed -i '' 's/^version = "1.55.11"/version = "1.56.0"/' pyproject.toml
sed -i '' 's/^__version__ = "1.55.11"/__version__ = "1.56.0"/' server.py
sed -i '' 's/version-1.55.11-blue/version-1.56.0-blue/' README.md
sed -i '' 's/^\*\*Current version: 1.55.11\*\*/**Current version: 1.56.0**/' CLAUDE.md
grep -n '1\.56\.0' pyproject.toml server.py README.md CLAUDE.md
```

Expected: one hit in each file.

- [ ] **Step 2: Add the version note**

In `CLAUDE.md`, directly after the line `94 tools. …` — which now reads `98 tools. \`python server.py --list-tools\` is the authoritative count; …` — and its blank line, insert:

```markdown
> **v1.56.0 (issue #67) — pickwave write tools: `generate_pick_waves`, `update_pick_wave`, `remove_orders_from_pick_waves`, plus the `get_pick_wave_detail` read (22 Sep 2026):** The write half split from #64. Built from `docs/superpowers/specs/2026-09-22-pickwave-write-tools-design.md` via the plan in `docs/superpowers/plans/2026-09-22-pickwave-write-tools.md`. **Found read-only before design, all on 22 Sep 2026:**
> - `GetPickingWave` returns full order and item detail for a LIVE wave; the v1.54.0 "returns zero" note only ever tested finished waves.
> - The header state filter does discriminate among live states: Unallocated → 2 waves, InProgress → 1, Allocated → 0.
> - `GeneratePickingWavesNode` is held, because both reads gated on it work.
> - Picker ids come from the roster: `warehouse+01`–`+05` = 68, 69, 70, 71, 73.
> - FIFO_READY is readable via `OpenOrders/GetIdentifiersByOrderIds` (UNWRAPPED; the wrapper 400s "OrderIds not provided in request").
> - Wave `Bins[]` carries real bin codes, even though `GetItemBinracks` can't return any here.
>
> **Design decisions (owner):** trolley planning deferred to a follow-up; one `GeneratePickingWave` per wave, because the batch form needs every order line's row id; FIFO_READY warns and doesn't block; only Abandoned/Paused/Unallocated are settable; weight and dimensions deferred. **Two traps designed around:** an abandoned wave drops out of `GetPickingWave`, so its read-back uses the Abandoned header list; and the spec only promises that a null `UserId` keeps the picker, so `update_pick_wave` always sends `State` and carries StartTime/EndTime through. Thresholds: generate 25 orders, remove 25 orders, update none (single wave). 98 tools.
```

(Task 7 adds the live-proof results to this note.)

- [ ] **Step 3: Run the full suite and the registration check**

Run: `.venv/bin/python3 -m pytest -q && .venv/bin/python3 server.py --list-tools | tail -1`
Expected: all pass (≈933 existing + ≈60 new); `98 tools registered`.

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml server.py README.md CLAUDE.md
git commit -m "Release v1.56.0: pickwave write tools (#67)

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 7: Live proof (contained test on the owner's login)

This is not TDD; it's the live verification the spec requires. **Every live write needs the owner's explicit approval in chat before it runs.** The running Claude Desktop MCP won't have the new tools until it's restarted, so drive everything in-process from the repo root with `.venv/bin/python3` (the `.env` credentials are already bootstrapped). Keep a running log of every call and response, because Step 9 records it.

**Stop rule:** if any step fails unexpectedly, stop and clean up from that point before doing anything else. That means removing the test order from any wave, abandoning the test wave, and cancelling the test order.

**Files:**
- Modify: `server.py` (`_PICKING_WRITE_WRAPPED` if needed; `_PICK_WAVE_STATE_LABELS` after owner confirmation; docstring wording)
- Modify: `tests/test_pick_waves.py` (`test_state_label_mapping_only_contains_live_observed_states`)
- Modify: `CLAUDE.md` (confirmed-endpoint rows, the version note, and the does-NOT-work table entry for `GetAllPickingWaves` / `GetPickingWave`)

- [ ] **Step 1: Snapshot every open wave (read-only)**

```bash
.venv/bin/python3 - <<'PY' 2>/dev/null
import json, server as s
snap = {}
for st in s._PICK_WAVE_OPEN_STATES:
    for h in s.get_pick_waves(state=st, location_id=s.DEFAULT_LOCATION_ID).get("waves", []):
        d = s.get_pick_wave_detail(h["picking_wave_id"])
        snap[h["picking_wave_id"]] = {"state": d.get("state"), "user_id": d.get("user_id"),
                                      "orders": sorted(o["order_id"] for o in d.get("orders", []))}
json.dump(snap, open("/tmp/claude-pickwave-snapshot-before.json", "w"), indent=1)
print(json.dumps(snap, indent=1))
PY
```

`get_pick_waves` returns its rows under `waves` (confirmed 22 Sep 2026). Expected: the live waves at that moment (on 22 Sep: 3549, 3550, 3531).

- [ ] **Step 2: Create the throwaway order (owner approval)**

Read the SKU used by the 18 Sep `create_order` proof (order 611394), or ask the owner for a cheap in-stock SKU:

```bash
.venv/bin/python3 -c "import server as s; o=s.get_order('611394'); print([(i['SKU'], i.get('price_per_unit')) for i in o['items']])" 2>/dev/null
```

Dry run first, then live **only after approval**. Replace `SKU_HERE`:

```bash
.venv/bin/python3 - <<'PY' 2>/dev/null
import json, server as s
args = dict(items=json.dumps([{"sku": "SKU_HERE", "quantity": 1, "price": 0.0}]),
            delivery_address=json.dumps({"full_name": "ZZZ MCP PICKWAVE TEST", "address1": "Brightley Mill",
                                         "town": "Okehampton", "postcode": "EX20 1RR", "country": "United Kingdom"}),
            reference_number="ZZZ-MCP-PICKWAVE-67", note="#67 live proof - cancel after test")
print(json.dumps(s.create_order(**args, dry_run=True), indent=1)[:3000])
PY
```

These parameter names and the JSON item fields (`sku`, `quantity`, `price`) and address fields (`full_name`, `address1`, `town`, `postcode`, `country`) match `create_order`'s signature as of v1.55.11. After approval, re-run with `dry_run=False` and record the new order number (`NUM`).

- [ ] **Step 3: Generate the wave (owner approval)**

```bash
.venv/bin/python3 -c "import json,server as s; print(json.dumps(s.generate_pick_waves([{'order_ids':['NUM'],'user_id':19}], dry_run=True), indent=1))" 2>/dev/null
```

Check the manifest: the order resolves; `pickable` should be True (it was just created and paid); FIFO_READY will most likely be False (a warning, as intended). After approval, run with `dry_run=False`.

- **Success:** outcome `created` with `readback_matches: true`. Record the wave id (`WAVE`).
- **HTTP 400 that mentions a missing or empty request/parameter:** the wrapper setting is wrong, and nothing was created. Confirm with `get_pick_waves(state="Unallocated")` that no new wave appeared. Flip `_PICKING_WRITE_WRAPPED["Picking/GeneratePickingWave"]`, rerun the tests, and retry once (with approval again). Record which shape worked.
- **Anything else:** stop and clean up (the stop rule).

- [ ] **Step 4: Update the wave, reading back after each call (owner approval for the batch of four)**

```bash
.venv/bin/python3 - <<'PY' 2>/dev/null
import json, server as s
W = WAVE
for kw in ({"unassign": True}, {"user_id": 19}, {"state": "Paused"}, {"state": "Unallocated"}):
    out = s.update_pick_wave(W, dry_run=False, **kw)
    print(kw, "->", out.get("outcome"), out.get("after_state"), out.get("after_user_id"), out.get("error", ""))
PY
```

Expected, in order:
1. unassign → `updated`, user None
2. `user_id=19` → `updated`, user 19
3. Paused → `updated`, **user still 19** (this proves a missing `UserId` keeps the picker)
4. Unallocated → `updated`

If the first call returns a 400 mentioning a missing request, fix `_PICKING_WRITE_WRAPPED["Picking/UpdatePickingWaveHeader"]` exactly as in Step 3. If the Paused step loses the user, record it: the "null keeps the user" promise doesn't hold, and the tool must then send the current `UserId` explicitly. Change the code, add a test, then continue.

- [ ] **Step 5: Owner confirms state labels against the Linnworks screen**

With the test wave now Unallocated, ask the owner to open the pickwave screen and confirm what wave `WAVE` shows. Repeat for any InProgress wave in the Step 1 snapshot, and for Paused (re-pause the test wave briefly if needed, with approval). For each state the owner confirms (as the exact wording on screen), add it to `_PICK_WAVE_STATE_LABELS` in `server.py`:

```python
_PICK_WAVE_STATE_LABELS: dict[str, str] = {
    "Abandoned": "Abandoned",
    "Shipped": "Shipped",
    "Unallocated": "Unallocated",   # confirmed on screen by the owner, 22 Sep 2026 (#67)
}
```

Add only the states actually confirmed, using the screen's wording as the label. Update the comment block above the dict to say which were added and when. Update `tests/test_pick_waves.py::TestModuleLevelSymbols::test_state_label_mapping_only_contains_live_observed_states` so its expected set matches the dict exactly.

- [ ] **Step 6: Remove the order (owner approval)**

```bash
.venv/bin/python3 -c "import json,server as s; print(json.dumps(s.remove_orders_from_pick_waves(['NUM'], dry_run=True), indent=1))" 2>/dev/null
```

The manifest should show the order in `WAVE`. After approval, run with `dry_run=False`.

- **Success:** outcome `removed`. That settles `DeletePickingWavesNode` as held.
- **Outcome `error` with a permission message:** record it verbatim. The permission is **not** held; the owner has to grant `DeletePickingWavesNode` to the app install. Clean up by abandoning the wave with the order still in it (Step 7). Note that this does test what abandoning does to the orders inside a wave, so record what happens to the order.

- [ ] **Step 7: Abandon the test wave (owner approval)**

```bash
.venv/bin/python3 -c "import json,server as s; print(json.dumps(s.update_pick_wave(WAVE, state='Abandoned', dry_run=False), indent=1))" 2>/dev/null
```

Expected: `updated`, `after_state` Abandoned, confirmed from the Abandoned header list.

- [ ] **Step 8: Cancel the test order and re-snapshot (owner approval for the cancel)**

```bash
.venv/bin/python3 -c "import json,server as s; print(json.dumps(s.cancel_order('NUM', note='#67 live proof cleanup', dry_run=False), indent=1))" 2>/dev/null
```

Expected: outcome `cancelled`, and the SKU's allocated stock released (check with `get_stock_level`). Then re-run Step 1's script, writing to `/tmp/claude-pickwave-snapshot-after.json`, and compare it with the before snapshot for every pre-existing wave. Ignore any wave the warehouse legitimately created, picked or finished in the meantime, but check that none of **our** calls touched one.

```bash
python3 -c "import json;a=json.load(open('/tmp/claude-pickwave-snapshot-before.json'));b=json.load(open('/tmp/claude-pickwave-snapshot-after.json'));print({k:(a[k],b.get(k)) for k in a if a[k]!=b.get(k)})"
```

- [ ] **Step 9: Record the results and tidy the "unverified" wording**

1. **`server.py`:** set `_PICKING_WRITE_WRAPPED` to the shapes that worked, and replace the "UNVERIFIED until their first live call" comment with "Live-confirmed 22 Sep 2026 (#67): …". In the `generate_pick_waves` and `update_pick_wave` docstrings, replace "(per the API spec — verified by the #67 live proof)" and the `_PICKING_WRITE_WRAPPED` line with what was actually observed.
2. **CLAUDE.md, confirmed-working-endpoints table:** add rows for `Picking/GetPickingWave` (live waves), `Picking/GeneratePickingWave`, `Picking/UpdatePickingWaveHeader`, `Picking/DeleteOrdersFromPickingWaves` and `OpenOrders/GetIdentifiersByOrderIds`, each with method, the exact body shape, what the response looked like, and "Live-tested 22 Sep 2026 (#67 contained test, wave `WAVE`, order `NUM`)". Include any `ValidationResults` refusal shape that came up.
3. **CLAUDE.md, does-NOT-work table:** change the `Picking/GetAllPickingWaves` / `Picking/GetPickingWave` row to say `GetPickingWave` works for LIVE waves and is empty only for finished ones.
4. **CLAUDE.md, v1.56.0 note:** add a sentence for each live finding: the wrapper shapes, whether a null `UserId` keeps the picker, the permission result, the labels confirmed, and what (if anything) was learned about abandoning a wave with orders in it.

Run: `.venv/bin/python3 -m pytest -q && .venv/bin/python3 server.py --list-tools | tail -1`
Expected: all pass; `98 tools registered`.

- [ ] **Step 10: Commit**

```bash
git add server.py tests/test_pick_waves.py CLAUDE.md
git commit -m "Record #67 pickwave live proof results

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 8: PR, merge and follow-ups

- [ ] **Step 1: Push the branch and open the PR**

```bash
git push -u origin feature/issue-67-pickwave-writes
gh pr create --repo josephgurney/linnworks-mcp --base main --head feature/issue-67-pickwave-writes \
  --title "Pickwave write tools: generate, update, remove orders + wave detail (closes #67, v1.56.0)" \
  --body-file /tmp/claude-pr67-body.md
```

Write `/tmp/claude-pr67-body.md` first. It should cover: what the four tools do; the owner decisions; the read-only findings; the live-proof results from Task 7 (wave id, order number, wrapper shapes, permission result, labels confirmed); tests added; "98 tools, v1.56.0"; and end with:

```
🤖 Generated with [Claude Code](https://claude.com/claude-code)
```

- [ ] **Step 2: Wait for CI, then merge on the owner's say-so**

Read CI with the `mcp__ccd_pr__get_status` tool (or watch `gh pr checks <n> --watch` in the background). Merge with `gh pr merge <n> --merge --delete-branch` only once `tests` passes **and** the owner has said to merge.

- [ ] **Step 3: File the follow-up issues and comment on #67**

```bash
gh issue create --repo josephgurney/linnworks-mcp \
  --title "Trolley-sized pickwave planning (follow-up to #67)" --body-file /tmp/claude-issue-trolley.md
gh issue create --repo josephgurney/linnworks-mcp \
  --title "Optional: single-call multi-wave generate (Pickwaves[] form) — follow-up to #67" --body-file /tmp/claude-issue-batch.md
```

- **Trolley planning issue body:** what the planning step needs (the trolley spec: totes per trolley, maximum weight, volume and orders); the weight/dimension question (14/14 SKUs in wave 3549 have values, but many look like packaging defaults, e.g. 0.4 kg 10×5×3 on four different items); and that the MCP primitives now exist.
- **Batch issue body:** approach B from the spec — needs every order line's row id, including bundle component lines — and is only worth doing if the call count ever matters.

Then comment on #67 with a summary and the PR link, if merging hasn't auto-closed it.

- [ ] **Step 4: Brain rollup**

Follow `.brain/rollup.md` for v1.56.0: update `.brain/BRAIN.md` (remove the #67 "held" entry, and add the live findings and follow-up issue numbers), then append `=== v1.56.0 RELEASED <date> — rollup complete ===` to `.brain/HOOK_LOG.md`.
