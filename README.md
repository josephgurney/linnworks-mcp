# Linnworks MCP Server

![Version](https://img.shields.io/badge/version-1.11.0-blue)

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
| `find_open_orders_for_sku` | Find all open orders containing a specific SKU — customer name, email, dispatch deadline |
| `find_orders_by_reference` | Look up orders by channel reference number (Shopify, Amazon, eBay) |
| `get_order_notes` | Fetch all notes on an order |

**Orders (write — all default to dry_run=True)**

| Tool | What it does |
|---|---|
| `set_order_address` | Update the delivery address on an open order |
| `add_order_note` | Add a note to any order (internal or customer-facing) |
| `update_order_note` | Replace the text of an existing note |
| `delete_order_note` | Remove a specific note by ID |
| `delete_order_notes_by_text` | Remove notes matching a text pattern |
| `cancel_order` | Cancel an open (unprocessed) order |
| `refund_order` | Full refund on a processed order |
| `refund_order_lines` | Partial refund of specific line items |

**Inventory (read)**

| Tool | What it does |
|---|---|
| `find_inventory_item` | Look up an inventory item by exact SKU |
| `get_stock_level` | Current stock level for a SKU across all locations |
| `get_extended_properties` | Fetch custom metadata (extended properties) for a product |
| `get_locations` | List all warehouse and fulfilment locations with their GUIDs |

**Inventory (write — all default to dry_run=True)**

| Tool | What it does |
|---|---|
| `create_or_update_inventory_item` | Create a new item or update an existing one by SKU — title, barcode, prices, category, dimensions |
| `set_stock_levels` | Set absolute stock levels for one or more SKUs (threshold: 25 items before staging) |
| `set_inventory_item_prices` | Set or update channel prices per SKU — supports Source/SubSource per channel |
| `set_extended_properties` | Create or update extended property key/value pairs on items |
| `set_inventory_item_descriptions` | Create or update channel-specific descriptions on items |
| `add_inventory_item_images` | Attach images to items by URL |
| `create_variation_group` | Link a parent item to its variant children (e.g. sizes/colours) |

**Reporting**

| Tool | What it does |
|---|---|
| `get_revenue_summary` | Total orders, revenue, and AOV for a date range — broken down by channel and country |
| `get_top_skus` | Top-selling SKUs by revenue or units for a date range — optional supplier filter |
| `get_category_report` | Revenue and units by product category for a date range |
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

Restart Claude Desktop. Open a new chat — you should see `linnworks` listed in the tools panel.

### 6. Try it

```
How many open orders do I have right now?
Which open orders are overdue?
What were my top 10 selling SKUs last week?
What's the stock level for SKU ABC-123?
```

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
| `add_inventory_item_images` | 100 items |

There is no hard cap — any batch size works once confirmed. The threshold is a staging gate, not a refusal.

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
