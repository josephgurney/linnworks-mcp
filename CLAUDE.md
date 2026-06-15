# Linnworks MCP Server — Claude context

**Current version: 1.12.0** — 52 tools. See `pyproject.toml` for full metadata.

---

## Brain — load at session start

This project has a private `.brain/` intelligence layer that tracks change history
and known patterns. Load it at the start of every session:

1. Read `.brain/HOOK_LOG.md` — recent commit annotations (what changed, why it matters, risk flags)
2. Read `.brain/BRAIN.md` — known fragile areas, patterns, decisions, carry-forwards

Confirm both are loaded before writing any code. If either file is missing, note it and continue.

**At version release:** follow the rollup instructions in `.brain/rollup.md` to update
`BRAIN.md` from the log, then write the version marker into `HOOK_LOG.md`.

---

A local stdio MCP server that exposes Linnworks data to Claude Desktop. This is **Phase 1** of a two-phase plan:

- **Phase 1 (current)**: Single-tenant stdio server, runs on your machine, no OAuth, no hosting. Used to iterate on tool design with a fast feedback loop.
- **Phase 2 (later)**: Same tool functions wrapped as a remote MCP server with OAuth 2.1 + Dynamic Client Registration, deployed to a public host, registered as a custom connector in claude.ai.

---

## Write-safety framework

Every bulk write tool (any tool that takes a list of items) must use two shared helpers defined near the top of `server.py`:

### `_write_guard(operation, items, confirmed_count, dry_run, threshold=None)`

Staged-manifest gate. Call this at the top of any write tool before touching the Linnworks API.

- **Batch ≤ threshold** → returns `None`; standard `dry_run` logic applies.
- **Batch > threshold, `confirmed_count` is None** → returns a blocking dict with `staged=True`. The caller should build the per-item manifest preview and merge it into this dict before returning. No writes happen.
- **Batch > threshold, `confirmed_count` ≠ `len(items)`** → returns an error dict. Prevents injection from bypassing staging by guessing a wrong count.
- **Batch > threshold, `confirmed_count` == `len(items)`** → returns `None`; execution proceeds.

The `confirmed_count` echo-back is the key protection: an injected instruction can't predict the exact count without first seeing the manifest.

Thresholds (defined in `WRITE_THRESHOLDS`):

| Operation | Threshold | Reason |
|---|---|---|
| `set_stock_levels` | 25 | Immediate channel availability impact |
| `set_inventory_item_prices` | 25 | Immediate channel price impact |
| `create_or_update_inventory_item` | 50 | Channel sync is async |
| `set_extended_properties` | 50 | Metadata, lower blast radius |
| `set_inventory_item_descriptions` | 50 | Content, lower blast radius |
| `add_inventory_item_images` | 100 | Additive only, no overwrites |
| `default` | 25 | Fallback for unlisted operations |

There is **no hard cap** — any batch size works once confirmed. The threshold is a staging gate, not a refusal.

### `_check_injection(field_name, value)`

Last-resort prompt injection tripwire. Call this on every free-text write parameter (titles, descriptions, notes, extended property values) before forwarding to Linnworks.

Raises `ValueError` naming the offending field if the value matches a known injection signature (e.g. `"ignore previous instructions"`, `"system:"` prefix, `[INST]` tags, `<|im_start|>` markers, `"act as"`, `"forget everything"`, etc.).

**This is not a comprehensive defence** — it fails loudly on obvious attacks and passes everything else. The primary safety nets remain `dry_run=True` default, read-before-write, and per-item result reporting.

### Usage pattern for a bulk write tool

```python
@mcp.tool()
def set_stock_levels(updates: list[dict], confirmed_count: int | None = None, dry_run: bool = True) -> dict:
    # 1. Injection check on all text fields
    for u in updates:
        _check_injection("note", u.get("note", ""))

    # 2. Build manifest (always — needed for both staging and dry_run)
    manifest = [_preview_stock_change(u) for u in updates]

    # 3. Write guard — may return a blocking staged dict
    guard = _write_guard("set_stock_levels", updates, confirmed_count, dry_run)
    if guard is not None:
        return {**guard, "manifest": manifest}  # merge in the preview

    if dry_run:
        return {"dry_run": True, "item_count": len(updates), "manifest": manifest}

    # 4. Execute in chunks of 25, collect per-item results
    results = _execute_in_chunks(updates, chunk_size=25)
    return {"dry_run": False, "item_count": len(updates), "results": results}
```

---

## Conventions

- **Python 3.10+**, type hints throughout. Tool docstrings become the descriptions Claude sees — write them carefully, that's the UX.
- **Secrets stay in env vars.** Never hardcode, never log, never commit. `.env` is gitignored; in production credentials come from Claude Desktop's config `env` block.
- **Authorization header is the raw token — no `Bearer ` prefix.** This is the most common auth bug.
- **Open Orders endpoints require the `{"request": {...}}` wrapper.** Without it: `'request' parameter is missing`.
- **Read before write.** Every write tool must (a) fetch current state first, (b) log old vs new, (c) read back after, and (d) default to `dry_run=True`. No exceptions.
- **Surface Linnworks errors verbatim** in tool exceptions — the response body almost always contains the real reason.
- **Cache the auth token for the process lifetime, re-auth once on 401.** Don't auth on every call (rate limits) and don't pretend tokens never expire (they do).

---

## Tools

51 tools (44 in v1.10.0 + 7 in v1.11.0). See `server.py` for full docstrings and parameter details.

> **v1.11.0:** Inventory write suite — `create_or_update_inventory_item`, `set_stock_levels`, `set_inventory_item_prices`, `set_extended_properties`, `set_inventory_item_descriptions`, `add_inventory_item_images`, `create_variation_group`. All protected by `_write_guard` + `_check_injection`. Spec-based; not yet live-tested.
>
> **v1.10.0:** Write-safety framework (`_write_guard`, `_check_injection`, `WRITE_THRESHOLDS`). `run_import` and `run_export`.
>
> **v1.5.0:** `get_order` now returns `delivery_address` and `billing_address` dicts with all address fields. New `set_order_address` write tool.

### Orders & stock (read)

| Tool | Endpoint | Key notes |
|---|---|---|
| `get_open_orders(location_id, limit, overdue_only)` | `OpenOrders/GetOrdersLowFidelity` | Always returns `overdue_count`; `overdue_only=True` filters past-deadline; `StatusLabel` decoded |
| `find_inventory_item(sku_or_title)` | `Inventory/GetInventoryItem` | **Exact SKU only** — no fuzzy/title search |
| `get_order(order_id)` | `Orders/GetOrdersById` or `GetOrderDetailsByNumOrderId` | GUID → POST; numeric ID → GET; returns `customer_name`, `customer_email`, `delivery_address`, `billing_address`, `notes` list, `totals` (subtotal/postage/tax/total/currency), `fulfilment_location_id`; each item now also includes `row_id` (OrderItemRowId — required by `refund_order_lines`), `price_per_unit`, `cost_inc_tax` |
| `set_order_address(order_id, ..., dry_run=True)` | `Orders/SetOrderCustomerInfo` | Update delivery address on open orders only; GUID or numeric order_id; pass only the fields to change (None = keep current); read-before-write diff; blocks on processed orders |
| `get_order_notes(order_id)` | `Orders/GetOrderNotes` | Fetch all notes on an order (open or processed); returns note_id, text, internal flag, timestamp, creator; GUID or numeric order_id |
| `add_order_note(order_id, note, internal=True, dry_run=True)` | `Orders/AddOrdersNote` | Add a note to any order; internal=True by default (staff-only); dry_run default; works on open and processed orders |
| `update_order_note(order_id, note_id, note, internal=None, dry_run=True)` | `ProcessedOrders/DeleteOrderNote` + `Orders/AddOrdersNote` | No dedicated update endpoint — deletes old note then adds replacement; preserves internal flag if not supplied; before/after diff |
| `delete_order_note(order_id, note_id, dry_run=True)` | `ProcessedOrders/DeleteOrderNote` | Permanently removes a note; read-before-write confirms existence and captures text; dry_run default |
| `delete_order_notes_by_text(order_id, text, match, case_sensitive, max_to_delete, dry_run=True)` | `Orders/GetOrderNotes` + `ProcessedOrders/DeleteOrderNote` | Delete notes by content match — no note_id needed; match modes: exact (default), contains, starts_with; case_insensitive by default; max_to_delete guard prevents accidental bulk deletion; zero matches = success |
| `find_open_orders_for_sku(sku, location_id)` | `GetOrdersLowFidelity` + `GetOrdersById` | Finds all open orders containing a SKU; searches composite children too; enriches with customer name + email; use for "who's waiting on this item?" |
| `find_orders_by_reference(reference, include_processed=False, location_id)` | `OpenOrders/SearchOrders` + `GetOrdersById` | Look up orders by channel reference (Shopify "#11177274", Amazon "202-...", eBay etc.); strips leading #; returns customer name + email + external_reference; include_processed=True extends to dispatched orders; **SearchOrders not yet live-tested on this tenant (May 2026)** |
| `cancel_order(order_id, note=None, dry_run=True)` | `Orders/CancelOrder` | Cancel an open (unprocessed) order; refuses if already processed; dry_run shows items that would be cancelled; `fulfilmentCenter` taken from `FulfilmentLocationId` on the order; no `return_to_stock` param in API — controlled by workspace settings |
| `refund_order(order_id, note=None, push_to_channel=True, dry_run=True)` | `ReturnsRefunds/GetRefundOptions` + `ReturnsRefunds/CreateRefund` + `ReturnsRefunds/ActionRefund` | Full refund of all items + postage on a **processed** order; `push_to_channel=True` calls ActionRefund to push to Shopify/Amazon/eBay; **spec-based, not yet live-tested** |
| `refund_order_lines(order_id, lines, refund_postage=False, note=None, push_to_channel=True, dry_run=True)` | `ReturnsRefunds/GetRefundOptions` + `ReturnsRefunds/CreateRefund` + `ReturnsRefunds/ActionRefund` | Partial refund of specific lines; each entry needs `row_id` from `get_order` items; optional `amount` override and `quantity`; **spec-based, not yet live-tested** |
| `get_stock_level(sku, location_id, include_empty_locations)` | `GetInventoryItem` → `Stock/GetStockLevel_Batch` | Zeros filtered by default; warns on virtual dropship duplicate rows |
| `get_processed_orders(from_date, to_date, date_field, page, page_size)` | `ProcessedOrders/SearchProcessedOrders` | Flat response; min page_size 20; overflows context at 500 — use aggregation tools for wide ranges |
| `get_locations()` | `Inventory/GetStockLocations` | Returns all physical + virtual locations |
| `get_extended_properties(sku)` | `Inventory/GetInventoryItemExtendedProperties` | API typo in field name: `ProperyName` not `PropertyName` |
| `get_processed_order_items(from_date, to_date, ...)` | `SearchProcessedOrders` + `GetOrdersById` batched | Line-item detail per order; use `get_top_skus` instead for aggregated SKU analysis |

### Reporting (read, autopaginating)

| Tool | Notes |
|---|---|
| `get_revenue_summary(from_date, to_date, date_field)` | Totals + breakdown by channel and country. Best for "what was revenue in April?" |
| `get_top_skus(from_date, to_date, date_field, top_n, rank_by, supplier_name)` | Rank SKUs by revenue or units; optional supplier filter (case-insensitive partial match) |
| `get_category_report(from_date, to_date, date_field, top_n)` | Revenue + units by product category |
| `get_period_comparison(current_from, current_to, prior_from, prior_to, date_field)` | Side-by-side totals with % deltas for orders, revenue, AOV |
| `get_sales_by_supplier(from_date, to_date, date_field, top_n, rank_by)` | Aggregates revenue, units, orders, SKU count by supplier |
| `get_component_sales(from_date, to_date, date_field, top_n, sku)` | **Units only.** Explodes composite order lines (bundles, custom completes, multipacks, option/linking SKUs) to component (child) level so hidden component demand becomes measurable. Each row splits `composite_units` vs `standalone_units` + `from_composite` flag. Optional `sku` restricts to the children of one composite parent. Relies on `CompositeSubItems` (processed-order detail), **not** `CompositeChild` (open orders only). Child `Quantity` is already resolved — never multiply by parent qty. Confirmed against tenant 13 Jun 2026 |

### Purchase orders

| Tool | Notes |
|---|---|
| `search_purchase_orders(status, from_date, to_date, ...)` | `PurchaseOrder/Search_PurchaseOrders2`; payload **unwrapped** — wrapping silently ignores all filters |
| `get_purchase_order(purchase_id)` | Returns header + items (with `outstanding` = qty − delivered) + delivery records; deleted items filtered |
| `get_suppliers()` | `Inventory/GetSuppliers` GET — not in public OpenAPI specs but confirmed working |
| `create_purchase_order(..., dry_run=True)` | `DateOfPurchase` always required (SQL rejects null); `SupplierReferenceNumber` always required (send `""` if none) |
| `update_purchase_order_header(..., dry_run=True)` | Carry all existing header fields — missing fields are cleared; blocks on DELIVERED status |
| `add_purchase_order_item(purchase_id, sku, quantity, cost, tax_rate, dry_run=True)` | Add a new line to a PENDING/OPEN/PARTIAL PO; resolves SKU → GUID automatically |
| `update_purchase_order_item(purchase_id, purchase_item_id, quantity, cost, tax_rate, dry_run=True)` | Edit a line item; only fields you provide change; shows before/after diff; use `get_purchase_order` to find `purchase_item_id` |
| `remove_purchase_order_item(purchase_id, purchase_item_id, dry_run=True)` | Delete a line item; confirms what will be removed; read-back after write |
| `open_purchase_order(purchase_id, dry_run=True)` | PENDING → OPEN only |
| `deliver_purchase_order(...)` | Marks items as delivered |
| `add_purchase_order_note(...)` | Use for carrier tracking numbers — the "Add delivery" dialog in the UI has no public API equivalent |

### Rules Engine

| Tool | Notes |
|---|---|
| `get_rules()` | Flat list of all rules with pkRuleId, RuleName, Enabled, RunOrder |
| `get_rule(rule_id)` | Full IF/THEN/subrule tree for one rule |

Rule types seen in practice: `Orders` (fires on incoming orders), `Test` (sandbox). Action types include: `AssignShippingService`, `AssignToFolder`, `AssignTagToOrder`, `AssignOrderExtendedProperty`, `ChangeOrderLockStatus`, `ChangeOrderParkStatus`, `BlockOrderFromMerging`, `ExecuteMacro`.

### Import / Export

| Tool | Notes |
|---|---|
| `get_import_list()` | Use this for status triage — `ImportStatus` on the detail endpoint is unreliable |
| `get_export_list()` | Same shape as import list |
| `get_import(import_id)` | Config inspection only (feed URL, column mappings, schedule) — `ImportStatus` returns `null` even for erroring imports |
| `get_export(export_id)` | Config inspection for exports |
| `run_import(import_id, dry_run=True)` | Queue an import for immediate execution; read-before-run shows config preview; refuses if already executing/queued; spec-based, not yet live-tested |
| `run_export(export_id, dry_run=True)` | Queue an export for immediate execution; same pattern as `run_import`; spec-based, not yet live-tested |

### Inventory writes (v1.11.0 — spec-based, not yet live-tested)

All write tools use `_write_guard` (staged-manifest gate) and `_check_injection` (injection tripwire). All default to `dry_run=True`. See the **Write-safety framework** section above for the protection design.

The helper `_resolve_sku_to_id(sku, cache)` is shared across all write tools — it resolves SKU → StockItemId GUID via `Inventory/GetInventoryItem`. Results are cached within a single tool call to avoid redundant API calls for the same SKU.

| Tool | Endpoint(s) | Threshold | Notes |
|---|---|---|---|
| `create_or_update_inventory_item(items, confirmed_count, dry_run=True)` | `Inventory/AddInventoryItem` / `Inventory/UpdateInventoryItem` | 50 | Upsert by SKU — probes existence with GetInventoryItem, then creates or updates. On update, reads existing fields and merges so unsupplied fields are preserved. Returns per-item action (created/updated/error). Fields: sku, title, barcode, retail_price, purchase_price, tax_rate, category_name, weight, height, width, depth, metadata. |
| `set_stock_levels(updates, confirmed_count, dry_run=True)` | `Stock/UpdateStockLevelsBulk` | 25 | Absolute stock level overwrite. Read-before-write captures current levels for diff. Per-item `Errors[]` in response reported individually. Fields: sku, stock_level, location_id. |
| `set_inventory_item_prices(prices, confirmed_count, dry_run=True)` | `Inventory/CreateInventoryItemPrices` / `Inventory/UpdateInventoryItemPrices` | 25 | Price upsert keyed by (StockItemId, Source, SubSource). Reads existing price rows first; creates new rows or updates existing by pkRowId. Fields: sku, price, source, sub_source. |
| `set_extended_properties(properties, confirmed_count, dry_run=True)` | `Inventory/CreateInventoryItemExtendedProperties` / `Inventory/UpdateInventoryItemExtendedProperties` | 50 | Property upsert keyed by ProperyName (deliberate API typo). Reads existing props first; creates or updates. `_check_injection` on property_name and property_value. Fields: sku, property_name, property_value, property_type. |
| `set_inventory_item_descriptions(descriptions, confirmed_count, dry_run=True)` | `Inventory/CreateInventoryItemDescriptions` / `Inventory/UpdateInventoryItemDescriptions` | 50 | Description upsert keyed by (StockItemId, Source, SubSource). `_check_injection` on description. Fields: sku, description, source, sub_source. |
| `add_inventory_item_images(images, confirmed_count, dry_run=True)` | `Inventory/AddImageToInventoryItem` | 100 | Additive only — existing images not removed. One API call per image (no bulk endpoint). `_check_injection` on image_url. Fields: sku, image_url, is_main. |
| `create_variation_group(group_name, parent_sku, child_skus, dry_run=True)` | `Stock/CreateVariationGroup` | — (single op) | Checks for existing group first (GetVariationGroupByName). Resolves all SKUs to GUIDs. Read-back confirms creation. `_check_injection` on group_name. |

---

## Confirmed working endpoints

| Endpoint | Method | Payload shape | Key notes |
|---|---|---|---|
| `Auth/AuthorizeByApplication` | POST | `{"ApplicationId","ApplicationSecret","Token"}` JSON body | Returns session `Token` + `Server` URL |
| `OpenOrders/GetOrdersLowFidelity` | POST | `{"request":{"LocationId":"..."}}` | Primary open-orders list |
| `OpenOrders/GetOpenOrdersDetails` | POST | `{"OrderIds":["pkOrderID-guid",...]}` **unwrapped** | Use GUID `pkOrderID`, not numeric |
| `Orders/GetOrdersById` | POST | `{"pkOrderIds":["guid",...]}` **unwrapped** | Bulk order detail; response includes `CustomerInfo.Address.EmailAddress`, `CustomerInfo.Address.FullName`, `CustomerInfo.ChannelBuyerName`; `OrderItem.RowId` = `OrderItemRowId` used by refund tools; `FulfilmentLocationId` used by `cancel_order`; **composite components nest under `OrderItem.CompositeSubItems`** (NOT `CompositeChild` — that field exists only on open orders via `GetOrdersLowFidelity`). Child `Quantity` is the resolved line total (e.g. 5 packs × 10 = 50) — **do not multiply by parent qty**. Children's `PricePerUnit`/`Cost` are `0.0` (money is on the parent line); `Level` is `0` on both parent and child, so detect composites by non-empty `CompositeSubItems`, not `Level`. `_batch_order_items` / `_flatten_order_item` preserve this nested array under `composite_sub_items`; `get_component_sales` consumes it. Confirmed 13 Jun 2026 |
| `Orders/GetOrderDetailsByNumOrderId` | GET | `?orderId=<numeric>` | Single order by human-facing number |
| `Inventory/GetInventoryItem` | POST | `{"sku":"..."}` or `{"stockItemId":"..."}` **unwrapped** | Exact match only; returns `StockItemId` |
| `Stock/GetStockLevel_Batch` | POST | `{"request":{"StockItemIds":["guid",...]}}` | Returns all location rows |
| `Stock/GetStockItemsFullByIds` | POST | `{"request":{"StockItemIds":["guid",...]}}` | Item metadata by GUID |
| `Stock/GetStockItemsFullByIds` (with suppliers) | POST | `{"request":{"StockItemIds":[...],"DataRequirements":[1]}}` | **`DataRequirements:[1]` required** to populate `Suppliers[]` — default `[0]` returns empty array |
| `Stock/GetStockItemsByIds` | POST | `{"stockItemIds":["guid",...]}` **unwrapped** | Alternate metadata endpoint |
| `OpenOrders/GetViewStats` | POST | `{"request":{}}` | Sanity check: returns order counts by view |
| `Inventory/GetStockLocations` | GET | — | All locations; Default location ID = `00000000-0000-0000-0000-000000000000` |
| `ProcessedOrders/SearchProcessedOrders` | POST | `{"request":{"DateField":"received","FromDate":"...","ToDate":"...","PageNumber":1,"ResultsPerPage":20}}` | `DateField` required (received/processed/payment/cancelled); min 20 results per page; flat response — no GeneralInfo/ShippingInfo nesting |
| `Inventory/GetInventoryItemExtendedProperties` | POST | `{"inventoryItemId":"guid"}` **unwrapped** | Field name typo in response: `ProperyName` not `PropertyName` |
| `Inventory/GetSuppliers` | GET | — | Not in public OpenAPI specs but confirmed working; returns flat supplier list |
| `PurchaseOrder/Search_PurchaseOrders2` | POST | `{"Status":"PENDING","EntriesPerPage":50,"PageNumber":1,...}` **unwrapped** | Wrapping in `{"request":{...}}` silently ignores all filters |
| `PurchaseOrder/Get_PurchaseOrder` | POST | `{"pkPurchaseId":"guid"}` **unwrapped** | Returns header + items + delivery records |
| `Orders/SetOrderCustomerInfo` | POST | `{"orderId":"guid","info":{"ChannelBuyerName":"...","Address":{...},"BillingAddress":{...}},"saveToCrm":false}` **unwrapped** | Address fields: FullName, Company, Address1/2/3, Town, Region, PostCode, Country, PhoneNumber, EmailAddress, CountryId, Continent; **not in public GitHub OpenAPI spec** but confirmed in apidocs.linnworks.net; returns OrderTotalsInfo |
| `PurchaseOrder/Update_PurchaseOrderHeader` | POST | `{"updateParameter":{...all fields...}}` | Must carry all existing fields — nulls clear values; blocks on DELIVERED |
| `PurchaseOrder/Add_PurchaseOrderItem` | POST | `{"addItemParameter":{"pkPurchaseId","fkStockItemId","Qty","Cost","TaxRate","PackQuantity","PackSize"}}` | Adds a new line; same endpoint used by `create_purchase_order` |
| `PurchaseOrder/Update_PurchaseOrderItem` | POST | `{"updateItemParameter":{"pkPurchaseItemId","pkPurchaseId","Quantity","PackQuantity","PackSize","Cost","TaxRate"}}` | Note: uses `Quantity` (not `Qty` like Add); all fields required — carry unchanged values through |
| `PurchaseOrder/Delete_PurchaseOrderItem` | POST | `{"deleteItemParameter":{"pkPurchaseItemId","pkPurchaseId"}}` | Removes a line item; `pkPurchaseItemId` from `Get_PurchaseOrder` response |
| `RulesEngine/GetRules` | GET | — | Flat list of rule headers |
| `RulesEngine/GetRuleConditionNodes` | GET | `?pkRuleId=<int>` | Full IF/THEN tree for one rule |
| `ImportExport/GetImportList` | GET | — | List endpoint — reliable status |
| `ImportExport/GetExportList` | GET | — | Same shape as import list |
| `ImportExport/GetImport` | GET | `?id=<int>` | Config only — `ImportStatus` is null even for erroring imports |
| `ImportExport/GetExport` | GET | `?id=<int>` | Config only |
| `ImportExport/RunNowImport` | POST | `{"importId": <int>}` **unwrapped** | Returns **204 No Content** — use `call_linnworks_void`, not `call_linnworks`; puts import in queue immediately; not yet live-tested |
| `ImportExport/RunNowExport` | POST | `{"exportId": <int>}` **unwrapped** | Returns **204 No Content** — same pattern; not yet live-tested |
| `OpenOrders/SearchOrders` | POST | `{"request":{"LocationId":"...","SearchTerm":"...","IncludeProcessed":false}}` | Searches by ReferenceNum, ExternalReference, and related fields; response: `{"OpenOrders":[{"OrderIds":["guid",...]}],"ProcessedOrders":["guid",...]}` — open orders grouped in view objects, processed orders as flat GUID list; **confirmed in public OpenAPI spec, not yet live-tested on this tenant (May 2026)** |
| `Orders/CancelOrder` | POST | `{"orderId":"guid","fulfilmentCenter":"guid","note":"..."}` **unwrapped** | Cancels an open order; `fulfilmentCenter` = `FulfilmentLocationId` from order detail; optional `refund` (double) field for attached refund amount; returns a string |
| `ReturnsRefunds/GetRefundOptions` | POST | `{"request":{"OrderId":"guid"}}` | Returns `RefundOptions` including `CanRefund`, `CannotRefundReason` enum; use as pre-flight check before CreateRefund; **spec-based, not yet live-tested on this tenant** |
| `ReturnsRefunds/GetRefundHeadersByOrderId` | POST | `{"request":{"OrderId":"guid"}}` | Returns all existing refund headers for an order — useful to audit whether an order has already been (partially) refunded before calling CreateRefund; **spec-based, not yet live-tested** |
| `ReturnsRefunds/CreateRefund` | POST | `{"request":{"OrderId":"guid","ChannelInitiated":false,"RefundLines":[{"OrderItemRowId":"guid","RefundedUnit":"Item","Amount":10.00,"FreeTextOrNote":"..."},{"RefundedUnit":"Shipping","Amount":5.00}]}}` | Creates a refund record; `OrderItemRowId` = `OrderItem.RowId` from `GetOrdersById`; omit `OrderItemRowId` for Shipping/Additional lines; `RefundedUnit` enum: `Item`, `Shipping`, `Service`, `Additional`; returns `{RefundHeaderId, RefundReference, Status, CannotRefundReason, Errors}`; **spec-based, not yet live-tested** |
| `ReturnsRefunds/ActionRefund` | POST | `{"request":{"RefundHeaderId":42,"OrderId":"guid"}}` | Pushes an approved refund to the sales channel (Shopify/Amazon/eBay); returns `{SuccessfullyActioned, Status, Errors}`; note: `SuccessfullyActioned` can be true while individual `Errors` still exist — check both; **spec-based, not yet live-tested** |
| `Orders/GetOrderNotes` | GET | `?orderId=<guid>` | Returns a plain JSON array of `OrderNote` objects; **canonical fields per OpenAPI spec (confirmed May 2026)**: `OrderNoteId`, `OrderId`, `NoteDate`, `Internal` (bool, no `Is` prefix), `Note`, `CreatedBy`, `NoteTypeId`; **GUID required** — numeric IDs must be resolved first; **previously documented field names `pkOrderNoteId`/`IsInternal`/`NoteCreatedOn` were wrong** (fixed in issue #7) |
| `Orders/AddOrdersNote` | POST | `{"OrderIds":["guid",...],"NoteText":"...","IsInternal":true,"IsProcessingNote":false}` **unwrapped** | Accepts a list of order GUIDs; works on both open and processed orders |
| `ProcessedOrders/DeleteOrderNote` | POST | `{"pkOrderNoteId":"guid"}` **unwrapped** | Deletes a single note by its GUID; works on both open and processed orders; **no UpdateOrderNote endpoint exists** — use delete+add via `update_order_note` instead |
| `Inventory/AddInventoryItem` | POST | `{"inventoryItem": {ItemNumber, ItemTitle, BarcodeNumber, RetailPrice, PurchasePrice, TaxRate, CategoryName, Weight, Height, Width, Depth, MetaData, ...}}` **unwrapped** | Creates a new inventory item; response includes `fkStockItemId` (the new GUID); **spec-based, not yet live-tested** |
| `Inventory/UpdateInventoryItem` | POST | `{"inventoryItem": {StockItemId, ItemNumber, ItemTitle, ...all fields...}}` **unwrapped** | Updates an existing item; must carry ALL fields (nulls clear values — same gotcha as PO header update); **spec-based, not yet live-tested** |
| `Inventory/GetInventoryItemPrices` | GET | `?inventoryItemId=<guid>` | Returns array of price rows `[{pkRowId, Source, SubSource, Price, Tag, ...}]`; used as read-before-write for `set_inventory_item_prices`; **spec-based, not yet live-tested** |
| `Inventory/CreateInventoryItemPrices` | POST | `{"inventoryItemPrices": [{StockItemId, Source, SubSource, Price}]}` | Creates new price rows (no pkRowId needed); **spec-based, not yet live-tested** |
| `Inventory/UpdateInventoryItemPrices` | POST | `{"inventoryItemPrices": [{StockItemId, pkRowId, Source, SubSource, Price}]}` | Updates existing price rows by pkRowId; **spec-based, not yet live-tested** |
| `Inventory/GetInventoryItemDescriptions` | GET | `?inventoryItemId=<guid>` | Returns array of description rows; used as read-before-write for `set_inventory_item_descriptions`; **spec-based, not yet live-tested** |
| `Inventory/CreateInventoryItemDescriptions` | POST | `{"inventoryItemDescriptions": [{StockItemId, Source, SubSource, Description}]}` | Creates new description rows; **spec-based, not yet live-tested** |
| `Inventory/UpdateInventoryItemDescriptions` | POST | `{"inventoryItemDescriptions": [{StockItemId, pkRowId, Source, SubSource, Description}]}` | Updates existing description rows by pkRowId; **spec-based, not yet live-tested** |
| `Inventory/CreateInventoryItemExtendedProperties` | POST | `{"inventoryItemExtendedProperties": [{fkStockItemId, SKU, ProperyName, PropertyValue, PropertyType}]}` | Creates new extended property rows; note deliberate API typo `ProperyName`; **spec-based, not yet live-tested** |
| `Inventory/UpdateInventoryItemExtendedProperties` | POST | `{"inventoryItemExtendedProperties": [{fkStockItemId, pkRowId, ProperyName, PropertyValue, PropertyType}]}` | Updates existing property rows by pkRowId; note deliberate API typo; **spec-based, not yet live-tested** |
| `Inventory/AddImageToInventoryItem` | POST | `{"request": {StockItemId, ImageUrl, IsMain}}` | Adds a single image by URL; no bulk equivalent — iterate per item; **spec-based, not yet live-tested** |
| `Stock/UpdateStockLevelsBulk` | POST | `{"Items": [{SKU, StockItemId, StockLocationId, StockLevel}]}` | Sets absolute stock levels; response mirrors request shape plus per-item `Errors[]` array; **spec-based, not yet live-tested** |
| `Stock/CreateVariationGroup` | POST | `{"template": {VariationGroupName, ParentSKU, ParentStockItemId, VariationItemIds: [guid]}}` | Creates a new variation group; all child GUIDs must resolve to existing items; **spec-based, not yet live-tested** |
| `Stock/GetVariationGroupByName` | GET | `?variationGroupName=<string>` | Returns the variation group matching the name, or 404 if not found; used as pre-flight check in `create_variation_group`; **spec-based, not yet live-tested** |

---

## ReturnsRefunds — `CannotRefundReason` enum reference

Both `GetRefundOptions` and `CreateRefund` return a `CannotRefundReason` field. `"None"` means no problem. Any other value means the refund cannot proceed:

| Value | Meaning | Action |
|---|---|---|
| `None` | No problem — refund can proceed | Proceed |
| `OpenOrderInLinnworks` | Order isn't processed yet | Use `cancel_order` instead |
| `OrderIsFullyRefundedInLinnworks` | Already fully refunded | Check with `GetRefundHeadersByOrderId` |
| `NotImplemented` | Channel doesn't support API-initiated refunds | Refund manually in the channel |
| `DisabledInConfig` | Refunds disabled in Linnworks workspace settings | Check workspace settings |
| `MissingOrderInLinnworks` | Order GUID doesn't exist in Linnworks | Verify the GUID |
| `Other` | Unclassified error | Surface the `Errors[]` array for details |

`PostSaleStatus.StatusHeader` enum (returned on refund lines): `OPEN`, `PENDING`, `PROCESSED`, `ERROR`, `ERROR_ACKED`.

---

## Endpoints that do NOT work

These have been probed and confirmed broken — don't waste time retrying them:

| Endpoint | Error |
|---|---|
| `OpenOrders/GetOpenOrders` | HTTP 400 null-reference |
| `OpenOrders/GetOpenOrderIds` | HTTP 400 null-reference |
| `Stock/GetStockItemsFull` | HTTP 400 — does not support keyword search |
| `Inventory/GetInventoryItems` | HTTP 400 — no working payload shape found; use `GetInventoryItem` (singular) for exact SKU lookup |
| `Stock/GetStockList` | HTTP 404 |
| `Inventory/SearchInventory` | HTTP 404 |
| `Inventory/GetInventoryItemSuppliers` | HTTP 404 — use `Stock/GetStockItemsFullByIds` with `DataRequirements:[1]` instead |
| `Inventory/GetStockItemSuppliers` | HTTP 404 |
| `Inventory/GetItemSuppliers` | HTTP 404 |
| `PurchaseOrder/GetSupplierItems` | HTTP 404 |
| `Inventory/GetInventoryItemSupplierStat` | HTTP 404 |
| PO "Add delivery" booking | No public API equivalent — use `Add_PurchaseOrderNote` as workaround |

---

## Autopaginating tools — run sequentially

These tools make hundreds of API calls internally (autopaginating + batched line-item fetches):

`get_top_skus` · `get_sales_by_supplier` · `get_category_report` · `get_revenue_summary` · `get_period_comparison` · `get_component_sales`

**Never fire two of these in parallel.** Concurrent calls hit Linnworks rate limits and the second call times out. Run them sequentially — wait for one to return before calling the next.

---

## Auth setup

1. Create an app in the Linnworks developer console — this gives you an App ID and App Secret
2. Visit `https://apps.linnworks.net/Authorization/Authorize/{ApplicationId}` while logged into Linnworks
3. The **Permanent token** is displayed on the installation page (not via the redirect URL)
4. If no token is shown, configure a Postback URL on the app and reinstall
5. Use the Permanent token as `LINNWORKS_INSTALLATION_TOKEN`
6. Call `Auth/AuthorizeByApplication` — this exchanges it for a session token; re-auth on 401

Credentials go in `.env` (local dev) or the Claude Desktop config `env` block (production). Never commit them.

---

## Testing

- **`python server.py --check-auth`** — verifies credentials and auth handshake without touching the MCP layer
- **Claude Desktop** — after registering in `claude_desktop_config.json` and restarting, ask conversational questions. Logs: `~/Library/Logs/Claude/mcp.log` (macOS) · `%APPDATA%\Claude\logs\mcp.log` (Windows)
- **Claude Code** — `claude mcp add` registers the server for a new session; useful for calling tools directly during development

---

## Build workflow (Claude Code CLI)

When asked to "build the latest approved issue" or "build issue #N", follow these steps exactly — do not ask clarifying questions, just execute:

### Step 1 — Find the issue
```bash
gh issue list --repo josephgurney/linnworks-mcp --label approved --state open --json number,title,body,labels
```
Take the lowest-numbered result. Read the full issue body carefully.

### Step 2 — Understand what to build
Read this CLAUDE.md (you're reading it now). Cross-check the request against:
- The existing tools table — does it already exist?
- The confirmed working endpoints — which endpoint does this need?
- The broken endpoints — is it blocked?

### Step 3 — Write the tool
Add the new tool function to `server.py` following these rules:
- Place it in the correct section (Orders, Purchase Orders, Reporting, etc.)
- Add `@mcp.tool()` decorator
- Follow the read-before-write pattern for any write tool
- Default `dry_run=True` for all write tools
- Write the docstring carefully — it's the UX description Claude sees
- Match the payload wrapper pattern of nearby tools (some endpoints need `{"request":{}}`, some don't — check the confirmed endpoints table)

### Step 4 — Verify syntax
```bash
.venv/bin/python3 -m py_compile server.py && echo "Syntax OK"
```
Fix any errors before proceeding.

### Step 5 — Verify auth
```bash
.venv/bin/python3 server.py --check-auth
```
Confirms the server loads and credentials are valid.

### Step 6 — Update CLAUDE.md
- Add the new tool to the Tools table
- Add any newly confirmed endpoints to the confirmed endpoints table
- Add any newly discovered broken endpoints to the broken endpoints table

### Step 7 — Commit and push
```bash
git add server.py CLAUDE.md
git commit -m "Add <tool_name> tool (closes #N)"
git push origin main
```

### Step 8 — Comment on the issue
```bash
gh issue comment N --repo josephgurney/linnworks-mcp --body "## Built

Added \`tool_name\` in this commit. To use it, pull the latest \`server.py\` and restart Claude Desktop.

**What was built:** [one sentence]
**Endpoint used:** \`Endpoint/Name\`
**Dry run default:** yes/no"
```

### Step 9 — Label the issue
```bash
gh issue edit N --repo josephgurney/linnworks-mcp --add-label "built" --remove-label "approved"
```

### Important notes for CLI builds
- The Linnworks MCP is **not connected** in the CLI session — you cannot call Linnworks tools directly to test. Use `--check-auth` to verify the server loads, and note in your commit that live testing should be done via Claude Desktop.
- Do not ask the user for confirmation between steps — execute the full workflow and report back at the end.
- If an endpoint is unknown, check `https://apidocs.linnworks.net` or fetch the relevant spec file from `https://raw.githubusercontent.com/LinnSystems/PublicApiSpecs/master/1.0/<name>.json` before guessing. Key spec files: `orders.json`, `openorders.json`, `returnsrefunds.json`, `purchaseorder.json`, `inventory.json`, `stock.json`, `processedorders.json`, `importexport.json`, `rulesengine.json`.

---

## Out of scope for Phase 1

- OAuth, Dynamic Client Registration — Phase 2
- Hosting, deployment, public URLs — Phase 2
- Multi-tenancy, per-user token storage — Phase 2

Flag any request that pushes toward these and discuss before proceeding.
