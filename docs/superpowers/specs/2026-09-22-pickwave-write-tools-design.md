# Pickwave write tools — design (issue #67)

**Date:** 22 Sep 2026 · **Issue:** #67 (split from #64) · **Target version:** v1.56.0 (94 → 98 tools)
**Status:** design agreed in chat 22 Sep 2026, awaiting spec review

## Goal

Give Claude the primitives to create, read back, trim and reassign or abandon Linnworks pickwaves, so waves can later be built to trolley size and kept tidy as orders change.

**In scope:** four tools: one read tool and three writes.
**Out of scope:**
- Trolley-sized planning (packing orders by weight, volume and count). That's a separate follow-up issue, needing the trolley spec.
- Weight and dimension data. Deferred to the planning piece.
- Item-level wave edits (`UpdatePickingWaveItem`, `UpdatePickingWaveItemWithNewBinrack`, `UpdatePickedItemDelta`).
- Linnworks' single-call multi-wave generate form.

## Decisions (owner, 22 Sep 2026)

| Question | Decision |
|---|---|
| Scope | The tools now; trolley planning later |
| Live proof | A contained test on the owner's login (user 19), with a throwaway order |
| FIFO_READY | Warn in the preview, don't block |
| Weight and dimensions | Deferred to the planning piece |
| Multi-wave generate | One `GeneratePickingWave` call per wave (approach A) |
| Settable wave states | Abandoned, Paused, Unallocated only |

## Facts established read-only before design (22 Sep 2026)

- **Permissions** (from the spec's per-endpoint notes):
  - `GeneratePickingWavesNode` is **held**. Both `GetPickwaveUsersWithSummary` and `CheckAllocatableToPickwave` require it, and both work live (v1.54.0).
  - `DeletePickingWavesNode` (needed by `DeleteOrdersFromPickingWaves`) is **unknown**. No read endpoint needs it, so the first remove call settles it.
  - `UpdatePickingWaveHeader` needs `PickingWavesNode`, the same permission as the proven read `GetItemBinracks`.
- **Picker ids** come from `get_pick_wave_users`: `warehouse+01`–`+05` = users 68, 69, 70, 71, 73; the owner is user 19. `UserId` is an integer.
- **`Picking/GetPickingWave?pickingWaveId=` returns full detail for a LIVE wave.** Wave 3549 (Unallocated, created 22 Sep) returned 8 orders, each carrying `PickingWaveOrdersRowId`, `OrderId`, `PickState`, `SortOrder`, `Items[]`, `Composition`, `OrderId_Guid`, `IsProcessed`, `IsCancelled`, `IsOnHold`, `IsLocked` and `IsPaid`. Items carry `PickingWaveItemsRowId`, `ToPickQuantity`, `PickedQuantity`, `ItemState`, `OrderItemRowId` and `StockItemId`. The response also has `Skus[]` (SKU, title, barcode, image) and `Bins[]`. The v1.54.0 "returns zero waves" finding applies only to **finished** waves (Shipped or Abandoned).
- **The header state filter discriminates among live states:** Unallocated → [3549, 3550], InProgress → [3531], Allocated → [], Paused → []. This resolves #64's open question.
- **FIFO_READY is an order identifier.** The `linnworks_auto_pickwaves` worker reads it through `OpenOrders/GetIdentifiersByOrderIds` and assigns or unassigns it through `OpenOrders/AssignOrderIdentifier` / `UnassignOrderIdentifier`.
- **No endpoint deletes a wave or adds an order to an existing wave.** Cleanup means removing orders and/or setting the wave Abandoned.
- **Write request shapes, per `picking.json`:**
  - `GeneratePickingWave`: body `PickingWaveGenerate` {`LocationId`, `UserId` int, `SortingType` BinPriority|OrderView, `GroupType` Items|Orders, `Orders[]` {`OrderId` int, `SortOrder` int}, `Pickwaves[]`}. Response {`ValidationResults[]`, `PickingWaves[]`, `Skus[]`, `Bins[]`}.
  - `DeleteOrdersFromPickingWaves`: `{"request":{"OrderIds":[int]}}`. Response {`ProcessedOrderIds[]`, `NoPickwaves[]`}.
  - `UpdatePickingWaveHeader`: body `PickingWaveUpdateRequest` {`PickingWaveId`, `UserId` (null keeps the current user, -1 unassigns), `State`, `StartTime`, `EndTime`}.
  - None of the three has required fields per the spec. Whether the Generate and Update bodies need the `{"request": …}` wrapper is **unverified**; the first live call settles it, and the existing `CheckAllocatableToPickwave` needed the wrapper.

## Tools

### `get_pick_wave_detail(picking_wave_id)` (read)

Named `…_detail` rather than `get_pick_wave` so it can't be confused with the existing `get_pick_waves` list tool.

- Returns the header (id, state and its label, location, assigned user if present, counts, group and sort type) and `orders[]`. Each order has `order_id`, `order_guid`, `pick_state`, `sort_order`, the `is_locked` / `is_on_hold` / `is_cancelled` / `is_processed` / `is_paid` flags, and `items[]`. Each item has SKU and title (joined from `Skus[]`), `to_pick`, `picked`, `item_state`, `order_item_row_id` and bin (from `Bins[]`, verbatim, where present).
- `blockers[]`: orders that are now locked, on hold, cancelled or processed, which are the candidates for removal.
- A finished wave that comes back empty is reported with a note saying so, never as "this wave has no orders".
- Reuses `_format_pick_wave` and `_pick_wave_state_label`. **No second state map.**
- A 429 is reported as `rate_limited` with `complete: false`, never as an empty wave.

### `generate_pick_waves(waves, location_id=DEFAULT_LOCATION_ID, confirmed_count=None, dry_run=True)`

- `waves`: a list of `{order_ids: [str|int], user_id: int|None, sorting_type: "BinPriority"|"OrderView" = "BinPriority", group_type: "Items"|"Orders" = "Items"}`. The defaults match how the live waves are set up.
- Order ids may be GUIDs or numbers and are resolved to the numeric `OrderId` that the endpoint needs. `SortOrder` is the order's position in the list.
- One `GeneratePickingWave` call per wave (approach A).
- Result per wave:
  - `outcome` is one of `created`, `refused` (Linnworks' `ValidationResults` verbatim), `rate_limited`, `error`, `unconfirmed` or `blocked` (a pre-write check failed).
  - `picking_wave_id` for a created wave.
  - Read-back fields: `readback_order_ids`, and `readback_matches` against the request.

### `remove_orders_from_pick_waves(order_ids, confirmed_count=None, dry_run=True)`

- The preview maps each order to the wave that currently holds it. The tool finds these by listing headers for the non-finished states (Unallocated, Allocated, InProgress, Paused, Complete, Packing) and reading each wave's detail. That's a handful of calls, since only a few waves are live at once.
- It warns when the holding wave is InProgress (a picker may already have those items on the trolley).
- The result reports removed orders separately from orders that weren't in any wave (`ProcessedOrderIds` vs `NoPickwaves`). Each affected wave is re-read to confirm the orders are gone.

### `update_pick_wave(picking_wave_id, user_id=None, unassign=False, state=None, allow_in_progress=False, dry_run=True)`

- `user_id` reassigns the wave; `unassign=True` sends `UserId: -1`. Passing both is refused.
- `state` is one of **Abandoned, Paused, Unallocated**. Any other value is refused before any call, with a message explaining that progress states describe physical work on the floor.
- Abandoning an InProgress wave, or moving it back to Unallocated, needs `allow_in_progress=True`. Pausing an InProgress wave is allowed.
- Reads back the user and state and reports `outcome` as `updated`, `not_applied`, `unconfirmed` or `rate_limited`.

## Safety

### Checks before any write

These also run on a dry run:

1. **Every order id must resolve.** An unresolved id blocks its own wave only.
2. **No order can appear in more than one wave in the same call.**
3. **Every `user_id` must be on the live picker roster** (from `GetPickwaveUsersWithSummary`). This applies to generate and update.
4. **Pickability pre-flight (generate):**
   - `CheckAllocatableToPickwave` runs for every order. It was proven side-effect free in v1.54.0.
   - Per-order problems (e.g. already in a wave, doesn't exist) are shown with Linnworks' reason, and that wave is flagged in the preview.
   - Flagged waves are **not** auto-dropped. The live call would be refused by Linnworks anyway, and a dry run should show what would happen rather than silently change the request.
5. **FIFO_READY (generate):** `OpenOrders/GetIdentifiersByOrderIds` is read, and orders without the tag get a warning. They are not blocked.

### Staging

| Operation | Threshold | Counted by |
|---|---|---|
| `generate_pick_waves` | 25 | Orders across all waves |
| `remove_orders_from_pick_waves` | 25 | Orders |
| `update_pick_wave` | — | Single-wave operation (same precedent as `remove_order_item`) |

All three default to `dry_run=True`. `_check_injection` is not needed, because none of the three takes free text.

### Read-back and honesty

- **Success comes from a fresh `get_pick_wave_detail` read, never from the write's own response.**
- A failed read-back is `unconfirmed`, never success.
- **Partial multi-wave run:** the message lists exactly which waves were created and says **not** to re-run the whole batch, because the waves already created would be duplicated.
- **`RateLimitError` is caught explicitly** at every call site and reported as `rate_limited`, with `complete: false`. It's never folded into `refused`, `not found` or `no waves` (the #34/#37 rule).
- Linnworks error bodies and validation reasons are passed through verbatim.

## Live proof

This is the contained test on the owner's login. **Each live write needs the owner's approval.**

1. **Snapshot every live wave.** Take headers for all non-finished states plus the detail of each wave.
2. **Create a throwaway order.** `create_order`: DIRECT source, one cheap in-stock SKU, paid. This path is proven.
3. **Generate.** Dry run, then live `generate_pick_waves` with that one order assigned to **user 19**, then `get_pick_wave_detail`.
4. **Update.** `update_pick_wave`: reassign (user 19 → unassign → user 19), then `state="Paused"`, then `state="Unallocated"`, reading back each time.
5. **Remove.** `remove_orders_from_pick_waves` on the test order, then read back. This settles the `DeletePickingWavesNode` question.
6. **Abandon.** `update_pick_wave(state="Abandoned")` on the now-empty wave, then read back.
7. **Cancel.** `cancel_order` on the test order, and check its stock is released.
8. **Re-snapshot.** Confirm none of the pre-existing waves changed.

If any step fails, stop and clean up from that point: remove the order from any wave it's in, abandon the test wave, cancel the order.

**Record in CLAUDE.md:**
- the wrapped vs unwrapped shape of each write endpoint;
- the permission result;
- what abandoning does to orders still in a wave (not tested directly, because orders are removed first; state this plainly);
- what `ValidationResults` looks like on a refusal, if one occurs;
- new `_PICK_WAVE_STATE_LABELS` entries for states seen on a real wave during the build (Unallocated, InProgress and Paused so far). The AC6 rule from #64 requires each label to be confirmed against the Linnworks UI, so the owner confirms them against the screen before labels are added.

## Tests (offline, mocked)

New file: `tests/test_pick_wave_writes.py`.

- **Refusals before any call:** unresolved order; an order in two waves; unknown `user_id`; an invalid or disallowed `state`; `user_id` together with `unassign`; abandoning or unallocating an InProgress wave without `allow_in_progress`.
- **Previews:** the pickability flag and the FIFO_READY warning appear in the dry-run manifest.
- **Staging:** above 25 orders, a batch stages; a wrong `confirmed_count` errors.
- **Outcomes:** per-wave outcomes on a partial multi-wave run (wave 1 created, wave 2 refused); the no-re-run message.
- **Rate limiting:** a 429 at each call site becomes `rate_limited` and `complete: false`.
- **Read-back:** `created` / `readback_matches`; `unconfirmed` when the read-back fails; a remove read-back that finds the order still present.
- **Detail read:** the `blockers` list; the finished-wave empty note.
- **Shared vocabulary:** all tools use the module-level `_format_pick_wave` / `_pick_wave_state_label` (no second map).
- **Registration:** `generate_pick_waves` and `remove_orders_from_pick_waves` appear in `WRITE_THRESHOLDS`; `update_pick_wave` (single-wave) and `get_pick_wave_detail` (read) do not.
- **Docs:** `tests/test_docs_consistency.py` covers the new rows.

## Delivery

- Branch `feature/issue-67-pickwave-writes`, then a PR. Never push to main.
- **server.py:** four tools in the Picking section; `WRITE_THRESHOLDS` gains `generate_pick_waves: 25` and `remove_orders_from_pick_waves: 25`, and `update_pick_wave` needs no row.
- **CLAUDE.md:**
  - new tools-table rows;
  - confirmed-endpoint rows with the live request shapes (the old `GetPickingWave` "not wrapped" row is corrected for live waves);
  - threshold table rows;
  - a v1.56.0 note;
  - header count 98.
- **README.md:** new rows and badges.
- **Version** 1.56.0 in `pyproject.toml`, `server.__version__`, both docs and the badge.
- **Follow-up issues when #67 closes:**
  - trolley-sized wave planning (needs the trolley spec, and weight/dimension data quality: real values or packaging defaults?);
  - optionally, the single-call batch generate form.

## Open items for spec review

1. **The name `get_pick_wave_detail`** (the chat design said `get_pick_wave`). Changed to avoid confusion with `get_pick_waves`.
2. **Adding state labels** (Unallocated, InProgress, Paused) needs the owner to confirm each against the Linnworks screen during the proof.

## Amendments during the build (22 Sep 2026)

Decisions and corrections made while implementing this spec and while running its Live proof section, kept here rather than rewritten into the sections above.

- **C1 — wrapping.** Generate and Update are sent UNWRAPPED, Delete WRAPPED, exactly as this spec's `_PICKING_WRITE_WRAPPED` guess assumed. The contained live proof (waves 3552/3553) confirmed all three first time; nothing needed to flip to the fallback wrapped shape.
- **I4 — read-back judges only what changed.** `update_pick_wave`'s read-back (`_read_back_updated_wave`) only compares the fields the caller actually asked to change: a `user_id`/`unassign` call is judged on the user, a `state` call on the state, both when both were passed. Anything the server changed on its own is reported as a flag (`state_changed_by_server` / `user_changed_by_server`) rather than folded into the pass/fail outcome, because Linnworks is free to move the untouched field (or not) and that isn't this call's failure.
- **I5 — the "started wave" guard is broader than InProgress.** Abandoning or unallocating a wave needs `allow_in_progress=True` whenever the wave is InProgress, Paused, Complete or Packing, or has any item already picked — not only InProgress. A picker can pause a wave, or a wave can show picked items while sitting in a state that isn't InProgress, and either case still means a trolley may hold stock.
- **M1 — select the wave by id, not "the first one".** `_format_pick_wave_detail` takes an optional `picking_wave_id` and, when given one, only accepts a response row whose `PickingWaveId` matches it. A response for a different wave is treated as not found rather than silently substituted.
- **M2 — refuse an unknown current state before writing.** `update_pick_wave` reads the wave's current `State` and refuses the call outright if that value isn't one of the documented pickwave states (including a missing state), because the tool always sends the current state back on every write and an unrecognised value risks resetting the wave to whatever Linnworks defaults an unknown enum to.
- **Structured refusals, not exceptions.** Every pre-write read (picker roster, pickability check, FIFO_READY check, wave lookup, locating an order's wave) catches `RateLimitError` and `RuntimeError` and turns them into a plain refusal dict — `{"success": False, "error": ...}` or the staged rate-limit shape — instead of letting the exception surface. The write calls and their read-backs go one step further: after `RateLimitError`/`RuntimeError`, each also catches a bare `Exception` (a `requests.exceptions.Timeout`, a dropped connection, an `OSError`) and reports `outcome: "unconfirmed"` with the request possibly having reached Linnworks — never a raised exception, and never a false "nothing happened".
- **Live findings that corrected this spec, not merely confirmed it:**
  - The detail endpoint (`GetPickingWave`, wrapped by `get_pick_wave_detail`) does **not** simply "drop finished waves" as earlier drafts assumed. It returns nothing for a SHIPPED wave or an EMPTIED wave (one whose last order was just removed, which auto-abandons it), and for an unknown id — but it **does** return an ABANDONED wave that still holds orders. "Finished" and "gone from this endpoint" are not the same thing.
  - Abandoning a wave that still holds orders is allowed by Linnworks and changes the header's state, but it does **not** release those orders — they stay attached to the abandoned wave and are refused ("Already exists in another wave") if you try to wave them again. `remove_orders_from_pick_waves` is still required before they can go anywhere else. Both `update_pick_wave` (on the abandon itself) and `remove_orders_from_pick_waves` (in the "not found" note) now say this.
  - Removing a wave's last order does not leave an empty wave sitting around — Linnworks auto-abandons it. There is no separate "retire an emptied wave" step, and the spec's original suggestion to call `update_pick_wave(state="Abandoned")` for that purpose is redundant for the emptied case (it remains the right call for abandoning a wave that still has orders in it, on purpose).
  - `DeleteOrdersFromPickingWaves` turned out to be neither location-scoped (already suspected) nor state-scoped (not previously known): it freed an order out of an already-ABANDONED wave that the tool's own open-states locate step had reported as "not found in any open wave".
