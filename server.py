"""
Linnworks MCP Server — Phase 1 (local stdio)

A single-tenant MCP server that exposes Linnworks data to Claude Desktop.
Phase 1 = stdio transport, your machine only, no OAuth, no hosting.

Run via Claude Desktop after registering this script in claude_desktop_config.json.
See README.md for setup instructions.
"""
from __future__ import annotations

import os
import sys
from typing import Optional

import re

import requests
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

# Load .env file if present (only matters when running outside Claude Desktop —
# Claude Desktop passes credentials via its config's env block instead).
load_dotenv()

# ---------- Configuration ----------

AUTH_URL = "https://api.linnworks.net/api/Auth/AuthorizeByApplication"

# The "Default" location in Linnworks — represents combined stock for most setups.
# Override per-call via the get_open_orders(location_id=...) parameter if needed.
DEFAULT_LOCATION_ID = "00000000-0000-0000-0000-000000000000"

APPLICATION_ID = os.environ.get("LINNWORKS_APPLICATION_ID")
APPLICATION_SECRET = os.environ.get("LINNWORKS_APPLICATION_SECRET")
INSTALLATION_TOKEN = os.environ.get("LINNWORKS_INSTALLATION_TOKEN")

if not all([APPLICATION_ID, APPLICATION_SECRET, INSTALLATION_TOKEN]):
    sys.stderr.write(
        "ERROR: Missing Linnworks credentials. Set LINNWORKS_APPLICATION_ID, "
        "LINNWORKS_APPLICATION_SECRET, and LINNWORKS_INSTALLATION_TOKEN — either in "
        "your Claude Desktop config's 'env' block, or in a .env file alongside "
        "this script for local testing.\n"
    )
    sys.exit(1)


# Matches GUID-style pkOrderID values returned by Linnworks order endpoints.
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


# ---------- Auth state (cached for the lifetime of this process) ----------

_session: requests.Session = requests.Session()
_token: Optional[str] = None
_server: Optional[str] = None


def authorize() -> tuple[str, str]:
    """
    Authenticate with Linnworks. Returns (token, server_url).

    The metaphor from the skill: hand over App ID + Secret + Installation Token
    to the front desk (api.linnworks.net), get back a session Token (boarding
    pass) and a Server (terminal). Every later call goes to that returned Server.
    """
    response = _session.post(
        AUTH_URL,
        data={
            "ApplicationId": APPLICATION_ID,
            "ApplicationSecret": APPLICATION_SECRET,
            "Token": INSTALLATION_TOKEN,
        },
        timeout=30,
    )
    response.raise_for_status()
    auth = response.json()
    token = auth.get("Token")
    server = auth.get("Server")
    if not token or not server:
        raise RuntimeError(
            f"Auth response missing Token/Server. Response keys: {list(auth.keys())}"
        )
    return token, server


def ensure_auth() -> tuple[str, str]:
    """Lazy auth: authenticate on first use, cache for the process lifetime."""
    global _token, _server
    if _token is None or _server is None:
        _token, _server = authorize()
    return _token, _server


def call_linnworks(method_path: str, payload: dict) -> dict:
    """
    POST a Linnworks API call with one automatic re-auth on token expiry.

    method_path: e.g. "OpenOrders/GetOrdersLowFidelity"
    payload:     the JSON body, already wrapped if the endpoint requires it
                 (Open Orders endpoints need {"request": {...}})
    """
    global _token, _server
    token, server = ensure_auth()
    url = f"{server.rstrip('/')}/api/{method_path}"

    response = _session.post(
        url,
        json=payload,
        headers={"Authorization": token},  # NB: NO 'Bearer ' prefix
        timeout=60,
    )

    # Token expired? Reauth once and retry.
    if response.status_code == 401:
        _token, _server = None, None
        token, server = ensure_auth()
        url = f"{server.rstrip('/')}/api/{method_path}"
        response = _session.post(
            url,
            json=payload,
            headers={"Authorization": token},
            timeout=60,
        )

    if not response.ok:
        # Surface the Linnworks error verbatim — the body usually contains the
        # real reason (missing 'request' wrapper, invalid LocationId, etc.)
        raise RuntimeError(
            f"Linnworks {method_path} failed: HTTP {response.status_code} — {response.text}"
        )

    return response.json()


def call_linnworks_get(method_path: str, params: dict | None = None) -> any:
    """
    GET a Linnworks API endpoint with one automatic re-auth on token expiry.

    method_path: e.g. "Orders/GetOrderDetailsByNumOrderId"
    params:      query-string parameters dict, e.g. {"orderId": "596475"}
    """
    global _token, _server
    token, server = ensure_auth()
    url = f"{server.rstrip('/')}/api/{method_path}"

    response = _session.get(
        url,
        params=params,
        headers={"Authorization": token},
        timeout=60,
    )

    if response.status_code == 401:
        _token, _server = None, None
        token, server = ensure_auth()
        url = f"{server.rstrip('/')}/api/{method_path}"
        response = _session.get(
            url,
            params=params,
            headers={"Authorization": token},
            timeout=60,
        )

    if not response.ok:
        raise RuntimeError(
            f"Linnworks {method_path} failed: HTTP {response.status_code} — {response.text}"
        )

    return response.json()


def _format_order_detail(raw: dict) -> dict:
    """Normalise a single Linnworks order detail record into a consistent shape."""
    general = raw.get("GeneralInfo") or {}
    shipping = raw.get("ShippingInfo") or {}
    items = raw.get("Items") or []
    return {
        "order_id": raw.get("OrderId"),
        "num_order_id": raw.get("NumOrderId"),
        "processed": raw.get("Processed"),
        "received_date": general.get("ReceivedDate"),
        "status": general.get("Status"),
        "is_parked": general.get("IsParked"),
        "marker": general.get("Marker"),
        "reference_num": general.get("ReferenceNum"),
        "source": general.get("Source"),
        "sub_source": general.get("SubSource"),
        "postal_service_name": shipping.get("PostalServiceName"),
        "tracking_number": shipping.get("TrackingNumber"),
        "items": [
            {
                "StockItemId": i.get("StockItemId"),
                "SKU": i.get("SKU"),
                "Title": i.get("Title"),
                "Quantity": i.get("Quantity"),
            }
            for i in items
        ],
    }


# ---------- MCP server ----------

mcp = FastMCP("linnworks")


@mcp.tool()
def get_open_orders(
    location_id: str = DEFAULT_LOCATION_ID,
    limit: int = 50,
) -> dict:
    """
    List currently open (unprocessed) orders from Linnworks.

    Returns a summary of each order including its IDs, status, dispatch deadline,
    and the SKUs it contains. Useful for answering questions like "how many open
    orders do I have right now", "which orders are overdue", or "what SKUs are
    in today's orders".

    Args:
        location_id: The Linnworks location to query. Defaults to the "Default"
            location, which represents combined stock for most setups.
        limit: Maximum number of orders to return in detail. Defaults to 50.
            The total open-order count is always returned regardless of limit.

    Returns:
        A dict with:
          - count:        total number of open orders at the location
          - returned:     how many are detailed in the `orders` list
          - location_id:  the location queried
          - orders:       list of order summaries
    """
    # Open Orders endpoints REQUIRE the {"request": {...}} wrapper —
    # sending the inner object directly returns "'request' parameter is missing."
    payload = {"request": {"LocationId": location_id}}
    response = call_linnworks("OpenOrders/GetOrdersLowFidelity", payload)

    raw_orders = response.get("Orders") or []

    summarized = []
    for o in raw_orders[:limit]:
        items = o.get("Items") or []
        summarized.append({
            "pkOrderID": o.get("pkOrderID"),
            "OrderId": o.get("OrderId"),
            "ReferenceNum": o.get("ReferenceNum"),
            "ExternalReference": o.get("ExternalReference"),
            "Status": o.get("Status"),
            "PostalTrackingNumber": o.get("PostalTrackingNumber"),
            "OrderDate": o.get("OrderDate"),
            "DispatchBy": o.get("DispatchBy"),
            "ItemCount": len(items),
            "SKUs": [i.get("SKU") for i in items],
        })

    return {
        "count": len(raw_orders),
        "returned": len(summarized),
        "location_id": location_id,
        "orders": summarized,
    }


@mcp.tool()
def find_inventory_item(
    sku_or_title: str,
    limit: int = 10,
) -> dict:
    """
    Search for inventory items in Linnworks by SKU or title keyword.

    Returns matching items with their stock item ID, SKU, title, category,
    and barcode. Use this to look up the internal StockItemId for a product
    before calling stock-level or write tools that require a GUID.

    Args:
        sku_or_title: A SKU (exact or partial) or a keyword from the item title.
            Linnworks searches across both fields.
        limit: Maximum number of matching items to return. Defaults to 10.

    Returns:
        A dict with:
          - query:    the search string used
          - count:    number of results returned
          - items:    list of matching inventory item summaries
    """
    # Inventory/GetInventoryItem: unwrapped, accepts {"sku": "..."} for exact SKU lookup.
    # Confirmed working in tenant testing — does not support fuzzy/title search.
    try:
        item = call_linnworks("Inventory/GetInventoryItem", {"sku": sku_or_title})
    except RuntimeError:
        return {"query": sku_or_title, "count": 0, "items": [], "note": "No item found for that SKU."}

    return {
        "query": sku_or_title,
        "count": 1,
        "items": [
            {
                "StockItemId": item.get("StockItemId"),
                "SKU": item.get("ItemNumber"),
                "Title": item.get("ItemTitle"),
                "Barcode": item.get("BarcodeNumber"),
                "IsCompositeParent": item.get("IsCompositeParent", False),
                "RetailPrice": item.get("RetailPrice"),
                "PurchasePrice": item.get("PurchasePrice"),
            }
        ],
    }


@mcp.tool()
def get_order(order_id: str) -> dict:
    """
    Fetch full detail for a single Linnworks order.

    Accepts either a GUID pkOrderID (as returned by get_open_orders) or a
    numeric order number (the human-facing NumOrderId). Automatically routes
    to the correct endpoint based on the input format.

    Returns the order's status, received date, postal service, parked flag,
    marker, source channel, and all item lines with SKUs and quantities.
    Useful for answering questions like "show me order 596475 in full" or
    "what items are in this order and what's the shipping method?".

    Args:
        order_id: Either a GUID pkOrderID (e.g. "a1b2c3d4-1234-...") or a
            numeric order number (e.g. "596475"). Both formats are accepted.

    Returns:
        A dict with order detail including status, shipping, and item lines.
    """
    order_id = order_id.strip()

    if _UUID_RE.match(order_id):
        # GUID path: Orders/GetOrdersById (POST, unwrapped — confirmed in tenant learnings)
        response = call_linnworks("Orders/GetOrdersById", {"pkOrderIds": [order_id]})
        # Response is a list of detail orders or may be wrapped
        if isinstance(response, list):
            orders = response
        else:
            orders = response.get("Orders") or response.get("Data") or []
        if not orders:
            return {"error": f"No order found for GUID '{order_id}'"}
        return _format_order_detail(orders[0])

    else:
        # Numeric path: Orders/GetOrderDetailsByNumOrderId (GET with query param)
        response = call_linnworks_get(
            "Orders/GetOrderDetailsByNumOrderId",
            params={"orderId": order_id},
        )
        if isinstance(response, dict):
            if "GeneralInfo" in response or "NumOrderId" in response:
                return _format_order_detail(response)
            orders = response.get("Orders") or []
            if orders:
                return _format_order_detail(orders[0])
        return {"error": f"No order found for numeric ID '{order_id}'", "raw": response}


@mcp.tool()
def get_stock_level(
    sku: str,
    location_id: str = DEFAULT_LOCATION_ID,
) -> dict:
    """
    Return the current stock level for a SKU, optionally at a specific location.

    Performs a two-step lookup: resolves the SKU to a StockItemId, then fetches
    stock levels across all locations. If location_id is the default "all
    locations" GUID, all location rows are returned. Otherwise only the matching
    location row is returned.

    Useful for questions like "how much stock do we have of SKU ABC-123?" or
    "what's the available quantity at our main warehouse?".

    Note: returns StockLevel (gross on-hand), Available (net of order-book
    allocations), InOrderBook, and Due. For FIFO purposes use StockLevel,
    not Available — see tenant learnings.

    Args:
        sku: The exact SKU / item number to look up (case-insensitive).
        location_id: The Linnworks StockLocationId to filter to. Defaults to
            the "Default" location (all-locations aggregate). Pass a specific
            location GUID to see a single warehouse row.

    Returns:
        A dict with the item identity and a list of stock level rows per location.
    """
    # Step 1: resolve SKU → StockItemId via Inventory/GetInventoryItem (exact SKU, unwrapped)
    try:
        item = call_linnworks("Inventory/GetInventoryItem", {"sku": sku})
    except RuntimeError:
        return {"error": f"No inventory item found for SKU '{sku}'", "sku": sku}

    stock_item_id = item.get("StockItemId")
    if not stock_item_id:
        return {"error": f"Item found for SKU '{sku}' but StockItemId was missing", "sku": sku}

    # Step 2: fetch stock levels via batch endpoint (confirmed working in tenant learnings)
    levels_response = call_linnworks(
        "Stock/GetStockLevel_Batch",
        {"request": {"StockItemIds": [stock_item_id]}},
    )

    # Response is a list: [{pkStockItemId, StockItemLevels: [...]}, ...]
    batch = levels_response if isinstance(levels_response, list) else []
    item_row = next((r for r in batch if r.get("pkStockItemId") == stock_item_id), None)
    levels = item_row.get("StockItemLevels") or [] if item_row else []

    # Filter to requested location unless the caller wants the default aggregate
    if location_id != DEFAULT_LOCATION_ID:
        levels = [
            l for l in levels
            if (l.get("Location") or {}).get("StockLocationId") == location_id
        ]

    return {
        "sku": sku,
        "stock_item_id": stock_item_id,
        "title": item.get("ItemTitle"),
        "location_id": location_id,
        "levels": [
            {
                "location_name": (l.get("Location") or {}).get("LocationName"),
                "location_id": (l.get("Location") or {}).get("StockLocationId"),
                "stock_level": l.get("StockLevel"),
                "available": l.get("Available"),
                "in_order_book": l.get("InOrderBook"),
                "due": l.get("Due"),
            }
            for l in levels
        ],
    }


@mcp.tool()
def get_processed_orders(
    from_date: str,
    to_date: str,
    date_field: str = "received",
    page: int = 1,
    page_size: int = 50,
) -> dict:
    """
    List processed (dispatched/fulfilled) orders from Linnworks within a date range.

    Processed orders are orders that have been marked as dispatched — they no longer
    appear in the open orders queue. Use this to answer questions like "what orders
    did we ship last week?", "how many orders were processed yesterday?", or
    "how many orders came in during May?".

    Args:
        from_date: Start of the date range in ISO format, e.g. "2026-05-01".
            Interpreted as midnight UTC on that date.
        to_date: End of the date range in ISO format, e.g. "2026-05-08".
            Interpreted as 23:59:59 UTC on that date.
        date_field: Which date to filter on. One of: "received" (default),
            "processed", "payment", "cancelled". Use "processed" for dispatch date,
            "received" for order received date.
        page: Page number for paginated results. Defaults to 1.
        page_size: Number of orders per page. Min 20, defaults to 50.

    Returns:
        A dict with:
          - from_date, to_date:  the date range queried
          - date_field:          which date field was filtered
          - page, total_pages:   pagination info
          - total_count:         total matching orders across all pages
          - count:               number of orders returned on this page
          - orders:              list of processed order summaries
    """
    page_size = max(20, page_size)

    payload = {
        "request": {
            "DateField": date_field,
            "FromDate": f"{from_date}T00:00:00",
            "ToDate": f"{to_date}T23:59:59",
            "PageNumber": page,
            "ResultsPerPage": page_size,
        }
    }

    response = call_linnworks("ProcessedOrders/SearchProcessedOrders", payload)

    # Response: {"ProcessedOrders": {"PageNumber", "EntriesPerPage", "TotalEntries", "TotalPages", "Data": [...]}}
    wrapper = response.get("ProcessedOrders") or {}
    raw_orders = wrapper.get("Data") or []
    total_count = wrapper.get("TotalEntries", 0)
    total_pages = wrapper.get("TotalPages", 1)

    orders = [
        {
            "order_id": o.get("pkOrderID"),
            "num_order_id": o.get("nOrderId"),
            "reference_num": o.get("ReferenceNum"),
            "external_reference": o.get("ExternalReference"),
            "source": o.get("Source"),
            "sub_source": o.get("SubSource"),
            "received_date": o.get("dReceivedDate"),
            "processed_date": o.get("dProcessedOn"),
            "total_charge": o.get("fTotalCharge"),
            "currency": o.get("cCurrency"),
            "postal_service_name": o.get("PostalServiceName"),
            "tracking_number": o.get("PostalTrackingNumber"),
            "country": o.get("cCountry"),
            "fulfilment_location": o.get("FulfilmentLocationName"),
        }
        for o in raw_orders
    ]

    return {
        "from_date": from_date,
        "to_date": to_date,
        "date_field": date_field,
        "page": page,
        "total_pages": total_pages,
        "total_count": total_count,
        "count": len(orders),
        "orders": orders,
    }


# ---------- Locations, extended properties, inventory item detail ----------

@mcp.tool()
def get_locations() -> dict:
    """
    List all stock locations configured in Linnworks.

    Returns every warehouse, fulfilment centre, and virtual location with its
    GUID, name, and type flags. Useful for resolving location names to IDs
    before calling tools that accept a location_id parameter, and for answering
    questions like "which locations do we have?" or "what is the GUID for our
    main warehouse?".

    Returns:
        A dict with:
          - count:     total number of locations
          - locations: list of location records
    """
    locations = call_linnworks_get("Inventory/GetStockLocations")

    return {
        "count": len(locations),
        "locations": [
            {
                "location_id": loc.get("StockLocationId"),
                "name": loc.get("LocationName"),
                "is_fulfillment_center": loc.get("IsFulfillmentCenter"),
                "is_warehouse_managed": loc.get("IsWarehouseManaged"),
                "is_not_trackable": loc.get("IsNotTrackable"),
                "city": loc.get("City"),
                "country": loc.get("Country"),
            }
            for loc in (locations if isinstance(locations, list) else [])
        ],
    }


@mcp.tool()
def get_extended_properties(sku: str) -> dict:
    """
    Fetch the extended properties (custom metadata) for an inventory item by SKU.

    Extended properties are key-value pairs attached to a product in Linnworks,
    used to store custom attributes like supplier codes, dimensions, materials,
    or any other product-level metadata. Useful for questions like "what are the
    extended properties for SKU ABC-123?" or "which products have a supplier code
    of XYZ?".

    Args:
        sku: The exact SKU / item number to look up.

    Returns:
        A dict with the item identity and a list of extended property records,
        each with a name, value, and type.
    """
    try:
        item = call_linnworks("Inventory/GetInventoryItem", {"sku": sku})
    except RuntimeError:
        return {"error": f"No inventory item found for SKU '{sku}'", "sku": sku}

    stock_item_id = item.get("StockItemId")
    if not stock_item_id:
        return {"error": f"Item found for SKU '{sku}' but StockItemId was missing", "sku": sku}

    props = call_linnworks(
        "Inventory/GetInventoryItemExtendedProperties",
        {"inventoryItemId": stock_item_id},  # unwrapped — confirmed working
    )

    return {
        "sku": sku,
        "stock_item_id": stock_item_id,
        "title": item.get("ItemTitle"),
        "count": len(props) if isinstance(props, list) else 0,
        "extended_properties": [
            {
                "name": p.get("ProperyName"),   # NB: Linnworks API typo — 'ProperyName'
                "value": p.get("PropertyValue"),
                "type": p.get("PropertyType"),
            }
            for p in (props if isinstance(props, list) else [])
        ],
    }


# ---------- Processed orders with line items ----------

def _batch_order_items(order_guids: list[str]) -> dict[str, list]:
    """
    Fetch line items for a list of order GUIDs via Orders/GetOrdersById.
    Returns a dict mapping each OrderId GUID to its list of item dicts.
    Batches in groups of 50 to avoid oversized requests.
    """
    result: dict[str, list] = {}
    batch_size = 50
    for i in range(0, len(order_guids), batch_size):
        batch = order_guids[i : i + batch_size]
        detail_orders = call_linnworks("Orders/GetOrdersById", {"pkOrderIds": batch})
        if not isinstance(detail_orders, list):
            detail_orders = detail_orders.get("Orders") or []
        for order in detail_orders:
            oid = order.get("OrderId")
            items = order.get("Items") or []
            result[oid] = [
                {
                    "sku": i.get("SKU"),
                    "title": i.get("Title"),
                    "quantity": i.get("Quantity"),
                    "price_per_unit": i.get("PricePerUnit"),
                    "line_total_ex_tax": i.get("Cost"),
                    "line_total_inc_tax": i.get("CostIncTax"),
                    "tax": i.get("Tax"),
                    "tax_rate": i.get("TaxRate"),
                    "category": i.get("CategoryName"),
                    "stock_item_id": i.get("StockItemId"),
                    "bin_rack": i.get("BinRack"),
                    "channel_sku": i.get("ChannelSKU"),
                }
                for i in items
            ]
    return result


@mcp.tool()
def get_processed_order_items(
    from_date: str,
    to_date: str,
    date_field: str = "received",
    page: int = 1,
    page_size: int = 50,
) -> dict:
    """
    List processed orders with full line-item detail within a date range.

    This is the primary tool for sales analysis: it returns each processed order
    along with every SKU, quantity, and price in that order. Use it to answer
    questions like "what were our top-selling SKUs last week?", "which items are
    commonly bought together?", "what was our revenue by product category?", or
    "how many units of SKU ABC-123 did we sell in May?".

    Internally fetches order summaries from ProcessedOrders/SearchProcessedOrders,
    then enriches with line items from Orders/GetOrdersById in batches.

    Args:
        from_date: Start of the date range in ISO format, e.g. "2026-05-01".
        to_date: End of the date range in ISO format, e.g. "2026-05-08".
        date_field: Which date to filter on — "received" (default), "processed",
            "payment", or "cancelled".
        page: Page number for paginated results. Defaults to 1.
        page_size: Orders per page. Min 20, defaults to 50. Use pagination for
            large date ranges — each page triggers a batch detail fetch.

    Returns:
        A dict with:
          - from_date, to_date, date_field: the query parameters used
          - page, total_pages, total_count:  pagination info
          - count:                           orders on this page
          - orders:                          list of orders, each with an `items` list
    """
    page_size = max(20, page_size)

    # Step 1: get order summaries + GUIDs
    summary_response = call_linnworks(
        "ProcessedOrders/SearchProcessedOrders",
        {
            "request": {
                "DateField": date_field,
                "FromDate": f"{from_date}T00:00:00",
                "ToDate": f"{to_date}T23:59:59",
                "PageNumber": page,
                "ResultsPerPage": page_size,
            }
        },
    )
    wrapper = summary_response.get("ProcessedOrders") or {}
    raw_orders = wrapper.get("Data") or []
    total_count = wrapper.get("TotalEntries", 0)
    total_pages = wrapper.get("TotalPages", 1)

    if not raw_orders:
        return {
            "from_date": from_date, "to_date": to_date, "date_field": date_field,
            "page": page, "total_pages": total_pages, "total_count": total_count,
            "count": 0, "orders": [],
        }

    # Step 2: batch-fetch line items for this page's orders
    guids = [o["pkOrderID"] for o in raw_orders]
    items_by_order = _batch_order_items(guids)

    orders = []
    for o in raw_orders:
        guid = o.get("pkOrderID")
        orders.append({
            "order_id": guid,
            "num_order_id": o.get("nOrderId"),
            "reference_num": o.get("ReferenceNum"),
            "external_reference": o.get("ExternalReference"),
            "source": o.get("Source"),
            "sub_source": o.get("SubSource"),
            "received_date": o.get("dReceivedDate"),
            "processed_date": o.get("dProcessedOn"),
            "total_charge": o.get("fTotalCharge"),
            "currency": o.get("cCurrency"),
            "postal_service_name": o.get("PostalServiceName"),
            "tracking_number": o.get("PostalTrackingNumber"),
            "country": o.get("cCountry"),
            "fulfilment_location": o.get("FulfilmentLocationName"),
            "items": items_by_order.get(guid, []),
        })

    return {
        "from_date": from_date,
        "to_date": to_date,
        "date_field": date_field,
        "page": page,
        "total_pages": total_pages,
        "total_count": total_count,
        "count": len(orders),
        "orders": orders,
    }


# ---------- Entrypoint ----------

def main() -> None:
    # Sanity-check credentials without launching the MCP server:
    #     python server.py --check-auth
    if "--check-auth" in sys.argv:
        try:
            token, server = authorize()
            masked = f"{token[:8]}...{token[-4:]}" if len(token) > 12 else "<short>"
            print(f"Auth OK")
            print(f"  Server: {server}")
            print(f"  Token:  {masked}")
            sys.exit(0)
        except Exception as e:
            print(f"Auth failed: {e}", file=sys.stderr)
            sys.exit(1)

    # Normal path: run the MCP server over stdio for Claude Desktop to consume.
    mcp.run()


if __name__ == "__main__":
    main()
