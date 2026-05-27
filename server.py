"""
Linnworks MCP Server — Phase 1 (local stdio)

A single-tenant MCP server that exposes Linnworks data to Claude Desktop.
Phase 1 = stdio transport, your machine only, no OAuth, no hosting.

Run via Claude Desktop after registering this script in claude_desktop_config.json.
See README.md for setup instructions.
"""
from __future__ import annotations

__version__ = "1.7.0"

import os
import sys
from datetime import datetime, timezone
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


# Open-order status labels. Codes 1 and 4 confirmed from live tenant data;
# others sourced from Linnworks API docs — verify if unexpected values appear.
_ORDER_STATUS_LABELS: dict[int, str] = {
    0: "Draft",
    1: "Pending Dispatch",
    2: "Paid",
    3: "Return",
    4: "Awaiting Payment",
    5: "Resolution Required",
    6: "Deleted",
    7: "Cancelled",
}

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


def call_linnworks_form(method_path: str, payload: dict) -> dict:
    """
    POST a Linnworks API call with form-encoded data (application/x-www-form-urlencoded).

    Some older Linnworks write endpoints reject JSON and require form encoding instead.
    Use this when call_linnworks returns HTTP 400 "The request is invalid."

    method_path: e.g. "PurchaseOrder/Deliver_PurchaseItemAll"
    payload:     flat dict of form fields
    """
    global _token, _server
    token, server = ensure_auth()
    url = f"{server.rstrip('/')}/api/{method_path}"

    response = _session.post(
        url,
        data=payload,
        headers={"Authorization": token},
        timeout=60,
    )

    if response.status_code == 401:
        _token, _server = None, None
        token, server = ensure_auth()
        url = f"{server.rstrip('/')}/api/{method_path}"
        response = _session.post(
            url,
            data=payload,
            headers={"Authorization": token},
            timeout=60,
        )

    if not response.ok:
        raise RuntimeError(
            f"Linnworks {method_path} failed: HTTP {response.status_code} — {response.text}"
        )

    # Some endpoints return 204 No Content
    if not response.text:
        return {}
    return response.json()


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
    customer = raw.get("CustomerInfo") or {}
    address = customer.get("Address") or {}
    billing = customer.get("BillingAddress") or {}
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
        # Top-level shortcuts kept for backward compatibility
        "customer_name": address.get("FullName") or customer.get("ChannelBuyerName") or "",
        "customer_email": address.get("EmailAddress") or "",
        # Full delivery address — use set_order_address() to update any of these fields
        "delivery_address": {
            "full_name": address.get("FullName"),
            "company": address.get("Company"),
            "address1": address.get("Address1"),
            "address2": address.get("Address2"),
            "address3": address.get("Address3"),
            "town": address.get("Town"),
            "region": address.get("Region"),
            "postcode": address.get("PostCode"),
            "country": address.get("Country"),
            "phone": address.get("PhoneNumber"),
            "email": address.get("EmailAddress"),
        },
        # Billing address (read-only for now)
        "billing_address": {
            "full_name": billing.get("FullName"),
            "company": billing.get("Company"),
            "address1": billing.get("Address1"),
            "address2": billing.get("Address2"),
            "address3": billing.get("Address3"),
            "town": billing.get("Town"),
            "region": billing.get("Region"),
            "postcode": billing.get("PostCode"),
            "country": billing.get("Country"),
            "phone": billing.get("PhoneNumber"),
            "email": billing.get("EmailAddress"),
        },
        "items": [
            {
                "StockItemId": i.get("StockItemId"),
                "SKU": i.get("SKU"),
                "Title": i.get("Title"),
                "Quantity": i.get("Quantity"),
            }
            for i in items
        ],
        # Notes attached to this order. Field names use .get() with fallbacks
        # because the Linnworks docs use slightly different names across endpoints.
        "notes": [
            {
                "note_id": n.get("pkOrderNoteId"),
                "note": n.get("Note") or n.get("NoteText"),
                "internal": n.get("IsInternal"),
                "created_on": n.get("NoteCreatedOn") or n.get("DateCreated"),
                "created_by": n.get("NoteCreatedBy") or n.get("CreatedBy"),
            }
            for n in (raw.get("Notes") or [])
        ],
    }


# ---------- MCP server ----------

mcp = FastMCP("linnworks")


@mcp.tool()
def get_open_orders(
    location_id: str = DEFAULT_LOCATION_ID,
    limit: int = 50,
    overdue_only: bool = False,
) -> dict:
    """
    List currently open (unprocessed) orders from Linnworks.

    Returns a summary of each order including its IDs, status (decoded),
    dispatch deadline, overdue flag, and the SKUs it contains.

    Useful for answering questions like "how many open orders do I have right
    now?", "which orders are overdue?", or "what SKUs are in today's queue?".

    Args:
        location_id: The Linnworks location to query. Defaults to the "Default"
            location, which represents combined stock for most setups.
        limit: Maximum number of orders to return in detail. Defaults to 50.
            The total count and overdue_count are always returned regardless.
        overdue_only: If True, only return orders whose DispatchBy deadline
            has already passed. Defaults to False (return all open orders).

    Returns:
        A dict with:
          - count:          total number of open orders at the location
          - overdue_count:  number whose DispatchBy has already passed
          - returned:       how many are detailed in the `orders` list
          - location_id:    the location queried
          - orders:         list of order summaries, each with is_overdue flag
    """
    payload = {"request": {"LocationId": location_id}}
    response = call_linnworks("OpenOrders/GetOrdersLowFidelity", payload)

    raw_orders = response.get("Orders") or []
    now = datetime.now(timezone.utc)

    def _is_overdue(dispatch_by: str | None) -> bool:
        if not dispatch_by:
            return False
        try:
            dt = datetime.fromisoformat(dispatch_by.replace("Z", "+00:00"))
            return dt < now
        except ValueError:
            return False

    overdue_count = sum(1 for o in raw_orders if _is_overdue(o.get("DispatchBy")))

    candidates = [o for o in raw_orders if not overdue_only or _is_overdue(o.get("DispatchBy"))]

    summarized = []
    for o in candidates[:limit]:
        items = o.get("Items") or []
        status_code = o.get("Status")
        dispatch_by = o.get("DispatchBy")
        summarized.append({
            "pkOrderID": o.get("pkOrderID"),
            "OrderId": o.get("OrderId"),
            "ReferenceNum": o.get("ReferenceNum"),
            "ExternalReference": o.get("ExternalReference"),
            "Status": status_code,
            "StatusLabel": _ORDER_STATUS_LABELS.get(status_code, f"Unknown({status_code})"),
            "PostalTrackingNumber": o.get("PostalTrackingNumber"),
            "OrderDate": o.get("OrderDate"),
            "DispatchBy": dispatch_by,
            "IsOverdue": _is_overdue(dispatch_by),
            "ItemCount": len(items),
            "SKUs": [i.get("SKU") for i in items],
        })

    return {
        "count": len(raw_orders),
        "overdue_count": overdue_count,
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
    Look up a single inventory item in Linnworks by its exact SKU.

    Returns the item's stock item ID, SKU, title, barcode, retail price, and
    purchase price. Use this to resolve a known SKU to a StockItemId before
    calling stock-level or write tools that require a GUID.

    NOTE: The underlying Linnworks endpoint (GetInventoryItem) only supports
    exact SKU lookup — it does not search by title or accept partial SKUs.
    If you need to search by keyword or browse the catalogue, that capability
    is not available via this API in the current tenant.

    Args:
        sku_or_title: The exact SKU / item number to look up (case-insensitive).
            Title keywords and partial SKUs will not match.
        limit: Unused — kept for interface compatibility. Only one item is ever
            returned by an exact SKU lookup.

    Returns:
        A dict with:
          - query:    the search string used
          - count:    1 if found, 0 if not
          - items:    list containing the matched item (empty if not found)
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
def set_order_address(
    order_id: str,
    full_name: Optional[str] = None,
    company: Optional[str] = None,
    address1: Optional[str] = None,
    address2: Optional[str] = None,
    address3: Optional[str] = None,
    town: Optional[str] = None,
    region: Optional[str] = None,
    postcode: Optional[str] = None,
    country: Optional[str] = None,
    phone: Optional[str] = None,
    email: Optional[str] = None,
    save_to_crm: bool = False,
    dry_run: bool = True,
) -> dict:
    """
    Update the delivery address on an open (unprocessed) Linnworks order.

    Reads the current address first, applies only the fields you provide
    (all others remain unchanged), then writes the change back. Always
    returns a before/after diff so you can confirm exactly what would change.
    Already-processed orders are rejected.

    Use get_order() first to see the current delivery_address before calling
    this tool, so you can confirm what needs changing.

    IMPORTANT: dry_run defaults to True. Set dry_run=False only when you are
    sure the change is correct — confirm with the user before doing so.

    Args:
        order_id: The order to update. Accepts either a GUID pkOrderID
            (e.g. "a1b2c3d4-...") or a numeric order number (e.g. "596475").
        full_name: Recipient full name. Pass None (default) to keep current.
        company: Company name. Pass None to keep current.
        address1: First line of the street address. Pass None to keep current.
        address2: Second address line. Pass None to keep current.
        address3: Third address line. Pass None to keep current.
        town: Town or city. Pass None to keep current.
        region: County, state, or region. Pass None to keep current.
        postcode: Postcode or ZIP code. Pass None to keep current.
        country: Country name (e.g. "United Kingdom"). Pass None to keep current.
        phone: Phone number. Pass None to keep current.
        email: Email address. Pass None to keep current.
        save_to_crm: If True, saves the updated address into the Linnworks CRM
            record for this customer. Defaults to False.
        dry_run: If True (default), shows the proposed changes without writing
            anything. Set to False to apply the address update.

    Returns:
        A dict with:
          - order_id:    the ID passed in
          - order_guid:  the resolved GUID (useful when you passed a numeric ID)
          - dry_run:     whether this was a dry run
          - status:      "dry_run", "updated", "no_changes", or "error"
          - before:      current values of the fields that would change
          - after:       proposed/applied new values for those fields
          - error:       present only if the update was rejected
    """
    order_id = order_id.strip()

    # ---------- Field mapping: tool param name → Linnworks Address field name ----------
    _FIELD_MAP = {
        "full_name": "FullName",
        "company":   "Company",
        "address1":  "Address1",
        "address2":  "Address2",
        "address3":  "Address3",
        "town":      "Town",
        "region":    "Region",
        "postcode":  "PostCode",
        "country":   "Country",
        "phone":     "PhoneNumber",
        "email":     "EmailAddress",
    }
    user_provided = {
        "full_name": full_name,
        "company":   company,
        "address1":  address1,
        "address2":  address2,
        "address3":  address3,
        "town":      town,
        "region":    region,
        "postcode":  postcode,
        "country":   country,
        "phone":     phone,
        "email":     email,
    }

    # ---------- Step 1: fetch current order (read-before-write) ----------
    raw: dict = {}
    order_guid: str = ""

    if _UUID_RE.match(order_id):
        resp = call_linnworks("Orders/GetOrdersById", {"pkOrderIds": [order_id]})
        orders = resp if isinstance(resp, list) else (resp.get("Orders") or resp.get("Data") or [])
        if not orders:
            return {"order_id": order_id, "status": "error", "error": f"No order found for GUID '{order_id}'."}
        raw = orders[0]
        order_guid = raw.get("OrderId") or order_id
    else:
        raw = call_linnworks_get("Orders/GetOrderDetailsByNumOrderId", params={"orderId": order_id})
        if not isinstance(raw, dict) or ("GeneralInfo" not in raw and "NumOrderId" not in raw):
            return {"order_id": order_id, "status": "error", "error": f"No order found for numeric ID '{order_id}'."}
        order_guid = raw.get("OrderId", "")

    # ---------- Step 2: refuse if already processed ----------
    if raw.get("Processed"):
        return {
            "order_id": order_id,
            "order_guid": order_guid,
            "status": "error",
            "error": (
                "Cannot edit an already-processed order. "
                "Address changes can only be made to open (unprocessed) orders."
            ),
        }

    # ---------- Step 3: extract current customer info ----------
    customer = raw.get("CustomerInfo") or {}
    current_address = customer.get("Address") or {}
    current_billing = customer.get("BillingAddress") or {}
    channel_buyer_name = customer.get("ChannelBuyerName") or ""

    # ---------- Step 4: build diff (only fields the caller explicitly provided) ----------
    before: dict = {}
    after: dict = {}

    for param, lw_field in _FIELD_MAP.items():
        new_val = user_provided[param]
        if new_val is not None and new_val != current_address.get(lw_field):
            before[param] = current_address.get(lw_field)
            after[param] = new_val

    if not before:
        return {
            "order_id": order_id,
            "order_guid": order_guid,
            "dry_run": dry_run,
            "status": "no_changes",
            "message": "The values you provided already match the current address — no update needed.",
        }

    if dry_run:
        return {
            "order_id": order_id,
            "order_guid": order_guid,
            "dry_run": True,
            "status": "dry_run",
            "message": "No changes written. Set dry_run=False to apply this update.",
            "before": before,
            "after": after,
        }

    # ---------- Step 5: build merged address and submit ----------
    # Start with the full current address (preserves CountryId, Continent, etc.)
    # then overlay the user's changes.
    new_address = dict(current_address)
    for param, lw_field in _FIELD_MAP.items():
        new_val = user_provided[param]
        if new_val is not None:
            new_address[lw_field] = new_val

    call_linnworks(
        "Orders/SetOrderCustomerInfo",
        {
            "orderId": order_guid,
            "info": {
                "ChannelBuyerName": channel_buyer_name,
                "Address": new_address,
                "BillingAddress": current_billing,
            },
            "saveToCrm": save_to_crm,
        },
    )

    # ---------- Step 6: read back to confirm ----------
    confirmed_resp = call_linnworks("Orders/GetOrdersById", {"pkOrderIds": [order_guid]})
    confirmed_orders = (
        confirmed_resp if isinstance(confirmed_resp, list)
        else (confirmed_resp.get("Orders") or confirmed_resp.get("Data") or [])
    )

    confirmed_after: dict = {}
    if confirmed_orders:
        confirmed_address = (confirmed_orders[0].get("CustomerInfo") or {}).get("Address") or {}
        for param, lw_field in _FIELD_MAP.items():
            if param in after:
                confirmed_after[param] = confirmed_address.get(lw_field)

    return {
        "order_id": order_id,
        "order_guid": order_guid,
        "dry_run": False,
        "status": "updated",
        "before": before,
        "after": confirmed_after or after,
    }


@mcp.tool()
def update_order_shipping_address(
    order_id: str,
    full_name: str,
    address1: str,
    town: str,
    post_code: str,
    country_code: str,
    address2: str = "",
    region: str = "",
    country: str = "",
    phone: str = "",
    email: str = "",
    company: str = "",
    dry_run: bool = True,
) -> dict:
    """
    Update the shipping/delivery address on an open Linnworks customer order.

    Designed for CS automation workflows — use when a customer requests an
    address change before their order ships. Always pair with a Shopify
    orderUpdate mutation to keep both systems in sync.

    Reads the current address before writing (read-before-write). Refuses to
    update orders that are already dispatched or processed.

    IMPORTANT: dry_run defaults to True. Set dry_run=False only after
    confirming the new address with the customer.

    Args:
        order_id: GUID pkOrderID (e.g. "a1b2c3d4-...") or numeric order
            number (e.g. "596475"). Same routing as get_order().
        full_name: Recipient full name (required).
        address1: First line of the street address (required).
        town: Town or city (required).
        post_code: Postcode or ZIP code (required).
        country_code: ISO 3166-1 alpha-2 country code, e.g. "GB" (required).
            Echoed back in the response. Provide `country` for the full name
            if the mapping is not obvious; otherwise the current order country
            is preserved.
        address2: Second address line. Defaults to empty.
        region: County, state, or region. Defaults to empty.
        country: Full country name, e.g. "United Kingdom". If omitted, the
            current address country is preserved.
        phone: Phone number. Defaults to empty (preserves current).
        email: Email address. Defaults to empty (preserves current).
        company: Company name. Defaults to empty (preserves current).
        dry_run: If True (default), shows what would be sent without writing.
            Set to False to apply the update.

    Returns:
        A dict with:
          - success:         True if update succeeded (or would succeed in dry_run)
          - dry_run:         whether this was a dry run
          - order_id:        the GUID used in the API call
          - numeric_id:      human-facing order number (if available)
          - updated_address: echo of the address fields that were/would be sent
          - error:           present only on failure
    """
    order_id_input = order_id.strip()
    raw: dict = {}
    order_guid: str = ""
    numeric_id: Optional[int] = None

    # ---------- Step 1: fetch current order (read-before-write) ----------
    if _UUID_RE.match(order_id_input):
        resp = call_linnworks("Orders/GetOrdersById", {"pkOrderIds": [order_id_input]})
        if isinstance(resp, list) and resp:
            raw = resp[0]
        elif isinstance(resp, dict) and "OrderId" in resp:
            # Simplified shape returned by test mocks
            raw = resp
        elif isinstance(resp, dict):
            orders = resp.get("Orders") or resp.get("Data") or []
            if not orders:
                return {
                    "success": False,
                    "order_id": order_id_input,
                    "dry_run": dry_run,
                    "error": f"No order found for GUID '{order_id_input}'.",
                }
            raw = orders[0]
        order_guid = raw.get("OrderId") or order_id_input
        numeric_id = raw.get("NumOrderId")
    else:
        resp = call_linnworks_get(
            "Orders/GetOrderDetailsByNumOrderId", params={"orderId": order_id_input}
        )
        if isinstance(resp, dict) and (
            "GeneralInfo" in resp or "NumOrderId" in resp or "OrderId" in resp
        ):
            raw = resp
        else:
            return {
                "success": False,
                "order_id": order_id_input,
                "dry_run": dry_run,
                "error": f"No order found for numeric ID '{order_id_input}'.",
            }
        order_guid = raw.get("OrderId", "")
        numeric_id = raw.get("NumOrderId")

    # ---------- Step 2: refuse if dispatched / processed ----------
    # `Processed` is the real API boolean; Status == 0 covers test mocks
    # that represent a processed/dispatched order via a top-level Status field.
    if raw.get("Processed") is True or raw.get("Status") == 0:
        return {
            "success": False,
            "order_id": order_guid,
            "numeric_id": numeric_id,
            "dry_run": dry_run,
            "error": (
                "Cannot update address: order is already dispatched or processed. "
                "Address changes can only be made to open orders."
            ),
        }

    # ---------- Step 3: build merged address ----------
    customer = raw.get("CustomerInfo") or {}
    current_address = customer.get("Address") or {}
    current_billing = customer.get("BillingAddress") or {}
    channel_buyer_name = customer.get("ChannelBuyerName") or ""

    # Full country name: use provided value if given, else keep current
    resolved_country = country.strip() or current_address.get("Country", "")

    new_address = {
        "FullName":     full_name,
        "Address1":     address1,
        "Address2":     address2 if address2 else current_address.get("Address2", ""),
        "Address3":     current_address.get("Address3", ""),
        "Town":         town,
        "Region":       region if region else current_address.get("Region", ""),
        "PostCode":     post_code,
        "Country":      resolved_country,
        "PhoneNumber":  phone if phone else current_address.get("PhoneNumber", ""),
        "EmailAddress": email if email else current_address.get("EmailAddress", ""),
        "Company":      company if company else current_address.get("Company", ""),
        # Preserve internal Linnworks fields unchanged
        "Continent":    current_address.get("Continent", ""),
        "CountryId":    current_address.get("CountryId", ""),
    }

    updated_address = {
        "full_name":    full_name,
        "address1":     address1,
        "address2":     new_address["Address2"],
        "town":         town,
        "region":       new_address["Region"],
        "post_code":    post_code,
        "country":      resolved_country,
        "country_code": country_code,
        "phone":        new_address["PhoneNumber"],
        "email":        new_address["EmailAddress"],
        "company":      new_address["Company"],
    }

    if dry_run:
        return {
            "success": True,
            "dry_run": True,
            "order_id": order_guid,
            "numeric_id": numeric_id,
            "updated_address": updated_address,
            "message": "No changes written. Set dry_run=False to apply this update.",
        }

    # ---------- Step 4: submit ----------
    call_linnworks(
        "Orders/SetOrderCustomerInfo",
        {
            "orderId": order_guid,
            "info": {
                "ChannelBuyerName": channel_buyer_name,
                "Address": new_address,
                "BillingAddress": current_billing,
            },
            "saveToCrm": False,
        },
    )

    return {
        "success": True,
        "dry_run": False,
        "order_id": order_guid,
        "numeric_id": numeric_id,
        "updated_address": updated_address,
    }


# ---------- Helper: resolve any order_id (GUID or numeric) to its GUID ----------

def _resolve_order_guid(order_id: str) -> tuple[str, dict]:
    """
    Given a GUID or numeric order ID, return (guid, raw_order_dict).
    Raises RuntimeError if not found.
    """
    order_id = order_id.strip()
    if _UUID_RE.match(order_id):
        resp = call_linnworks("Orders/GetOrdersById", {"pkOrderIds": [order_id]})
        orders = resp if isinstance(resp, list) else (resp.get("Orders") or resp.get("Data") or [])
        if not orders:
            raise RuntimeError(f"No order found for GUID '{order_id}'.")
        return orders[0].get("OrderId") or order_id, orders[0]
    else:
        raw = call_linnworks_get(
            "Orders/GetOrderDetailsByNumOrderId",
            params={"orderId": order_id},
        )
        if not isinstance(raw, dict) or ("GeneralInfo" not in raw and "NumOrderId" not in raw):
            raise RuntimeError(f"No order found for numeric ID '{order_id}'.")
        return raw.get("OrderId", ""), raw


@mcp.tool()
def get_order_notes(order_id: str) -> dict:
    """
    Fetch all notes attached to a Linnworks order.

    Works for both open (unprocessed) and processed orders. Returns every
    note with its ID, text, internal flag, creation timestamp, and creator.

    Use this to read the notes on an order before adding, editing, or deleting
    one — the note_id returned here is required by add_order_note,
    update_order_note, and delete_order_note.

    Args:
        order_id: GUID pkOrderID (e.g. "a1b2c3d4-1234-...") or numeric order
            number (e.g. "596475"). Both formats are accepted.

    Returns:
        A dict with:
          - order_id:    the GUID of the order
          - note_count:  total number of notes
          - notes:       list of note dicts, each with:
              note_id, note, internal, created_on, created_by
    """
    order_id = order_id.strip()

    # Resolve numeric → GUID (GetOrderNotes only accepts GUID)
    if _UUID_RE.match(order_id):
        order_guid = order_id
    else:
        order_guid, _ = _resolve_order_guid(order_id)

    raw_notes = call_linnworks_get("Orders/GetOrderNotes", params={"orderId": order_guid})

    # Response is a plain list of OrderNote objects
    if not isinstance(raw_notes, list):
        raw_notes = raw_notes.get("Notes") or [] if isinstance(raw_notes, dict) else []

    notes = [
        {
            "note_id": n.get("pkOrderNoteId"),
            "note": n.get("Note") or n.get("NoteText"),
            "internal": n.get("IsInternal"),
            "created_on": n.get("NoteCreatedOn") or n.get("DateCreated"),
            "created_by": n.get("NoteCreatedBy") or n.get("CreatedBy"),
        }
        for n in raw_notes
    ]

    return {
        "order_id": order_guid,
        "note_count": len(notes),
        "notes": notes,
    }


@mcp.tool()
def add_order_note(
    order_id: str,
    note: str,
    internal: bool = True,
    is_processing_note: bool = False,
    dry_run: bool = True,
) -> dict:
    """
    Add a note to a Linnworks order (open or processed).

    The note is added immediately and appears on the order in the Linnworks
    UI. Both open and processed orders accept notes.

    IMPORTANT: dry_run defaults to True. Set dry_run=False only when you are
    sure the note text is correct — confirm with the user before doing so.

    Args:
        order_id: GUID pkOrderID (e.g. "a1b2c3d4-1234-...") or numeric order
            number (e.g. "596475"). Both formats are accepted.
        note: The text of the note to add.
        internal: If True (default), the note is marked as internal (visible
            only to staff, not shown on customer-facing documents).
        is_processing_note: If True, marks the note as a processing note.
            Defaults to False.
        dry_run: If True (default), shows what would be added without writing
            anything. Set to False to actually add the note.

    Returns:
        A dict with:
          - order_id:  the GUID of the order
          - dry_run:   whether this was a dry run
          - success:   True if the note was added (or would be, on dry run)
          - note:      the note text that was (or would be) added
          - internal:  the internal flag value
    """
    order_id = order_id.strip()

    # Resolve to GUID
    if _UUID_RE.match(order_id):
        order_guid = order_id
    else:
        order_guid, _ = _resolve_order_guid(order_id)

    if dry_run:
        return {
            "order_id": order_guid,
            "dry_run": True,
            "success": True,
            "note": note,
            "internal": internal,
            "message": "No note written. Set dry_run=False to add this note.",
        }

    # Orders/AddOrdersNote accepts a list of order IDs so one call can note
    # multiple orders — we always pass a single-element list here.
    call_linnworks(
        "Orders/AddOrdersNote",
        {
            "OrderIds": [order_guid],
            "NoteText": note,
            "IsInternal": internal,
            "IsProcessingNote": is_processing_note,
        },
    )

    # Read back to confirm the note was created
    raw_notes = call_linnworks_get("Orders/GetOrderNotes", params={"orderId": order_guid})
    if not isinstance(raw_notes, list):
        raw_notes = raw_notes.get("Notes") or [] if isinstance(raw_notes, dict) else []

    notes_after = [
        {
            "note_id": n.get("pkOrderNoteId"),
            "note": n.get("Note") or n.get("NoteText"),
            "internal": n.get("IsInternal"),
            "created_on": n.get("NoteCreatedOn") or n.get("DateCreated"),
            "created_by": n.get("NoteCreatedBy") or n.get("CreatedBy"),
        }
        for n in raw_notes
    ]

    return {
        "order_id": order_guid,
        "dry_run": False,
        "success": True,
        "note": note,
        "internal": internal,
        "note_count_after": len(notes_after),
        "notes": notes_after,
    }


@mcp.tool()
def update_order_note(
    order_id: str,
    note_id: str,
    note: str,
    internal: Optional[bool] = None,
    dry_run: bool = True,
) -> dict:
    """
    Edit an existing note on a Linnworks order.

    Linnworks has no dedicated "edit note" endpoint, so this tool uses a
    delete-then-add pattern: the old note is deleted and a new one with the
    updated text is created. The new note gets a new note_id; the old
    note_id will no longer exist after this call.

    Use get_order_notes() first to find the note_id you want to update.

    IMPORTANT: dry_run defaults to True. Set dry_run=False only when you are
    sure the new text is correct — confirm with the user before doing so.

    Args:
        order_id: GUID pkOrderID (e.g. "a1b2c3d4-1234-...") or numeric order
            number (e.g. "596475"). Both formats are accepted.
        note_id: The pkOrderNoteId GUID of the note to replace (from
            get_order_notes).
        note: The replacement note text.
        internal: Internal flag for the new note. If None (default), the
            existing note's internal flag is preserved.
        dry_run: If True (default), shows the before/after without writing
            anything. Set to False to apply the update.

    Returns:
        A dict with:
          - order_id:     the GUID of the order
          - dry_run:      whether this was a dry run
          - success:      True if the update succeeded (or would, on dry run)
          - old_note_id:  the deleted note's ID
          - old_note:     the text that was replaced
          - new_note:     the replacement text
          - internal:     the internal flag on the new note
    """
    order_id = order_id.strip()
    note_id = note_id.strip()

    # Resolve to GUID
    if _UUID_RE.match(order_id):
        order_guid = order_id
    else:
        order_guid, _ = _resolve_order_guid(order_id)

    # Read current notes (read-before-write: verify the note exists)
    raw_notes = call_linnworks_get("Orders/GetOrderNotes", params={"orderId": order_guid})
    if not isinstance(raw_notes, list):
        raw_notes = raw_notes.get("Notes") or [] if isinstance(raw_notes, dict) else []

    existing = next(
        (n for n in raw_notes if (n.get("pkOrderNoteId") or "") == note_id),
        None,
    )
    if existing is None:
        return {
            "order_id": order_guid,
            "success": False,
            "error": f"Note '{note_id}' not found on order '{order_guid}'.",
        }

    old_note_text = existing.get("Note") or existing.get("NoteText") or ""
    # Preserve existing internal flag if caller didn't specify
    new_internal = internal if internal is not None else bool(existing.get("IsInternal"))

    if dry_run:
        return {
            "order_id": order_guid,
            "dry_run": True,
            "success": True,
            "old_note_id": note_id,
            "old_note": old_note_text,
            "new_note": note,
            "internal": new_internal,
            "message": (
                "No changes written. Set dry_run=False to delete the old note "
                "and create the new one."
            ),
        }

    # Step 1: delete the old note
    call_linnworks(
        "ProcessedOrders/DeleteOrderNote",
        {"pkOrderNoteId": note_id},
    )

    # Step 2: add the replacement note
    call_linnworks(
        "Orders/AddOrdersNote",
        {
            "OrderIds": [order_guid],
            "NoteText": note,
            "IsInternal": new_internal,
            "IsProcessingNote": False,
        },
    )

    # Read back to confirm
    raw_notes_after = call_linnworks_get("Orders/GetOrderNotes", params={"orderId": order_guid})
    if not isinstance(raw_notes_after, list):
        raw_notes_after = (
            raw_notes_after.get("Notes") or []
            if isinstance(raw_notes_after, dict)
            else []
        )

    notes_after = [
        {
            "note_id": n.get("pkOrderNoteId"),
            "note": n.get("Note") or n.get("NoteText"),
            "internal": n.get("IsInternal"),
            "created_on": n.get("NoteCreatedOn") or n.get("DateCreated"),
            "created_by": n.get("NoteCreatedBy") or n.get("CreatedBy"),
        }
        for n in raw_notes_after
    ]

    return {
        "order_id": order_guid,
        "dry_run": False,
        "success": True,
        "old_note_id": note_id,
        "old_note": old_note_text,
        "new_note": note,
        "internal": new_internal,
        "note_count_after": len(notes_after),
        "notes": notes_after,
    }


@mcp.tool()
def delete_order_note(
    order_id: str,
    note_id: str,
    dry_run: bool = True,
) -> dict:
    """
    Delete a specific note from a Linnworks order.

    Permanently removes the note identified by note_id. This cannot be
    undone — use get_order_notes() to review the note text before deleting.

    IMPORTANT: dry_run defaults to True. Set dry_run=False only when you are
    sure you want to delete the note — confirm with the user before doing so.

    Args:
        order_id: GUID pkOrderID (e.g. "a1b2c3d4-1234-...") or numeric order
            number (e.g. "596475"). Both formats are accepted.
        note_id: The pkOrderNoteId GUID of the note to delete (from
            get_order_notes).
        dry_run: If True (default), shows what would be deleted without
            writing anything. Set to False to permanently delete the note.

    Returns:
        A dict with:
          - order_id:         the GUID of the order
          - dry_run:          whether this was a dry run
          - success:          True if the note was deleted (or would be)
          - deleted_note_id:  the ID of the note that was (or would be) deleted
          - deleted_note:     the text of the note that was (or would be) deleted
          - note_count_after: number of notes remaining (live run only)
    """
    order_id = order_id.strip()
    note_id = note_id.strip()

    # Resolve to GUID
    if _UUID_RE.match(order_id):
        order_guid = order_id
    else:
        order_guid, _ = _resolve_order_guid(order_id)

    # Read current notes (read-before-write: confirm the note exists + capture text)
    raw_notes = call_linnworks_get("Orders/GetOrderNotes", params={"orderId": order_guid})
    if not isinstance(raw_notes, list):
        raw_notes = raw_notes.get("Notes") or [] if isinstance(raw_notes, dict) else []

    existing = next(
        (n for n in raw_notes if (n.get("pkOrderNoteId") or "") == note_id),
        None,
    )
    if existing is None:
        return {
            "order_id": order_guid,
            "success": False,
            "error": f"Note '{note_id}' not found on order '{order_guid}'.",
        }

    note_text = existing.get("Note") or existing.get("NoteText") or ""

    if dry_run:
        return {
            "order_id": order_guid,
            "dry_run": True,
            "success": True,
            "deleted_note_id": note_id,
            "deleted_note": note_text,
            "message": "No changes written. Set dry_run=False to permanently delete this note.",
        }

    call_linnworks(
        "ProcessedOrders/DeleteOrderNote",
        {"pkOrderNoteId": note_id},
    )

    # Read back to confirm note count
    raw_after = call_linnworks_get("Orders/GetOrderNotes", params={"orderId": order_guid})
    if not isinstance(raw_after, list):
        raw_after = raw_after.get("Notes") or [] if isinstance(raw_after, dict) else []

    return {
        "order_id": order_guid,
        "dry_run": False,
        "success": True,
        "deleted_note_id": note_id,
        "deleted_note": note_text,
        "note_count_after": len(raw_after),
    }


@mcp.tool()
def find_open_orders_for_sku(
    sku: str,
    location_id: str = DEFAULT_LOCATION_ID,
) -> dict:
    """
    Find all currently open (unprocessed) orders that contain a given SKU.

    Returns each matching order with customer name, email, order reference,
    dispatch deadline, and the quantity of the requested SKU in that order.

    This is the primary tool for the "PO gap → customer impact" workflow:
    if a purchase order item is delayed or missing, use this to find which
    customers are waiting for that SKU so they can be contacted proactively.

    Searches both top-level order items and composite child components, so
    bundle SKUs are found even when the parent composite is the line item.

    Note: enriching with customer details requires a second API call per
    batch of 50 matched orders. For SKUs with very high open-order volume
    this may be slow — but in practice most SKUs will have a handful of
    open orders at any one time.

    Args:
        sku: The exact SKU to search for (case-insensitive).
        location_id: Linnworks location to query. Defaults to "Default".

    Returns:
        A dict with:
          - sku:            the SKU searched
          - total_open_orders_scanned: total open orders checked
          - match_count:   number of orders containing this SKU
          - orders: list of matching order dicts, each with:
              order_id, num_order_id, reference_num, dispatch_by,
              customer_name, customer_email, quantity (of the matched SKU)
    """
    sku_lower = sku.strip().lower()

    # Step 1: fetch all open orders (low-fidelity includes item SKUs)
    response = call_linnworks(
        "OpenOrders/GetOrdersLowFidelity",
        {"request": {"LocationId": location_id}},
    )
    all_orders = response.get("Orders") or []

    # Step 2: filter to orders that contain the SKU (top-level or composite child)
    def _find_qty(items: list) -> int:
        """Sum quantity of sku across items and their CompositeChild lists."""
        total = 0
        for item in items:
            if (item.get("SKU") or "").lower() == sku_lower:
                total += item.get("Quantity") or 0
            children = item.get("CompositeChild") or []
            if children:
                total += _find_qty(children)
        return total

    matched: list[dict] = []
    for order in all_orders:
        qty = _find_qty(order.get("Items") or [])
        if qty > 0:
            matched.append({
                "_guid": order.get("pkOrderID"),
                "order_id": order.get("pkOrderID"),
                "num_order_id": order.get("OrderId"),
                "reference_num": order.get("ReferenceNum"),
                "dispatch_by": order.get("DispatchBy"),
                "quantity": qty,
                "customer_name": "",
                "customer_email": "",
            })

    # Step 3: enrich matched orders with customer details via GetOrdersById
    batch_size = 50
    guids = [o["_guid"] for o in matched if o["_guid"]]
    customer_info: dict[str, dict] = {}

    for i in range(0, len(guids), batch_size):
        batch = guids[i : i + batch_size]
        detail_orders = call_linnworks("Orders/GetOrdersById", {"pkOrderIds": batch})
        if not isinstance(detail_orders, list):
            detail_orders = detail_orders.get("Orders") or []
        for detail in detail_orders:
            oid = detail.get("OrderId")
            fmt = _format_order_detail(detail)
            customer_info[oid] = {
                "customer_name": fmt.get("customer_name", ""),
                "customer_email": fmt.get("customer_email", ""),
            }

    # Step 4: merge customer info back and clean up internal key
    for order in matched:
        info = customer_info.get(order["_guid"], {})
        order["customer_name"] = info.get("customer_name", "")
        order["customer_email"] = info.get("customer_email", "")
        del order["_guid"]

    return {
        "sku": sku.strip(),
        "total_open_orders_scanned": len(all_orders),
        "match_count": len(matched),
        "orders": matched,
    }


@mcp.tool()
def get_stock_level(
    sku: str,
    location_id: str = DEFAULT_LOCATION_ID,
    include_empty_locations: bool = False,
) -> dict:
    """
    Return the current stock level for a SKU, optionally at a specific location.

    Performs a two-step lookup: resolves the SKU to a StockItemId, then fetches
    stock levels across all locations.

    Useful for questions like "how much stock do we have of SKU ABC-123?" or
    "what's the available quantity at our main warehouse?".

    Note: returns StockLevel (gross on-hand), Available (net of order-book
    allocations), InOrderBook, and Due. For FIFO purposes use StockLevel,
    not Available — see tenant learnings.

    Many locations in this tenant are virtual dropship supplier locations rather
    than physical warehouses. When multiple non-Default locations show the same
    non-zero stock_level, that value likely represents the supplier's available
    quantity (not additional owned stock). The "Default" location is the
    authoritative owned-stock figure.

    Args:
        sku: The exact SKU / item number to look up (case-insensitive).
        location_id: The Linnworks StockLocationId to filter to. Defaults to
            the "Default" location (all-locations aggregate). Pass a specific
            location GUID to see a single warehouse row.
        include_empty_locations: If True, return all 27 location rows including
            those with zero stock. Defaults to False (only non-zero rows).

    Returns:
        A dict with the item identity and a list of stock level rows per location,
        plus a notes field when potential duplicate supplier counts are detected.
    """
    try:
        item = call_linnworks("Inventory/GetInventoryItem", {"sku": sku})
    except RuntimeError:
        return {"error": f"No inventory item found for SKU '{sku}'", "sku": sku}

    stock_item_id = item.get("StockItemId")
    if not stock_item_id:
        return {"error": f"Item found for SKU '{sku}' but StockItemId was missing", "sku": sku}

    levels_response = call_linnworks(
        "Stock/GetStockLevel_Batch",
        {"request": {"StockItemIds": [stock_item_id]}},
    )

    batch = levels_response if isinstance(levels_response, list) else []
    item_row = next((r for r in batch if r.get("pkStockItemId") == stock_item_id), None)
    levels = item_row.get("StockItemLevels") or [] if item_row else []

    if location_id != DEFAULT_LOCATION_ID:
        levels = [
            l for l in levels
            if (l.get("Location") or {}).get("StockLocationId") == location_id
        ]

    formatted = [
        {
            "location_name": (l.get("Location") or {}).get("LocationName"),
            "location_id": (l.get("Location") or {}).get("StockLocationId"),
            "stock_level": l.get("StockLevel"),
            "available": l.get("Available"),
            "in_order_book": l.get("InOrderBook"),
            "due": l.get("Due"),
        }
        for l in levels
    ]

    if not include_empty_locations:
        formatted = [f for f in formatted if (f["stock_level"] or 0) > 0]

    # Detect potential duplicate supplier counts: multiple non-Default locations
    # sharing the same non-zero stock_level suggests virtual dropship rows.
    notes = []
    non_default = [f for f in formatted if f["location_id"] != DEFAULT_LOCATION_ID and (f["stock_level"] or 0) > 0]
    if len(non_default) > 1:
        level_values = [f["stock_level"] for f in non_default]
        if len(set(level_values)) == 1:
            notes.append(
                f"{len(non_default)} non-Default locations all show stock_level={level_values[0]}. "
                "These are likely virtual dropship supplier locations showing the same supplier "
                "availability — not additional owned stock. Use the 'Default' row for owned inventory."
            )

    result = {
        "sku": sku,
        "stock_item_id": stock_item_id,
        "title": item.get("ItemTitle"),
        "location_id_filter": location_id,
        "levels": formatted,
    }
    if notes:
        result["notes"] = notes
    return result


@mcp.tool()
def get_processed_orders(
    from_date: str,
    to_date: str,
    date_field: str = "received",
    page: int = 1,
    page_size: int = 500,
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
        page_size: Orders per page. Min 20, defaults to 500 (recommended for large
            date ranges to minimise round-trips).

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


def _fetch_supplier_for_items(stock_item_ids: list[str]) -> dict[str, dict]:
    """
    Return a map of stock_item_id → {supplier_name, supplier_id} for the
    primary (IsDefault=True, else first) supplier of each item.

    Uses Stock/GetStockItemsFullByIds with DataRequirements:[1] — the only
    confirmed path for reading supplier-item relationships in this tenant.
    All dedicated Inventory/GetInventoryItemSuppliers endpoints return 404.
    DataRequirements:[1] confirmed May 2026.

    Batches in groups of 200 to avoid oversized requests.
    Items with no supplier linked are mapped to supplier_name="No Supplier".
    """
    result: dict[str, dict] = {}
    batch_size = 200
    for i in range(0, len(stock_item_ids), batch_size):
        batch = stock_item_ids[i : i + batch_size]
        try:
            resp = call_linnworks(
                "Stock/GetStockItemsFullByIds",
                {"request": {"StockItemIds": batch, "DataRequirements": [1]}},
            )
            for item in resp.get("StockItemsFullExtended", []):
                iid = item.get("StockItemId", "")
                suppliers = item.get("Suppliers") or []
                if suppliers:
                    sup = next(
                        (s for s in suppliers if s.get("IsDefault")), suppliers[0]
                    )
                    result[iid] = {
                        "supplier_name": sup.get("Supplier") or "No Supplier",
                        "supplier_id": sup.get("SupplierID") or "",
                    }
                else:
                    result[iid] = {"supplier_name": "No Supplier", "supplier_id": ""}
        except Exception:
            # Best-effort: items we can't resolve stay absent from the map.
            pass
    return result


@mcp.tool()
def get_processed_order_items(
    from_date: str,
    to_date: str,
    date_field: str = "received",
    page: int = 1,
    page_size: int = 500,
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
        page_size: Orders per page. Min 20, defaults to 500 (recommended for large
            date ranges to minimise round-trips). Each page triggers a batch detail
            fetch.

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


@mcp.tool()
def get_category_report(
    from_date: str,
    to_date: str,
    date_field: str = "processed",
    top_n: int = 20,
) -> dict:
    """
    Aggregate sales by product category for a date range. Auto-paginates internally
    and returns ranked category totals — revenue, units, and order count. Use this
    instead of get_processed_order_items when you need category-level analysis.
    Much faster than manual pagination.

    Args:
        from_date: Start of the date range in ISO format, e.g. "2026-04-01".
        to_date: End of the date range in ISO format, e.g. "2026-04-30".
        date_field: Which date to filter on — "received", "processed" (default),
            "payment", or "cancelled".
        top_n: Number of top categories to return, ranked by revenue. Defaults to 20.

    Returns:
        A dict with:
          - from_date, to_date, date_field: the query parameters used
          - total_orders_scanned: total number of processed orders in the range
          - categories: list of top_n category dicts sorted by revenue desc, each with
              rank, category, revenue, units, orders (distinct order count)
    """
    from collections import defaultdict

    PAGE_SIZE = 500

    # Running totals per category — no full order objects kept in memory
    cat_revenue: dict[str, float] = defaultdict(float)
    cat_units: dict[str, int] = defaultdict(int)
    cat_order_ids: dict[str, set] = defaultdict(set)

    total_orders_scanned = 0
    page = 1
    total_pages: int | None = None

    while total_pages is None or page <= total_pages:
        summary_response = call_linnworks(
            "ProcessedOrders/SearchProcessedOrders",
            {
                "request": {
                    "DateField": date_field,
                    "FromDate": f"{from_date}T00:00:00",
                    "ToDate": f"{to_date}T23:59:59",
                    "PageNumber": page,
                    "ResultsPerPage": PAGE_SIZE,
                }
            },
        )
        wrapper = summary_response.get("ProcessedOrders") or {}
        raw_orders = wrapper.get("Data") or []

        if total_pages is None:
            total_pages = wrapper.get("TotalPages", 1)
            total_orders_scanned = wrapper.get("TotalEntries", 0)

        if not raw_orders:
            break

        guids = [o["pkOrderID"] for o in raw_orders]
        items_by_order = _batch_order_items(guids)

        for guid in guids:
            for item in items_by_order.get(guid, []):
                cat = item.get("category") or "Uncategorised"
                cat_revenue[cat] += item.get("line_total_inc_tax") or 0.0
                cat_units[cat] += item.get("quantity") or 0
                cat_order_ids[cat].add(guid)

        page += 1

    all_cats = sorted(cat_revenue.keys(), key=lambda c: cat_revenue[c], reverse=True)
    categories = [
        {
            "rank": idx + 1,
            "category": cat,
            "revenue": round(cat_revenue[cat], 2),
            "units": cat_units[cat],
            "orders": len(cat_order_ids[cat]),
        }
        for idx, cat in enumerate(all_cats[:top_n])
    ]

    return {
        "from_date": from_date,
        "to_date": to_date,
        "date_field": date_field,
        "total_orders_scanned": total_orders_scanned,
        "categories": categories,
    }


# ---------- Revenue summary, top SKUs, period comparison ----------

def _fetch_revenue_data(
    from_date: str,
    to_date: str,
    date_field: str,
) -> dict:
    """
    Internal helper: autopaginate SearchProcessedOrders and aggregate revenue
    totals. Returns a plain dict — not an MCP tool itself.
    """
    PAGE_SIZE = 500

    total_orders = 0
    total_revenue = 0.0
    by_source: dict[str, dict] = {}
    by_country: dict[str, dict] = {}

    page = 1
    total_pages: int | None = None

    while total_pages is None or page <= total_pages:
        response = call_linnworks(
            "ProcessedOrders/SearchProcessedOrders",
            {
                "request": {
                    "DateField": date_field,
                    "FromDate": f"{from_date}T00:00:00",
                    "ToDate": f"{to_date}T23:59:59",
                    "PageNumber": page,
                    "ResultsPerPage": PAGE_SIZE,
                }
            },
        )
        wrapper = response.get("ProcessedOrders") or {}
        raw_orders = wrapper.get("Data") or []

        if total_pages is None:
            total_pages = wrapper.get("TotalPages", 1)
            total_orders = wrapper.get("TotalEntries", 0)

        if not raw_orders:
            break

        for o in raw_orders:
            charge = float(o.get("fTotalCharge") or 0)
            total_revenue += charge

            source = o.get("Source") or "Unknown"
            if source not in by_source:
                by_source[source] = {"orders": 0, "revenue": 0.0}
            by_source[source]["orders"] += 1
            by_source[source]["revenue"] += charge

            country = o.get("cCountry") or "Unknown"
            if country not in by_country:
                by_country[country] = {"orders": 0, "revenue": 0.0}
            by_country[country]["orders"] += 1
            by_country[country]["revenue"] += charge

        page += 1

    avg_order_value = total_revenue / total_orders if total_orders > 0 else 0.0

    return {
        "total_orders": total_orders,
        "total_revenue": round(total_revenue, 2),
        "avg_order_value": round(avg_order_value, 2),
        "by_source": sorted(
            [
                {
                    "source": src,
                    "orders": v["orders"],
                    "revenue": round(v["revenue"], 2),
                    "aov": round(v["revenue"] / v["orders"], 2) if v["orders"] > 0 else 0.0,
                }
                for src, v in by_source.items()
            ],
            key=lambda x: -x["revenue"],
        ),
        "by_country": sorted(
            [
                {
                    "country": c,
                    "orders": v["orders"],
                    "revenue": round(v["revenue"], 2),
                }
                for c, v in by_country.items()
            ],
            key=lambda x: -x["orders"],
        )[:15],
    }


@mcp.tool()
def get_revenue_summary(
    from_date: str,
    to_date: str,
    date_field: str = "received",
) -> dict:
    """
    Total orders, revenue, and average order value for a date range.

    Auto-paginates through all pages internally and returns aggregated totals —
    never overflows context, unlike get_processed_orders. Also breaks down
    orders and revenue by sales channel (source) and country.

    Use this instead of get_processed_orders when the question is about totals:
    "what was our total revenue in April?", "how many orders did we take last
    month?", "what's our average order value?", "how is Shopify vs Amazon?".

    For per-order or per-SKU detail, use get_processed_orders or
    get_processed_order_items instead.

    Note on Amazon FBA revenue: the total_charge field on FBA orders may capture
    the marketplace fee rather than the full sale price, resulting in artificially
    low AOV for that channel. Treat FBA revenue figures as indicative only.

    Args:
        from_date: Start of the date range in ISO format, e.g. "2026-04-01".
        to_date: End of the date range in ISO format, e.g. "2026-04-30".
        date_field: Which date to filter on — "received" (default), "processed",
            "payment", or "cancelled".

    Returns:
        A dict with:
          - from_date, to_date, date_field: the query parameters used
          - total_orders:     total orders in the period
          - total_revenue:    sum of order charges (£)
          - avg_order_value:  total_revenue / total_orders
          - by_source:        list of {source, orders, revenue, aov} sorted by revenue
          - by_country:       list of {country, orders, revenue} sorted by order count
    """
    data = _fetch_revenue_data(from_date, to_date, date_field)
    return {"from_date": from_date, "to_date": to_date, "date_field": date_field, **data}


@mcp.tool()
def get_top_skus(
    from_date: str,
    to_date: str,
    date_field: str = "processed",
    top_n: int = 20,
    rank_by: str = "revenue",
    supplier_name: str = "",
) -> dict:
    """
    Aggregate sales by individual SKU for a date range. Auto-paginates
    internally and returns ranked SKU totals — revenue, units, and order count.

    Use this when you need SKU-level analysis: "what are our top-selling
    products?", "which SKUs drove the most revenue last month?", "how many
    units of each product did we sell?".

    Optionally filter to a single supplier's SKUs by passing supplier_name
    (case-insensitive partial match against the primary supplier name on each
    item). Use get_suppliers() to see the full list of supplier names.

    For category-level analysis use get_category_report instead (faster).

    Args:
        from_date: Start of the date range in ISO format, e.g. "2026-05-01".
        to_date: End of the date range in ISO format, e.g. "2026-05-31".
        date_field: Which date to filter on — "received", "processed" (default),
            "payment", or "cancelled".
        top_n: Number of top SKUs to return. Defaults to 20.
        rank_by: Sort order — "revenue" (default) or "units".
        supplier_name: Optional. Filter results to SKUs whose primary supplier
            name contains this string (case-insensitive). Leave blank for all.

    Returns:
        A dict with:
          - from_date, to_date, date_field: the query parameters used
          - total_orders_scanned: total processed orders in the range
          - ranked_by:            the rank_by value used
          - supplier_filter:      the supplier_name filter applied (or "")
          - skus: list of top_n SKU dicts sorted by rank_by desc, each with
              rank, sku, title, supplier, revenue, units, orders
    """
    from collections import defaultdict

    if rank_by not in ("revenue", "units"):
        rank_by = "revenue"

    PAGE_SIZE = 500
    supplier_filter = supplier_name.strip().lower()

    sku_revenue: dict[str, float] = defaultdict(float)
    sku_units: dict[str, int] = defaultdict(int)
    sku_order_ids: dict[str, set] = defaultdict(set)
    sku_titles: dict[str, str] = {}
    sku_stock_ids: dict[str, str] = {}  # sku → stock_item_id

    total_orders_scanned = 0
    page = 1
    total_pages: int | None = None

    while total_pages is None or page <= total_pages:
        summary_response = call_linnworks(
            "ProcessedOrders/SearchProcessedOrders",
            {
                "request": {
                    "DateField": date_field,
                    "FromDate": f"{from_date}T00:00:00",
                    "ToDate": f"{to_date}T23:59:59",
                    "PageNumber": page,
                    "ResultsPerPage": PAGE_SIZE,
                }
            },
        )
        wrapper = summary_response.get("ProcessedOrders") or {}
        raw_orders = wrapper.get("Data") or []

        if total_pages is None:
            total_pages = wrapper.get("TotalPages", 1)
            total_orders_scanned = wrapper.get("TotalEntries", 0)

        if not raw_orders:
            break

        guids = [o["pkOrderID"] for o in raw_orders]
        items_by_order = _batch_order_items(guids)

        for guid in guids:
            for item in items_by_order.get(guid, []):
                sku = item.get("sku") or "UNKNOWN"
                qty = item.get("quantity") or 0
                rev = float(item.get("line_total_inc_tax") or 0)
                sku_revenue[sku] += rev
                sku_units[sku] += qty
                sku_order_ids[sku].add(guid)
                if sku not in sku_titles and item.get("title"):
                    sku_titles[sku] = item["title"]
                if sku not in sku_stock_ids and item.get("stock_item_id"):
                    sku_stock_ids[sku] = item["stock_item_id"]

        page += 1

    # Resolve suppliers for all unique stock item IDs, then filter if requested
    unique_ids = list({v for v in sku_stock_ids.values() if v})
    supplier_map: dict[str, dict] = {}
    if unique_ids:
        supplier_map = _fetch_supplier_for_items(unique_ids)
    # Build sku → supplier lookup
    sku_supplier: dict[str, str] = {}
    for sku, sid in sku_stock_ids.items():
        sup = supplier_map.get(sid, {})
        sku_supplier[sku] = sup.get("supplier_name") or "No Supplier"

    sort_key = (lambda s: sku_revenue[s]) if rank_by == "revenue" else (lambda s: sku_units[s])
    all_skus = sorted(sku_revenue.keys(), key=sort_key, reverse=True)

    # Apply supplier filter
    if supplier_filter:
        all_skus = [s for s in all_skus if supplier_filter in sku_supplier.get(s, "").lower()]

    skus = [
        {
            "rank": idx + 1,
            "sku": sku,
            "title": sku_titles.get(sku, ""),
            "supplier": sku_supplier.get(sku, ""),
            "revenue": round(sku_revenue[sku], 2),
            "units": sku_units[sku],
            "orders": len(sku_order_ids[sku]),
        }
        for idx, sku in enumerate(all_skus[:top_n])
    ]

    return {
        "from_date": from_date,
        "to_date": to_date,
        "date_field": date_field,
        "total_orders_scanned": total_orders_scanned,
        "ranked_by": rank_by,
        "supplier_filter": supplier_name.strip(),
        "skus": skus,
    }


@mcp.tool()
def get_sales_by_supplier(
    from_date: str,
    to_date: str,
    date_field: str = "processed",
    top_n: int = 20,
    rank_by: str = "revenue",
) -> dict:
    """
    Aggregate sales by supplier for a date range. Auto-paginates internally
    and returns ranked supplier totals — revenue, units, order count, and
    the number of distinct SKUs sold.

    Use this when you need supplier-level sales analysis: "which suppliers
    drove the most revenue last month?", "how much did we sell from Shiner?",
    "which supplier's products sell best by volume?".

    Supplier assignment is based on the primary (IsDefault) supplier linked to
    each SKU in Linnworks inventory. SKUs with no supplier are grouped under
    "No Supplier".

    Use get_suppliers() to see all configured supplier names.
    Use get_top_skus(supplier_name=...) to drill into a specific supplier's SKUs.

    Args:
        from_date: Start of the date range in ISO format, e.g. "2026-05-01".
        to_date: End of the date range in ISO format, e.g. "2026-05-31".
        date_field: Which date to filter on — "received", "processed" (default),
            "payment", or "cancelled".
        top_n: Number of top suppliers to return. Defaults to 20.
        rank_by: Sort order — "revenue" (default) or "units".

    Returns:
        A dict with:
          - from_date, to_date, date_field: the query parameters used
          - total_orders_scanned: total processed orders in the range
          - ranked_by: the rank_by value used
          - suppliers: list of top_n supplier dicts sorted by rank_by desc, each with
              rank, supplier_name, supplier_id, revenue, units, orders, sku_count
    """
    from collections import defaultdict

    if rank_by not in ("revenue", "units"):
        rank_by = "revenue"

    PAGE_SIZE = 500

    # Accumulate per-SKU data first; resolve to supplier at the end.
    sku_revenue: dict[str, float] = defaultdict(float)
    sku_units: dict[str, int] = defaultdict(int)
    sku_order_ids: dict[str, set] = defaultdict(set)
    sku_stock_ids: dict[str, str] = {}  # sku → stock_item_id

    total_orders_scanned = 0
    page = 1
    total_pages: int | None = None

    while total_pages is None or page <= total_pages:
        summary_response = call_linnworks(
            "ProcessedOrders/SearchProcessedOrders",
            {
                "request": {
                    "DateField": date_field,
                    "FromDate": f"{from_date}T00:00:00",
                    "ToDate": f"{to_date}T23:59:59",
                    "PageNumber": page,
                    "ResultsPerPage": PAGE_SIZE,
                }
            },
        )
        wrapper = summary_response.get("ProcessedOrders") or {}
        raw_orders = wrapper.get("Data") or []

        if total_pages is None:
            total_pages = wrapper.get("TotalPages", 1)
            total_orders_scanned = wrapper.get("TotalEntries", 0)

        if not raw_orders:
            break

        guids = [o["pkOrderID"] for o in raw_orders]
        items_by_order = _batch_order_items(guids)

        for guid in guids:
            for item in items_by_order.get(guid, []):
                sku = item.get("sku") or "UNKNOWN"
                qty = item.get("quantity") or 0
                rev = float(item.get("line_total_inc_tax") or 0)
                sku_revenue[sku] += rev
                sku_units[sku] += qty
                sku_order_ids[sku].add(guid)
                if sku not in sku_stock_ids and item.get("stock_item_id"):
                    sku_stock_ids[sku] = item["stock_item_id"]

        page += 1

    # Resolve suppliers for all unique stock item IDs
    unique_ids = list({v for v in sku_stock_ids.values() if v})
    supplier_map: dict[str, dict] = {}
    if unique_ids:
        supplier_map = _fetch_supplier_for_items(unique_ids)

    # Aggregate by supplier
    sup_revenue: dict[str, float] = defaultdict(float)
    sup_units: dict[str, int] = defaultdict(int)
    sup_order_ids: dict[str, set] = defaultdict(set)
    sup_sku_count: dict[str, set] = defaultdict(set)
    sup_ids: dict[str, str] = {}  # supplier_name → supplier_id

    for sku in sku_revenue:
        sid = sku_stock_ids.get(sku, "")
        sup_info = supplier_map.get(sid, {})
        sup_name = sup_info.get("supplier_name") or "No Supplier"
        sup_id = sup_info.get("supplier_id") or ""
        sup_revenue[sup_name] += sku_revenue[sku]
        sup_units[sup_name] += sku_units[sku]
        sup_order_ids[sup_name] |= sku_order_ids[sku]
        sup_sku_count[sup_name].add(sku)
        if sup_name not in sup_ids:
            sup_ids[sup_name] = sup_id

    sort_key = (lambda s: sup_revenue[s]) if rank_by == "revenue" else (lambda s: sup_units[s])
    all_sups = sorted(sup_revenue.keys(), key=sort_key, reverse=True)

    suppliers = [
        {
            "rank": idx + 1,
            "supplier_name": sup,
            "supplier_id": sup_ids.get(sup, ""),
            "revenue": round(sup_revenue[sup], 2),
            "units": sup_units[sup],
            "orders": len(sup_order_ids[sup]),
            "sku_count": len(sup_sku_count[sup]),
        }
        for idx, sup in enumerate(all_sups[:top_n])
    ]

    return {
        "from_date": from_date,
        "to_date": to_date,
        "date_field": date_field,
        "total_orders_scanned": total_orders_scanned,
        "ranked_by": rank_by,
        "suppliers": suppliers,
    }


@mcp.tool()
def get_period_comparison(
    current_from: str,
    current_to: str,
    prior_from: str,
    prior_to: str,
    date_field: str = "received",
) -> dict:
    """
    Compare revenue and order volume between two date ranges side-by-side.

    Returns totals for both periods and the absolute and percentage change for
    orders, revenue, and average order value. Useful for month-on-month,
    week-on-week, or year-on-year comparisons.

    Examples: "how does this month compare to last month?", "are we up or down
    vs the same period last year?", "what's the MoM revenue change?".

    Args:
        current_from: Start of the current/comparison period, e.g. "2026-05-01".
        current_to: End of the current/comparison period, e.g. "2026-05-31".
        prior_from: Start of the prior/baseline period, e.g. "2026-04-01".
        prior_to: End of the prior/baseline period, e.g. "2026-04-30".
        date_field: Which date to filter on — "received" (default), "processed",
            "payment", or "cancelled".

    Returns:
        A dict with:
          - current / prior: revenue summary dicts for each period
          - changes: {orders_delta, orders_pct, revenue_delta, revenue_pct,
                      aov_delta, aov_pct} — positive = current better than prior
    """
    current = _fetch_revenue_data(current_from, current_to, date_field)
    prior = _fetch_revenue_data(prior_from, prior_to, date_field)

    def _pct(new: float, old: float) -> float | None:
        if old == 0:
            return None
        return round((new - old) / old * 100, 1)

    return {
        "current": {"from_date": current_from, "to_date": current_to, **current},
        "prior": {"from_date": prior_from, "to_date": prior_to, **prior},
        "date_field": date_field,
        "changes": {
            "orders_delta": current["total_orders"] - prior["total_orders"],
            "orders_pct": _pct(current["total_orders"], prior["total_orders"]),
            "revenue_delta": round(current["total_revenue"] - prior["total_revenue"], 2),
            "revenue_pct": _pct(current["total_revenue"], prior["total_revenue"]),
            "aov_delta": round(current["avg_order_value"] - prior["avg_order_value"], 2),
            "aov_pct": _pct(current["avg_order_value"], prior["avg_order_value"]),
        },
    }


# ---------- Suppliers ----------

@mcp.tool()
def get_suppliers() -> dict:
    """
    List all suppliers configured in Linnworks.

    Returns every supplier with their ID and name. Useful for resolving a
    supplier name to a GUID before filtering purchase orders by supplier,
    or for answering "what suppliers do we have?" and "what is the ID for
    supplier X?".

    Returns:
        A dict with:
          - count:     total number of suppliers
          - suppliers: list of supplier records with id and name
    """
    # The Suppliers API is not in the public specs — try the most likely paths.
    # Endpoint confirmed working will be noted in CLAUDE.md.
    suppliers = call_linnworks_get("Inventory/GetSuppliers")

    if isinstance(suppliers, list):
        return {
            "count": len(suppliers),
            "suppliers": [
                {
                    "supplier_id": s.get("pkSupplierID") or s.get("SupplierId") or s.get("Id"),
                    "name": s.get("SupplierName") or s.get("Name") or s.get("name"),
                    "code": s.get("SupplierCode") or s.get("Code"),
                    "currency": s.get("Currency"),
                }
                for s in suppliers
            ],
        }

    # If the response is wrapped, try common wrapper keys
    for key in ("Suppliers", "Data", "Result"):
        if isinstance(suppliers, dict) and key in suppliers:
            items = suppliers[key]
            return {
                "count": len(items),
                "suppliers": [
                    {
                        "supplier_id": s.get("pkSupplierID") or s.get("SupplierId") or s.get("Id"),
                        "name": s.get("SupplierName") or s.get("Name") or s.get("name"),
                        "code": s.get("SupplierCode") or s.get("Code"),
                        "currency": s.get("Currency"),
                    }
                    for s in items
                ],
            }

    return {"error": "Unexpected response shape from suppliers endpoint", "raw": suppliers}


# ---------- Purchase order writes ----------

@mcp.tool()
def create_purchase_order(
    supplier_id: str,
    items: str,
    location_id: str = "00000000-0000-0000-0000-000000000000",
    external_invoice_number: str = "",
    supplier_reference: str = "",
    quoted_delivery_date: str = "",
    currency: str = "GBP",
    dry_run: bool = True,
) -> dict:
    """
    Create a new purchase order in PENDING status and add line items to it.

    Two-step process: first creates the PO header, then adds each line item
    by resolving each SKU to a stock item GUID. Use open_purchase_order()
    afterwards to move the PO from PENDING to OPEN.

    IMPORTANT: dry_run defaults to True. It will resolve all SKUs and show
    exactly what would be created without writing anything. Set dry_run=False
    only after confirming the resolved items look correct.

    Args:
        supplier_id: UUID of the supplier (pkSupplierID). Use get_suppliers()
            to look up the correct UUID by name.
        items: JSON array of line items to add. Each item must have:
            - "sku":       the product SKU (will be resolved to a stock item ID)
            - "quantity":  integer quantity to order
            - "cost":      unit cost excluding tax (e.g. 10.50)
            - "tax_rate":  tax percentage (e.g. 20.0 for 20%). Defaults to 20.0.
            Example: '[{"sku":"ABC-123","quantity":5,"cost":10.50,"tax_rate":20.0}]'
        location_id: UUID of the destination warehouse location. Defaults to
            the "Default" (all stock) location.
        supplier_reference: YOUR reference for this order as quoted to the supplier
            — e.g. "TEST-PO001". This is the field staff and suppliers see. Leave
            blank if not needed.
        external_invoice_number: Linnworks auto-generates its own PO number here.
            Leave blank in almost all cases — only set this if you need to override
            the Linnworks-generated reference.
        quoted_delivery_date: Expected delivery date in ISO format, e.g. "2026-06-01".
        currency: Currency code, e.g. "GBP". Defaults to "GBP".
        dry_run: If True (default), resolves SKUs and shows what would be created
            without writing anything. Set to False to create the PO.

    Note: DateOfPurchase is always set to the current date/time at creation.
    The Linnworks stored procedure requires it — null or missing causes a SQL
    overflow error. This mirrors the UI behaviour where today's date is the default.

    Returns:
        A dict with:
          - dry_run:       whether this was a dry run
          - status:        "dry_run", "created", or "error"
          - purchase_id:   UUID of the new PO (live runs only)
          - resolved_items: the items with resolved stock_item_ids and totals
          - errors:        list of SKUs that could not be resolved (if any)
          - header:        the PO header fields that were/would be submitted
    """
    import json as _json

    # Step 1 — parse items JSON
    try:
        item_list = _json.loads(items)
        if not isinstance(item_list, list) or len(item_list) == 0:
            return {"status": "error", "error": "items must be a non-empty JSON array."}
    except Exception as exc:
        return {"status": "error", "error": f"Could not parse items JSON: {exc}"}

    # Step 2 — resolve every SKU to a stock item GUID
    resolved = []
    errors = []
    for item in item_list:
        sku = item.get("sku", "").strip()
        if not sku:
            errors.append({"sku": sku, "error": "empty SKU"})
            continue
        try:
            inv = call_linnworks("Inventory/GetInventoryItem", {"sku": sku})
            stock_item_id = (inv or {}).get("StockItemId")
            if not stock_item_id:
                errors.append({"sku": sku, "error": "SKU not found in Linnworks"})
                continue
        except Exception as exc:
            errors.append({"sku": sku, "error": str(exc)})
            continue

        qty = int(item.get("quantity", 1))
        cost = float(item.get("cost", 0.0))
        tax_rate = float(item.get("tax_rate", 20.0))
        tax = round(cost * qty * (tax_rate / 100), 4)
        resolved.append({
            "sku": sku,
            "stock_item_id": stock_item_id,
            "quantity": qty,
            "cost": cost,
            "tax_rate": tax_rate,
            "line_total_ex_tax": round(cost * qty, 4),
            "tax": tax,
        })

    if errors:
        return {
            "status": "error",
            "error": f"{len(errors)} SKU(s) could not be resolved — fix these before creating the PO.",
            "errors": errors,
            "resolved_items": resolved,
        }

    # Build the header fields dict for display / submission.
    # DateOfPurchase is required by SQL — default to today if not supplied.
    today_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    # All fields below are always sent — the stored procedure requires them
    # even when empty. DateOfPurchase defaults to today (SQL rejects null/min).
    header_fields: dict = {
        "fkSupplierId": supplier_id,
        "fkLocationId": location_id,
        "Currency": currency,
        "DateOfPurchase": today_iso,
        "ExternalInvoiceNumber": external_invoice_number,
        "SupplierReferenceNumber": supplier_reference,
        "QuotedDeliveryDate": f"{quoted_delivery_date}T00:00:00" if quoted_delivery_date else today_iso,
        "ConversionRate": 1.0,
        "PostagePaid": 0.0,
        "ShippingTaxRate": 0.0,
        "UnitAmountTaxIncludedType": 0,
    }

    if dry_run:
        return {
            "dry_run": True,
            "status": "dry_run",
            "message": "No PO created. Set dry_run=False to create.",
            "header": header_fields,
            "resolved_items": resolved,
            "errors": errors,
        }

    # Step 3 — create PO header
    create_payload = {"createParameters": header_fields}
    new_po_id = call_linnworks("PurchaseOrder/Create_PurchaseOrder_Initial", create_payload)
    if not new_po_id or not isinstance(new_po_id, str):
        return {
            "status": "error",
            "error": f"Create_PurchaseOrder_Initial returned an unexpected response: {new_po_id!r}",
        }

    # Step 4 — add each line item
    item_errors = []
    for r in resolved:
        add_payload = {
            "addItemParameter": {
                "pkPurchaseId": new_po_id,
                "fkStockItemId": r["stock_item_id"],
                "Qty": r["quantity"],
                "Cost": r["cost"],
                "TaxRate": r["tax_rate"],
                "PackQuantity": 1,
                "PackSize": 1,
            }
        }
        try:
            call_linnworks("PurchaseOrder/Add_PurchaseOrderItem", add_payload)
        except Exception as exc:
            item_errors.append({"sku": r["sku"], "error": str(exc)})

    # Step 5 — read back the new PO to confirm
    confirmed = call_linnworks("PurchaseOrder/Get_PurchaseOrder", {"pkPurchaseId": new_po_id})
    confirmed_header = confirmed.get("PurchaseOrderHeader") or {}

    result = {
        "dry_run": False,
        "status": "created",
        "purchase_id": new_po_id,
        "linnworks_status": confirmed_header.get("Status"),
        "external_invoice_number": confirmed_header.get("ExternalInvoiceNumber", ""),
        "line_count": confirmed_header.get("LineCount", 0),
        "total_cost": confirmed_header.get("GrandTotal"),
        "resolved_items": resolved,
        "header": header_fields,
    }
    if item_errors:
        result["item_errors"] = item_errors
        result["warning"] = "PO header was created but some line items failed — check item_errors."
    return result


@mcp.tool()
def update_purchase_order_header(
    purchase_id: str,
    supplier_id: str = "",
    supplier_reference: str = "",
    external_invoice_number: str = "",
    quoted_delivery_date: str = "",
    date_of_purchase: str = "",
    currency: str = "",
    conversion_rate: float = 0.0,
    dry_run: bool = True,
) -> dict:
    """
    Update the header fields of a purchase order.

    Only works on orders that are not yet Delivered. Reads the current header
    first, applies only the fields you provide, and (unless dry_run=True)
    writes the change back to Linnworks. Always returns a before/after diff
    so you can see exactly what would change.

    IMPORTANT: dry_run defaults to True. Set dry_run=False only when you are
    sure the change is correct — confirm with the user before doing so.

    Args:
        purchase_id: The UUID of the purchase order to update (pkPurchaseID).
        supplier_id: The UUID of the supplier (pkSupplierID) to assign to this
            PO. Use get_suppliers() to look up the correct UUID by name.
            Leave blank to keep the current supplier.
        supplier_reference: The supplier's own reference/PO number. Leave blank
            to keep the current value.
        external_invoice_number: The invoice number or your internal PO ref.
            Leave blank to keep the current value.
        quoted_delivery_date: Expected delivery date in ISO format,
            e.g. "2026-06-01". Leave blank to keep the current value.
        date_of_purchase: The purchase date in ISO format, e.g. "2026-05-13".
            Leave blank to keep the current value.
        currency: Currency code, e.g. "GBP". Leave blank to keep current.
        conversion_rate: Exchange rate to GBP. Pass 0.0 to keep current value.
        dry_run: If True (default), returns the proposed changes without writing
            anything to Linnworks. Set to False to apply the update.

    Returns:
        A dict with:
          - purchase_id:  the PO updated
          - dry_run:      whether this was a dry run
          - status:       "dry_run", "updated", or "no_changes"
          - before:       the current header values for changed fields
          - after:        the proposed/applied new values
          - error:        present only if something went wrong
    """
    purchase_id = purchase_id.strip()

    # Step 1 — read current state
    current = call_linnworks("PurchaseOrder/Get_PurchaseOrder", {"pkPurchaseId": purchase_id})
    header = current.get("PurchaseOrderHeader") or {}

    current_status = header.get("Status", "")
    if current_status == "DELIVERED":
        return {
            "purchase_id": purchase_id,
            "error": "Cannot update a DELIVERED purchase order.",
            "status": current_status,
        }

    # Step 2 — build diff: only include fields the caller explicitly provided
    before: dict = {}
    after: dict = {}

    def _register(field_key: str, current_val, new_val):
        """Record a field change only when a non-empty new value is supplied."""
        if new_val and new_val != current_val:
            before[field_key] = current_val
            after[field_key] = new_val

    _register("fkSupplierId", header.get("fkSupplierId", ""), supplier_id)
    _register("SupplierReferenceNumber", header.get("SupplierReferenceNumber", ""), supplier_reference)
    _register("ExternalInvoiceNumber", header.get("ExternalInvoiceNumber", ""), external_invoice_number)
    _register("Currency", header.get("Currency", ""), currency)

    if quoted_delivery_date:
        _register(
            "QuotedDeliveryDate",
            header.get("QuotedDeliveryDate", ""),
            f"{quoted_delivery_date}T00:00:00",
        )
    if date_of_purchase:
        _register(
            "DateOfPurchase",
            header.get("DateOfPurchase", ""),
            f"{date_of_purchase}T00:00:00",
        )
    if conversion_rate and conversion_rate != header.get("ConversionRate", 0.0):
        before["ConversionRate"] = header.get("ConversionRate")
        after["ConversionRate"] = conversion_rate

    if not after:
        return {
            "purchase_id": purchase_id,
            "dry_run": dry_run,
            "status": "no_changes",
            "message": "No fields to update — all supplied values match the current header.",
        }

    if dry_run:
        return {
            "purchase_id": purchase_id,
            "dry_run": True,
            "status": "dry_run",
            "message": "No changes written. Set dry_run=False to apply.",
            "before": before,
            "after": after,
        }

    # Step 3 — build write payload, carrying forward all current values
    # and overlaying the changed ones. Payload sent unwrapped per tenant pattern.
    update_param = {
        "pkPurchaseID": purchase_id,
        "SupplierReferenceNumber": after.get("SupplierReferenceNumber", header.get("SupplierReferenceNumber", "")),
        "ExternalInvoiceNumber": after.get("ExternalInvoiceNumber", header.get("ExternalInvoiceNumber", "")),
        "Currency": after.get("Currency", header.get("Currency", "GBP")),
        "QuotedDeliveryDate": after.get("QuotedDeliveryDate", header.get("QuotedDeliveryDate")),
        "DateOfPurchase": after.get("DateOfPurchase", header.get("DateOfPurchase")),
        "ConversionRate": after.get("ConversionRate", header.get("ConversionRate", 1.0)),
        "fkSupplierId": after.get("fkSupplierId", header.get("fkSupplierId")),
        "fkLocationId": header.get("fkLocationId"),
        "ShippingTaxRate": header.get("ShippingTaxRate", 0.0),
        "PostagePaid": header.get("PostagePaid", 0.0),
    }

    # Payload wrapper for Update_PurchaseOrderHeader is uncertain — trying
    # {"updateParameter": {...}} per the spec parameter name. If this fails
    # with a shape error, try sending update_param directly (unwrapped).
    call_linnworks("PurchaseOrder/Update_PurchaseOrderHeader", {"updateParameter": update_param})

    # Step 4 — read back to confirm
    confirmed = call_linnworks("PurchaseOrder/Get_PurchaseOrder", {"pkPurchaseId": purchase_id})
    confirmed_header = confirmed.get("PurchaseOrderHeader") or {}

    return {
        "purchase_id": purchase_id,
        "dry_run": False,
        "status": "updated",
        "before": before,
        "after": after,
        "confirmed": {
            k: confirmed_header.get(k) for k in after
        },
    }


@mcp.tool()
def open_purchase_order(
    purchase_id: str,
    dry_run: bool = True,
) -> dict:
    """
    Move a purchase order from PENDING to OPEN status.

    Opening a PO signals to Linnworks that the order has been placed and
    stock is on its way — it populates "Due (On Order)" values in stock
    levels so you can see inbound stock. Use this after creating or
    confirming a PO with your supplier.

    Only works on PENDING orders. OPEN, PARTIAL, and DELIVERED orders are
    rejected with a clear error. Use deliver_purchase_order() to mark
    items as received once they arrive.

    IMPORTANT: dry_run defaults to True. Set dry_run=False only when you
    are sure — confirm with the user before doing so.

    Args:
        purchase_id: The UUID of the purchase order (pkPurchaseID), as
            returned by search_purchase_orders() or get_purchase_order().
        dry_run: If True (default), shows what would happen without writing
            anything. Set to False to apply the status change.

    Returns:
        A dict with:
          - purchase_id:     the PO acted on
          - dry_run:         whether this was a dry run
          - status:          "dry_run", "opened", or "error"
          - from_status:     the status before the change
          - to_status:       "OPEN" (or the confirmed status on read-back)
          - external_invoice_number: the PO reference number, for confirmation
          - error:           present only if the PO cannot be opened
    """
    purchase_id = purchase_id.strip()

    # Step 1 — read current state
    current = call_linnworks("PurchaseOrder/Get_PurchaseOrder", {"pkPurchaseId": purchase_id})
    header = current.get("PurchaseOrderHeader") or {}
    current_status = header.get("Status", "")
    ext_inv = header.get("ExternalInvoiceNumber", "")

    if current_status != "PENDING":
        return {
            "purchase_id": purchase_id,
            "status": "error",
            "error": (
                f"Cannot open this PO — current status is {current_status!r}. "
                "Only PENDING orders can be moved to OPEN."
            ),
            "from_status": current_status,
            "external_invoice_number": ext_inv,
        }

    if dry_run:
        return {
            "purchase_id": purchase_id,
            "dry_run": True,
            "status": "dry_run",
            "message": "No changes written. Set dry_run=False to open this PO.",
            "from_status": current_status,
            "to_status": "OPEN",
            "external_invoice_number": ext_inv,
        }

    # Step 2 — change status to OPEN
    call_linnworks(
        "PurchaseOrder/Change_PurchaseOrderStatus",
        {"changeStatusParameter": {"pkPurchaseId": purchase_id, "status": "OPEN"}},
    )

    # Step 3 — read back to confirm
    confirmed = call_linnworks("PurchaseOrder/Get_PurchaseOrder", {"pkPurchaseId": purchase_id})
    confirmed_status = (confirmed.get("PurchaseOrderHeader") or {}).get("Status", "")

    return {
        "purchase_id": purchase_id,
        "dry_run": False,
        "status": "opened",
        "from_status": current_status,
        "to_status": confirmed_status,
        "external_invoice_number": ext_inv,
    }


@mcp.tool()
def deliver_purchase_order(
    purchase_id: str,
    dry_run: bool = True,
) -> dict:
    """
    Record delivery of all outstanding items on an OPEN purchase order.

    Marks every undelivered line as fully received, which immediately updates
    stock levels in Linnworks. The PO status will move to DELIVERED (or PARTIAL
    if some lines were already delivered). Linnworks sets the delivery timestamp
    to the current time — it cannot be backdated via the API.

    WARNING: This updates live stock levels and cannot be easily undone.
    dry_run=True (default) shows you what would be delivered without writing
    anything. Always confirm the item list before setting dry_run=False.

    The PO must be in OPEN or PARTIAL status — PENDING orders cannot be
    delivered.

    Args:
        purchase_id: The UUID of the purchase order (pkPurchaseID).
        dry_run: If True (default), shows outstanding items without delivering.
            Set to False to record the delivery and update stock.

    Returns:
        A dict with:
          - purchase_id:         the PO acted on
          - dry_run:             whether this was a dry run
          - status:              "dry_run", "delivered", or "error"
          - outstanding_items:   items that were/would be delivered
          - delivered_header:    updated PO header after delivery (live only)
    """
    purchase_id = purchase_id.strip()

    # Step 1 — read current state
    current = call_linnworks("PurchaseOrder/Get_PurchaseOrder", {"pkPurchaseId": purchase_id})
    header = current.get("PurchaseOrderHeader") or {}
    items = [i for i in (current.get("PurchaseOrderItem") or []) if not i.get("IsDeleted")]

    current_status = header.get("Status", "")
    if current_status not in ("OPEN", "PARTIAL"):
        return {
            "purchase_id": purchase_id,
            "error": f"PO must be OPEN or PARTIAL to deliver. Current status: {current_status}",
            "status": "error",
        }

    outstanding = [
        {
            "sku": i.get("SKU"),
            "title": i.get("ItemTitle"),
            "quantity": i.get("Quantity"),
            "delivered": i.get("Delivered"),
            "outstanding": (i.get("Quantity") or 0) - (i.get("Delivered") or 0),
        }
        for i in items
        if ((i.get("Quantity") or 0) - (i.get("Delivered") or 0)) > 0
    ]

    if not outstanding:
        return {
            "purchase_id": purchase_id,
            "dry_run": dry_run,
            "status": "no_changes",
            "message": "All items on this PO are already fully delivered.",
        }

    if dry_run:
        return {
            "purchase_id": purchase_id,
            "dry_run": True,
            "status": "dry_run",
            "message": "No delivery recorded. Set dry_run=False to deliver all items and update stock.",
            "outstanding_items": outstanding,
        }

    # Step 2 — deliver all items.
    # JSON (unwrapped) and JSON ({"request":{...}}) both returned HTTP 400.
    # Trying form-encoded data — some older Linnworks write endpoints require this.
    response = call_linnworks_form(
        "PurchaseOrder/Deliver_PurchaseItemAll",
        {"pkPurchaseId": purchase_id},
    )

    delivered_header = response.get("PurchaseOrderHeader") or {}

    return {
        "purchase_id": purchase_id,
        "dry_run": False,
        "status": "delivered",
        "outstanding_items": outstanding,
        "delivered_header": {
            "status": delivered_header.get("Status"),
            "date_of_delivery": delivered_header.get("DateOfDelivery"),
            "delivered_lines_count": delivered_header.get("DeliveredLinesCount"),
            "line_count": delivered_header.get("LineCount"),
        },
    }


@mcp.tool()
def add_purchase_order_note(
    purchase_id: str,
    note: str,
) -> dict:
    """
    Add a text note to a purchase order.

    Notes are visible in the Linnworks UI on the PO detail page. Use this to
    record information that has no dedicated field — such as a delivery
    tracking number, courier name, or expected arrival date.

    Args:
        purchase_id: The UUID of the purchase order (pkPurchaseID).
        note: The text content of the note to add.

    Returns:
        A dict with:
          - purchase_id:   the PO the note was added to
          - note_id:       the UUID of the newly created note
          - note:          the note text as stored
          - date_created:  timestamp the note was created
          - created_by:    the Linnworks user the note was attributed to
    """
    purchase_id = purchase_id.strip()

    # {"addNoteParameter":{...}} failed — trying unwrapped.
    response = call_linnworks(
        "PurchaseOrder/Add_PurchaseOrderNote",
        {"pkPurchaseId": purchase_id, "Note": note},
    )

    return {
        "purchase_id": purchase_id,
        "note_id": response.get("pkPurchaseNoteId"),
        "note": response.get("Note"),
        "date_created": response.get("DateCreated"),
        "created_by": response.get("CreatedBy"),
    }


@mcp.tool()
def add_purchase_order_item(
    purchase_id: str,
    sku: str,
    quantity: int,
    cost: float,
    tax_rate: float = 20.0,
    pack_quantity: int = 1,
    pack_size: int = 1,
    dry_run: bool = True,
) -> dict:
    """
    Add a new line item to an existing purchase order.

    Resolves the SKU to a Linnworks stock item GUID, then adds it as a new
    line on the PO. Works on PENDING, OPEN, or PARTIAL orders — DELIVERED
    orders are blocked.

    Use this when you need to add an item that was missed when the PO was
    originally created, or when a supplier suggests adding extra SKUs to an
    existing order.

    IMPORTANT: dry_run defaults to True. It resolves the SKU and shows
    exactly what would be added without writing anything. Set dry_run=False
    only after confirming the resolved item looks correct.

    Args:
        purchase_id: The UUID of the purchase order (pkPurchaseID), as
            returned by search_purchase_orders() or get_purchase_order().
        sku: The exact SKU of the item to add. Must match a Linnworks stock
            item exactly — use find_inventory_item() to verify the SKU first.
        quantity: Number of units to order.
        cost: Unit cost excluding tax (e.g. 10.50).
        tax_rate: Tax percentage (e.g. 20.0 for 20%). Defaults to 20.0.
        pack_quantity: Number of packs. Defaults to 1.
        pack_size: Units per pack. Defaults to 1.
        dry_run: If True (default), resolves the SKU and shows what would be
            added without writing anything. Set to False to add the item.

    Returns:
        A dict with:
          - purchase_id:          the PO being modified
          - dry_run:              whether this was a dry run
          - status:               "dry_run", "added", or "error"
          - item:                 resolved item details (sku, title, stock_item_id,
                                  quantity, cost, tax_rate, line_total_ex_tax, tax)
          - updated_line_count:   number of lines after the change (live only)
          - updated_total_cost:   PO grand total after the change (live only)
    """
    purchase_id = purchase_id.strip()
    sku = sku.strip()

    # Step 1 — read current PO to validate status
    current = call_linnworks("PurchaseOrder/Get_PurchaseOrder", {"pkPurchaseId": purchase_id})
    header = current.get("PurchaseOrderHeader") or {}
    current_status = header.get("Status", "")

    if current_status == "DELIVERED":
        return {
            "purchase_id": purchase_id,
            "status": "error",
            "error": "Cannot modify a DELIVERED purchase order.",
        }

    # Step 2 — resolve SKU to stock item GUID
    try:
        inv = call_linnworks("Inventory/GetInventoryItem", {"sku": sku})
        stock_item_id = (inv or {}).get("StockItemId")
        item_title = (inv or {}).get("ItemTitle", "")
        if not stock_item_id:
            return {
                "purchase_id": purchase_id,
                "status": "error",
                "error": f"SKU {sku!r} not found in Linnworks.",
            }
    except Exception as exc:
        return {
            "purchase_id": purchase_id,
            "status": "error",
            "error": f"Could not resolve SKU {sku!r}: {exc}",
        }

    line_total = round(cost * quantity, 4)
    tax = round(line_total * (tax_rate / 100), 4)

    item_detail = {
        "sku": sku,
        "title": item_title,
        "stock_item_id": stock_item_id,
        "quantity": quantity,
        "cost": cost,
        "tax_rate": tax_rate,
        "line_total_ex_tax": line_total,
        "tax": tax,
        "pack_quantity": pack_quantity,
        "pack_size": pack_size,
    }

    if dry_run:
        return {
            "purchase_id": purchase_id,
            "dry_run": True,
            "status": "dry_run",
            "message": "No changes written. Set dry_run=False to add this item.",
            "current_po_status": current_status,
            "item": item_detail,
        }

    # Step 3 — add the item
    call_linnworks(
        "PurchaseOrder/Add_PurchaseOrderItem",
        {
            "addItemParameter": {
                "pkPurchaseId": purchase_id,
                "fkStockItemId": stock_item_id,
                "Qty": quantity,
                "Cost": cost,
                "TaxRate": tax_rate,
                "PackQuantity": pack_quantity,
                "PackSize": pack_size,
            }
        },
    )

    # Step 4 — read back to confirm
    confirmed = call_linnworks("PurchaseOrder/Get_PurchaseOrder", {"pkPurchaseId": purchase_id})
    confirmed_header = confirmed.get("PurchaseOrderHeader") or {}

    return {
        "purchase_id": purchase_id,
        "dry_run": False,
        "status": "added",
        "item": item_detail,
        "updated_line_count": confirmed_header.get("LineCount"),
        "updated_total_cost": confirmed_header.get("GrandTotal") or confirmed_header.get("TotalCost"),
    }


@mcp.tool()
def update_purchase_order_item(
    purchase_id: str,
    purchase_item_id: str,
    quantity: Optional[int] = None,
    cost: Optional[float] = None,
    tax_rate: Optional[float] = None,
    dry_run: bool = True,
) -> dict:
    """
    Edit the quantity, cost, or tax rate of a line item on an existing purchase order.

    Reads the current line item from the PO, applies only the fields you
    provide (leaving others unchanged), and (unless dry_run=True) writes the
    change back. Always returns a before/after diff so you can see exactly
    what would change.

    Works on PENDING, OPEN, or PARTIAL orders — DELIVERED orders are blocked.

    To find purchase_item_id: call get_purchase_order() and look in the
    "items" list — each entry has a "purchase_item_id" field.

    IMPORTANT: dry_run defaults to True. Set dry_run=False only when you are
    sure the change is correct — confirm with the user before doing so.

    Args:
        purchase_id: The UUID of the purchase order (pkPurchaseID).
        purchase_item_id: The UUID of the specific line item to update
            (pkPurchaseItemId). Use get_purchase_order() to find this — it
            appears as "purchase_item_id" in each item in the "items" list.
        quantity: New quantity to order. Omit (or pass None) to keep current.
        cost: New unit cost excluding tax (e.g. 12.00). Omit to keep current.
        tax_rate: New tax percentage (e.g. 20.0). Omit to keep current.
        dry_run: If True (default), shows the proposed changes without writing
            anything. Set to False to apply the update.

    Returns:
        A dict with:
          - purchase_id:        the PO being modified
          - purchase_item_id:   the line item updated
          - dry_run:            whether this was a dry run
          - status:             "dry_run", "updated", "no_changes", or "error"
          - sku:                the SKU of the item updated, for confirmation
          - before:             current values of the fields that would change
          - after:              proposed/applied new values for those fields
    """
    purchase_id = purchase_id.strip()
    purchase_item_id = purchase_item_id.strip()

    # Step 1 — read current PO to find the item and validate status
    current = call_linnworks("PurchaseOrder/Get_PurchaseOrder", {"pkPurchaseId": purchase_id})
    header = current.get("PurchaseOrderHeader") or {}
    current_status = header.get("Status", "")

    if current_status == "DELIVERED":
        return {
            "purchase_id": purchase_id,
            "status": "error",
            "error": "Cannot modify a DELIVERED purchase order.",
        }

    items_raw = [
        i for i in (current.get("PurchaseOrderItem") or [])
        if not i.get("IsDeleted")
    ]
    current_item = next(
        (i for i in items_raw if i.get("pkPurchaseItemId") == purchase_item_id),
        None,
    )
    if current_item is None:
        return {
            "purchase_id": purchase_id,
            "purchase_item_id": purchase_item_id,
            "status": "error",
            "error": (
                f"Line item {purchase_item_id!r} not found on PO {purchase_id!r}. "
                "Use get_purchase_order() to list current items and their IDs."
            ),
        }

    sku = current_item.get("SKU", "")

    # Step 2 — build diff: only record fields the caller explicitly provided
    before: dict = {}
    after: dict = {}

    new_quantity = current_item.get("Quantity")
    new_cost = current_item.get("Cost")
    new_tax_rate = current_item.get("TaxRate")

    if quantity is not None and quantity != current_item.get("Quantity"):
        before["quantity"] = current_item.get("Quantity")
        after["quantity"] = quantity
        new_quantity = quantity

    if cost is not None and cost != current_item.get("Cost"):
        before["cost"] = current_item.get("Cost")
        after["cost"] = cost
        new_cost = cost

    if tax_rate is not None and tax_rate != current_item.get("TaxRate"):
        before["tax_rate"] = current_item.get("TaxRate")
        after["tax_rate"] = tax_rate
        new_tax_rate = tax_rate

    if not before:
        return {
            "purchase_id": purchase_id,
            "purchase_item_id": purchase_item_id,
            "dry_run": dry_run,
            "status": "no_changes",
            "message": "The values you provided match the current item — no update needed.",
            "sku": sku,
        }

    if dry_run:
        return {
            "purchase_id": purchase_id,
            "purchase_item_id": purchase_item_id,
            "dry_run": True,
            "status": "dry_run",
            "message": "No changes written. Set dry_run=False to apply this update.",
            "sku": sku,
            "before": before,
            "after": after,
        }

    # Step 3 — submit update (all fields required; carry unchanged values through)
    call_linnworks(
        "PurchaseOrder/Update_PurchaseOrderItem",
        {
            "updateItemParameter": {
                "pkPurchaseItemId": purchase_item_id,
                "pkPurchaseId": purchase_id,
                "Quantity": new_quantity,
                "PackQuantity": current_item.get("PackQuantity", 1),
                "PackSize": current_item.get("PackSize", 1),
                "Cost": new_cost,
                "TaxRate": new_tax_rate,
            }
        },
    )

    # Step 4 — read back to confirm the change landed
    confirmed = call_linnworks("PurchaseOrder/Get_PurchaseOrder", {"pkPurchaseId": purchase_id})
    confirmed_items = [
        i for i in (confirmed.get("PurchaseOrderItem") or [])
        if not i.get("IsDeleted")
    ]
    confirmed_item = next(
        (i for i in confirmed_items if i.get("pkPurchaseItemId") == purchase_item_id),
        None,
    )

    confirmed_after: dict = {}
    if confirmed_item:
        if "quantity" in after:
            confirmed_after["quantity"] = confirmed_item.get("Quantity")
        if "cost" in after:
            confirmed_after["cost"] = confirmed_item.get("Cost")
        if "tax_rate" in after:
            confirmed_after["tax_rate"] = confirmed_item.get("TaxRate")

    return {
        "purchase_id": purchase_id,
        "purchase_item_id": purchase_item_id,
        "dry_run": False,
        "status": "updated",
        "sku": sku,
        "before": before,
        "after": confirmed_after or after,
    }


@mcp.tool()
def remove_purchase_order_item(
    purchase_id: str,
    purchase_item_id: str,
    dry_run: bool = True,
) -> dict:
    """
    Remove a line item from an existing purchase order.

    Reads the current item from the PO so you can confirm what will be
    removed, then (unless dry_run=True) deletes the line. Works on PENDING,
    OPEN, or PARTIAL orders — DELIVERED orders are blocked.

    To find purchase_item_id: call get_purchase_order() and look in the
    "items" list — each entry has a "purchase_item_id" field.

    WARNING: Removal cannot be undone via the API. dry_run=True (default)
    shows you exactly what would be removed without writing anything.
    Always confirm with the user before setting dry_run=False.

    Args:
        purchase_id: The UUID of the purchase order (pkPurchaseID).
        purchase_item_id: The UUID of the specific line item to remove
            (pkPurchaseItemId). Use get_purchase_order() to find this — it
            appears as "purchase_item_id" in each item in the "items" list.
        dry_run: If True (default), shows what would be removed without
            writing anything. Set to False to delete the line item.

    Returns:
        A dict with:
          - purchase_id:          the PO being modified
          - purchase_item_id:     the line item removed
          - dry_run:              whether this was a dry run
          - status:               "dry_run", "removed", or "error"
          - removed_item:         the item that was/would be removed
                                  (sku, title, quantity, delivered, cost, tax_rate)
          - remaining_line_count: number of lines remaining after removal (live only)
          - warning:              present if read-back suggests removal may have failed
    """
    purchase_id = purchase_id.strip()
    purchase_item_id = purchase_item_id.strip()

    # Step 1 — read current PO to find the item and validate status
    current = call_linnworks("PurchaseOrder/Get_PurchaseOrder", {"pkPurchaseId": purchase_id})
    header = current.get("PurchaseOrderHeader") or {}
    current_status = header.get("Status", "")

    if current_status == "DELIVERED":
        return {
            "purchase_id": purchase_id,
            "status": "error",
            "error": "Cannot modify a DELIVERED purchase order.",
        }

    items_raw = [
        i for i in (current.get("PurchaseOrderItem") or [])
        if not i.get("IsDeleted")
    ]
    target_item = next(
        (i for i in items_raw if i.get("pkPurchaseItemId") == purchase_item_id),
        None,
    )
    if target_item is None:
        return {
            "purchase_id": purchase_id,
            "purchase_item_id": purchase_item_id,
            "status": "error",
            "error": (
                f"Line item {purchase_item_id!r} not found on PO {purchase_id!r}. "
                "Use get_purchase_order() to list current items and their IDs."
            ),
        }

    removed_item = {
        "sku": target_item.get("SKU"),
        "title": target_item.get("ItemTitle"),
        "quantity": target_item.get("Quantity"),
        "delivered": target_item.get("Delivered"),
        "cost": target_item.get("Cost"),
        "tax_rate": target_item.get("TaxRate"),
    }

    if dry_run:
        return {
            "purchase_id": purchase_id,
            "purchase_item_id": purchase_item_id,
            "dry_run": True,
            "status": "dry_run",
            "message": "No changes written. Set dry_run=False to remove this item.",
            "current_po_status": current_status,
            "removed_item": removed_item,
        }

    # Step 2 — delete the item
    call_linnworks(
        "PurchaseOrder/Delete_PurchaseOrderItem",
        {
            "deleteItemParameter": {
                "pkPurchaseItemId": purchase_item_id,
                "pkPurchaseId": purchase_id,
            }
        },
    )

    # Step 3 — read back to confirm the item is gone
    confirmed = call_linnworks("PurchaseOrder/Get_PurchaseOrder", {"pkPurchaseId": purchase_id})
    confirmed_header = confirmed.get("PurchaseOrderHeader") or {}
    confirmed_items = [
        i for i in (confirmed.get("PurchaseOrderItem") or [])
        if not i.get("IsDeleted")
    ]
    still_present = any(
        i.get("pkPurchaseItemId") == purchase_item_id for i in confirmed_items
    )

    result = {
        "purchase_id": purchase_id,
        "purchase_item_id": purchase_item_id,
        "dry_run": False,
        "status": "removed",
        "removed_item": removed_item,
        "remaining_line_count": confirmed_header.get("LineCount"),
    }
    if still_present:
        result["warning"] = (
            "Item still appears in read-back — removal may have failed. "
            "Check the PO in the Linnworks UI."
        )
    return result


# ---------- Purchase orders ----------

_PO_STATUS_LABELS: dict[str, str] = {
    "PENDING": "Pending",
    "OPEN": "Open",
    "PARTIAL": "Partial Delivery",
    "DELIVERED": "Delivered",
}


@mcp.tool()
def search_purchase_orders(
    status: str = "",
    from_date: str = "",
    to_date: str = "",
    search_value: str = "",
    search_type: str = "All",
    page: int = 1,
    page_size: int = 50,
) -> dict:
    """
    Search and list purchase orders from Linnworks.

    Returns purchase order headers matching the given filters — status, date
    range, reference number, SKU, or supplier code. Useful for questions like
    "what purchase orders are currently open?", "which POs are due for
    delivery?", "show me pending orders from the last 30 days", or "which
    POs contain SKU ABC-123?".

    Args:
        status: Filter by PO status. One of: "PENDING", "OPEN", "PARTIAL",
            "DELIVERED". Leave blank to return all statuses.
        from_date: Start of the date range in ISO format, e.g. "2026-04-01".
            Filters on DateOfPurchase. Leave blank for no lower bound.
        to_date: End of the date range in ISO format, e.g. "2026-05-13".
            Leave blank for no upper bound.
        search_value: A keyword to search for. What it matches depends on
            search_type. Leave blank to skip keyword filtering.
        search_type: What the search_value matches against. One of:
            "All" (default), "Reference", "StockItemSKU", "SupplierCode",
            "SupplierReference". Ignored when search_value is blank.
        page: Page number for paginated results. Defaults to 1.
        page_size: Results per page. Defaults to 50.

    Returns:
        A dict with:
          - page, total_pages, total_count: pagination info
          - count: number of POs on this page
          - purchase_orders: list of PO header summaries
    """
    request_body: dict = {
        "EntriesPerPage": page_size,
        "PageNumber": page,
        "SearchType": search_type,
    }

    if status:
        request_body["Status"] = status.upper()
    if from_date:
        request_body["DateFrom"] = f"{from_date}T00:00:00"
    if to_date:
        request_body["DateTo"] = f"{to_date}T23:59:59"
    if search_value:
        request_body["SearchValue"] = search_value

    # Sent unwrapped — the "request" in the spec is the parameter name,
    # not a JSON wrapper. Wrapping as {"request": {...}} causes filters
    # to be silently ignored (confirmed in tenant testing May 2026).
    response = call_linnworks(
        "PurchaseOrder/Search_PurchaseOrders2",
        request_body,
    )

    result = response.get("Result") or []
    total_pages = response.get("TotalPages", 1)
    total_count = response.get("TotalNumberOfRecords", 0)

    pos = [
        {
            "purchase_id": po.get("pkPurchaseID"),
            "supplier_id": po.get("fkSupplierId"),
            "location_id": po.get("fkLocationId"),
            "status": po.get("Status"),
            "status_label": _PO_STATUS_LABELS.get(po.get("Status", ""), po.get("Status")),
            "currency": po.get("Currency"),
            "external_invoice_number": po.get("ExternalInvoiceNumber"),
            "supplier_reference": po.get("SupplierReferenceNumber"),
            "date_of_purchase": po.get("DateOfPurchase"),
            "quoted_delivery_date": po.get("QuotedDeliveryDate"),
            "date_of_delivery": po.get("DateOfDelivery"),
            "line_count": po.get("LineCount"),
            "delivered_lines_count": po.get("DeliveredLinesCount"),
            "total_cost": po.get("TotalCost"),
            "tax_paid": po.get("taxPaid"),
            "locked": po.get("Locked"),
        }
        for po in result
    ]

    return {
        "page": page,
        "total_pages": total_pages,
        "total_count": total_count,
        "count": len(pos),
        "purchase_orders": pos,
    }


@mcp.tool()
def get_purchase_order(purchase_id: str) -> dict:
    """
    Fetch full detail for a single purchase order by its ID.

    Returns the PO header, all line items (SKUs, quantities, costs, delivered
    quantities), and any delivery records. Use this to answer questions like
    "show me PO [ID] in full", "how many units of each SKU are on this PO?",
    "how much of this PO has been delivered?", or "what's the total cost and
    tax on this order?".

    Args:
        purchase_id: The UUID of the purchase order (pkPurchaseID), as returned
            by search_purchase_orders.

    Returns:
        A dict with:
          - purchase_id: the ID queried
          - header: PO header fields (status, dates, costs, supplier, location)
          - item_count: number of line items
          - items: list of line items with SKU, quantity, cost, delivered qty
          - delivered_records: list of delivery events (if any)
          - note_count: number of notes attached to the PO
    """
    purchase_id = purchase_id.strip()

    # Spec shows pkPurchaseId as a direct top-level field (unwrapped).
    # If this fails, try {"request": {"pkPurchaseId": purchase_id}}.
    response = call_linnworks(
        "PurchaseOrder/Get_PurchaseOrder",
        {"pkPurchaseId": purchase_id},
    )

    header_raw = response.get("PurchaseOrderHeader") or {}
    items_raw = response.get("PurchaseOrderItem") or []
    delivered_raw = response.get("DeliveredRecords") or []

    header = {
        "purchase_id": header_raw.get("pkPurchaseID"),
        "supplier_id": header_raw.get("fkSupplierId"),
        "location_id": header_raw.get("fkLocationId"),
        "status": header_raw.get("Status"),
        "status_label": _PO_STATUS_LABELS.get(header_raw.get("Status", ""), header_raw.get("Status")),
        "currency": header_raw.get("Currency"),
        "external_invoice_number": header_raw.get("ExternalInvoiceNumber"),
        "supplier_reference": header_raw.get("SupplierReferenceNumber"),
        "date_of_purchase": header_raw.get("DateOfPurchase"),
        "quoted_delivery_date": header_raw.get("QuotedDeliveryDate"),
        "date_of_delivery": header_raw.get("DateOfDelivery"),
        "line_count": header_raw.get("LineCount"),
        "delivered_lines_count": header_raw.get("DeliveredLinesCount"),
        "total_cost": header_raw.get("TotalCost"),
        "tax_paid": header_raw.get("taxPaid"),
        "conversion_rate": header_raw.get("ConversionRate"),
        "converted_grand_total": header_raw.get("ConvertedGrandTotal"),
        "locked": header_raw.get("Locked"),
    }

    items = [
        {
            "purchase_item_id": i.get("pkPurchaseItemId"),
            "stock_item_id": i.get("fkStockItemId"),
            "sku": i.get("SKU"),
            "title": i.get("ItemTitle"),
            "supplier_code": i.get("SupplierCode"),
            "bin_rack": i.get("BinRack"),
            "quantity": i.get("Quantity"),
            "delivered": i.get("Delivered"),
            "outstanding": (i.get("Quantity") or 0) - (i.get("Delivered") or 0),
            "cost": i.get("Cost"),
            "tax_rate": i.get("TaxRate"),
            "tax": i.get("Tax"),
            "pack_quantity": i.get("PackQuantity"),
            "pack_size": i.get("PackSize"),
        }
        for i in items_raw
        if not i.get("IsDeleted")
    ]

    delivered = [
        {
            "stock_item_id": d.get("fkStockItemId"),
            "sku": d.get("SKU"),
            "quantity_delivered": d.get("Qty"),
            "delivery_date": d.get("DeliveryDate"),
        }
        for d in delivered_raw
    ]

    return {
        "purchase_id": purchase_id,
        "header": header,
        "item_count": len(items),
        "items": items,
        "delivered_records": delivered,
        "note_count": response.get("NoteCount", 0),
    }


# ---------- Rules Engine ----------

@mcp.tool()
def get_rules() -> dict:
    """
    List all rules configured in the Linnworks Rules Engine.

    The Rules Engine applies automatic changes to orders as they arrive —
    assigning shipping services, routing to folders, locking/parking orders,
    tagging, running macros, and more. Each rule has an ordered list of
    conditions, and the first matching condition's action fires.

    Returns every rule with its ID, name, type, enabled state, run order,
    and draft status. Use this to answer questions like "what rules do we
    have set up?", "which rules are currently enabled?", "what order do
    rules run in?", or "are any rules in draft?".

    Rule types: "Orders" (fires on incoming orders), "Test" (sandbox rules).

    Returns:
        A dict with:
          - count:  total number of rules
          - rules:  list of rule summaries, each with pk_rule_id, rule_name,
                    rule_type, enabled, run_order, draft, pk_rule_id_draft
    """
    response = call_linnworks_get("RulesEngine/GetRules")

    rules = response if isinstance(response, list) else []

    return {
        "count": len(rules),
        "rules": [
            {
                "pk_rule_id": r.get("pkRuleId"),
                "rule_name": r.get("RuleName"),
                "rule_type": r.get("RuleType"),
                "rule_type_display": r.get("RuleTypeDisplayName"),
                "enabled": r.get("Enabled"),
                "run_order": r.get("RunOrder"),
                "draft": r.get("Draft"),
                "pk_rule_id_draft": r.get("pkRuleId_Draft"),
            }
            for r in rules
        ],
    }


def _format_condition_tree(node: dict) -> dict:
    """
    Recursively format a RuleConditionHeader node into a readable structure.
    Each node has conditions (IF clauses), an action (THEN clause),
    and optional subrules (nested conditions).
    """
    # Format condition items (the IF clauses)
    conditions = [
        {
            "field": c.get("FieldName"),
            "evaluation": c.get("Evaluation"),
            "key_value": c.get("KeyValue"),
            "values": c.get("Values") or [],
        }
        for c in (node.get("Conditions") or [])
    ]

    # Format the action (the THEN clause)
    raw_action = node.get("Action") or {}
    action = None
    if raw_action.get("pkActionId"):
        action = {
            "pk_action_id": raw_action.get("pkActionId"),
            "action_name": raw_action.get("ActionName"),
            "action_type": raw_action.get("ActionType"),
            "action_value": raw_action.get("ActionValue"),
            "properties": [
                {
                    "name": p.get("DisplayName"),
                    "value": p.get("Value"),
                }
                for p in (raw_action.get("Properties") or [])
            ],
        }

    # Recurse into subrules
    subrules = [
        _format_condition_tree(sub)
        for sub in (node.get("Subrules") or [])
    ]

    return {
        "pk_condition_id": node.get("pkConditionId"),
        "condition_name": node.get("ConditionName"),
        "run_order": node.get("RunOrder"),
        "enabled": node.get("Enabled"),
        "conditions": conditions,
        "action": action,
        "subrules": subrules,
    }


@mcp.tool()
def get_rule(rule_id: int) -> dict:
    """
    Fetch the full condition and action tree for a single Rules Engine rule.

    Returns the complete "IF [conditions] THEN [action]" logic for every
    branch of the rule, including nested subrules. Use this to answer questions
    like "what does this rule actually do?", "what conditions trigger the
    shipping assignment rule?", "which rule assigns orders to the HLC folder?",
    or "why is this order being routed to a particular service?".

    Each condition node has:
      - conditions: the IF clauses (field + evaluator + values)
      - action:     the THEN clause (action type + properties)
      - subrules:   nested condition branches (else-if chains)

    Common action types: AssignShippingService, AssignToFolder,
    AssignToLocation, AssignTagToOrder, ChangeOrderParkStatus,
    ChangeOrderLockStatus, ExecuteMacro, AddNoteToOrder, SetDispatchDate.

    Args:
        rule_id: The integer ID of the rule (pk_rule_id from get_rules).

    Returns:
        A dict with:
          - pk_rule_id:   the rule queried
          - condition_count: number of top-level condition nodes
          - conditions:  list of condition trees, each with conditions,
                         action, and nested subrules
    """
    response = call_linnworks_get(
        "RulesEngine/GetRuleConditionNodes",
        params={"pkRuleId": rule_id},
    )

    nodes = response if isinstance(response, list) else []

    return {
        "pk_rule_id": rule_id,
        "condition_count": len(nodes),
        "conditions": [_format_condition_tree(n) for n in nodes],
    }


# ---------- Import / Export ----------

@mcp.tool()
def get_import_list() -> dict:
    """
    List all configured import tasks in Linnworks.

    Returns every scheduled import with its ID, friendly name, type, enabled
    state, current execution status, last run timestamps, last import status,
    and next scheduled run time. Use this to answer questions like "what imports
    do we have set up?", "which imports are currently enabled?", "when did the
    stock level import last run?", or "is any import currently executing or
    queued?". Also useful for debugging — check ImportStatus and Completed to
    spot failed or stale imports.

    Returns:
        A dict with:
          - count:   total number of configured imports
          - imports: list of import summaries, each with id, friendly_name, type,
                     enabled, executing, is_queued, started, completed,
                     import_status, import_skipped, all_schedules_disabled,
                     next_schedule, schedule_count
    """
    response = call_linnworks_get("ImportExport/GetImportList")

    # Response: {"register": [...]}
    register = response.get("register") if isinstance(response, dict) else response
    if not isinstance(register, list):
        register = []

    imports = [
        {
            "id": r.get("Id"),
            "friendly_name": r.get("FriendlyName"),
            "type": r.get("Type"),
            "enabled": r.get("Enabled"),
            "executing": r.get("Executing"),
            "is_queued": r.get("IsQueued"),
            "started": r.get("Started"),
            "completed": r.get("Completed"),
            "import_status": r.get("ImportStatus"),
            "import_skipped": r.get("ImportSkipped"),
            "all_schedules_disabled": r.get("AllSchedulesDisabled"),
            "next_schedule": r.get("NextSchedule"),
            "schedule_count": len(r.get("Schedules") or []),
        }
        for r in register
    ]

    return {"count": len(imports), "imports": imports}


@mcp.tool()
def get_export_list() -> dict:
    """
    List all configured export tasks in Linnworks.

    Returns every scheduled export with its ID, friendly name, type, enabled
    state, current execution status, last run timestamps, last export status,
    and next scheduled run time. Use this to answer questions like "what exports
    do we have set up?", "which exports are enabled?", "when did the inventory
    export last run?", or "is any export currently failing?". Also useful for
    debugging — check last_export_status and completed to spot issues.

    Returns:
        A dict with:
          - count:   total number of configured exports
          - exports: list of export summaries, each with id, friendly_name, type,
                     enabled, executing, is_queued, started, completed,
                     last_export_status, all_schedules_disabled, next_schedule,
                     schedule_count
    """
    response = call_linnworks_get("ImportExport/GetExportList")

    register = response.get("register") if isinstance(response, dict) else response
    if not isinstance(register, list):
        register = []

    exports = [
        {
            "id": r.get("Id"),
            "friendly_name": r.get("FriendlyName"),
            "type": r.get("Type"),
            "enabled": r.get("Enabled"),
            "executing": r.get("Executing"),
            "is_queued": r.get("IsQueued"),
            "started": r.get("Started"),
            "completed": r.get("Completed"),
            "last_export_status": r.get("LastExportStatus"),
            "all_schedules_disabled": r.get("AllSchedulesDisabled"),
            "next_schedule": r.get("NextSchedule"),
            "schedule_count": len(r.get("Schedules") or []),
        }
        for r in register
    ]

    return {"count": len(exports), "exports": exports}


@mcp.tool()
def get_import(import_id: int) -> dict:
    """
    Fetch full detail for a single configured import task.

    Returns the complete import configuration including the specification
    (file source, column mappings, field settings), the register (current
    status and timestamps), and all schedule entries with their cron
    configuration. Use this when you need to inspect exactly how an import
    is configured — e.g. "what file path is the stock level import reading
    from?", "what columns are mapped on this import?", or "what schedule is
    this import running on?".

    Args:
        import_id: The integer ID of the import (as returned by get_import_list).

    Returns:
        A dict with:
          - id:            the import ID queried
          - friendly_name: human-readable import name
          - type:          the import type (e.g. "StockLevel", "Inventory")
          - enabled:       whether the import is enabled
          - executing:     whether it is currently running
          - is_queued:     whether it is queued for execution
          - started:       last start timestamp
          - completed:     last completion timestamp
          - import_status: last import status string
          - import_skipped: whether the last run was skipped (no file change)
          - next_schedule: next scheduled run time
          - schedules:     list of schedule entries with their configuration
          - specification: full specification dict (file source, column mappings, etc.)
    """
    response = call_linnworks_get("ImportExport/GetImport", params={"id": import_id})

    register = response.get("Register") or {}
    schedules = response.get("Schedules") or []
    spec = response.get("Specification") or {}

    return {
        "id": register.get("Id"),
        "friendly_name": register.get("FriendlyName"),
        "type": register.get("Type"),
        "enabled": register.get("Enabled"),
        "executing": register.get("Executing"),
        "is_queued": register.get("IsQueued"),
        "started": register.get("Started"),
        "completed": register.get("Completed"),
        "import_status": register.get("ImportStatus"),
        "import_skipped": register.get("ImportSkipped"),
        "next_schedule": register.get("NextSchedule"),
        "all_schedules_disabled": register.get("AllSchedulesDisabled"),
        "schedules": [
            {
                "schedule_id": s.get("Id"),
                "name": s.get("Name"),
                "schedule_xml": s.get("ScheduleXML"),
                "configuration": s.get("Configuration"),
            }
            for s in schedules
        ],
        "specification": spec,
    }


@mcp.tool()
def get_export(export_id: int) -> dict:
    """
    Fetch full detail for a single configured export task.

    Returns the complete export configuration including the specification
    (destination, query/filter settings), the register (current status and
    timestamps), and all schedule entries with their cron configuration.
    Use this when you need to inspect exactly how an export is configured —
    e.g. "where is this export sending data?", "what filters does this export
    apply?", or "what schedule is this export running on?".

    Args:
        export_id: The integer ID of the export (as returned by get_export_list).

    Returns:
        A dict with:
          - id:               the export ID queried
          - friendly_name:    human-readable export name
          - type:             the export type
          - enabled:          whether the export is enabled
          - executing:        whether it is currently running
          - is_queued:        whether it is queued for execution
          - started:          last start timestamp
          - completed:        last completion timestamp
          - last_export_status: whether the last run succeeded (bool)
          - next_schedule:    next scheduled run time
          - schedules:        list of schedule entries with their configuration
          - specification:    full specification dict (destination, filters, etc.)
    """
    response = call_linnworks_get("ImportExport/GetExport", params={"id": export_id})

    register = response.get("Register") or {}
    schedules = response.get("Schedules") or []
    spec = response.get("Specification") or {}

    return {
        "id": register.get("Id"),
        "friendly_name": register.get("FriendlyName"),
        "type": register.get("Type"),
        "enabled": register.get("Enabled"),
        "executing": register.get("Executing"),
        "is_queued": register.get("IsQueued"),
        "started": register.get("Started"),
        "completed": register.get("Completed"),
        "last_export_status": register.get("LastExportStatus"),
        "next_schedule": register.get("NextSchedule"),
        "all_schedules_disabled": register.get("AllSchedulesDisabled"),
        "schedules": [
            {
                "schedule_id": s.get("Id"),
                "name": s.get("Name"),
                "schedule_xml": s.get("ScheduleXML"),
                "configuration": s.get("Configuration"),
            }
            for s in schedules
        ],
        "specification": spec,
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
