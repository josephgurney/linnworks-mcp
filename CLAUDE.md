# Linnworks MCP Server — Claude context

A local stdio MCP server that exposes Linnworks data to Claude Desktop. This is **Phase 1** of a two-phase plan:

- **Phase 1 (current)**: Single-tenant stdio server, runs on your machine, no OAuth, no hosting. Used to iterate on tool design with a fast feedback loop.
- **Phase 2 (later)**: Same tool functions wrapped as a remote MCP server with OAuth 2.1 + Dynamic Client Registration, deployed to a public host, registered as a custom connector in claude.ai.

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

26 tools, all live-tested. See `server.py` for full docstrings and parameter details.

### Orders & stock (read)

| Tool | Endpoint | Key notes |
|---|---|---|
| `get_open_orders(location_id, limit, overdue_only)` | `OpenOrders/GetOrdersLowFidelity` | Always returns `overdue_count`; `overdue_only=True` filters past-deadline; `StatusLabel` decoded |
| `find_inventory_item(sku_or_title)` | `Inventory/GetInventoryItem` | **Exact SKU only** — no fuzzy/title search |
| `get_order(order_id)` | `Orders/GetOrdersById` or `GetOrderDetailsByNumOrderId` | GUID → POST; numeric ID → GET |
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

### Purchase orders

| Tool | Notes |
|---|---|
| `search_purchase_orders(status, from_date, to_date, ...)` | `PurchaseOrder/Search_PurchaseOrders2`; payload **unwrapped** — wrapping silently ignores all filters |
| `get_purchase_order(purchase_id)` | Returns header + items (with `outstanding` = qty − delivered) + delivery records; deleted items filtered |
| `get_suppliers()` | `Inventory/GetSuppliers` GET — not in public OpenAPI specs but confirmed working |
| `create_purchase_order(..., dry_run=True)` | `DateOfPurchase` always required (SQL rejects null); `SupplierReferenceNumber` always required (send `""` if none) |
| `update_purchase_order_header(..., dry_run=True)` | Carry all existing header fields — missing fields are cleared; blocks on DELIVERED status |
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

---

## Confirmed working endpoints

| Endpoint | Method | Payload shape | Key notes |
|---|---|---|---|
| `Auth/AuthorizeByApplication` | POST | `{"ApplicationId","ApplicationSecret","Token"}` JSON body | Returns session `Token` + `Server` URL |
| `OpenOrders/GetOrdersLowFidelity` | POST | `{"request":{"LocationId":"..."}}` | Primary open-orders list |
| `OpenOrders/GetOpenOrdersDetails` | POST | `{"OrderIds":["pkOrderID-guid",...]}` **unwrapped** | Use GUID `pkOrderID`, not numeric |
| `Orders/GetOrdersById` | POST | `{"pkOrderIds":["guid",...]}` **unwrapped** | Bulk order detail |
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
| `PurchaseOrder/Update_PurchaseOrderHeader` | POST | `{"updateParameter":{...all fields...}}` | Must carry all existing fields — nulls clear values; blocks on DELIVERED |
| `RulesEngine/GetRules` | GET | — | Flat list of rule headers |
| `RulesEngine/GetRuleConditionNodes` | GET | `?pkRuleId=<int>` | Full IF/THEN tree for one rule |
| `ImportExport/GetImportList` | GET | — | List endpoint — reliable status |
| `ImportExport/GetExportList` | GET | — | Same shape as import list |
| `ImportExport/GetImport` | GET | `?id=<int>` | Config only — `ImportStatus` is null even for erroring imports |
| `ImportExport/GetExport` | GET | `?id=<int>` | Config only |

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

`get_top_skus` · `get_sales_by_supplier` · `get_category_report` · `get_revenue_summary` · `get_period_comparison`

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

## Out of scope for Phase 1

- OAuth, Dynamic Client Registration — Phase 2
- Hosting, deployment, public URLs — Phase 2
- Multi-tenancy, per-user token storage — Phase 2

Flag any request that pushes toward these and discuss before proceeding.
