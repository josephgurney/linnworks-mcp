# Linnworks MCP Server

A local [MCP](https://modelcontextprotocol.io/) server that connects Claude Desktop to your Linnworks account. Ask Claude natural-language questions about your orders, stock, and inventory — it calls the Linnworks API on your behalf.

This is a **single-tenant stdio server**: it runs on your machine, connects to your Linnworks account using your own API credentials, and is not hosted anywhere. Each person who installs it uses their own credentials.

---

## What you can ask Claude

Once installed, Claude gets access to these tools:

**Orders & stock**

| Tool | What it does |
|---|---|
| `get_open_orders` | List current open (unprocessed) orders — count, SKUs, dispatch deadlines, overdue flag |
| `get_processed_orders` | Search dispatched orders by date range — volume, sources, tracking |
| `get_processed_order_items` | Processed orders **with full line items** — top-selling SKUs, sold-together analysis, revenue by product |
| `get_order` | Full detail on a single order by numeric ID or GUID |
| `get_stock_level` | Current stock level for a SKU across all locations |
| `find_inventory_item` | Look up an inventory item by exact SKU |
| `get_extended_properties` | Fetch custom metadata (extended properties) for a product |
| `get_locations` | List all warehouse and fulfilment locations with their GUIDs |

**Reporting**

| Tool | What it does |
|---|---|
| `get_revenue_summary` | Total orders, revenue, and AOV for a date range — broken down by channel and country |
| `get_top_skus` | Top-selling SKUs by revenue or units for a date range |
| `get_category_report` | Revenue and units by product category for a date range |
| `get_period_comparison` | Side-by-side revenue comparison between two date ranges (MoM, YoY, etc.) |

**Purchase orders**

| Tool | What it does |
|---|---|
| `search_purchase_orders` | Search POs by status, date range, or keyword |
| `get_purchase_order` | Full detail for a single PO — header, line items, delivery records |
| `get_suppliers` | List all suppliers with their GUIDs |
| `create_purchase_order` | Create a new PO and add line items (dry-run by default) |
| `update_purchase_order_header` | Edit PO header fields — supplier, reference, dates, currency (dry-run by default) |
| `open_purchase_order` | Move a PO from PENDING → OPEN status (dry-run by default) |
| `deliver_purchase_order` | Record delivery of all outstanding items on an OPEN PO (dry-run by default) |
| `add_purchase_order_note` | Add a text note to a PO (e.g. tracking number, expected arrival) |

**Rules Engine**

| Tool | What it does |
|---|---|
| `get_rules` | List all Rules Engine rules — name, type, enabled state, run order, draft status |
| `get_rule` | Full IF/THEN condition tree for a single rule — every condition clause and action with nested subrules |

**Import / Export monitoring**

| Tool | What it does |
|---|---|
| `get_import_list` | List all configured import tasks — name, type, enabled state, last run, status, next schedule |
| `get_export_list` | List all configured export tasks — same fields plus last export success/fail |
| `get_import` | Full detail for one import — feed URL, column mappings, schedule config |
| `get_export` | Full detail for one export — destination, filters, schedule config |

Example questions you can ask:

> How many open orders do I have right now, and which are overdue?
> What were our top 10 selling SKUs last week?
> Which products are most commonly bought together?
> What's the stock level for SKU ABC-123?
> What extended properties does product XYZ have?
> Which of our locations hold inventory?
> How does this month's revenue compare to last month?
> Which imports are currently in error?
> When did the stock level import last run, and what's its feed URL?
> Show me all purchase orders from this supplier that are currently open.
> Which rules are currently enabled, and in what order do they run?
> What does the "Nathan Shipping Rules" rule actually do — show me the full conditions and actions.

---

## Prerequisites

- **Python 3.10+**
- **Claude Desktop** — [download here](https://claude.ai/download)
- **Linnworks API credentials** — Application ID, Application Secret, and an Installation Token (see step 2 below)

---

## Setup

### 1. Clone and install

```bash
git clone https://github.com/YOUR_USERNAME/linnworks-mcp.git
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
Which products were bought together most often in May?
What's the stock level for SKU ABC-123?
What extended properties does SKU XYZ have?
```

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
- Write tools (`create_purchase_order`, `update_purchase_order_header`, `open_purchase_order`, `deliver_purchase_order`) all default to `dry_run=True` and will not modify data unless you explicitly confirm.

---

## Project structure

```
linnworks-mcp/
├── server.py          # MCP server — all tools defined here
├── requirements.txt   # mcp, requests, python-dotenv
├── .env.example       # Credential template (copy to .env, never commit .env)
├── .gitignore
└── README.md
```
