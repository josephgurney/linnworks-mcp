# Linnworks MCP Server — Claude context

**Current version: 1.16.0** — 54 tools. See `pyproject.toml` for full metadata.

> **v1.16.0 (issue #15):** Fixed PO line-cost write — Linnworks `Cost` is the **tax-inclusive line total** (`unit × qty × (1+rate)`), not the ex-VAT unit cost. `create_purchase_order`, `add_purchase_order_item`, and `update_purchase_order_item` now convert via `_po_line_cost_inc_tax()` before writing (was sending the bare unit cost → wrong unit prices and PO totals). Read-back paths (`get_purchase_order`, the update diff) expose a derived `unit_cost_ex_tax` via `_po_line_unit_ex_tax()` so reads reconcile with the ex-VAT unit the tools accept.

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

> **v1.11.0:** Inventory write suite — `create_or_update_inventory_item`, `set_stock_levels`, `set_inventory_item_prices`, `set_extended_properties`, `set_inventory_item_descriptions`, `add_inventory_item_images`, `create_variation_group`. All protected by `_write_guard` + `_check_injection`. **Live-tested 15 Jun 2026** against an isolated test SKU (create/update/stock/price/description/extended-property all verified by read-back) — this surfaced and fixed the `StockItemId`/`pkRowId`/empty-body/default-price gotchas; see the Inventory writes section. `add_inventory_item_images` and `create_variation_group` not yet exercised live.
>
> **v1.10.0:** Write-safety framework (`_write_guard`, `_check_injection`, `WRITE_THRESHOLDS`). `run_import` and `run_export`.
>
> **v1.5.0:** `get_order` now returns `delivery_address` and `billing_address` dicts with all address fields. New `set_order_address` write tool.

### Orders & stock (read)

| Tool | Endpoint | Key notes |
|---|---|---|
| `get_open_orders(location_id, limit, overdue_only)` | `OpenOrders/GetOrdersLowFidelity` | Always returns `overdue_count`; `overdue_only=True` filters past-deadline; `StatusLabel` decoded |
| `find_inventory_item(sku_or_title)` | `Inventory/GetInventoryItem` | **Exact SKU only** — no fuzzy/title search; use `search_inventory_items` for keyword/name lookup |
| `search_inventory_items(keyword, page, per_page)` | `Stock/GetStockItems` (GET) | **Keyword discovery** — matches title, SKU, and barcode (the UI search box endpoint). Paged (`per_page` capped at 200); returns `total_entries`/`total_pages` + per-item `sku`, `stock_item_id`, `title`, `stock_level`, `available`, `barcode`, `retail_price`, etc. so results chain straight into stock/price/write tools. **Live-tested 16 Jun 2026** (closes #14) |
| `get_order(order_id)` | `Orders/GetOrdersById` or `GetOrderDetailsByNumOrderId` | GUID → POST; numeric ID → GET; returns `customer_name`, `customer_email`, `delivery_address`, `billing_address`, `notes` list, `totals` (subtotal/postage/tax/total/currency), `fulfilment_location_id`; each item now also includes `row_id` (OrderItemRowId — required by `refund_order_lines`), `price_per_unit`, `cost_inc_tax` |
| `set_order_address(order_id, ..., dry_run=True)` | `Orders/SetOrderCustomerInfo` | Update delivery address on open orders only; GUID or numeric order_id; pass only the fields to change (None = keep current); read-before-write diff; blocks on processed orders |
| `get_order_notes(order_id)` | `Orders/GetOrderNotes` | Fetch all notes on an order (open or processed); returns note_id, text, internal flag, timestamp, creator; GUID or numeric order_id |
| `add_order_note(order_id, note, internal=True, dry_run=True)` | `Orders/AddOrdersNote` | Add a note to any order; internal=True by default (staff-only); dry_run default; works on open and processed orders |
| `update_order_note(order_id, note_id, note, internal=None, dry_run=True)` | `ProcessedOrders/DeleteOrderNote` + `Orders/AddOrdersNote` | No dedicated update endpoint — deletes old note then adds replacement; preserves internal flag if not supplied; before/after diff |
| `delete_order_note(order_id, note_id, dry_run=True)` | `ProcessedOrders/DeleteOrderNote` | Permanently removes a note; read-before-write confirms existence and captures text; dry_run default |
| `delete_order_notes_by_text(order_id, text, match, case_sensitive, max_to_delete, dry_run=True)` | `Orders/GetOrderNotes` + `ProcessedOrders/DeleteOrderNote` | Delete notes by content match — no note_id needed; match modes: exact (default), contains, starts_with; case_insensitive by default; max_to_delete guard prevents accidental bulk deletion; zero matches = success |
| `find_open_orders_for_sku(sku, location_id)` | `GetOrdersLowFidelity` + `GetOrdersById` | Finds all open orders containing a SKU; searches composite children too; enriches with customer name + email; use for "who's waiting on this item?" |
| `find_orders_by_reference(reference, include_processed=False, location_id)` | `OpenOrders/SearchOrders` + `GetOrdersById` | Look up orders by channel reference (Shopify "#11177274", Amazon "202-...", eBay etc.); strips leading #; returns customer name + email + external_reference; include_processed=True extends to dispatched orders; **live-tested 15 Jun 2026** — payload must be sent **unwrapped** (see endpoints table) |
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
| `create_purchase_order(..., dry_run=True)` | `DateOfPurchase` always required (SQL rejects null); `SupplierReferenceNumber` always required (send `""` if none). `cost` is the **ex-VAT unit cost** — converted to a tax-inclusive line total on write (issue #15) |
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

### Inventory writes (v1.11.0 — **live-tested 15 Jun 2026** against an isolated test SKU)

> **Write-endpoint gotchas confirmed live 15 Jun 2026** (all now handled in code):
> - **`AddInventoryItem` needs a client-generated `StockItemId` GUID** — omitting it returns HTTP 400 "StockItem StockItemId could not be empty". The tool generates one with `uuid.uuid4()`.
> - **`Create*` sub-entity endpoints (prices, descriptions, extended properties) need a client-generated `pkRowId` GUID** — omitting it collides on the table PK (HTTP 400 "Violation of PRIMARY KEY ... duplicate key (00000000-...)"). The tools generate one per create row.
> - **The default price row (empty Source+SubSource) exists implicitly as the zero-GUID row but is NOT returned by `GetInventoryItemPrices`** — so a default-price write must UPDATE pkRowId `00000000-...`, not create. `set_inventory_item_prices` special-cases this; genuine channel rows are created with a fresh `pkRowId`.
> - **Most write endpoints return a 2xx with an EMPTY body on success** (AddInventoryItem, UpdateInventoryItem, the Create/Update sub-entity calls, UpdateStockLevelsBulk). `call_linnworks` now treats an empty 2xx body as `{}` instead of raising a JSON-decode error. `UpdateStockLevelsBulk` does not echo `Items`, so `set_stock_levels` infers per-item success from the 2xx and recommends a `get_stock_level` read-back.
> - **`Create`/`UpdateInventoryItemExtendedProperties` return a 2xx with a NON-empty, NON-JSON body on success** — `response.json()` rejects it with "Expecting value: line 1 column 1 (char 0)", which the empty-body guard alone doesn't catch. `call_linnworks` now wraps `response.json()` in a `try/except ValueError` and falls back to `{"raw": text}` on any 2xx, so a successful write is never mis-reported as a failure. This fixed `set_extended_properties` live writes, which previously failed on every `dry_run=False` call (issue #13, 15 Jun 2026).

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
| `delete_inventory_item(skus, confirmed_count, dry_run=True)` | `Inventory/DeleteInventoryItems` | 10 | **IRREVERSIBLE.** Read-before-write resolves each SKU → StockItemId and captures title + total stock into the manifest; unresolvable SKUs become error rows (don't abort the batch). Lowest staging threshold of the suite (10). Single delete call with the resolved GUID list; per-SKU read-back via GetInventoryItem confirms each is gone (`deleted: true/false`). Any Linnworks delete error surfaced verbatim in `delete_error`. **Live-tested 15 Jun 2026** — create → dry-run → delete → confirm-gone cycle verified against a throwaway `ZZZ-MCP-TEST-*` SKU (closes #12). |

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
| `Stock/GetStockItems` | GET | `?keyWord=<term>&entriesPerPage=<n>&pageNumber=<n>` | **Keyword search behind the UI inventory box** — matches title, SKU, and barcode (partial, case-insensitive). Response: `{"PageNumber","EntriesPerPage","TotalEntries","TotalPages","Data":[...]}`; each `Data` row has `ItemNumber` (SKU), `ItemTitle`, `BarcodeNumber`, `Quantity`, `Available`, `InOrder`, `RetailPrice`, `PurchasePrice`, `CategoryName`, `StockItemId`, `IsCompositeParent`, `IsVariationParent`. Optional `locationId` query param scopes stock figures to one location. **This is the working keyword-search endpoint** — the plural `Stock/GetStockItemsFull` and `Inventory/GetInventoryItems` both 400 here, but this singular GET does not. **Live-tested 16 Jun 2026** (issue #14) |
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
| `PurchaseOrder/Add_PurchaseOrderItem` | POST | `{"addItemParameter":{"pkPurchaseId","fkStockItemId","Qty","Cost","TaxRate","PackQuantity","PackSize"}}` | Adds a new line; same endpoint used by `create_purchase_order`. **`Cost` is the tax-inclusive LINE TOTAL** per the spec: `(unitcost × qty) + tax` — NOT the unit cost. Sending the bare unit cost makes Linnworks back-derive a wrong unit (`unit/1.2/qty`) and wrong PO totals (issue #15). Tools convert via `_po_line_cost_inc_tax()`. The header `UnitAmountTaxIncludedType:0` is correct and unchanged |
| `PurchaseOrder/Update_PurchaseOrderItem` | POST | `{"updateItemParameter":{"pkPurchaseItemId","pkPurchaseId","Quantity","PackQuantity","PackSize","Cost","TaxRate"}}` | Note: uses `Quantity` (not `Qty` like Add); all fields required — carry unchanged values through. **`Cost` is the same tax-inclusive line total** as Add (issue #15) — the tool tracks the diff in ex-VAT unit terms and converts on write |
| `PurchaseOrder/Delete_PurchaseOrderItem` | POST | `{"deleteItemParameter":{"pkPurchaseItemId","pkPurchaseId"}}` | Removes a line item; `pkPurchaseItemId` from `Get_PurchaseOrder` response |
| `RulesEngine/GetRules` | GET | — | Flat list of rule headers |
| `RulesEngine/GetRuleConditionNodes` | GET | `?pkRuleId=<int>` | Full IF/THEN tree for one rule |
| `ImportExport/GetImportList` | GET | — | List endpoint — reliable status |
| `ImportExport/GetExportList` | GET | — | Same shape as import list |
| `ImportExport/GetImport` | GET | `?id=<int>` | Config only — `ImportStatus` is null even for erroring imports |
| `ImportExport/GetExport` | GET | `?id=<int>` | Config only |
| `ImportExport/RunNowImport` | POST | `{"importId": <int>}` **unwrapped** | Returns **204 No Content** — use `call_linnworks_void`, not `call_linnworks`; puts import in queue immediately; not yet live-tested |
| `ImportExport/RunNowExport` | POST | `{"exportId": <int>}` **unwrapped** | Returns **204 No Content** — same pattern; not yet live-tested |
| `OpenOrders/SearchOrders` | POST | `{"LocationId":"...","SearchTerm":"...","IncludeProcessed":false}` **unwrapped** | Searches by ReferenceNum, ExternalReference, and related fields; response: `{"OpenOrders":[{"ViewId":n,"OrderIds":["guid",...]}],"ProcessedOrders":["guid",...]}` — open orders grouped in view objects (the SAME GUID can appear under multiple ViewIds — dedupe), processed orders as flat GUID list; **live-tested 15 Jun 2026: must be sent UNWRAPPED.** The `{"request":{...}}` wrapper returns HTTP 400 "Must provide a search term." even though the OpenAPI spec names the body parameter `request` identically to `GetOrdersLowFidelity` (which DOES require the wrapper). Same gotcha as `Search_PurchaseOrders2` |
| `Orders/CancelOrder` | POST | `{"orderId":"guid","fulfilmentCenter":"guid","note":"..."}` **unwrapped** | Cancels an open order; `fulfilmentCenter` = `FulfilmentLocationId` from order detail; optional `refund` (double) field for attached refund amount; returns a string |
| `ReturnsRefunds/GetRefundOptions` | POST | `{"request":{"OrderId":"guid"}}` | Returns `RefundOptions` including `CanRefund`, `CannotRefundReason` enum; use as pre-flight check before CreateRefund; **read confirmed live 15 Jun 2026** (wrapped payload correct; returns full options object with `CanRefund`/`CanRefundShipping`/etc.) |
| `ReturnsRefunds/GetRefundHeadersByOrderId` | POST | `{"request":{"OrderId":"guid"}}` | Returns all existing refund headers for an order — useful to audit whether an order has already been (partially) refunded before calling CreateRefund; **spec-based, not yet live-tested** |
| `ReturnsRefunds/CreateRefund` | POST | `{"request":{"OrderId":"guid","ChannelInitiated":false,"RefundLines":[{"OrderItemRowId":"guid","RefundedUnit":"Item","Amount":10.00,"FreeTextOrNote":"..."},{"RefundedUnit":"Shipping","Amount":5.00}]}}` | Creates a refund record; `OrderItemRowId` = `OrderItem.RowId` from `GetOrdersById`; omit `OrderItemRowId` for Shipping/Additional lines; `RefundedUnit` enum: `Item`, `Shipping`, `Service`, `Additional`; returns `{RefundHeaderId, RefundReference, Status, CannotRefundReason, Errors}`; **spec-based, not yet live-tested** |
| `ReturnsRefunds/ActionRefund` | POST | `{"request":{"RefundHeaderId":42,"OrderId":"guid"}}` | Pushes an approved refund to the sales channel (Shopify/Amazon/eBay); returns `{SuccessfullyActioned, Status, Errors}`; note: `SuccessfullyActioned` can be true while individual `Errors` still exist — check both; **spec-based, not yet live-tested** |
| `Orders/GetOrderNotes` | GET | `?orderId=<guid>` | Returns a plain JSON array of `OrderNote` objects; **canonical fields per OpenAPI spec (confirmed May 2026)**: `OrderNoteId`, `OrderId`, `NoteDate`, `Internal` (bool, no `Is` prefix), `Note`, `CreatedBy`, `NoteTypeId`; **GUID required** — numeric IDs must be resolved first; **previously documented field names `pkOrderNoteId`/`IsInternal`/`NoteCreatedOn` were wrong** (fixed in issue #7) |
| `Orders/AddOrdersNote` | POST | `{"OrderIds":["guid",...],"NoteText":"...","IsInternal":true,"IsProcessingNote":false}` **unwrapped** | Accepts a list of order GUIDs; works on both open and processed orders |
| `ProcessedOrders/DeleteOrderNote` | POST | `{"pkOrderNoteId":"guid"}` **unwrapped** | Deletes a single note by its GUID; works on both open and processed orders; **no UpdateOrderNote endpoint exists** — use delete+add via `update_order_note` instead |
| `Inventory/AddInventoryItem` | POST | `{"inventoryItem": {StockItemId, ItemNumber, ItemTitle, BarcodeNumber, RetailPrice, ...}}` **unwrapped** | Creates a new inventory item; **`StockItemId` is REQUIRED and must be a client-generated GUID** (`uuid.uuid4()`) — omitting it returns HTTP 400 "StockItem StockItemId could not be empty"; returns an empty 2xx body on success (the generated GUID is the new item ID); **live-tested 15 Jun 2026** |
| `Inventory/UpdateInventoryItem` | POST | `{"inventoryItem": {StockItemId, ItemNumber, ItemTitle, ...all fields...}}` **unwrapped** | Updates an existing item; must carry ALL fields (nulls clear values — same gotcha as PO header update); empty 2xx body on success; **live-tested 15 Jun 2026** |
| `Inventory/DeleteInventoryItems` | POST | `{"inventoryItemIds": ["guid",...]}` **unwrapped** | Permanently deletes items by StockItemId GUID; empty 2xx body on success; wrapped by the `delete_inventory_item` tool (v1.14.0); **live-tested 15 Jun 2026** — create → delete → read-back-gone cycle confirmed |
| `Inventory/GetInventoryItemPrices` | GET | `?inventoryItemId=<guid>` | Returns array of price rows `[{pkRowId, Source, SubSource, Price, Tag, ...}]`; used as read-before-write for `set_inventory_item_prices`; **read confirmed live 15 Jun 2026** (one row per Source/SubSource channel) |
| `Inventory/CreateInventoryItemPrices` | POST | `{"inventoryItemPrices": [{pkRowId, StockItemId, Source, SubSource, Price}]}` | Creates new channel price rows; **`pkRowId` must be a client-generated GUID** (omitting it → PK collision on zero-GUID); empty 2xx body; **only for genuine channel rows** — the default price (empty Source+SubSource) is the implicit zero-GUID row not returned by the GET, so set it via Update (pkRowId `00000000-...`) instead; **live-tested 15 Jun 2026** |
| `Inventory/UpdateInventoryItemPrices` | POST | `{"inventoryItemPrices": [{StockItemId, pkRowId, Source, SubSource, Price}]}` | Updates existing price rows by pkRowId; **spec-based, not yet live-tested** |
| `Inventory/GetInventoryItemDescriptions` | GET | `?inventoryItemId=<guid>` | Returns array of description rows `[{pkRowId, Source, SubSource, Description}]`; used as read-before-write for `set_inventory_item_descriptions`; **read confirmed live 15 Jun 2026** |
| `Inventory/CreateInventoryItemDescriptions` | POST | `{"inventoryItemDescriptions": [{pkRowId, StockItemId, Source, SubSource, Description}]}` | Creates new description rows; **`pkRowId` must be a client-generated GUID**; empty 2xx body; no implicit default row (unlike prices); **live-tested 15 Jun 2026** |
| `Inventory/UpdateInventoryItemDescriptions` | POST | `{"inventoryItemDescriptions": [{StockItemId, pkRowId, Source, SubSource, Description}]}` | Updates existing description rows by pkRowId; **spec-based, not yet live-tested** |
| `Inventory/CreateInventoryItemExtendedProperties` | POST | `{"inventoryItemExtendedProperties": [{pkRowId, fkStockItemId, SKU, ProperyName, PropertyValue, PropertyType}]}` | Creates new extended property rows; **`pkRowId` must be a client-generated GUID**; note deliberate API typo `ProperyName`; **returns a 2xx with a NON-empty, NON-JSON body on success** (not an empty body like the sibling write endpoints) — `call_linnworks` now falls back to `{"raw": text}` on `JSONDecodeError` so the write isn't mis-reported as failing (issue #13, 15 Jun 2026) |
| `Inventory/UpdateInventoryItemExtendedProperties` | POST | `{"inventoryItemExtendedProperties": [{fkStockItemId, pkRowId, ProperyName, PropertyValue, PropertyType}]}` | Updates existing property rows by pkRowId; note deliberate API typo; **same non-JSON 2xx success body as the Create endpoint** — handled by the tolerant parse in `call_linnworks`; **create + update both live-tested 15 Jun 2026** against an isolated test SKU (persistence confirmed: the update path read back the value the create wrote) — issue #13 |
| `Inventory/AddImageToInventoryItem` | POST | `{"request": {StockItemId, ImageUrl, IsMain}}` | Adds a single image by URL; no bulk equivalent — iterate per item; **spec-based, not yet live-tested** |
| `Stock/UpdateStockLevelsBulk` | POST | `{"Items": [{SKU, StockItemId, StockLocationId, StockLevel}]}` | Sets absolute stock levels; **returns an empty 2xx body — does NOT echo `Items`** on this tenant, so per-item success is inferred from the 2xx (verify with `get_stock_level`); **live-tested 15 Jun 2026** |
| `Stock/CreateVariationGroup` | POST | `{"template": {VariationGroupName, ParentSKU, ParentStockItemId, VariationItemIds: [guid]}}` | Creates a new variation group; all child GUIDs must resolve to existing items; **spec-based, not yet live-tested** |
| `Stock/GetVariationGroupByName` | GET | `?variationGroupName=<string>` | Returns the matching variation group, or **`null`** (HTTP 200, not a 404) if the name doesn't exist; used as pre-flight check in `create_variation_group` — the code guards with `if existing_group and existing_group.get("VariationGroupName")`, so null is handled; **read confirmed live 15 Jun 2026** |

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
| `Stock/GetStockItemsFull` | HTTP 400 — does not support keyword search; use `Stock/GetStockItems` (singular, GET) for keyword search instead |
| `Inventory/GetInventoryItems` | HTTP 400 — no working payload shape found; use `GetInventoryItem` (singular) for exact SKU lookup, or `Stock/GetStockItems` (GET) for keyword/title/barcode search |
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

- **`python server.py --list-tools`** — offline smoke test, **no credentials needed**. Lists every registered MCP tool and a total count. Confirms the module imports cleanly and your new tool actually registered — catches decorator typos, duplicate tool names, and import-time errors that `py_compile` alone misses. This is the primary CLI build-loop check (the credential gate is now lazy, so the module imports credential-free).
- **`python server.py --check-auth`** — verifies credentials and the auth handshake without touching the MCP layer. Requires real credentials, so it only works where a `.env` (or env block) is present.
- **Bootstrap CLI credentials from the Desktop config (local Mac)** — a CLI session doesn't inherit Claude Desktop's `env` block, so by default `--check-auth` and in-process live tests can't authenticate. But the creds already live on this machine in `~/Library/Application Support/Claude/claude_desktop_config.json` under `mcpServers.linnworks.env`. Generate a gitignored `.env` from them once and the whole local CLI build loop can self-test:
  ```bash
  python3 -c "import json,os; e=json.load(open(os.path.expanduser('~/Library/Application Support/Claude/claude_desktop_config.json')))['mcpServers']['linnworks']['env']; open('.env','w').write(''.join(f'{k}={e[k]}\n' for k in ('LINNWORKS_APPLICATION_ID','LINNWORKS_APPLICATION_SECRET','LINNWORKS_INSTALLATION_TOKEN')))"
  ```
  `.env` is gitignored (never commit it). This only works on the local Mac with Desktop installed — **genuinely-remote routine builds on claude.ai/code have no Desktop config and still can't authenticate**. Keep the `.env` in sync if the Desktop creds are rotated.
- **In-process live testing** — once `.env` exists, `import server` and call any tool function directly against the live tenant (the running Desktop MCP won't hot-reload edits, so iterate in-process here, then restart Desktop to pick up the fix). For write tools, exercise `dry_run=False` against an isolated `ZZZ-MCP-TEST-*` SKU you create and delete (via `Inventory/DeleteInventoryItems`) — never a real catalogue SKU. NB: live production writes still require user approval (the auto-mode classifier blocks them otherwise).
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

### Step 5 — Verify the tool registered (offline)
```bash
.venv/bin/python3 server.py --list-tools | grep <new_tool_name> && \
  .venv/bin/python3 server.py --list-tools | tail -1
```
This is the real CLI build-loop check — it needs no credentials. It confirms the
module imports cleanly **and** your new tool registered (catching decorator typos,
duplicate names, and import-time errors `py_compile` misses). The `grep` must
match and the total count should have gone up by the number of tools you added.

`--check-auth` and live testing need credentials. On the local Mac you can
bootstrap them: generate a gitignored `.env` from the Desktop config's
`mcpServers.linnworks.env` (see the **Testing** section for the one-liner), then
`--check-auth` passes and you can live-test tools in-process. On a genuinely
remote build (claude.ai/code, no Desktop config) creds are unavailable — fall
back to `--list-tools` only and defer live testing to Claude Desktop.

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
- The Linnworks MCP isn't connected as MCP tools in the CLI session, but on the local Mac you **can** authenticate: bootstrap a gitignored `.env` from the Desktop config (see **Testing**), then `--check-auth` passes and you can `import server` and call tool functions in-process against the live tenant — including `dry_run=False` on an isolated `ZZZ-MCP-TEST-*` SKU. Always run `python server.py --list-tools` (offline) as the baseline registration check. Live production writes still require user approval (the auto-mode classifier blocks them). On a genuinely remote build with no Desktop config, defer live testing to Claude Desktop.
- Do not ask the user for confirmation between steps — execute the full workflow and report back at the end.
- If an endpoint is unknown, check `https://apidocs.linnworks.net` or fetch the relevant spec file from `https://raw.githubusercontent.com/LinnSystems/PublicApiSpecs/master/1.0/<name>.json` before guessing. Key spec files: `orders.json`, `openorders.json`, `returnsrefunds.json`, `purchaseorder.json`, `inventory.json`, `stock.json`, `processedorders.json`, `importexport.json`, `rulesengine.json`.

---

## Out of scope for Phase 1

- OAuth, Dynamic Client Registration — Phase 2
- Hosting, deployment, public URLs — Phase 2
- Multi-tenancy, per-user token storage — Phase 2

Flag any request that pushes toward these and discuss before proceeding.
