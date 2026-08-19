# Linnworks MCP Server

![Version](https://img.shields.io/badge/version-1.46.0-blue)
![Tools](https://img.shields.io/badge/tools-84-blue)

A local [MCP](https://modelcontextprotocol.io/) server that connects Claude Desktop to your Linnworks account. Ask Claude natural-language questions about your orders, stock, and inventory — it calls the Linnworks API on your behalf.

This is a **single-tenant stdio server**: it runs on your machine, connects to your Linnworks account using your own API credentials, and is not hosted anywhere. Each person who installs it uses their own credentials.

---

## What you can ask Claude

Once installed, Claude gets access to these tools:

**Orders (read)**

| Tool | What it does |
|---|---|
| `get_open_orders` | List current open (unprocessed) orders — count, SKUs, dispatch deadlines, overdue flag |
| `get_processed_orders` | Search dispatched orders by date range |
| `get_processed_order_items` | Processed orders with full line items — sold-together analysis, revenue by product |
| `get_order` | Full detail on a single order by numeric ID or GUID — customer name, email, address, notes, totals |
| `get_order_notes` | Fetch all notes on an order |
| `find_open_orders_for_sku` | Find all open orders containing a specific SKU — customer name, email, dispatch deadline |
| `find_orders_by_reference` | Look up orders by channel reference number (Shopify, Amazon, eBay) |

**Orders (write — all default to dry_run=True)**

| Tool | What it does |
|---|---|
| `set_order_address` | Update the delivery address on an open order. Pass just the fields you want to change, or the whole address — add `require_complete=True` when the destination itself is changing, so a half-supplied address is refused rather than merged with the old one |
| `add_order_note` | Add a note to any order (internal or customer-facing) |
| `update_order_note` | Replace the text of an existing note |
| `delete_order_note` | Remove a specific note by ID |
| `delete_order_notes_by_text` | Remove notes matching a text pattern |
| `cancel_order` | Cancel an open (unprocessed) order |
| `set_order_status` | Lock/unlock an order (hold it from dispatch) or mark it paid/unpaid — bulk. Note: locking releases the order's allocated stock, and park/unpark has no public API |
| `refund_order` | Full refund on a processed order |
| `refund_order_lines` | Partial refund of specific line items |

**Inventory (read)**

| Tool | What it does |
|---|---|
| `find_inventory_item` | Look up an inventory item by exact SKU |
| `search_inventory_items` | Keyword search across title, SKU, and barcode — paged (the UI search box) |
| `list_inventory_items` | Enumerate ALL active items with per-location stock — the bulk sweep behind "what's out of stock everywhere?". Filter to `zero_stock_only`, scope to one `location_id`, or exclude bundle/variation parents. Autopaginating with `all_pages=True` |
| `get_stock_level` | Current stock level for a SKU across all locations |
| `get_stock_change_history` | Stock movement history + "when did this go out of stock?". Derives `out_of_stock_since` (the transition to zero, not the newest zero row), `days_out_of_stock`, `last_sale_date`, `last_received_date`. Per-location; `all_locations=True` adds `zero_at_all_locations_since`. `change_source` is derived from the note — Linnworks returns no such field |
| `get_item_relationships` | Resolve a SKU's variation/composite parent–child links (parent → children, and child → variation parent) |
| `find_composite_parents` | The reverse lookup: which bundles/composites CONTAIN these SKUs, and are those parents still listed. The safety gate before archiving or deleting a component. Batch — pass every candidate SKU in one call |
| `get_extended_properties` | Fetch custom metadata (extended properties) for a product |
| `get_inventory_item_titles` | Channel-specific listing titles for a SKU (per Source/SubSource) |
| `get_inventory_item_descriptions` | Channel-specific descriptions for a SKU |
| `get_inventory_item_suppliers` | Which suppliers an item can be bought from — code, cost, lead time, and which is default |
| `get_inventory_item_images` | Images on an item — count, main image, URLs |
| `get_inventory_item_images_bulk` | Image check across many SKUs at once — also accepts `stock_item_ids` to skip resolution |
| `get_locations` | List all warehouse and fulfilment locations with their GUIDs |

**Inventory (write — all default to dry_run=True)**

| Tool | What it does |
|---|---|
| `create_or_update_inventory_item` | Create a new item or update an existing one by SKU — title, barcode, prices, category, dimensions |
| `set_stock_levels` | Set absolute stock levels for one or more SKUs |
| `set_inventory_item_prices` | Set or update channel prices per SKU — supports Source/SubSource per channel |
| `set_inventory_item_titles` | Set or update channel-specific listing titles (override the base title per channel) |
| `set_inventory_item_descriptions` | Create or update channel-specific descriptions on items |
| `set_extended_properties` | Create or update extended property key/value pairs on items |
| `set_inventory_item_suppliers` | Attach or update an item's supplier links — supplier code, cost, lead time, default flag |
| `add_inventory_item_images` | Attach images to items by URL |
| `delete_inventory_item_images` | Remove images from an item by image ID — irreversible, staged |
| `set_inventory_item_image_order` | Reorder an item's images and set the main/hero image (Linnworks always pins the main image first) |
| `create_variation_group` | Create a variation group — note the parent SKU must be brand new, not an existing item |
| `add_variation_group_items` | Add child SKUs to an existing variation group (idempotent) |
| `archive_inventory_items` | Archive items by SKU — hides them from the active catalogue, reversible |
| `unarchive_inventory_items` | Restore archived items — takes StockItemId GUIDs, since archived SKUs can't be resolved by SKU |
| `delete_inventory_item` | Permanently delete items by SKU — irreversible, staged |

**Categories (writes default to dry_run=True)**

| Tool | What it does |
|---|---|
| `get_categories` | List all inventory categories; `with_counts=True` also tallies items per category and flags empty ones |
| `create_category` | Create a new category (duplicate-name guarded) |
| `rename_category` | Rename a category (Default protected) |
| `delete_categories` | Delete specific categories by id — refuses non-empty ones unless `force=True` |
| `delete_empty_categories` | Find and delete every category with no items in it |

**Channels & listings (writes default to dry_run=True)**

| Tool | What it does |
|---|---|
| `get_channel_listings` | Check whether a SKU is listed, and on which channel/store |
| `get_channel_listings_bulk` | The same listing check across many SKUs at once. Pass `stock_item_ids` instead of SKUs for large batches — it skips per-SKU resolution entirely (5,391 items: 15.6s vs 187s). Rate-limited lookups are reported separately from genuinely-missing ones |
| `list_to_shopify` | List existing inventory to Shopify via a saved configurator. Two dedupe layers: the same item already listed, **and** a different SKU with the same title already live (the SKU-migration case that created 177 duplicate products) — the latter is excluded unless `allow_duplicate_titles=True` |
| `refresh_channel_listing` | Re-push edited item data to a live Shopify listing (revise); pre-flight staleness check on the template's stored snapshot |
| `unpublish_channel_listing` | Take down / end a live listing on one channel and store — Shopify, Amazon, TikTok, Magento or Walmart. Each template is verified individually after the delete, so a template that survived is never reported as taken down. A variation child is retired via its parent's template only when no other member of the group would lose a listing; otherwise it is blocked with the parent and its live siblings named |
| `repair_channel_listing_images` | Push an item's CURRENT Linnworks images onto its EXISTING Shopify listing — attach what's missing, make the Linnworks main image the featured image, and detach media the item no longer has. Talks to the Shopify Admin API directly, because the GLT cannot do this (it re-pushes the template's stored, sometimes deleted, image URL and silently no-ops). Images are matched by the Linnworks GUID that Shopify preserves in the CDN filename, so it compares pictures rather than counts. Hand-uploaded media is never removed, and on a variation group (one Shopify product, per-variant Linnworks images) a sibling's photo is never mistaken for a stale one. Needs Shopify Admin credentials |
| `delist_all_channel_listings` | Take down every listing for an item across all channels and stores at once. eBay, Etsy and Mirakl are reported as skipped and left up — they can only be ended in their own admin. Every SKU that can't be retired carries a `blocked_reason`, so a small take-down count never reads as a completed cleanup |
| `delist_all_shopify_listings` | The Shopify-only slice of the above, for when you deliberately want just Shopify |

**Reporting**

| Tool | What it does |
|---|---|
| `get_revenue_summary` | Total orders, revenue, and AOV for a date range — broken down by channel and country |
| `get_top_skus` | Top-selling SKUs by revenue or units for a date range — optional supplier filter |
| `get_category_report` | Revenue and units by product category for a date range |
| `get_component_sales` | Explode composite/bundle sales to component (child) level — surfaces hidden component demand (units) |
| `get_period_comparison` | Side-by-side revenue comparison between two date ranges (MoM, YoY, etc.) |
| `get_sales_by_supplier` | Revenue, units, and order count aggregated by supplier for a date range |

**Purchase orders**

| Tool | What it does |
|---|---|
| `search_purchase_orders` | Search POs by status, date range, or keyword |
| `get_purchase_order` | Full detail for a single PO — header, line items, delivery records |
| `get_suppliers` | List all suppliers with their GUIDs |
| `create_purchase_order` | Create a new PO and add line items |
| `update_purchase_order_header` | Edit PO header fields — supplier, reference, dates, currency |
| `add_purchase_order_item` | Add a line item to an existing PO |
| `update_purchase_order_item` | Edit quantity, cost, or tax rate on a PO line |
| `remove_purchase_order_item` | Delete a line item from a PO |
| `open_purchase_order` | Move a PO from PENDING → OPEN status |
| `deliver_purchase_order` | Record delivery of all outstanding items on an OPEN PO |
| `add_purchase_order_note` | Add a text note to a PO (e.g. tracking number, expected arrival) |
| `delete_purchase_order` | Delete whole POs — header, lines and notes. Irreversible, staged |

**Rules Engine**

| Tool | What it does |
|---|---|
| `get_rules` | List all Rules Engine rules — name, type, enabled state, run order, draft status |
| `get_rule` | Full IF/THEN condition tree for a single rule — every condition clause and action with nested subrules |

**Import / Export**

| Tool | What it does |
|---|---|
| `get_import_list` | List all configured import tasks — name, type, enabled state, last run, status, next schedule |
| `get_export_list` | List all configured export tasks — same fields plus last export success/fail |
| `get_import` | Full detail for one import — feed URL, column mappings, schedule config |
| `get_export` | Full detail for one export — destination, filters, schedule config |
| `run_import` | Queue an import for immediate execution |
| `run_export` | Queue an export for immediate execution |

Example questions you can ask:

> How many open orders do I have right now, and which are overdue?
> What were our top 10 selling SKUs last week?
> Which products are most commonly bought together?
> What's the stock level for SKU ABC-123?
> What extended properties does product XYZ have?
> Which of our locations hold inventory?
> How does this month's revenue compare to last month?
> Which suppliers drove the most revenue last month?
> Which customers have open orders waiting on SKU ABC-123?
> Show me order 596475 — what's the customer's email address?
> Which imports are currently in error?
> Set the stock level for SKU VEN-DECK-80-SKU to 15 at the Default location.
> Create a new inventory item with SKU NEW-001 and title "Test Board 8.0".
> Update the retail price for SKU ABC-123 to £29.99.
> Which inventory categories are empty, and can you clean them up?
> Is SKU ABC-123 already listed on Shopify, and on which store?

---

## Prerequisites

- **Python 3.10+**
- **Claude Desktop** — [download here](https://claude.ai/download)
- **Linnworks API credentials** — Application ID, Application Secret, and an Installation Token (see step 2 below)

---

## Setup

### 1. Clone and install

```bash
git clone https://github.com/josephgurney/linnworks-mcp.git
cd linnworks-mcp
python -m venv .venv
source .venv/bin/activate        # macOS / Linux
# .venv\Scripts\activate          # Windows PowerShell
pip install -r requirements.txt
```

### 2. Get your Linnworks credentials

You need three values:

1. **Application ID + Application Secret** — create an app at [developers.linnworks.net](https://developers.linnworks.net/). Note both values.
2. **Installation Token** (Permanent Token):
   - Visit `https://apps.linnworks.net/Authorization/Authorize/{YOUR_APPLICATION_ID}` while logged into Linnworks
   - The Permanent Token is displayed on that page — copy it
   - If no token appears, you need to configure a Postback URL in your app settings first, then reinstall

### 3. Create your `.env` file

```bash
cp .env.example .env
# Open .env and paste your three credential values
```

### 4. Verify auth works

Before touching Claude Desktop, confirm the credentials are correct:

```bash
python server.py --check-auth
```

Expected output:
```
Auth OK
  Server: https://eu-ext.linnworks.net
  Token:  abc12345...wxyz
```

If this fails, fix the credentials before going further.

### 5. Register with Claude Desktop

Open your Claude Desktop config file:
- **macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows**: `%APPDATA%\Claude\claude_desktop_config.json`

Add a `linnworks` entry under `mcpServers`. Use **absolute paths** — `~/` shortcuts don't work here:

```json
{
  "mcpServers": {
    "linnworks": {
      "command": "/absolute/path/to/linnworks-mcp/.venv/bin/python",
      "args": ["/absolute/path/to/linnworks-mcp/server.py"],
      "env": {
        "LINNWORKS_APPLICATION_ID": "your-application-id",
        "LINNWORKS_APPLICATION_SECRET": "your-application-secret",
        "LINNWORKS_INSTALLATION_TOKEN": "your-installation-token"
      }
    }
  }
}
```

> **Why credentials in the config rather than `.env`?** Claude Desktop launches the script in its own environment and doesn't pick up `.env` files reliably. The `env` block is the supported way to pass secrets. The `.env` file is only used when running `--check-auth` from your terminal.

> **Windows paths**: use double backslashes (`C:\\Users\\...`) or forward slashes in the JSON.

> **Optional — Shopify Admin credentials.** One tool, `repair_channel_listing_images`, writes corrected images onto live Shopify listings; the Linnworks GLT cannot do that (it re-pushes the template's stored, sometimes deleted, image URL and silently no-ops). It needs an Admin API access token that Linnworks does not provide — add `SHOPIFY_SHOP_DOMAIN`, `SHOPIFY_ADMIN_ACCESS_TOKEN` and `SHOPIFY_DEFAULT_SUB_SOURCE` to the same `env` block (or `SHOPIFY_STORES` as JSON for several stores), with Admin scopes `read_products`, `write_products`, `read_files`, `write_files`. See `.env.example`. Leave them unset and that one tool returns setup instructions rather than writing; every other tool is unaffected.

Restart Claude Desktop. Open a new chat — you should see `linnworks` listed in the tools panel.

### 6. Try it

```
How many open orders do I have right now?
Which open orders are overdue?
What were my top 10 selling SKUs last week?
What's the stock level for SKU ABC-123?
```

---

## Slow tools — ask for one at a time

A few read tools page through the whole catalogue or a whole date range internally, making hundreds of API calls per question:

`get_top_skus` · `get_sales_by_supplier` · `get_category_report` · `get_revenue_summary` · `get_period_comparison` · `get_component_sales` · `find_composite_parents` (first call — then cached 15 min) · `list_inventory_items(all_pages=True)` · `get_categories(with_counts=True)` · `delete_empty_categories`

Expect roughly 1–2 minutes each. **Ask for one at a time** — two of these running together hit the Linnworks rate limit and the second will time out.

---

## Write tools and safety

All write tools default to `dry_run=True` — they will describe what they would do without making any changes. Set `dry_run=False` only after reviewing the output.

**Large batch protection:** inventory write tools have per-operation staging thresholds. If you ask Claude to update more items than the threshold in one go, the tool will return a manifest preview and ask you to confirm before executing. This prevents accidental large-scale changes.

| Tool | Threshold |
|---|---|
| `set_stock_levels` | 25 items |
| `set_inventory_item_prices` | 25 items |
| `create_or_update_inventory_item` | 50 items |
| `set_extended_properties` | 50 items |
| `set_inventory_item_descriptions` | 50 items |
| `set_inventory_item_titles` | 50 items |
| `set_inventory_item_suppliers` | 50 items |
| `add_inventory_item_images` | 100 items |
| `set_inventory_item_image_order` | 25 items |
| `set_order_status` | 25 orders |
| `archive_inventory_items` | 25 items |
| `unarchive_inventory_items` | 25 items |
| `list_to_shopify` | 25 listings |
| `refresh_channel_listing` | 25 listings |
| `unpublish_channel_listing` | 10 listings |
| `repair_channel_listing_images` | 10 listings |
| `delist_all_channel_listings` | 10 listings |
| `delist_all_shopify_listings` | 10 listings |
| `delete_inventory_item_images` | 10 images |
| `delete_inventory_item` | 10 items |
| `delete_purchase_order` | 10 POs |
| `delete_categories` | 10 categories |
| `delete_empty_categories` | 10 categories |

There is no hard cap — any batch size works once confirmed. The threshold is a staging gate, not a refusal. The tightest thresholds (10) are on the irreversible/destructive tools — item and category deletes, and taking channel listings down.

---

## Troubleshooting

**`Could not determine inventory item id from SKU`** — SKU matching is exact and case-sensitive. Confirm the SKU in the Linnworks UI.

**`Search parameters are empty`** from processed order tools — `from_date` and `to_date` are required. The minimum page size is 20.

**No tools visible in Claude Desktop** — check the MCP log:
- macOS: `~/Library/Logs/Claude/mcp.log`
- Windows: `%APPDATA%\Claude\logs\mcp.log`

The most common causes are wrong absolute paths in the config, or Python below 3.10.

**Auth fails after previously working** — session tokens expire; the server re-auths automatically on 401. If `--check-auth` itself fails, your Installation Token may have been revoked — reinstall the app in Linnworks to get a fresh one.

---

## Security

- Credentials go directly from your machine to Linnworks. Nothing is hosted or proxied.
- `.env` is gitignored. Never commit it.
- All write tools default to `dry_run=True` and will not modify data unless you explicitly confirm.
- Large batches are staged before execution — the tool shows you a manifest and waits for your `confirmed_count` before writing.

---

## Project structure

```
linnworks-mcp/
├── server.py          # MCP server — all tools defined here
├── requirements.txt   # mcp, requests, python-dotenv
├── .env.example       # Credential template (copy to .env, never commit .env)
├── .gitignore
├── README.md
└── tests/             # Automated tests (pytest)
```
