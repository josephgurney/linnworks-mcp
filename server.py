"""
Linnworks MCP Server — Phase 1 (local stdio)

A single-tenant MCP server that exposes Linnworks data to Claude Desktop.
Phase 1 = stdio transport, your machine only, no OAuth, no hosting.

Run via Claude Desktop after registering this script in claude_desktop_config.json.
See README.md for setup instructions.
"""
from __future__ import annotations

__version__ = "1.17.0"

import os
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Literal, Optional

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

def _require_credentials() -> None:
    """Exit with a clear error if Linnworks credentials are not configured.

    Called only on code paths that actually talk to Linnworks (running the MCP
    server over stdio, or `--check-auth`). Deliberately NOT run at import time,
    so the module can be imported credential-free for offline verification —
    `--list-tools`, tool-registration smoke tests, and the CLI build loop, none
    of which need live credentials. (Before this, importing the module without
    credentials called sys.exit(1), which blocked all offline introspection.)
    """
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

    # Several write endpoints (AddInventoryItem, UpdateInventoryItem,
    # CreateInventoryItemPrices, UpdateStockLevelsBulk, etc.) return a 2xx with
    # an EMPTY body on success. response.json() raises on empty text, which
    # would mis-report a successful write as an error — so treat an empty 2xx
    # body as a successful no-content result. Confirmed live 15 Jun 2026.
    if not response.text.strip():
        return {}

    # Some write endpoints return a 2xx with a NON-empty, NON-JSON body on
    # success — e.g. Create/UpdateInventoryItemExtendedProperties return a
    # plain-text body that json() rejects with "Expecting value: line 1
    # column 1 (char 0)". A non-raising 2xx already means the write succeeded,
    # so fall back to wrapping the raw text instead of mis-reporting it as a
    # failure. Confirmed 15 Jun 2026 (issue #13).
    try:
        return response.json()
    except ValueError:
        return {"raw": response.text}


def call_linnworks_void(method_path: str, payload: dict) -> None:
    """
    POST a Linnworks API call that returns 204 No Content (no response body).

    Used for endpoints like ImportExport/RunNowImport that signal success
    purely via HTTP 204. Raises RuntimeError on any non-2xx response.
    """
    global _token, _server
    token, server = ensure_auth()
    url = f"{server.rstrip('/')}/api/{method_path}"

    response = _session.post(
        url,
        json=payload,
        headers={"Authorization": token},
        timeout=60,
    )

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
        raise RuntimeError(
            f"Linnworks {method_path} failed: HTTP {response.status_code} — {response.text}"
        )
    # 204 No Content — success, nothing to return


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


# ---------- Write-safety framework ----------
#
# Two shared utilities used by every bulk write tool:
#
#   _write_guard()   — staged-manifest gate for large batches
#   _check_injection() — last-resort tripwire for obvious prompt injection
#
# Design intent
# ─────────────
# Neither utility is a comprehensive security system.  _write_guard prevents
# accidental large-scale writes by forcing the caller to review a manifest
# before executing.  _check_injection raises loudly on obvious adversarial
# patterns embedded in Linnworks data that has flowed into tool parameters —
# it is not an LLM alignment layer.
#
# The primary safety net for all write tools remains:
#   1. dry_run=True default
#   2. read-before-write + read-back-after
#   3. per-item result reporting so every change is visible

import re as _re

# Patterns that suggest injected instructions rather than product/order data.
# Matched case-insensitively against the stripped start (or anywhere for markers).
_INJECTION_PATTERNS: list[str] = [
    r"(?i)^ignore\s+(previous|all|above|prior)",
    r"(?i)^(system|assistant|user)\s*:",
    r"(?i)<\|.*?\|>",                           # <|im_start|> style markers
    r"(?i)\[/?INST\]",                          # LLaMA instruction tags
    r"(?i)###\s*(instruction|system|prompt)",
    r"(?i)^you are now\b",
    r"(?i)^forget\s+(everything|all|previous|prior)",
    r"(?i)^new\s+instruction",
    r"(?i)^act as\b",
    r"(?i)^disregard\s+(all|previous|prior|above)",
    r"---+\s*(system|instructions?|prompt)\b",  # separator + label pattern
]

# Per-operation staging thresholds.  Batches at or below the threshold proceed
# normally (dry_run default still applies).  Batches above require a staged
# manifest pass followed by confirmed_count echo-back.
WRITE_THRESHOLDS: dict[str, int] = {
    "set_stock_levels":               25,   # immediate channel availability impact
    "set_inventory_item_prices":      25,   # immediate channel price impact
    "create_or_update_inventory_item": 50,  # channel sync is async, less instant
    "set_extended_properties":        50,   # metadata, lower blast radius
    "set_inventory_item_descriptions": 50,  # content, lower blast radius
    "set_inventory_item_titles":      50,   # channel title overrides, lower blast radius
    "set_inventory_item_suppliers":   50,   # purchasing metadata, lower blast radius
    "add_inventory_item_images":      100,  # additive-only, no overwrites
    "delete_inventory_item_images":   10,   # IRREVERSIBLE — removes images from an item
    "set_inventory_item_image_order": 25,   # reorders images; main image = storefront hero
    "delete_inventory_item":          10,   # IRREVERSIBLE — lowest threshold of all
    "delete_purchase_order":          10,   # IRREVERSIBLE — deletes whole PO (header + all lines)
    "list_to_shopify":                25,   # creates live customer-facing channel listings
    "refresh_channel_listing":        25,   # re-pushes live customer-facing channel listings
    "unpublish_channel_listing":      10,   # TAKES DOWN live customer-facing listings — destructive
    "delist_all_shopify_listings":    10,   # TAKES DOWN every Shopify listing for an item — destructive
    "delete_categories":              10,   # IRREVERSIBLE — deletes categories (non-empty → items reassigned)
    "delete_empty_categories":        10,   # IRREVERSIBLE — bulk-deletes empty categories
    "archive_inventory_items":        25,   # hides items from channels; reversible via unarchive
    "unarchive_inventory_items":      25,   # restores items to active; reversible via archive
    "default":                        25,   # fallback for any unlisted operation
}


def _write_guard(
    operation: str,
    items: list,
    confirmed_count: int | None,
    dry_run: bool,
    threshold: int | None = None,
) -> dict | None:
    """
    Safety gate for bulk write operations.

    Call this at the top of any write tool that takes a list of items.
    Returns None when execution should proceed normally.
    Returns a blocking dict when execution must be halted — the caller
    should return that dict immediately without performing any writes.

    Behaviour
    ─────────
    batch ≤ threshold
        Standard dry_run logic applies; this function returns None.

    batch > threshold, confirmed_count is None
        Forced staging: returns a manifest-prompt dict regardless of
        dry_run.  The caller is responsible for building and including
        the per-item preview in the returned dict.

    batch > threshold, confirmed_count ≠ len(items)
        Count mismatch: returns an error dict.  Prevents an injection
        from bypassing staging by guessing a wrong count.

    batch > threshold, confirmed_count == len(items)
        Human explicitly acknowledged the manifest; returns None so
        execution proceeds.

    Args:
        operation:       Name of the calling tool (used in messages and
                         to look up threshold from WRITE_THRESHOLDS).
        items:           The list of items to be written.
        confirmed_count: Value passed by the caller; None means "not yet
                         confirmed".
        dry_run:         The tool's dry_run flag (used in messages only;
                         the guard does not itself enforce dry_run).
        threshold:       Override the default threshold for this operation.
                         If None, looks up WRITE_THRESHOLDS[operation] then
                         WRITE_THRESHOLDS["default"].
    """
    if threshold is None:
        threshold = WRITE_THRESHOLDS.get(operation, WRITE_THRESHOLDS["default"])

    count = len(items)

    if count <= threshold:
        return None  # small batch — proceed normally

    if confirmed_count is None:
        return {
            "staged": True,
            "success": False,
            "operation": operation,
            "item_count": count,
            "threshold": threshold,
            "confirmed_count": None,
            "message": (
                f"Batch of {count} items exceeds the {threshold}-item staging "
                f"threshold for '{operation}'. Review the manifest below, then "
                f"call again with confirmed_count={count} to execute."
            ),
        }

    if confirmed_count != count:
        return {
            "staged": False,
            "success": False,
            "operation": operation,
            "item_count": count,
            "threshold": threshold,
            "confirmed_count": confirmed_count,
            "message": (
                f"confirmed_count={confirmed_count} does not match batch "
                f"size={count}. Call again with confirmed_count={count} to proceed."
            ),
        }

    return None  # confirmed — proceed


def _check_injection(field_name: str, value: str) -> None:
    """
    Scan a write parameter for obvious prompt injection patterns.

    Raises ValueError with the field name and a clear message if the value
    matches a known injection signature.  Intended as a last-resort server-
    side tripwire — not a comprehensive defence.

    Call this for every free-text write parameter (titles, descriptions,
    notes, extended property values) before forwarding to the Linnworks API.

    Args:
        field_name: Human-readable name for the parameter (used in the error).
        value:      The string value to check.

    Raises:
        ValueError if the value matches an injection pattern.
    """
    if not isinstance(value, str) or not value.strip():
        return
    for pattern in _INJECTION_PATTERNS:
        if _re.search(pattern, value.strip()):
            raise ValueError(
                f"Parameter '{field_name}' contains a pattern that looks like "
                f"an injected instruction rather than legitimate product data "
                f"(matched: {pattern!r}). If this is genuine data, rephrase it "
                f"to avoid instruction-like prefixes."
            )


def _format_order_note(n: dict) -> dict:
    """
    Normalise a single Linnworks OrderNote into the MCP-facing shape.

    Centralised here so all five places that read notes (the order-detail
    formatter, get_order_notes, and the read-back paths in add/update/delete)
    use the same field mapping — and so future API field-name surprises only
    need fixing in one place.

    Verified from the OpenAPI spec (orders.json, OrderNote definition) the
    canonical fields on a GetOrderNotes response are:
        OrderNoteId, OrderId, NoteDate, Internal, Note, CreatedBy, NoteTypeId
    Fallback keys are kept for resilience but should not be relied on.
    """
    internal = n.get("Internal")
    if internal is None:
        internal = n.get("IsInternal")
    return {
        "note_id":    n.get("OrderNoteId") or n.get("pkOrderNoteId"),
        "note":       n.get("Note") or n.get("NoteText"),
        "internal":   internal,
        "created_on": n.get("NoteDate") or n.get("NoteCreatedOn") or n.get("DateCreated"),
        "created_by": n.get("CreatedBy") or n.get("NoteCreatedBy"),
    }


def _note_id_of(n: dict) -> str:
    """Return whichever field carries the note GUID (handles old + new shapes)."""
    return n.get("OrderNoteId") or n.get("pkOrderNoteId") or ""


def _format_order_detail(raw: dict) -> dict:
    """Normalise a single Linnworks order detail record into a consistent shape."""
    general = raw.get("GeneralInfo") or {}
    shipping = raw.get("ShippingInfo") or {}
    customer = raw.get("CustomerInfo") or {}
    address = customer.get("Address") or {}
    billing = customer.get("BillingAddress") or {}
    items = raw.get("Items") or []
    totals_raw = raw.get("TotalsInfo") or {}
    return {
        "order_id": raw.get("OrderId"),
        "num_order_id": raw.get("NumOrderId"),
        "processed": raw.get("Processed"),
        # FulfilmentLocationId is required by cancel_order (passed as fulfilmentCenter)
        "fulfilment_location_id": raw.get("FulfilmentLocationId"),
        "received_date": general.get("ReceivedDate"),
        "status": general.get("Status"),
        "is_parked": general.get("IsParked"),
        "marker": general.get("Marker"),
        "reference_num": general.get("ReferenceNum"),
        "external_reference": general.get("ExternalReference"),
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
        # Order totals — used by refund tools to compute refund amounts
        "totals": {
            "subtotal":  totals_raw.get("Subtotal"),
            "postage":   totals_raw.get("PostageCost"),
            "tax":       totals_raw.get("Tax"),
            "total":     totals_raw.get("TotalCharge"),
            "currency":  totals_raw.get("Currency"),
            "discount":  totals_raw.get("TotalDiscount"),
        },
        "items": [
            {
                "StockItemId":    i.get("StockItemId"),
                "SKU":            i.get("SKU"),
                "Title":          i.get("Title"),
                "Quantity":       i.get("Quantity"),
                # row_id is the OrderItemRowId required by refund_order_lines
                "row_id":         i.get("RowId"),
                "price_per_unit": i.get("PricePerUnit"),
                # cost_inc_tax is the total line cost including tax (all units)
                "cost_inc_tax":   i.get("CostIncTax"),
            }
            for i in items
        ],
        # Notes attached to this order. Field names use .get() with fallbacks
        # because the Linnworks docs use slightly different names across endpoints.
        "notes": [_format_order_note(n) for n in (raw.get("Notes") or [])],
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
                "IsVariationParent": item.get("IsVariationParent", False),
                "RetailPrice": item.get("RetailPrice"),
                "PurchasePrice": item.get("PurchasePrice"),
            }
        ],
    }


@mcp.tool()
def search_inventory_items(
    keyword: str,
    page: int = 1,
    per_page: int = 50,
) -> dict:
    """
    Search the inventory catalogue by a free-text keyword.

    This is the discovery counterpart to find_inventory_item. Unlike that tool
    (which only matches an EXACT SKU), this matches the keyword against item
    TITLE, SKU, and BARCODE — the same search the Linnworks inventory search
    box uses. Use it to resolve a human-readable product name (or a partial SKU
    / barcode) to a SKU and StockItemId, so the result can chain straight into
    stock-level, price, or write tools without a second lookup.

    Example: search_inventory_items("Wistman") returns every Wistman variant
    with its SKU and stock_item_id, so "reduce stock of Luma Wistman's silver
    by one" becomes actionable without guessing the SKU string.

    Results are paged — keyword searches can return many rows. Use `page` to
    walk through them; `total_entries` / `total_pages` in the response tell you
    how many there are.

    Args:
        keyword: Free-text term matched against title, SKU, and barcode
            (case-insensitive, partial matches allowed).
        page: 1-based page number to return (default 1).
        per_page: Items per page (default 50, capped at 200).

    Returns:
        A dict with:
          - query:         the keyword searched
          - page:          the page returned
          - per_page:      items per page used
          - total_entries: total matching items across all pages
          - total_pages:   total number of pages
          - count:         number of items in this page
          - items:         list of matched items, each with sku, stock_item_id,
                           title, stock_level, available, in_order, barcode,
                           retail_price, purchase_price, category,
                           is_composite_parent, is_variation_parent
    """
    per_page = max(1, min(per_page, 200))
    page = max(1, page)

    # Stock/GetStockItems: GET, keyWord matches title/SKU/barcode. This is the
    # endpoint behind the UI inventory search box — confirmed working in tenant
    # testing (the Stock/GetStockItemsFull and Inventory/GetInventoryItems
    # plural endpoints both return HTTP 400 here; this one does not).
    result = call_linnworks_get(
        "Stock/GetStockItems",
        {
            "keyWord": keyword,
            "entriesPerPage": per_page,
            "pageNumber": page,
        },
    )

    data = result.get("Data", []) if isinstance(result, dict) else []
    items = [
        {
            "sku": row.get("ItemNumber"),
            "stock_item_id": row.get("StockItemId"),
            "title": row.get("ItemTitle"),
            "stock_level": row.get("Quantity"),
            "available": row.get("Available"),
            "in_order": row.get("InOrder"),
            "barcode": row.get("BarcodeNumber"),
            "retail_price": row.get("RetailPrice"),
            "purchase_price": row.get("PurchasePrice"),
            "category": row.get("CategoryName"),
            "is_composite_parent": row.get("IsCompositeParent", False),
            "is_variation_parent": row.get("IsVariationParent", False),
        }
        for row in data
    ]

    return {
        "query": keyword,
        "page": result.get("PageNumber", page) if isinstance(result, dict) else page,
        "per_page": result.get("EntriesPerPage", per_page) if isinstance(result, dict) else per_page,
        "total_entries": result.get("TotalEntries", len(items)) if isinstance(result, dict) else len(items),
        "total_pages": result.get("TotalPages", 1) if isinstance(result, dict) else 1,
        "count": len(items),
        "items": items,
    }


def _format_variation_member(m: dict) -> dict:
    """Map a Stock/GetVariationItems member row to the MCP-facing shape."""
    return {
        "sku": m.get("ItemNumber") or m.get("SKU"),
        "stock_item_id": m.get("pkStockItemId") or m.get("StockItemId"),
        "title": m.get("ItemTitle"),
    }


def _resolve_variation(sku: str, stock_item_id: str) -> dict:
    """
    Resolve a SKU's variation-group relationship in BOTH directions.

    Forward (parent -> children): Stock/GetVariationGroupByParentId returns the
        group for a parent stock item (HTTP 200 `null` for non-parents); the
        parent's StockItemId IS the group's pkVariationItemId. Stock/GetVariationItems
        then lists the child members (the parent itself is NOT in that list).

    Reverse (child -> parent + siblings): Stock/SearchVariationGroups with
        searchType=ItemSKU substring-matches member SKUs. Because it is a substring
        match, candidate groups are confirmed by exact SKU membership via
        GetVariationItems before being accepted — this avoids false positives and
        is what lets reverse_lookup_confirmed be True.

    All endpoint shapes confirmed live in this tenant 18 Jun 2026 (issue #17).
    NB: the IsVariationParent flag on GetInventoryItem is unreliable here (observed
    False on a genuine parent), so role is derived from these relationship
    endpoints, never from the flag.
    """
    # --- Forward: is this SKU a variation PARENT? ---
    group = call_linnworks_get(
        "Stock/GetVariationGroupByParentId", {"pkStockItemId": stock_item_id}
    )
    if group:  # non-null dict => parent
        pk = group.get("pkVariationItemId")
        members = call_linnworks_get(
            "Stock/GetVariationItems", {"pkVariationItemId": pk}
        ) or []
        return {
            "role": "parent",
            "group_name": group.get("VariationGroupName"),
            "parent_sku": sku,
            "parent_stock_item_id": stock_item_id,
            "children": [_format_variation_member(m) for m in members],
            "siblings": [],
            "reverse_lookup_confirmed": True,
        }

    # --- Reverse: is this SKU a variation CHILD? ---
    target = (sku or "").strip().lower()
    page: int | None = 1
    pages_scanned = 0
    while page and pages_scanned < 5:
        res = call_linnworks_get(
            "Stock/SearchVariationGroups",
            {"searchType": "ItemSKU", "searchText": sku,
             "pageNumber": page, "entriesPerPage": 100},
        )
        candidates = res.get("Data", []) if isinstance(res, dict) else []
        total_pages = res.get("TotalPages", 1) if isinstance(res, dict) else 1
        for cand in candidates:
            members = call_linnworks_get(
                "Stock/GetVariationItems",
                {"pkVariationItemId": cand.get("pkVariationItemId")},
            ) or []
            member_rows = [_format_variation_member(m) for m in members]
            if any((r["sku"] or "").strip().lower() == target for r in member_rows):
                return {
                    "role": "child",
                    "group_name": cand.get("VariationGroupName"),
                    "parent_sku": cand.get("VariationSKU"),
                    # The parent's StockItemId IS the group's pkVariationItemId
                    # (confirmed live, issue #17) — carried so callers can reach
                    # the parent without a second SKU resolution.
                    "parent_stock_item_id": cand.get("pkVariationItemId"),
                    "children": [],
                    "siblings": [
                        r for r in member_rows
                        if (r["sku"] or "").strip().lower() != target
                    ],
                    "reverse_lookup_confirmed": True,
                }
        pages_scanned += 1
        page = page + 1 if page < total_pages else None

    return {
        "role": "none",
        "group_name": None,
        "parent_sku": None,
        "children": [],
        "siblings": [],
        "reverse_lookup_confirmed": True,
        "note": "Not part of any variation group (forward + reverse checked).",
    }


def _resolve_composition(sku: str, stock_item_id: str) -> dict:
    """
    Resolve a SKU's composite/bundle relationship.

    Forward (parent -> components): Inventory/GetInventoryItemCompositions returns
        the component list for a composite parent and `[]` for a non-composite.
        Each row's LinkedStockItemId is the COMPONENT's stock item id; the row's
        own StockItemId field is the PARENT's id (do not use it as the component id).

    Reverse (component -> parent composites) is NOT supported: Linnworks exposes no
        endpoint mapping a component back to the composites that contain it, and no
        working catalogue-list endpoint exists in this tenant to scan every
        composite (Inventory/GetInventoryItems and Stock/GetStockItemsFull both 400).
        So belongs_to is always empty and reverse_lookup_supported is always False —
        this means a non-parent cannot be positively classified as a "component"
        (Open Q2, issue #17). Confirmed live 18 Jun 2026.
    """
    comps = call_linnworks_get(
        "Inventory/GetInventoryItemCompositions",
        {"inventoryItemId": stock_item_id, "getFullDetail": "true"},
    ) or []

    if comps:
        return {
            "role": "parent",
            "components": [
                {
                    "sku": c.get("SKU"),
                    "linked_stock_item_id": c.get("LinkedStockItemId"),
                    "title": c.get("ItemTitle"),
                    "quantity": c.get("Quantity"),
                    "purchase_price": c.get("PurchasePrice"),
                }
                for c in comps
            ],
            "belongs_to": [],
            "reverse_lookup_supported": False,
        }

    return {
        "role": "none",
        "components": [],
        "belongs_to": [],
        "reverse_lookup_supported": False,
        "note": ("Not a composite parent. Whether this SKU is a COMPONENT of "
                 "another composite cannot be determined — Linnworks exposes no "
                 "component->parent reverse lookup (issue #17, Open Q2)."),
    }


@mcp.tool()
def get_item_relationships(sku: str) -> dict:
    """
    Resolve how a SKU relates to parent/child groupings — variation groups AND
    composites (bundles) — in both directions, in a single call.

    This answers the question "does this SKU belong to a parent, and which one?"
    that find_inventory_item and search_inventory_items cannot: those only tell
    you whether a SKU *is* a parent (flags), never what it rolls up under or what
    it contains.

    What it resolves:
      • Variation — forward (parent -> its child variants) and reverse
        (child -> its parent group + sibling variants). Reverse is confirmed
        working in this tenant, so a child SKU returns its parent and siblings.
      • Composite — forward (bundle/custom-complete parent -> its components, with
        per-component quantity and purchase price). Reverse (component -> the
        bundles that contain it) is NOT available from the Linnworks API and is
        always reported empty (composite.reverse_lookup_supported = False).

    Args:
        sku: The exact SKU / item number (case-insensitive). Partial SKUs and
            title keywords will not match — use search_inventory_items first if
            you only have a product name.

    Returns:
        A dict with:
          - sku, stock_item_id, title, found
          - is_variation_parent, is_composite_parent — derived from the live
            relationship endpoints (the raw item flags are unreliable in this
            tenant), not from GetInventoryItem's flags
          - variation: {role ("parent"|"child"|"none"), group_name, parent_sku,
              children[] (when parent), siblings[] (when child),
              reverse_lookup_confirmed}
          - composite: {role ("parent"|"none"), components[] (when parent),
              belongs_to[] (always empty), reverse_lookup_supported (always False)}
        Children/siblings/components each carry sku, stock_item_id/linked id, and
        title; components also carry quantity and purchase_price.

        If the SKU does not exist, returns {sku, found: False, note}.
    """
    # Step 0 — resolve SKU -> StockItemId (+ title). GetInventoryItem is exact-SKU
    # only and raises on a miss (same contract as find_inventory_item).
    try:
        item = call_linnworks("Inventory/GetInventoryItem", {"sku": sku})
    except RuntimeError:
        return {
            "sku": sku,
            "found": False,
            "note": ("No inventory item found for that exact SKU. Use "
                     "search_inventory_items for keyword / partial / barcode lookup."),
        }

    stock_item_id = item.get("StockItemId")
    if not stock_item_id:
        return {"sku": sku, "found": False,
                "note": "Item found but returned no StockItemId."}

    canonical_sku = item.get("ItemNumber") or sku
    variation = _resolve_variation(canonical_sku, stock_item_id)
    composite = _resolve_composition(canonical_sku, stock_item_id)

    return {
        "sku": canonical_sku,
        "stock_item_id": stock_item_id,
        "title": item.get("ItemTitle"),
        "found": True,
        "is_variation_parent": variation["role"] == "parent",
        "is_composite_parent": composite["role"] == "parent",
        "variation": variation,
        "composite": composite,
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

    notes = [_format_order_note(n) for n in raw_notes]

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

    notes_after = [_format_order_note(n) for n in raw_notes]

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
        note_id: The OrderNoteId GUID of the note to replace (from
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
        (n for n in raw_notes if _note_id_of(n) == note_id),
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
    existing_internal = existing.get("Internal")
    if existing_internal is None:
        existing_internal = existing.get("IsInternal")
    new_internal = internal if internal is not None else bool(existing_internal)

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

    notes_after = [_format_order_note(n) for n in raw_notes_after]

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
        note_id: The OrderNoteId GUID of the note to delete (from
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
        (n for n in raw_notes if _note_id_of(n) == note_id),
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
def delete_order_notes_by_text(
    order_id: str,
    text: str,
    match: Literal["exact", "contains", "starts_with"] = "exact",
    case_sensitive: bool = False,
    max_to_delete: Optional[int] = None,
    dry_run: bool = True,
) -> dict:
    """
    Delete order notes that match a text pattern, without needing to know
    their note_id in advance.

    Fetches all notes on the order, filters by the search text and match mode,
    then deletes each match. Useful for removing test notes, boilerplate, or
    notes identified by their content rather than an opaque GUID.

    Zero matches is not an error — returns success with deleted=0.

    The max_to_delete guard prevents accidental bulk deletion: if more notes
    match than the limit, the tool refuses and lists the matches so you can
    re-scope the search.

    IMPORTANT: dry_run defaults to True. Dry-run output lists every note that
    would be deleted — review it carefully before setting dry_run=False.

    Args:
        order_id: GUID pkOrderID or numeric order number (e.g. "596475").
        text: Text to search for in note content.
        match: How to match the text against each note:
            - "exact" (default): note text must equal search text exactly
            - "contains": note text must contain the search text
            - "starts_with": note text must start with the search text
        case_sensitive: If False (default), comparison is case-insensitive.
        max_to_delete: Safety cap. If more than this many notes match, refuse
            and return the matches so the caller can re-scope. None = no cap.
        dry_run: If True (default), lists what would be deleted without
            deleting anything. Set to False to execute.

    Returns:
        A dict with:
          - dry_run:         whether this was a dry run
          - success:         True (even on zero matches)
          - matched_count:   number of notes that matched
          - deleted:         number actually deleted (0 on dry run)
          - matched:         list of matched notes (note_id, note, created_on, created_by)
          - note_count_after: remaining notes after deletion (live run only)
    """
    order_id = order_id.strip()

    # Resolve to GUID
    if _UUID_RE.match(order_id):
        order_guid = order_id
    else:
        try:
            order_guid, _ = _resolve_order_guid(order_id)
        except RuntimeError as exc:
            return {"error": str(exc)}

    # Fetch all notes on the order
    raw_notes = call_linnworks_get("Orders/GetOrderNotes", params={"orderId": order_guid})
    if not isinstance(raw_notes, list):
        raw_notes = raw_notes.get("Notes") or [] if isinstance(raw_notes, dict) else []

    # Build comparison strings
    needle = text if case_sensitive else text.lower()

    def _matches(raw_note: dict) -> bool:
        note_text = raw_note.get("Note") or raw_note.get("NoteText") or ""
        haystack = note_text if case_sensitive else note_text.lower()
        if match == "exact":
            return haystack == needle
        elif match == "contains":
            return needle in haystack
        elif match == "starts_with":
            return haystack.startswith(needle)
        return False

    matched_raw = [n for n in raw_notes if _matches(n)]

    # Format matched notes for output
    matched_formatted = [_format_order_note(n) for n in matched_raw]

    # max_to_delete guard
    if max_to_delete is not None and len(matched_raw) > max_to_delete:
        return {
            "success": False,
            "error": (
                f"{len(matched_raw)} notes matched but max_to_delete={max_to_delete}. "
                "Re-scope your search or increase max_to_delete."
            ),
            "matched_count": len(matched_raw),
            "matched": matched_formatted,
        }

    if dry_run:
        return {
            "dry_run": True,
            "success": True,
            "matched_count": len(matched_raw),
            "deleted": 0,
            "matched": matched_formatted,
            "message": (
                "No changes written. Set dry_run=False to delete these notes."
                if matched_raw
                else "No notes matched. Nothing to delete."
            ),
        }

    # Live deletion — delete each matched note
    deleted = 0
    errors: list[str] = []
    for raw_note in matched_raw:
        note_id = _note_id_of(raw_note)
        if not note_id:
            errors.append("Skipped a note with no note_id.")
            continue
        try:
            call_linnworks(
                "ProcessedOrders/DeleteOrderNote",
                {"pkOrderNoteId": note_id},
            )
            deleted += 1
        except Exception as exc:
            errors.append(f"Failed to delete note {note_id}: {exc}")

    # Read back to confirm remaining count
    raw_after = call_linnworks_get("Orders/GetOrderNotes", params={"orderId": order_guid})
    if not isinstance(raw_after, list):
        raw_after = raw_after.get("Notes") or [] if isinstance(raw_after, dict) else []

    result: dict = {
        "dry_run": False,
        "success": deleted == len(matched_raw),
        "matched_count": len(matched_raw),
        "deleted": deleted,
        "matched": matched_formatted,
        "note_count_after": len(raw_after),
    }
    if errors:
        result["errors"] = errors
    return result


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
def find_orders_by_reference(
    reference: str,
    include_processed: bool = False,
    location_id: str = DEFAULT_LOCATION_ID,
) -> dict:
    """
    Find Linnworks orders by channel or source reference number.

    Accepts the identifier that appears in customer emails, support tickets,
    and the selling channel's own dashboard — not the internal Linnworks
    numeric order ID. Examples:
      - Shopify:  "11177274" or "#11177274"
      - Amazon:   "202-3420523-7292364"
      - eBay:     the 13-digit reference

    Leading '#' characters are stripped automatically, so you can paste
    references directly from Shopify or customer emails.

    By default only open (unprocessed) orders are searched. Pass
    include_processed=True to also search dispatched orders.

    Returns 0, 1, or multiple matching orders. If more than one order
    matches (e.g. the same reference appears on different channels), all
    are returned so you can disambiguate before acting.

    Backed by OpenOrders/SearchOrders. This endpoint searches across
    ReferenceNum, ExternalReference, and related fields — it is the
    API-native approach for this use case.

    Note: SearchOrders is confirmed in the public Linnworks OpenAPI spec
    but had not been live-tested on this tenant as of May 2026. If it
    returns an unexpected error, fall back to find_open_orders_for_sku
    or get_open_orders filtered manually.

    Args:
        reference: The channel/source order reference to search for.
            Accepts Shopify "#11177274", Amazon "202-3420523-7292364",
            eBay references, or any Linnworks ReferenceNum/ExternalReference.
        include_processed: If True, also searches dispatched/processed orders.
            Defaults to False (open orders only).
        location_id: Linnworks location to search. Defaults to "Default".

    Returns:
        A dict with:
          - reference:    the reference string searched (# stripped)
          - match_count:  number of orders found
          - orders:       list of matching order dicts, each with:
              order_id, num_order_id, reference_num, external_reference,
              source, sub_source, status, processed, received_date,
              customer_name, customer_email
    """
    # Strip leading # so users can paste "#11177274" directly
    reference = reference.strip().lstrip("#").strip()
    if not reference:
        return {"error": "reference must not be empty after stripping '#'"}

    # --- Step 1: API-native search ---
    # SearchOrders returns GUIDs grouped into OpenOrders views and ProcessedOrders.
    # Payload must be sent UNWRAPPED (no {"request": {...}} wrapper) — despite the
    # OpenAPI spec naming the body parameter "request" exactly like
    # GetOrdersLowFidelity (which DOES need the wrapper). Live-tested 15 Jun 2026:
    # the wrapped form returns HTTP 400 "Must provide a search term." (the SearchTerm
    # gets buried), while the unwrapped form returns HTTP 200. Same surprise as
    # PurchaseOrder/Search_PurchaseOrders2.
    resp = call_linnworks(
        "OpenOrders/SearchOrders",
        {
            "LocationId": location_id,
            "SearchTerm": reference,
            "IncludeProcessed": include_processed,
        },
    )

    # --- Step 2: collect all GUIDs (deduplicated) ---
    # OpenOrders: list of OrderViewIds objects, each with an OrderIds array.
    # ProcessedOrders: flat list of GUIDs.
    seen: set[str] = set()
    all_guids: list[str] = []

    for view in (resp.get("OpenOrders") or []):
        for guid in (view.get("OrderIds") or []):
            if guid and guid not in seen:
                seen.add(guid)
                all_guids.append(guid)

    if include_processed:
        for guid in (resp.get("ProcessedOrders") or []):
            if guid and guid not in seen:
                seen.add(guid)
                all_guids.append(guid)

    if not all_guids:
        return {
            "reference": reference,
            "match_count": 0,
            "orders": [],
        }

    # --- Step 3: enrich with full order detail (customer name, email, etc.) ---
    orders: list[dict] = []
    batch_size = 50
    for i in range(0, len(all_guids), batch_size):
        batch = all_guids[i : i + batch_size]
        detail_resp = call_linnworks("Orders/GetOrdersById", {"pkOrderIds": batch})
        detail_list = (
            detail_resp if isinstance(detail_resp, list)
            else (detail_resp.get("Orders") or detail_resp.get("Data") or [])
        )
        for order in detail_list:
            fmt = _format_order_detail(order)
            orders.append({
                "order_id":           fmt["order_id"],
                "num_order_id":       fmt["num_order_id"],
                "reference_num":      fmt["reference_num"],
                "external_reference": fmt["external_reference"],
                "source":             fmt["source"],
                "sub_source":         fmt["sub_source"],
                "status":             fmt["status"],
                "processed":          fmt["processed"],
                "received_date":      fmt["received_date"],
                "customer_name":      fmt["customer_name"],
                "customer_email":     fmt["customer_email"],
            })

    return {
        "reference": reference,
        "match_count": len(orders),
        "orders": orders,
    }


# ---------- Order cancellation and refunds ----------

def _get_refund_options(order_guid: str) -> dict:
    """Call ReturnsRefunds/GetRefundOptions for the given order GUID."""
    return call_linnworks(
        "ReturnsRefunds/GetRefundOptions",
        {"request": {"OrderId": order_guid}},
    )


@mcp.tool()
def cancel_order(
    order_id: str,
    note: Optional[str] = None,
    dry_run: bool = True,
) -> dict:
    """
    Cancel an open (unprocessed) Linnworks order.

    Reads the order first to confirm it is open, then cancels it via
    Orders/CancelOrder. Only open orders can be cancelled this way — if the
    order is already processed (dispatched), this tool will refuse.

    Note: The Linnworks CancelOrder API does not expose a return_to_stock flag.
    Stock management on cancellation is controlled by your Linnworks workspace
    settings.

    IMPORTANT: dry_run defaults to True. Set dry_run=False only when you are
    sure the cancellation is correct — confirm with the user before doing so.
    Cancellations cannot be reversed via the API.

    Args:
        order_id: The order to cancel. Accepts a GUID pkOrderID or a numeric
            order number (e.g. "596475").
        note: Optional note to attach to the cancellation.
        dry_run: If True (default), shows what would be cancelled without
            actually cancelling. Set to False to execute.

    Returns:
        A dict with:
          - dry_run:       whether this was a dry run
          - order_id:      the GUID
          - num_order_id:  the numeric order number
          - customer_name: customer name
          - items:         list of items in the order
          - status:        "would_cancel" (dry run) or "cancelled"
    """
    try:
        guid, raw = _resolve_order_guid(order_id)
    except RuntimeError as exc:
        return {"error": str(exc)}

    fmt = _format_order_detail(raw)

    if fmt.get("processed"):
        return {
            "error": (
                "This order is already processed (dispatched). "
                "Only open orders can be cancelled via this tool."
            ),
            "order_id": fmt.get("order_id"),
            "num_order_id": fmt.get("num_order_id"),
        }

    summary = {
        "order_id":           fmt.get("order_id"),
        "num_order_id":       fmt.get("num_order_id"),
        "customer_name":      fmt.get("customer_name"),
        "customer_email":     fmt.get("customer_email"),
        "reference_num":      fmt.get("reference_num"),
        "external_reference": fmt.get("external_reference"),
        "source":             fmt.get("source"),
        "items":              fmt.get("items", []),
        "note":               note,
    }

    if dry_run:
        return {
            "dry_run": True,
            "status": "would_cancel",
            "message": "Set dry_run=False to execute this cancellation.",
            **summary,
        }

    # Live cancellation
    fulfilment_location_id = (
        raw.get("FulfilmentLocationId") or DEFAULT_LOCATION_ID
    )
    payload = {
        "orderId": guid,
        "fulfilmentCenter": fulfilment_location_id,
        "note": note or "",
    }
    result = call_linnworks("Orders/CancelOrder", payload)

    return {
        "dry_run": False,
        "status": "cancelled",
        "linnworks_response": result,
        **summary,
    }


@mcp.tool()
def refund_order(
    order_id: str,
    note: Optional[str] = None,
    push_to_channel: bool = True,
    dry_run: bool = True,
) -> dict:
    """
    Issue a full refund for a processed Linnworks order.

    Refunds all items and postage. The order must already be processed
    (dispatched). For open orders, use cancel_order instead.

    Flow when dry_run=False:
      1. Fetches the order to get items, totals, and OrderItemRowIds.
      2. Calls GetRefundOptions to confirm the order can be refunded.
      3. Creates a refund record via ReturnsRefunds/CreateRefund.
      4. If push_to_channel=True, actions the refund via ReturnsRefunds/ActionRefund,
         which pushes the refund to the sales channel (Shopify, Amazon, eBay, etc.).

    NOTE: The refund endpoints (ReturnsRefunds/CreateRefund, ActionRefund) are
    implemented from the Linnworks OpenAPI spec but have not been live-tested
    against this tenant. Run with dry_run=True first to confirm order data,
    then dry_run=False when ready. Any Linnworks API errors are surfaced verbatim.

    IMPORTANT: dry_run defaults to True. Refunds move real customer money —
    always confirm the order and refund amounts before setting dry_run=False.

    Args:
        order_id: Order to refund. Accepts a GUID pkOrderID or numeric order number.
        note: Optional note/reason to include with the refund.
        push_to_channel: If True (default), also submits the refund to the sales
            channel (Shopify, Amazon, etc.). If False, records the refund in
            Linnworks only.
        dry_run: If True (default), shows the refund that would be created
            without creating it. Set to False to execute.

    Returns:
        A dict with order details, per-line refund amounts, total refund, and
        if live: the refund header ID, status, and action result.
    """
    try:
        guid, raw = _resolve_order_guid(order_id)
    except RuntimeError as exc:
        return {"error": str(exc)}

    fmt = _format_order_detail(raw)

    if not fmt.get("processed"):
        return {
            "error": (
                "This order is not yet processed. "
                "Use cancel_order for open (unprocessed) orders."
            ),
            "order_id": fmt.get("order_id"),
            "num_order_id": fmt.get("num_order_id"),
        }

    # Build refund lines for every item
    refund_lines: list[dict] = []
    for item in (raw.get("Items") or []):
        row_id = item.get("RowId")
        if not row_id:
            continue
        line: dict = {
            "OrderItemRowId": row_id,
            "RefundedUnit": "Item",
            "Amount": float(item.get("CostIncTax") or 0.0),
        }
        if note:
            line["FreeTextOrNote"] = note
        refund_lines.append(line)

    # Add shipping line if postage > 0
    totals_raw = raw.get("TotalsInfo") or {}
    postage = float(totals_raw.get("PostageCost") or 0.0)
    if postage > 0:
        shipping_line: dict = {"RefundedUnit": "Shipping", "Amount": postage}
        if note:
            shipping_line["FreeTextOrNote"] = note
        refund_lines.append(shipping_line)

    total_refund = sum(rl["Amount"] for rl in refund_lines)
    currency = totals_raw.get("Currency")

    if dry_run:
        return {
            "dry_run": True,
            "order_id": fmt.get("order_id"),
            "num_order_id": fmt.get("num_order_id"),
            "customer_name": fmt.get("customer_name"),
            "customer_email": fmt.get("customer_email"),
            "reference_num": fmt.get("reference_num"),
            "external_reference": fmt.get("external_reference"),
            "total_refund": total_refund,
            "currency": currency,
            "push_to_channel": push_to_channel,
            "refund_lines": [
                {
                    "unit":              rl.get("RefundedUnit"),
                    "amount":            rl.get("Amount"),
                    "order_item_row_id": rl.get("OrderItemRowId"),
                }
                for rl in refund_lines
            ],
            "note": note,
            "message": "Set dry_run=False to execute this full refund.",
        }

    # Check refund eligibility
    options_resp = _get_refund_options(guid)
    options = options_resp.get("RefundOptions") or {}
    cannot_reason = options.get("CannotRefundReason") or "None"
    if cannot_reason != "None":
        return {
            "error": f"Linnworks cannot refund this order: {cannot_reason}",
            "order_id": guid,
            "num_order_id": fmt.get("num_order_id"),
        }

    # Create refund
    create_resp = call_linnworks(
        "ReturnsRefunds/CreateRefund",
        {
            "request": {
                "OrderId": guid,
                "ChannelInitiated": False,
                "RefundLines": refund_lines,
            }
        },
    )
    refund_header_id = create_resp.get("RefundHeaderId")
    create_errors = create_resp.get("Errors") or []
    cannot = create_resp.get("CannotRefundReason") or "None"

    if cannot != "None" or create_errors:
        return {
            "error": (
                f"Refund creation failed. "
                f"Reason: {cannot}. Errors: {create_errors}"
            ),
            "order_id": guid,
            "create_response": create_resp,
        }

    result: dict = {
        "dry_run": False,
        "order_id": guid,
        "num_order_id": fmt.get("num_order_id"),
        "customer_name": fmt.get("customer_name"),
        "refund_header_id": refund_header_id,
        "refund_reference": create_resp.get("RefundReference"),
        "total_refund": total_refund,
        "currency": currency,
        "status": (create_resp.get("Status") or {}),
        "push_to_channel": push_to_channel,
        "actioned": False,
    }

    # Action refund (push to channel)
    if push_to_channel and refund_header_id is not None:
        action_resp = call_linnworks(
            "ReturnsRefunds/ActionRefund",
            {"request": {"RefundHeaderId": refund_header_id, "OrderId": guid}},
        )
        result["actioned"] = action_resp.get("SuccessfullyActioned", False)
        result["action_status"] = (action_resp.get("Status") or {})
        result["action_errors"] = action_resp.get("Errors") or []

    return result


@mcp.tool()
def refund_order_lines(
    order_id: str,
    lines: list,
    refund_postage: bool = False,
    note: Optional[str] = None,
    push_to_channel: bool = True,
    dry_run: bool = True,
) -> dict:
    """
    Issue a partial refund for specific line items on a processed Linnworks order.

    Each entry in `lines` must include `row_id` (the OrderItemRowId from get_order's
    items list). Optionally supply `amount` to override the full line cost, or
    `quantity` when the refund is part of a return.

    Use get_order() first to see the order's items including their `row_id` values,
    then call this tool with only the lines you want to refund.

    NOTE: The refund endpoints (ReturnsRefunds/CreateRefund, ActionRefund) are
    implemented from the Linnworks OpenAPI spec but have not been live-tested
    against this tenant. Run dry_run=True first to verify, then dry_run=False.

    IMPORTANT: dry_run defaults to True. Refunds move real customer money.

    Args:
        order_id: Order to partially refund. Accepts GUID or numeric order number.
        lines: List of dicts, each with:
            - row_id (str, required): OrderItemRowId from get_order items.
            - amount (float, optional): Amount to refund for this line. Defaults
              to the full cost_inc_tax of the matched item.
            - quantity (int, optional): Quantity being refunded.
        refund_postage: If True, also refund the postage/shipping cost.
        note: Optional note/reason to include with the refund.
        push_to_channel: If True (default), submits the refund to the sales
            channel. If False, records in Linnworks only.
        dry_run: If True (default), shows what would be refunded without doing it.

    Returns:
        A dict with the refund summary and, if live, the result from Linnworks.
    """
    try:
        guid, raw = _resolve_order_guid(order_id)
    except RuntimeError as exc:
        return {"error": str(exc)}

    fmt = _format_order_detail(raw)

    if not fmt.get("processed"):
        return {
            "error": (
                "This order is not yet processed. "
                "Use cancel_order for open (unprocessed) orders."
            ),
            "order_id": fmt.get("order_id"),
            "num_order_id": fmt.get("num_order_id"),
        }

    # Build lookup of RowId → raw item for amount defaults
    item_by_row_id: dict[str, dict] = {
        i["RowId"]: i
        for i in (raw.get("Items") or [])
        if i.get("RowId")
    }

    refund_lines: list[dict] = []
    unknown_row_ids: list[str] = []

    for ln in (lines or []):
        row_id = ln.get("row_id")
        if not row_id:
            return {"error": "Each entry in 'lines' must have a 'row_id' field."}
        item = item_by_row_id.get(row_id)
        if item is None:
            unknown_row_ids.append(row_id)
            continue
        amount = float(ln["amount"]) if ln.get("amount") is not None else float(
            item.get("CostIncTax") or 0.0
        )
        rl: dict = {
            "OrderItemRowId": row_id,
            "RefundedUnit": "Item",
            "Amount": amount,
        }
        if ln.get("quantity") is not None:
            rl["Quantity"] = int(ln["quantity"])
        if note:
            rl["FreeTextOrNote"] = note
        refund_lines.append(rl)

    if unknown_row_ids:
        return {
            "error": (
                f"row_id(s) not found in order items: {unknown_row_ids}. "
                "Use get_order() to list valid row_ids for this order."
            ),
            "order_id": guid,
        }

    # Optionally add shipping line
    totals_raw = raw.get("TotalsInfo") or {}
    postage = float(totals_raw.get("PostageCost") or 0.0)
    if refund_postage and postage > 0:
        shipping_line: dict = {"RefundedUnit": "Shipping", "Amount": postage}
        if note:
            shipping_line["FreeTextOrNote"] = note
        refund_lines.append(shipping_line)

    if not refund_lines:
        return {"error": "No valid refund lines to process."}

    total_refund = sum(rl["Amount"] for rl in refund_lines)
    currency = totals_raw.get("Currency")

    if dry_run:
        return {
            "dry_run": True,
            "order_id": fmt.get("order_id"),
            "num_order_id": fmt.get("num_order_id"),
            "customer_name": fmt.get("customer_name"),
            "customer_email": fmt.get("customer_email"),
            "reference_num": fmt.get("reference_num"),
            "external_reference": fmt.get("external_reference"),
            "total_refund": total_refund,
            "currency": currency,
            "push_to_channel": push_to_channel,
            "refund_lines": [
                {
                    "unit":              rl.get("RefundedUnit"),
                    "amount":            rl.get("Amount"),
                    "order_item_row_id": rl.get("OrderItemRowId"),
                    "quantity":          rl.get("Quantity"),
                }
                for rl in refund_lines
            ],
            "note": note,
            "message": "Set dry_run=False to execute this partial refund.",
        }

    # Check refund eligibility
    options_resp = _get_refund_options(guid)
    options = options_resp.get("RefundOptions") or {}
    cannot_reason = options.get("CannotRefundReason") or "None"
    if cannot_reason != "None":
        return {
            "error": f"Linnworks cannot refund this order: {cannot_reason}",
            "order_id": guid,
            "num_order_id": fmt.get("num_order_id"),
        }

    # Create refund
    create_resp = call_linnworks(
        "ReturnsRefunds/CreateRefund",
        {
            "request": {
                "OrderId": guid,
                "ChannelInitiated": False,
                "RefundLines": refund_lines,
            }
        },
    )
    refund_header_id = create_resp.get("RefundHeaderId")
    create_errors = create_resp.get("Errors") or []
    cannot = create_resp.get("CannotRefundReason") or "None"

    if cannot != "None" or create_errors:
        return {
            "error": (
                f"Refund creation failed. "
                f"Reason: {cannot}. Errors: {create_errors}"
            ),
            "order_id": guid,
            "create_response": create_resp,
        }

    result: dict = {
        "dry_run": False,
        "order_id": guid,
        "num_order_id": fmt.get("num_order_id"),
        "customer_name": fmt.get("customer_name"),
        "refund_header_id": refund_header_id,
        "refund_reference": create_resp.get("RefundReference"),
        "total_refund": total_refund,
        "currency": currency,
        "status": (create_resp.get("Status") or {}),
        "push_to_channel": push_to_channel,
        "actioned": False,
    }

    if push_to_channel and refund_header_id is not None:
        action_resp = call_linnworks(
            "ReturnsRefunds/ActionRefund",
            {"request": {"RefundHeaderId": refund_header_id, "OrderId": guid}},
        )
        result["actioned"] = action_resp.get("SuccessfullyActioned", False)
        result["action_status"] = (action_resp.get("Status") or {})
        result["action_errors"] = action_resp.get("Errors") or []

    return result


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

def _flatten_order_item(i: dict) -> dict:
    """
    Flatten a single Orders/GetOrdersById line item into our internal shape.

    Composite components are nested under `composite_sub_items` (the field is
    `CompositeSubItems` on processed-order detail — note this differs from
    open orders, where children live under `CompositeChild`). Sub-items are
    flattened recursively with the same shape so callers can explode composites
    to component (child) level. Most consumers ignore this key and read the
    top-level fields unchanged.

    Child rows carry `Quantity` already resolved to the total child units for
    the whole line (e.g. 5 packs × 10 = 50) — do NOT multiply by the parent
    quantity. Children's `StockItemId` may instead arrive as `ItemId`.
    """
    sub = i.get("CompositeSubItems") or []
    return {
        "sku": i.get("SKU"),
        "title": i.get("Title"),
        "quantity": i.get("Quantity"),
        "price_per_unit": i.get("PricePerUnit"),
        "line_total_ex_tax": i.get("Cost"),
        "line_total_inc_tax": i.get("CostIncTax"),
        "tax": i.get("Tax"),
        "tax_rate": i.get("TaxRate"),
        "category": i.get("CategoryName"),
        "stock_item_id": i.get("StockItemId") or i.get("ItemId"),
        "bin_rack": i.get("BinRack"),
        "channel_sku": i.get("ChannelSKU"),
        "composite_sub_items": [_flatten_order_item(s) for s in sub],
    }


def _batch_order_items(order_guids: list[str]) -> dict[str, list]:
    """
    Fetch line items for a list of order GUIDs via Orders/GetOrdersById.
    Returns a dict mapping each OrderId GUID to its list of item dicts.
    Each item dict preserves nested composite components under
    `composite_sub_items` (see _flatten_order_item).
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
            result[oid] = [_flatten_order_item(i) for i in items]
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
def get_component_sales(
    from_date: str,
    to_date: str,
    date_field: str = "processed",
    top_n: int = 50,
    sku: str = "",
) -> dict:
    """
    Aggregate UNIT sales at the composite-child (component) level for a date
    range. Auto-paginates internally.

    Every other sales tool attributes a composite sale entirely to the parent
    SKU, so the real demand for the components hidden inside it (decks, trucks,
    wheels, grip, items in a bundle/multipack, option/linking SKUs) is invisible.
    This tool explodes each composite order line into its components and counts
    the component units, so you can answer "how many decks did we actually sell
    once bundles and custom completes are broken out?".

    Units only. Composite children carry no price (the money lives on the parent
    line), so revenue cannot be attributed to a component without a modelling
    assumption — that is deliberately out of scope here.

    How exploding works:
      - A line with composite components contributes its CHILDREN's units, not
        its own. Child quantities are already resolved to the line total
        (e.g. 5 packs x 10 = 50), so they are summed directly.
      - A normal (non-composite) line contributes its own units, so the report
        is complete rather than composites-only.
      - Only leaf items are counted (nested composites recurse), so nothing is
        double-counted.
    Each row reports composite_units vs standalone_units separately so you can
    tell exploded demand from direct sales of the same SKU.

    This is an auto-paginating tool: it can make hundreds of API calls. NEVER
    run it in parallel with another auto-paginating tool (get_top_skus,
    get_sales_by_supplier, get_category_report, get_revenue_summary,
    get_period_comparison) — run them sequentially.

    Args:
        from_date: Start of the date range in ISO format, e.g. "2026-04-01".
        to_date: End of the date range in ISO format, e.g. "2026-06-30".
        date_field: Which date to filter on — "received", "processed" (default),
            "payment", or "cancelled".
        top_n: Number of top components to return. Defaults to 50.
        sku: Optional. Restrict output to the components (children) of this one
            composite parent SKU (case-insensitive exact match). Leave blank to
            report every component and standalone SKU across all orders.

    Returns:
        A dict with:
          - from_date, to_date, date_field: the query parameters used
          - total_orders_scanned: total processed orders in the range
          - parent_sku_filter:    the sku filter applied (or "")
          - components: list of top_n component dicts sorted by units desc, each
              with rank, sku, title, units, composite_units, standalone_units,
              orders, from_composite (True when all units came from composites)
    """
    from collections import defaultdict

    PAGE_SIZE = 500
    sku_filter = sku.strip().lower()

    comp_units: dict[str, int] = defaultdict(int)
    standalone_units: dict[str, int] = defaultdict(int)
    order_ids: dict[str, set] = defaultdict(set)
    titles: dict[str, str] = {}

    def _credit_leaves(item: dict, guid: str, from_composite: bool) -> None:
        """Walk an item; credit only leaf nodes (those with no sub-items)."""
        subs = item.get("composite_sub_items") or []
        if subs:
            for s in subs:
                _credit_leaves(s, guid, True)
            return
        leaf_sku = item.get("sku") or "UNKNOWN"
        qty = item.get("quantity") or 0
        if from_composite:
            comp_units[leaf_sku] += qty
        else:
            standalone_units[leaf_sku] += qty
        order_ids[leaf_sku].add(guid)
        if leaf_sku not in titles and item.get("title"):
            titles[leaf_sku] = item["title"]

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
                subs = item.get("composite_sub_items") or []
                if sku_filter:
                    # Only explode the children of the requested parent SKU.
                    if subs and (item.get("sku") or "").lower() == sku_filter:
                        for s in subs:
                            _credit_leaves(s, guid, True)
                else:
                    _credit_leaves(item, guid, False)

        page += 1

    all_skus = set(comp_units) | set(standalone_units)

    def total_units(s: str) -> int:
        return comp_units.get(s, 0) + standalone_units.get(s, 0)

    ranked = sorted(all_skus, key=total_units, reverse=True)

    components = []
    for idx, s in enumerate(ranked[:top_n]):
        cu = comp_units.get(s, 0)
        su = standalone_units.get(s, 0)
        components.append({
            "rank": idx + 1,
            "sku": s,
            "title": titles.get(s, ""),
            "units": cu + su,
            "composite_units": cu,
            "standalone_units": su,
            "orders": len(order_ids[s]),
            "from_composite": cu > 0 and su == 0,
        })

    return {
        "from_date": from_date,
        "to_date": to_date,
        "date_field": date_field,
        "total_orders_scanned": total_orders_scanned,
        "parent_sku_filter": sku.strip(),
        "components": components,
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

def _po_line_cost_inc_tax(unit_cost_ex_tax: float, quantity: int, tax_rate: float) -> float:
    """
    Convert an ex-VAT *unit* cost into the value Linnworks expects in a purchase
    order line's `Cost` field.

    Per the Linnworks PurchaseOrder spec, the line `Cost` is the **tax-inclusive
    line total**: `(unit_cost * qty) + tax`. The MCP tools take an ex-VAT unit
    cost as their friendly `cost` parameter, so every PO line write must convert:

        Cost = unit_cost_ex_tax * quantity * (1 + tax_rate / 100)

    Sending the bare unit cost (the pre-fix bug, issue #15) made Linnworks treat
    it as the inclusive line total and back-derive a wrong unit price
    (`unit / 1.2 / qty`) and wrong PO grand totals.
    """
    return round(unit_cost_ex_tax * quantity * (1 + tax_rate / 100.0), 4)


def _po_line_unit_ex_tax(stored_cost_inc_tax: float, quantity: int, tax_rate: float) -> float:
    """
    Inverse of `_po_line_cost_inc_tax`: recover the ex-VAT unit cost from a stored
    Linnworks PO line `Cost` (which is the tax-inclusive line total).

    Used by read-before-write so update diffs and read-backs are expressed in the
    same ex-VAT unit terms the tools accept as input. Guards against divide-by-zero
    on zero-quantity / zero-rate lines.
    """
    q = quantity or 1
    denom = (1 + (tax_rate or 0.0) / 100.0) or 1.0
    return round(stored_cost_inc_tax / denom / q, 4)


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
            # The tax-inclusive line total is what actually gets written to the
            # Linnworks `Cost` field (see _po_line_cost_inc_tax / issue #15).
            "line_total_inc_tax": _po_line_cost_inc_tax(cost, qty, tax_rate),
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
                # Linnworks `Cost` = tax-inclusive line total, NOT the unit cost.
                "Cost": r["line_total_inc_tax"],
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
        # Header field is `TotalCost` (tax-inclusive); `GrandTotal` doesn't exist
        # on the header and returned None — kept as a last-resort fallback.
        "total_cost": confirmed_header.get("TotalCost") or confirmed_header.get("GrandTotal"),
        "tax_paid": confirmed_header.get("taxPaid"),
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
        # The tax-inclusive line total is what gets written to Linnworks `Cost`.
        "line_total_inc_tax": _po_line_cost_inc_tax(cost, quantity, tax_rate),
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
                # Linnworks `Cost` = tax-inclusive line total, NOT the unit cost.
                "Cost": item_detail["line_total_inc_tax"],
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

    # Step 2 — build diff: only record fields the caller explicitly provided.
    # The stored `Cost` is the tax-inclusive line total (Linnworks convention),
    # but this tool's `cost` parameter is an ex-VAT *unit* cost — so we work the
    # whole diff in ex-VAT unit terms and convert back to a line total on write.
    before: dict = {}
    after: dict = {}

    current_qty = current_item.get("Quantity") or 0
    current_tax_rate = current_item.get("TaxRate") or 0.0
    current_unit_ex = _po_line_unit_ex_tax(
        current_item.get("Cost") or 0.0, current_qty, current_tax_rate
    )

    new_quantity = current_qty
    new_unit_ex = current_unit_ex
    new_tax_rate = current_tax_rate

    if quantity is not None and quantity != current_qty:
        before["quantity"] = current_qty
        after["quantity"] = quantity
        new_quantity = quantity

    if cost is not None and round(cost, 4) != current_unit_ex:
        before["cost"] = current_unit_ex
        after["cost"] = round(cost, 4)
        new_unit_ex = cost

    if tax_rate is not None and tax_rate != current_tax_rate:
        before["tax_rate"] = current_tax_rate
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
                # Linnworks `Cost` = tax-inclusive line total — convert from the
                # ex-VAT unit cost the diff is tracked in (issue #15).
                "Cost": _po_line_cost_inc_tax(new_unit_ex, new_quantity, new_tax_rate),
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
            # Stored Cost is the tax-inclusive line total — present it back as the
            # ex-VAT unit cost so before/after stay in the same units.
            confirmed_after["cost"] = _po_line_unit_ex_tax(
                confirmed_item.get("Cost") or 0.0,
                confirmed_item.get("Quantity") or 0,
                confirmed_item.get("TaxRate") or 0.0,
            )
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


@mcp.tool()
def delete_purchase_order(
    purchase_ids: list[str],
    confirmed_count: int | None = None,
    dry_run: bool = True,
) -> dict:
    """
    PERMANENTLY delete one or more entire purchase orders (header + all lines).

    ⚠️  IRREVERSIBLE.  Deleting a PO destroys its header, every line item, and
    its notes — there is no API to restore it.  This is the whole-PO counterpart
    to remove_purchase_order_item (which removes a single line).  Use it to clear
    out mistaken, duplicate, or abandoned draft POs.

    The dry-run manifest and the staging gate are the main safety nets.  Always
    run dry_run=True first and read the manifest carefully before setting
    dry_run=False.

    ⚠️  A DELIVERED (or partially delivered) PO records goods actually received
    into stock — deleting it loses that history.  This tool BLOCKS PENDING/OPEN
    deletion only when you have not confirmed; DELIVERED and PARTIAL POs are
    flagged in the manifest with a "delivered" warning so you can decide, but are
    NOT auto-blocked (Linnworks itself allows the delete).  Prefer to only delete
    PENDING/OPEN draft POs.

    For batches larger than 10 POs this tool enters a staging mode: it returns a
    manifest of exactly what would be destroyed and asks you to confirm with
    confirmed_count=<N> before executing.

    Uses PurchaseOrder/Delete_PurchaseOrder (payload sent UNWRAPPED as
    {"pkPurchaseId": "<guid>"} — empty 2xx body on success; a subsequent
    Get_PurchaseOrder then returns HTTP 400 "Purchase Order does not exist",
    which is used as the per-PO read-back confirmation).

    Args:
        purchase_ids: List of purchase order UUIDs (pkPurchaseID), as returned by
            search_purchase_orders.  Each is read back first to capture a summary;
            a PO id that does not resolve is reported as an error row and does NOT
            abort the rest of the batch.
        confirmed_count: For batches > 10, pass len(purchase_ids) here after
            reviewing the manifest to confirm the destruction.
        dry_run: If True (default), returns the manifest of what would be deleted
            without writing.  Set to False to execute.

    Returns:
        A dict with:
          - dry_run:     whether this was a dry run
          - po_count:    number of PO ids in the batch
          - manifest:    per-PO preview (purchase_id, status, supplier_id,
                         line_count, total_cost, date_of_purchase, delivered flag,
                         resolved/error) — always present
          - results:     per-PO outcome with deleted flag (live run only)
          - deleted:     count of POs confirmed gone (live run only)
          - errors:      count of PO ids that failed to resolve or delete
    """
    if not purchase_ids:
        raise ValueError("purchase_ids must contain at least one PO id.")

    # ── Read-before-write: read each PO header for the manifest ───────────────
    manifest: list[dict] = []

    for pid in purchase_ids:
        pid = (pid or "").strip()
        if not pid:
            manifest.append({
                "purchase_id": pid, "resolved": False, "error": "empty PO id",
            })
            continue
        try:
            current = call_linnworks(
                "PurchaseOrder/Get_PurchaseOrder", {"pkPurchaseId": pid}
            )
        except RuntimeError as exc:
            manifest.append({
                "purchase_id": pid, "resolved": False,
                "error": f"not found: {exc}",
            })
            continue

        header = current.get("PurchaseOrderHeader") or {}
        status = header.get("Status", "")
        live_lines = [
            i for i in (current.get("PurchaseOrderItem") or [])
            if not i.get("IsDeleted")
        ]
        row = {
            "purchase_id":      header.get("pkPurchaseID") or pid,
            "status":           status,
            "status_label":     _PO_STATUS_LABELS.get(status, status),
            "supplier_id":      header.get("fkSupplierId"),
            "supplier_reference": header.get("SupplierReferenceNumber"),
            "line_count":       len(live_lines),
            "total_cost":       header.get("TotalCost"),
            "currency":         header.get("Currency"),
            "date_of_purchase": header.get("DateOfPurchase"),
            "delivered":        status in ("DELIVERED", "PARTIAL"),
            "resolved":         True,
        }
        if row["delivered"]:
            row["warning"] = (
                f"PO is {row['status_label']} — deleting loses received-stock "
                "history. Confirm this is intentional."
            )
        manifest.append(row)

    resolved_rows = [m for m in manifest if m.get("resolved")]
    resolve_errors = [m for m in manifest if not m.get("resolved")]

    # ── Write guard (threshold 10) ────────────────────────────────────────────
    guard = _write_guard("delete_purchase_order", purchase_ids, confirmed_count, dry_run)
    if guard is not None:
        return {**guard, "manifest": manifest}

    if dry_run:
        delivered_n = sum(1 for m in resolved_rows if m.get("delivered"))
        return {
            "dry_run":  True,
            "po_count": len(purchase_ids),
            "manifest": manifest,
            "message": (
                f"Dry run — nothing deleted. {len(resolved_rows)} PO(s) would be "
                f"PERMANENTLY deleted ({delivered_n} of them delivered/partial), "
                f"{len(resolve_errors)} could not be resolved. Review the manifest, "
                "then set dry_run=False to execute. This cannot be undone."
            ),
        }

    if not resolved_rows:
        return {
            "dry_run":  False,
            "po_count": len(purchase_ids),
            "deleted":  0,
            "errors":   len(resolve_errors),
            "results":  [],
            "manifest": manifest,
            "message":  "No PO ids resolved; nothing was deleted.",
        }

    # ── Live execution: delete each resolved PO, then read back ───────────────
    results = []
    deleted = 0
    errors = len(resolve_errors)
    for m in resolved_rows:
        pid = m["purchase_id"]
        delete_error: str | None = None
        try:
            call_linnworks(
                "PurchaseOrder/Delete_PurchaseOrder", {"pkPurchaseId": pid}
            )
        except RuntimeError as exc:
            delete_error = str(exc)

        # Read-back: a deleted PO makes Get_PurchaseOrder raise ("does not exist")
        gone = False
        try:
            call_linnworks("PurchaseOrder/Get_PurchaseOrder", {"pkPurchaseId": pid})
            gone = False  # still exists
        except RuntimeError:
            gone = True   # not found => deleted

        if gone:
            deleted += 1
        else:
            errors += 1

        row = {
            "purchase_id":  pid,
            "status_label": m.get("status_label"),
            "line_count":   m.get("line_count"),
            "total_cost":   m.get("total_cost"),
            "deleted":      gone,
        }
        if delete_error is not None:
            row["delete_error"] = delete_error
        if not gone:
            row["note"] = "still exists after delete call"
        results.append(row)

    for m in resolve_errors:
        results.append({
            "purchase_id": m["purchase_id"],
            "deleted":     False,
            "error":       m.get("error"),
        })

    return {
        "dry_run":  False,
        "po_count": len(purchase_ids),
        "deleted":  deleted,
        "errors":   errors,
        "results":  results,
        "manifest": manifest,
    }


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
            # `cost` / `line_total_inc_tax` is the raw Linnworks field: the
            # tax-inclusive line total. `unit_cost_ex_tax` is the derived ex-VAT
            # unit price that the create/add/update tools take as input — exposed
            # so read-back reconciles cleanly with what was written (issue #15).
            "cost": i.get("Cost"),
            "line_total_inc_tax": i.get("Cost"),
            "unit_cost_ex_tax": _po_line_unit_ex_tax(
                i.get("Cost") or 0.0, i.get("Quantity") or 0, i.get("TaxRate") or 0.0
            ),
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


@mcp.tool()
def run_import(import_id: int, dry_run: bool = True) -> dict:
    """
    Trigger an existing Linnworks import profile to run immediately.

    Puts the import into the queue — Linnworks picks it up within seconds.
    Use this to manually fire an import that would otherwise wait for its
    next scheduled run, or to run an import on demand (e.g. after uploading
    a new supplier catalogue CSV).

    Always reads the import configuration first so you can confirm exactly
    which import will run, what feed URL it reads from, and what type of
    Linnworks data it updates — before anything is triggered.

    Refuses to queue if the import is already executing or already queued,
    preventing accidental double-runs.

    Args:
        import_id: Integer ID of the import to trigger (from get_import_list).
        dry_run:   If True (default), shows the full import config and what
                   would be triggered — without actually queuing it.
                   Set to False to queue the import for immediate execution.

    Returns:
        A dict with:
          - dry_run:        whether this was a preview only
          - import_id:      the ID queried
          - friendly_name:  human-readable import name
          - type:           the import type (e.g. "Inventory", "StockLevel")
          - feed_url:       source file URL the import reads from
          - enabled:        whether the import is enabled in Linnworks
          - was_executing:  whether it was already running at read time
          - was_queued:     whether it was already queued at read time
          - queued:         True if successfully queued (live run only)
          - now_executing:  post-trigger executing state (live run only)
          - now_queued:     post-trigger queued state (live run only)
          - message:        human-readable status summary
    """
    # ── Read before run ───────────────────────────────────────────────────────
    response = call_linnworks_get("ImportExport/GetImport", params={"id": import_id})
    register = response.get("Register") or {}
    spec     = response.get("Specification") or {}
    feed     = spec.get("Feed") or {}

    friendly_name = register.get("FriendlyName") or f"Import {import_id}"
    import_type   = register.get("Type")
    enabled       = register.get("Enabled", False)
    was_executing = register.get("Executing", False)
    was_queued    = register.get("IsQueued", False)
    feed_url      = feed.get("Url") or feed.get("FileUrl") or feed.get("FeedUrl")

    base = {
        "import_id":     import_id,
        "friendly_name": friendly_name,
        "type":          import_type,
        "feed_url":      feed_url,
        "enabled":       enabled,
        "was_executing": was_executing,
        "was_queued":    was_queued,
    }

    # ── Guards ────────────────────────────────────────────────────────────────
    if was_executing:
        return {
            **base, "dry_run": dry_run, "queued": False,
            "message": (
                f"Import '{friendly_name}' is already executing — "
                f"not queued again to avoid a double-run."
            ),
        }

    if was_queued:
        return {
            **base, "dry_run": dry_run, "queued": False,
            "message": (
                f"Import '{friendly_name}' is already queued — "
                f"not queued again to avoid a double-run."
            ),
        }

    if dry_run:
        return {
            **base, "dry_run": True, "queued": False,
            "message": (
                f"Dry run — would queue import '{friendly_name}' "
                f"(type: {import_type}, feed: {feed_url or 'not configured'}). "
                f"Set dry_run=False to trigger."
            ),
        }

    # ── Live: queue the import ────────────────────────────────────────────────
    call_linnworks_void("ImportExport/RunNowImport", {"importId": import_id})

    # Read back to confirm state
    readback    = call_linnworks_get("ImportExport/GetImport", params={"id": import_id})
    rb_register = readback.get("Register") or {}
    now_queued    = rb_register.get("IsQueued", False)
    now_executing = rb_register.get("Executing", False)

    return {
        **base,
        "dry_run":       False,
        "queued":        now_queued or now_executing,
        "now_executing": now_executing,
        "now_queued":    now_queued,
        "message": (
            f"Import '{friendly_name}' queued successfully — "
            f"Linnworks will execute it momentarily."
            if (now_queued or now_executing)
            else (
                f"Import '{friendly_name}' triggered (RunNowImport accepted). "
                f"Read-back shows not yet queued — Linnworks may have picked it "
                f"up instantly or there may be a brief delay before state updates."
            )
        ),
    }


@mcp.tool()
def run_export(export_id: int, dry_run: bool = True) -> dict:
    """
    Trigger an existing Linnworks export profile to run immediately.

    Puts the export into the queue — Linnworks picks it up within seconds.
    Use this to manually fire an export that would otherwise wait for its
    next scheduled run, or to produce an on-demand data extract.

    Always reads the export configuration first so you can confirm exactly
    which export will run and what it produces — before anything is triggered.

    Refuses to queue if the export is already executing or already queued,
    preventing accidental double-runs.

    Args:
        export_id: Integer ID of the export to trigger (from get_export_list).
        dry_run:   If True (default), shows the export config and what would
                   be triggered — without actually queuing it.
                   Set to False to queue the export for immediate execution.

    Returns:
        A dict with:
          - dry_run:        whether this was a preview only
          - export_id:      the ID queried
          - friendly_name:  human-readable export name
          - type:           the export type
          - enabled:        whether the export is enabled in Linnworks
          - was_executing:  whether it was already running at read time
          - was_queued:     whether it was already queued at read time
          - queued:         True if successfully queued (live run only)
          - now_executing:  post-trigger executing state (live run only)
          - now_queued:     post-trigger queued state (live run only)
          - message:        human-readable status summary
    """
    # ── Read before run ───────────────────────────────────────────────────────
    response = call_linnworks_get("ImportExport/GetExport", params={"id": export_id})
    register = response.get("Register") or {}

    friendly_name = register.get("FriendlyName") or f"Export {export_id}"
    export_type   = register.get("Type")
    enabled       = register.get("Enabled", False)
    was_executing = register.get("Executing", False)
    was_queued    = register.get("IsQueued", False)

    base = {
        "export_id":     export_id,
        "friendly_name": friendly_name,
        "type":          export_type,
        "enabled":       enabled,
        "was_executing": was_executing,
        "was_queued":    was_queued,
    }

    # ── Guards ────────────────────────────────────────────────────────────────
    if was_executing:
        return {
            **base, "dry_run": dry_run, "queued": False,
            "message": (
                f"Export '{friendly_name}' is already executing — "
                f"not queued again to avoid a double-run."
            ),
        }

    if was_queued:
        return {
            **base, "dry_run": dry_run, "queued": False,
            "message": (
                f"Export '{friendly_name}' is already queued — "
                f"not queued again to avoid a double-run."
            ),
        }

    if dry_run:
        return {
            **base, "dry_run": True, "queued": False,
            "message": (
                f"Dry run — would queue export '{friendly_name}' "
                f"(type: {export_type}). "
                f"Set dry_run=False to trigger."
            ),
        }

    # ── Live: queue the export ─────────────────────────────────────────────────
    call_linnworks_void("ImportExport/RunNowExport", {"exportId": export_id})

    readback    = call_linnworks_get("ImportExport/GetExport", params={"id": export_id})
    rb_register = readback.get("Register") or {}
    now_queued    = rb_register.get("IsQueued", False)
    now_executing = rb_register.get("Executing", False)

    return {
        **base,
        "dry_run":       False,
        "queued":        now_queued or now_executing,
        "now_executing": now_executing,
        "now_queued":    now_queued,
        "message": (
            f"Export '{friendly_name}' queued successfully — "
            f"Linnworks will execute it momentarily."
            if (now_queued or now_executing)
            else (
                f"Export '{friendly_name}' triggered (RunNowExport accepted). "
                f"Read-back shows not yet queued — Linnworks may have picked it "
                f"up instantly or there may be a brief delay before state updates."
            )
        ),
    }


# ---------- Inventory write helpers ----------

def _resolve_sku_to_id(sku: str, cache: dict | None = None) -> str:
    """
    Resolve a SKU string to its Linnworks StockItemId (GUID).

    Uses Inventory/GetInventoryItem (exact SKU match only — no fuzzy search).
    Raises ValueError if the SKU is not found.

    Args:
        sku:   The exact SKU / item number.
        cache: Optional dict for within-call deduplication.  If provided,
               already-resolved SKUs are returned from cache without an API call.
    """
    if cache is not None and sku in cache:
        return cache[sku]
    try:
        item = call_linnworks("Inventory/GetInventoryItem", {"sku": sku})
    except RuntimeError as exc:
        raise ValueError(f"SKU '{sku}' not found in Linnworks: {exc}") from exc
    stock_item_id = item.get("StockItemId")
    if not stock_item_id:
        raise ValueError(f"SKU '{sku}' was found but returned no StockItemId.")
    if cache is not None:
        cache[sku] = stock_item_id
    return stock_item_id


# ---------- Inventory write tools ----------


@mcp.tool()
def create_or_update_inventory_item(
    items: list[dict],
    confirmed_count: int | None = None,
    dry_run: bool = True,
) -> dict:
    """
    Create or update inventory items in Linnworks (upsert by SKU).

    Each item is looked up by its SKU.  If the SKU already exists the item is
    updated (UpdateInventoryItem); if it does not exist it is created
    (AddInventoryItem).  Only the fields you supply are changed on updates —
    but because the Linnworks UpdateInventoryItem endpoint requires a full
    payload, all existing field values are read first and merged with your
    changes so that unsupplied fields are preserved.

    For batches larger than 50 items this tool enters a staging mode: it returns
    a manifest preview and asks you to confirm with confirmed_count=<N> before
    executing.  This prevents accidental large-scale inventory changes.

    Args:
        items: List of dicts.  Required key: "sku".  Optional keys:
            - title (str):          ItemTitle
            - barcode (str):        BarcodeNumber
            - retail_price (float): RetailPrice
            - purchase_price (float): PurchasePrice
            - tax_rate (float):     TaxRate (e.g. 20.0 for 20%)
            - category_name (str):  CategoryName (Linnworks auto-resolves to ID)
            - weight (float):       Weight in kg
            - height (float):       Height in cm
            - width (float):        Width in cm
            - depth (float):        Depth in cm
            - metadata (str):       MetaData free-text field
            - barcode (str):        BarcodeNumber
        confirmed_count: For batches > 50 items, pass len(items) here after
            reviewing the manifest to confirm the write.
        dry_run: If True (default), returns the manifest without writing.
            Set to False to execute.

    Returns:
        A dict with:
          - dry_run:      whether this was a dry run
          - item_count:   number of items in the batch
          - manifest:     per-item preview (always present)
          - results:      per-item outcome (live run only)
          - created:      count of newly created items (live run only)
          - updated:      count of updated items (live run only)
          - errors:       count of failed items (live run only)
    """
    # ── Injection check on all free-text fields ───────────────────────────────
    for entry in items:
        _check_injection("title",     entry.get("title", ""))
        _check_injection("barcode",   entry.get("barcode", ""))
        _check_injection("metadata",  entry.get("metadata", ""))

    # ── Build manifest preview ────────────────────────────────────────────────
    manifest = [
        {
            "sku":            entry.get("sku"),
            "title":          entry.get("title"),
            "barcode":        entry.get("barcode"),
            "retail_price":   entry.get("retail_price"),
            "purchase_price": entry.get("purchase_price"),
            "tax_rate":       entry.get("tax_rate"),
            "category_name":  entry.get("category_name"),
            "weight":         entry.get("weight"),
            "height":         entry.get("height"),
            "width":          entry.get("width"),
            "depth":          entry.get("depth"),
            "metadata":       entry.get("metadata"),
        }
        for entry in items
    ]

    # ── Write guard ───────────────────────────────────────────────────────────
    guard = _write_guard("create_or_update_inventory_item", items, confirmed_count, dry_run)
    if guard is not None:
        return {**guard, "manifest": manifest}

    if dry_run:
        return {
            "dry_run": True,
            "item_count": len(items),
            "manifest": manifest,
            "message": "Dry run — no changes written. Set dry_run=False to execute.",
        }

    # ── Live execution ─────────────────────────────────────────────────────────
    sku_cache: dict[str, str] = {}
    results = []
    created = 0
    updated = 0
    errors  = 0

    for entry in items:
        sku = (entry.get("sku") or "").strip()
        if not sku:
            results.append({"sku": "", "action": "error", "error": "Missing 'sku' field."})
            errors += 1
            continue

        try:
            # Probe whether the item exists.
            try:
                existing = call_linnworks("Inventory/GetInventoryItem", {"sku": sku})
                stock_item_id = existing.get("StockItemId")
                action = "update"
            except RuntimeError:
                existing = None
                stock_item_id = None
                action = "create"

            if action == "update":
                # Merge: start from the existing item, overlay supplied fields.
                item_payload = {
                    "StockItemId":            stock_item_id,
                    "ItemNumber":             existing.get("ItemNumber", sku),
                    "ItemTitle":              entry.get("title")          or existing.get("ItemTitle", ""),
                    "BarcodeNumber":          entry.get("barcode")        or existing.get("BarcodeNumber", ""),
                    "RetailPrice":            entry.get("retail_price")   if entry.get("retail_price") is not None else existing.get("RetailPrice"),
                    "PurchasePrice":          entry.get("purchase_price") if entry.get("purchase_price") is not None else existing.get("PurchasePrice"),
                    "TaxRate":                entry.get("tax_rate")       if entry.get("tax_rate") is not None else existing.get("TaxRate"),
                    "CategoryName":           entry.get("category_name")  or existing.get("CategoryName", ""),
                    "CategoryId":             existing.get("CategoryId", ""),
                    "Weight":                 entry.get("weight")         if entry.get("weight") is not None else existing.get("Weight"),
                    "Height":                 entry.get("height")         if entry.get("height") is not None else existing.get("Height"),
                    "Width":                  entry.get("width")          if entry.get("width") is not None else existing.get("Width"),
                    "Depth":                  entry.get("depth")          if entry.get("depth") is not None else existing.get("Depth"),
                    "MetaData":               entry.get("metadata")       or existing.get("MetaData", ""),
                    "IsCompositeParent":      existing.get("IsCompositeParent", False),
                    "ShippedSeparately":      existing.get("ShippedSeparately", False),
                    "IsVariationParent":      existing.get("IsVariationParent", False),
                    "isBatchedStockType":     existing.get("isBatchedStockType", False),
                    "PostalServiceId":        existing.get("PostalServiceId", ""),
                    "PostalServiceName":      existing.get("PostalServiceName", ""),
                    "PackageGroupId":         existing.get("PackageGroupId", ""),
                    "PackageGroupName":       existing.get("PackageGroupName", ""),
                    "InventoryTrackingType":  existing.get("InventoryTrackingType", 0),
                    "BatchNumberScanRequired":existing.get("BatchNumberScanRequired", False),
                    "SerialNumberScanRequired":existing.get("SerialNumberScanRequired", False),
                }
                call_linnworks("Inventory/UpdateInventoryItem", {"inventoryItem": item_payload})
                updated += 1
                results.append({
                    "sku": sku, "action": "updated", "stock_item_id": stock_item_id,
                    "title": item_payload["ItemTitle"],
                })

            else:
                # Create path — only the fields we have.
                # Linnworks AddInventoryItem requires a client-generated
                # StockItemId GUID even for new items; omitting it returns
                # HTTP 400 "StockItem StockItemId could not be empty"
                # (confirmed live 15 Jun 2026). Generate one and reuse it as
                # the new item's ID.
                new_guid = str(uuid.uuid4())
                item_payload = {
                    "StockItemId":   new_guid,
                    "ItemNumber":    sku,
                    "ItemTitle":     entry.get("title", ""),
                    "BarcodeNumber": entry.get("barcode", ""),
                    "RetailPrice":   entry.get("retail_price", 0.0),
                    "PurchasePrice": entry.get("purchase_price", 0.0),
                    "TaxRate":       entry.get("tax_rate", 0.0),
                    "CategoryName":  entry.get("category_name", ""),
                    "Weight":        entry.get("weight", 0.0),
                    "Height":        entry.get("height", 0.0),
                    "Width":         entry.get("width", 0.0),
                    "Depth":         entry.get("depth", 0.0),
                    "MetaData":      entry.get("metadata", ""),
                }
                resp = call_linnworks("Inventory/AddInventoryItem", {"inventoryItem": item_payload})
                new_id = (resp.get("fkStockItemId") if isinstance(resp, dict) else None) or new_guid
                created += 1
                results.append({
                    "sku": sku, "action": "created", "stock_item_id": new_id,
                    "title": item_payload["ItemTitle"],
                })

        except Exception as exc:
            errors += 1
            results.append({"sku": sku, "action": "error", "error": str(exc)})

    return {
        "dry_run":    False,
        "item_count": len(items),
        "created":    created,
        "updated":    updated,
        "errors":     errors,
        "results":    results,
    }


@mcp.tool()
def set_stock_levels(
    updates: list[dict],
    confirmed_count: int | None = None,
    dry_run: bool = True,
) -> dict:
    """
    Set absolute stock levels for one or more SKUs in Linnworks.

    WARNING: This overwrites live stock figures immediately and affects
    channel availability (Amazon, eBay, Shopify) in near-real-time.
    Always use dry_run=True first to review the changes.

    For batches larger than 25 items this tool enters a staging mode: it
    returns a manifest showing current → new stock levels and asks you to
    confirm with confirmed_count=<N> before executing.

    Uses Stock/UpdateStockLevelsBulk which returns per-item errors so every
    line can be individually reported.

    Args:
        updates: List of dicts, each with:
            - sku (str):         The item's SKU / ItemNumber  [required]
            - stock_level (int): The new absolute stock level  [required]
            - location_id (str): Linnworks location UUID.
                                 Defaults to the Default location.
        confirmed_count: For batches > 25, pass len(updates) here after
            reviewing the manifest to confirm the write.
        dry_run: If True (default), returns a before/after manifest without
            writing. Set to False to execute.

    Returns:
        A dict with:
          - dry_run:     whether this was a dry run
          - item_count:  number of SKUs in the batch
          - manifest:    per-item before/after preview (always present)
          - results:     per-item outcome with errors (live run only)
          - errors:      count of SKUs that failed (live run only)
    """
    # ── Collect SKUs ──────────────────────────────────────────────────────────
    skus = [(u.get("sku") or "").strip() for u in updates]
    for i, s in enumerate(skus):
        if not s:
            raise ValueError(f"updates[{i}] is missing 'sku'.")
    for u in updates:
        if u.get("stock_level") is None:
            raise ValueError(f"updates entry for SKU '{u.get('sku')}' is missing 'stock_level'.")

    # ── Read current stock levels (read-before-write diff) ────────────────────
    sku_cache: dict[str, str] = {}
    current_levels: dict[str, int | None] = {}

    for sku in skus:
        try:
            stock_item_id = _resolve_sku_to_id(sku, sku_cache)
            levels_resp = call_linnworks(
                "Stock/GetStockLevel_Batch",
                {"request": {"StockItemIds": [stock_item_id]}},
            )
            # Response is a list of location rows per item
            rows = levels_resp if isinstance(levels_resp, list) else (levels_resp.get("StockItemLevels") or [])
            # Find the matching location row
            for u in updates:
                if (u.get("sku") or "").strip() == sku:
                    target_loc = u.get("location_id", DEFAULT_LOCATION_ID)
            loc_level = None
            for row_group in rows:
                for row in (row_group if isinstance(row_group, list) else [row_group]):
                    if row.get("Location", {}).get("StockLocationId") == target_loc:
                        loc_level = row.get("StockLevel")
                        break
            current_levels[sku] = loc_level
        except (ValueError, RuntimeError):
            current_levels[sku] = None

    # ── Build manifest ────────────────────────────────────────────────────────
    manifest = []
    for u in updates:
        sku = (u.get("sku") or "").strip()
        new_level = u.get("stock_level")
        loc_id = u.get("location_id", DEFAULT_LOCATION_ID)
        current = current_levels.get(sku)
        delta = None if current is None else (new_level - current)
        manifest.append({
            "sku":              sku,
            "location_id":      loc_id,
            "current_level":    current,
            "new_level":        new_level,
            "delta":            delta,
        })

    # ── Write guard ───────────────────────────────────────────────────────────
    guard = _write_guard("set_stock_levels", updates, confirmed_count, dry_run)
    if guard is not None:
        return {**guard, "manifest": manifest}

    if dry_run:
        return {
            "dry_run":     True,
            "item_count":  len(updates),
            "manifest":    manifest,
            "message": (
                "Dry run — no stock levels changed. "
                "Review the manifest and set dry_run=False to execute."
            ),
        }

    # ── Live execution ─────────────────────────────────────────────────────────
    bulk_items = []
    for u in updates:
        sku = (u.get("sku") or "").strip()
        try:
            stock_item_id = _resolve_sku_to_id(sku, sku_cache)
        except ValueError:
            stock_item_id = ""
        bulk_items.append({
            "SKU":             sku,
            "StockItemId":     stock_item_id,
            "StockLocationId": u.get("location_id", DEFAULT_LOCATION_ID),
            "StockLevel":      u.get("stock_level"),
        })

    response = call_linnworks("Stock/UpdateStockLevelsBulk", {"Items": bulk_items})
    response_items = response.get("Items") or []

    results = []
    errors = 0

    # UpdateStockLevelsBulk returns a 2xx with an EMPTY body on success on this
    # tenant — it does NOT echo the Items array (confirmed live 15 Jun 2026). When
    # nothing is echoed, treat every submitted line as applied (a non-2xx would
    # have raised in call_linnworks). Read back with get_stock_level to verify.
    if not response_items:
        for bi in bulk_items:
            results.append({
                "sku":         bi.get("SKU"),
                "location_id": bi.get("StockLocationId"),
                "new_level":   bi.get("StockLevel"),
                "success":     True,
                "errors":      [],
                "note":        "API returned no content; success inferred from 2xx. Verify with get_stock_level.",
            })
        return {
            "dry_run":    False,
            "item_count": len(updates),
            "errors":     errors,
            "results":    results,
            "manifest":   manifest,
        }

    for idx, item in enumerate(response_items):
        errs = item.get("Errors") or []
        ok = len(errs) == 0
        if not ok:
            errors += 1
        results.append({
            "sku":         item.get("SKU"),
            "location_id": item.get("StockLocationId"),
            "new_level":   item.get("StockLevel"),
            "success":     ok,
            "errors":      errs,
        })

    return {
        "dry_run":    False,
        "item_count": len(updates),
        "errors":     errors,
        "results":    results,
        "manifest":   manifest,
    }


@mcp.tool()
def set_inventory_item_prices(
    prices: list[dict],
    confirmed_count: int | None = None,
    dry_run: bool = True,
) -> dict:
    """
    Set or update channel prices for one or more inventory items in Linnworks.

    Prices in Linnworks are keyed by (StockItemId, Source, SubSource).  This
    tool reads existing price rows for each SKU, then creates or updates rows
    as needed.  Pass source="" and sub_source="" to set the default price row.

    WARNING: Price changes are pushed to connected sales channels
    (Amazon, eBay, Shopify) in near-real-time.  Always use dry_run=True
    first to review the manifest.

    For batches larger than 25 items this tool requires confirmed_count=<N>.

    Args:
        prices: List of dicts, each with:
            - sku (str):         Item SKU  [required]
            - price (float):     The new price  [required]
            - source (str):      Channel source (e.g. "AMAZON", "EBAY", "").
                                 Defaults to "" (default price row).
            - sub_source (str):  Channel sub-source (e.g. "DEFAULT").
                                 Defaults to "".
        confirmed_count: For batches > 25, pass len(prices) here.
        dry_run: If True (default), returns a before/after manifest without
            writing. Set to False to execute.

    Returns:
        A dict with:
          - dry_run:     whether this was a dry run
          - item_count:  number of price rows in the batch
          - manifest:    per-item before/after preview (always present)
          - results:     per-item outcome (live run only)
          - created:     new rows created (live run only)
          - updated:     existing rows updated (live run only)
          - errors:      rows that failed (live run only)
    """
    # ── Validate ──────────────────────────────────────────────────────────────
    for i, p in enumerate(prices):
        if not p.get("sku"):
            raise ValueError(f"prices[{i}] is missing 'sku'.")
        if p.get("price") is None:
            raise ValueError(f"prices[{i}] (SKU '{p.get('sku')}') is missing 'price'.")

    sku_cache: dict[str, str] = {}

    # ── Read current prices + build manifest ──────────────────────────────────
    manifest = []
    # {stock_item_id: [existing price rows]}
    existing_prices: dict[str, list] = {}

    for p in prices:
        sku        = p["sku"].strip()
        source     = p.get("source", "")
        sub_source = p.get("sub_source", "")
        new_price  = p["price"]

        try:
            stock_item_id = _resolve_sku_to_id(sku, sku_cache)
        except ValueError as exc:
            manifest.append({
                "sku": sku, "source": source, "sub_source": sub_source,
                "new_price": new_price, "current_price": None, "error": str(exc),
            })
            continue

        if stock_item_id not in existing_prices:
            rows = call_linnworks_get(
                "Inventory/GetInventoryItemPrices",
                params={"inventoryItemId": stock_item_id},
            )
            existing_prices[stock_item_id] = rows if isinstance(rows, list) else []

        # Find matching row
        match = next(
            (r for r in existing_prices[stock_item_id]
             if r.get("Source", "") == source and r.get("SubSource", "") == sub_source),
            None,
        )
        # Decide create vs update. The DEFAULT price row (empty Source+SubSource)
        # exists implicitly with the zero GUID but is NOT returned by
        # GetInventoryItemPrices — so creating it collides on PK_StockItem_Pricing.
        # Update the zero-GUID row instead. Channel rows that genuinely don't
        # exist are created (with a fresh pkRowId — see live-execution block).
        # Confirmed live 15 Jun 2026.
        if match:
            action, pk_row_id = "update", match.get("pkRowId")
        elif source == "" and sub_source == "":
            action, pk_row_id = "update", "00000000-0000-0000-0000-000000000000"
        else:
            action, pk_row_id = "create", None
        manifest.append({
            "sku":           sku,
            "stock_item_id": stock_item_id,
            "source":        source,
            "sub_source":    sub_source,
            "current_price": match.get("Price") if match else None,
            "new_price":     new_price,
            "action":        action,
            "pk_row_id":     pk_row_id,
        })

    # ── Write guard ───────────────────────────────────────────────────────────
    guard = _write_guard("set_inventory_item_prices", prices, confirmed_count, dry_run)
    if guard is not None:
        return {**guard, "manifest": manifest}

    if dry_run:
        return {
            "dry_run":    True,
            "item_count": len(prices),
            "manifest":   manifest,
            "message":    "Dry run — no prices changed. Set dry_run=False to execute.",
        }

    # ── Live execution ─────────────────────────────────────────────────────────
    to_create = [m for m in manifest if m.get("action") == "create" and not m.get("error")]
    to_update = [m for m in manifest if m.get("action") == "update" and not m.get("error")]
    error_rows = [m for m in manifest if m.get("error")]

    results   = []
    created   = 0
    updated   = 0
    errors    = len(error_rows)

    if to_create:
        create_payload = [
            {
                "pkRowId":     str(uuid.uuid4()),  # Linnworks needs a client-supplied GUID
                "StockItemId": m["stock_item_id"],
                "Source":      m["source"],
                "SubSource":   m["sub_source"],
                "Price":       m["new_price"],
            }
            for m in to_create
        ]
        call_linnworks(
            "Inventory/CreateInventoryItemPrices",
            {"inventoryItemPrices": create_payload},
        )
        for m in to_create:
            created += 1
            results.append({
                "sku": m["sku"], "action": "created",
                "source": m["source"], "sub_source": m["sub_source"],
                "new_price": m["new_price"],
            })

    if to_update:
        update_payload = [
            {
                "StockItemId": m["stock_item_id"],
                "pkRowId":     m["pk_row_id"],
                "Source":      m["source"],
                "SubSource":   m["sub_source"],
                "Price":       m["new_price"],
            }
            for m in to_update
        ]
        call_linnworks(
            "Inventory/UpdateInventoryItemPrices",
            {"inventoryItemPrices": update_payload},
        )
        for m in to_update:
            updated += 1
            results.append({
                "sku": m["sku"], "action": "updated",
                "source": m["source"], "sub_source": m["sub_source"],
                "old_price": m["current_price"], "new_price": m["new_price"],
            })

    for m in error_rows:
        results.append({"sku": m["sku"], "action": "error", "error": m["error"]})

    return {
        "dry_run":    False,
        "item_count": len(prices),
        "created":    created,
        "updated":    updated,
        "errors":     errors,
        "results":    results,
        "manifest":   manifest,
    }


@mcp.tool()
def set_extended_properties(
    properties: list[dict],
    confirmed_count: int | None = None,
    dry_run: bool = True,
) -> dict:
    """
    Create or update extended properties on Linnworks inventory items.

    Extended properties are key/value metadata pairs attached to a stock item
    (e.g. "Colour": "Red", "Material": "Maple").  This tool upserts each
    property: if a property with the given name already exists on the item it
    is updated; if not, it is created.

    Note: The Linnworks API has a confirmed typo in the field name — "ProperyName"
    (one 't') — which this tool handles transparently.

    For batches larger than 50 items this tool requires confirmed_count=<N>.

    Args:
        properties: List of dicts, each with:
            - sku (str):            Item SKU  [required]
            - property_name (str):  Name of the extended property  [required]
            - property_value (str): Value to set  [required]
            - property_type (str):  Property type label (e.g. "Attribute",
                                    "Specification"). Defaults to "Attribute".
        confirmed_count: For batches > 50, pass len(properties) here.
        dry_run: If True (default), returns a manifest without writing.
            Set to False to execute.

    Returns:
        A dict with:
          - dry_run:     whether this was a dry run
          - item_count:  number of property rows in the batch
          - manifest:    per-item preview (always present)
          - results:     per-item outcome (live run only)
          - created:     new properties created (live run only)
          - updated:     existing properties updated (live run only)
          - errors:      rows that failed (live run only)
    """
    # ── Injection check ───────────────────────────────────────────────────────
    for p in properties:
        _check_injection("property_name",  p.get("property_name", ""))
        _check_injection("property_value", p.get("property_value", ""))

    # ── Validate ──────────────────────────────────────────────────────────────
    for i, p in enumerate(properties):
        if not p.get("sku"):
            raise ValueError(f"properties[{i}] is missing 'sku'.")
        if not p.get("property_name"):
            raise ValueError(f"properties[{i}] (SKU '{p.get('sku')}') is missing 'property_name'.")
        if p.get("property_value") is None:
            raise ValueError(f"properties[{i}] (SKU '{p.get('sku')}') is missing 'property_value'.")

    sku_cache: dict[str, str] = {}
    # {stock_item_id: [existing property rows]}
    existing_props: dict[str, list] = {}

    # ── Read current props + build manifest ───────────────────────────────────
    manifest = []
    for p in properties:
        sku      = p["sku"].strip()
        prop_name = p["property_name"]
        prop_val  = str(p["property_value"])
        prop_type = p.get("property_type", "Attribute")

        try:
            stock_item_id = _resolve_sku_to_id(sku, sku_cache)
        except ValueError as exc:
            manifest.append({
                "sku": sku, "property_name": prop_name,
                "property_value": prop_val, "error": str(exc),
            })
            continue

        if stock_item_id not in existing_props:
            rows = call_linnworks(
                "Inventory/GetInventoryItemExtendedProperties",
                {"inventoryItemId": stock_item_id},
            )
            existing_props[stock_item_id] = rows if isinstance(rows, list) else []

        # Note the confirmed API typo: "ProperyName" not "PropertyName"
        match = next(
            (r for r in existing_props[stock_item_id] if r.get("ProperyName") == prop_name),
            None,
        )
        manifest.append({
            "sku":             sku,
            "stock_item_id":   stock_item_id,
            "property_name":   prop_name,
            "old_value":       match.get("PropertyValue") if match else None,
            "new_value":       prop_val,
            "property_type":   prop_type,
            "action":          "update" if match else "create",
            "pk_row_id":       match.get("pkRowId") if match else None,
        })

    # ── Write guard ───────────────────────────────────────────────────────────
    guard = _write_guard("set_extended_properties", properties, confirmed_count, dry_run)
    if guard is not None:
        return {**guard, "manifest": manifest}

    if dry_run:
        return {
            "dry_run":    True,
            "item_count": len(properties),
            "manifest":   manifest,
            "message":    "Dry run — no properties changed. Set dry_run=False to execute.",
        }

    # ── Live execution — batch creates and updates separately ─────────────────
    to_create = [m for m in manifest if m.get("action") == "create" and not m.get("error")]
    to_update = [m for m in manifest if m.get("action") == "update" and not m.get("error")]
    error_rows = [m for m in manifest if m.get("error")]

    results = []
    created = 0
    updated = 0
    errors  = len(error_rows)

    if to_create:
        create_payload = [
            {
                "pkRowId":       str(uuid.uuid4()),  # Linnworks needs a client-supplied GUID
                "fkStockItemId": m["stock_item_id"],
                "SKU":           m["sku"],
                "ProperyName":   m["property_name"],   # deliberate API typo
                "PropertyValue": m["new_value"],
                "PropertyType":  m["property_type"],
            }
            for m in to_create
        ]
        call_linnworks(
            "Inventory/CreateInventoryItemExtendedProperties",
            {"inventoryItemExtendedProperties": create_payload},
        )
        for m in to_create:
            created += 1
            results.append({
                "sku": m["sku"], "action": "created",
                "property_name": m["property_name"], "value": m["new_value"],
            })

    if to_update:
        update_payload = [
            {
                "fkStockItemId": m["stock_item_id"],
                "pkRowId":       m["pk_row_id"],
                "ProperyName":   m["property_name"],   # deliberate API typo
                "PropertyValue": m["new_value"],
                "PropertyType":  m["property_type"],
            }
            for m in to_update
        ]
        call_linnworks(
            "Inventory/UpdateInventoryItemExtendedProperties",
            {"inventoryItemExtendedProperties": update_payload},
        )
        for m in to_update:
            updated += 1
            results.append({
                "sku": m["sku"], "action": "updated",
                "property_name": m["property_name"],
                "old_value": m["old_value"], "new_value": m["new_value"],
            })

    for m in error_rows:
        results.append({"sku": m["sku"], "action": "error", "error": m["error"]})

    return {
        "dry_run":    False,
        "item_count": len(properties),
        "created":    created,
        "updated":    updated,
        "errors":     errors,
        "results":    results,
        "manifest":   manifest,
    }


@mcp.tool()
def set_inventory_item_descriptions(
    descriptions: list[dict],
    confirmed_count: int | None = None,
    dry_run: bool = True,
) -> dict:
    """
    Create or update channel-specific descriptions on Linnworks inventory items.

    Descriptions in Linnworks are keyed by (StockItemId, Source, SubSource),
    allowing different description text per channel.  Pass source="" and
    sub_source="" to set the default description.

    For batches larger than 50 items this tool requires confirmed_count=<N>.

    Args:
        descriptions: List of dicts, each with:
            - sku (str):         Item SKU  [required]
            - description (str): The description text  [required]
            - source (str):      Channel source (e.g. "AMAZON"). Defaults to "".
            - sub_source (str):  Channel sub-source. Defaults to "".
        confirmed_count: For batches > 50, pass len(descriptions) here.
        dry_run: If True (default), returns a manifest without writing.
            Set to False to execute.

    Returns:
        A dict with:
          - dry_run:     whether this was a dry run
          - item_count:  number of description rows in the batch
          - manifest:    per-item preview (always present)
          - results:     per-item outcome (live run only)
          - created:     new rows created (live run only)
          - updated:     existing rows updated (live run only)
          - errors:      rows that failed (live run only)
    """
    # ── Injection check ───────────────────────────────────────────────────────
    for d in descriptions:
        _check_injection("description", d.get("description", ""))

    # ── Validate ──────────────────────────────────────────────────────────────
    for i, d in enumerate(descriptions):
        if not d.get("sku"):
            raise ValueError(f"descriptions[{i}] is missing 'sku'.")
        if d.get("description") is None:
            raise ValueError(f"descriptions[{i}] (SKU '{d.get('sku')}') is missing 'description'.")

    sku_cache: dict[str, str] = {}
    existing_descs: dict[str, list] = {}

    # ── Read existing + build manifest ────────────────────────────────────────
    manifest = []
    for d in descriptions:
        sku        = d["sku"].strip()
        source     = d.get("source", "")
        sub_source = d.get("sub_source", "")
        new_desc   = d["description"]

        try:
            stock_item_id = _resolve_sku_to_id(sku, sku_cache)
        except ValueError as exc:
            manifest.append({
                "sku": sku, "source": source, "sub_source": sub_source,
                "description": new_desc, "error": str(exc),
            })
            continue

        if stock_item_id not in existing_descs:
            rows = call_linnworks_get(
                "Inventory/GetInventoryItemDescriptions",
                params={"inventoryItemId": stock_item_id},
            )
            existing_descs[stock_item_id] = rows if isinstance(rows, list) else []

        match = next(
            (r for r in existing_descs[stock_item_id]
             if r.get("Source", "") == source and r.get("SubSource", "") == sub_source),
            None,
        )
        manifest.append({
            "sku":             sku,
            "stock_item_id":   stock_item_id,
            "source":          source,
            "sub_source":      sub_source,
            "old_description": match.get("Description") if match else None,
            "new_description": new_desc,
            "action":          "update" if match else "create",
            "pk_row_id":       match.get("pkRowId") if match else None,
        })

    # ── Write guard ───────────────────────────────────────────────────────────
    guard = _write_guard("set_inventory_item_descriptions", descriptions, confirmed_count, dry_run)
    if guard is not None:
        return {**guard, "manifest": manifest}

    if dry_run:
        return {
            "dry_run":    True,
            "item_count": len(descriptions),
            "manifest":   manifest,
            "message":    "Dry run — no descriptions changed. Set dry_run=False to execute.",
        }

    # ── Live execution ─────────────────────────────────────────────────────────
    to_create  = [m for m in manifest if m.get("action") == "create" and not m.get("error")]
    to_update  = [m for m in manifest if m.get("action") == "update" and not m.get("error")]
    error_rows = [m for m in manifest if m.get("error")]

    results = []
    created = 0
    updated = 0
    errors  = len(error_rows)

    if to_create:
        create_payload = [
            {
                "pkRowId":     str(uuid.uuid4()),  # Linnworks needs a client-supplied GUID
                "StockItemId": m["stock_item_id"],
                "Source":      m["source"],
                "SubSource":   m["sub_source"],
                "Description": m["new_description"],
            }
            for m in to_create
        ]
        call_linnworks(
            "Inventory/CreateInventoryItemDescriptions",
            {"inventoryItemDescriptions": create_payload},
        )
        for m in to_create:
            created += 1
            results.append({
                "sku": m["sku"], "action": "created",
                "source": m["source"], "sub_source": m["sub_source"],
            })

    if to_update:
        update_payload = [
            {
                "StockItemId": m["stock_item_id"],
                "pkRowId":     m["pk_row_id"],
                "Source":      m["source"],
                "SubSource":   m["sub_source"],
                "Description": m["new_description"],
            }
            for m in to_update
        ]
        call_linnworks(
            "Inventory/UpdateInventoryItemDescriptions",
            {"inventoryItemDescriptions": update_payload},
        )
        for m in to_update:
            updated += 1
            results.append({
                "sku": m["sku"], "action": "updated",
                "source": m["source"], "sub_source": m["sub_source"],
                "old_description": m["old_description"],
                "new_description": m["new_description"],
            })

    for m in error_rows:
        results.append({"sku": m["sku"], "action": "error", "error": m["error"]})

    return {
        "dry_run":    False,
        "item_count": len(descriptions),
        "created":    created,
        "updated":    updated,
        "errors":     errors,
        "results":    results,
        "manifest":   manifest,
    }


@mcp.tool()
def get_inventory_item_descriptions(sku: str) -> dict:
    """
    Read the channel-specific descriptions for ONE inventory item.

    Descriptions in Linnworks are keyed by (StockItemId, Source, SubSource), so an
    item can carry a different description per channel plus a default (Source and
    SubSource both blank). This is the read-side companion to
    `set_inventory_item_descriptions` (which previously had no getter).

    Args:
        sku: The exact SKU / ItemNumber to look up.

    Returns:
        A dict with:
          - sku, stock_item_id, title
          - description_count: number of description rows
          - default_description: the text of the default row (blank Source +
            SubSource), or None if no default row exists
          - descriptions: per-row entries (source, sub_source, description,
            pk_row_id)
    """
    try:
        item = call_linnworks("Inventory/GetInventoryItem", {"sku": sku})
    except RuntimeError:
        return {"error": f"No inventory item found for SKU '{sku}'", "sku": sku}

    stock_item_id = item.get("StockItemId")
    if not stock_item_id:
        return {"error": f"Item found for SKU '{sku}' but StockItemId was missing", "sku": sku}

    rows = call_linnworks_get(
        "Inventory/GetInventoryItemDescriptions",
        params={"inventoryItemId": stock_item_id},
    )
    descriptions = [
        {
            "source":      r.get("Source", ""),
            "sub_source":  r.get("SubSource", ""),
            "description": r.get("Description"),
            "pk_row_id":   r.get("pkRowId"),
        }
        for r in (rows if isinstance(rows, list) else [])
    ]
    default = next(
        (d["description"] for d in descriptions if not d["source"] and not d["sub_source"]),
        None,
    )
    return {
        "sku":                 sku,
        "stock_item_id":       stock_item_id,
        "title":               item.get("ItemTitle"),
        "description_count":   len(descriptions),
        "default_description": default,
        "descriptions":        descriptions,
    }


@mcp.tool()
def get_inventory_item_titles(sku: str) -> dict:
    """
    Read the channel-specific titles for ONE inventory item.

    Linnworks stores per-channel listing titles that can diverge from the base
    `ItemTitle`. They are keyed by (StockItemId, Source, SubSource), so an item
    can have a different title on each channel/store plus a default (Source and
    SubSource both blank). The base `ItemTitle` (set via
    `create_or_update_inventory_item`) is separate and returned here as
    `base_title` for comparison. This is the read-side companion to
    `set_inventory_item_titles`.

    Args:
        sku: The exact SKU / ItemNumber to look up.

    Returns:
        A dict with:
          - sku, stock_item_id, base_title
          - title_count: number of channel-title rows
          - default_title: the text of the default row (blank Source +
            SubSource), or None if no default row exists
          - titles: per-row entries (source, sub_source, title, pk_row_id)
    """
    try:
        item = call_linnworks("Inventory/GetInventoryItem", {"sku": sku})
    except RuntimeError:
        return {"error": f"No inventory item found for SKU '{sku}'", "sku": sku}

    stock_item_id = item.get("StockItemId")
    if not stock_item_id:
        return {"error": f"Item found for SKU '{sku}' but StockItemId was missing", "sku": sku}

    rows = call_linnworks_get(
        "Inventory/GetInventoryItemTitles",
        params={"inventoryItemId": stock_item_id},
    )
    titles = [
        {
            "source":     r.get("Source", ""),
            "sub_source": r.get("SubSource", ""),
            "title":      r.get("Title"),
            "pk_row_id":  r.get("pkRowId"),
        }
        for r in (rows if isinstance(rows, list) else [])
    ]
    default = next(
        (t["title"] for t in titles if not t["source"] and not t["sub_source"]),
        None,
    )
    return {
        "sku":           sku,
        "stock_item_id": stock_item_id,
        "base_title":    item.get("ItemTitle"),
        "title_count":   len(titles),
        "default_title": default,
        "titles":        titles,
    }


@mcp.tool()
def set_inventory_item_titles(
    titles: list[dict],
    confirmed_count: int | None = None,
    dry_run: bool = True,
) -> dict:
    """
    Create or update channel-specific titles on Linnworks inventory items.

    Linnworks stores per-channel listing titles that can diverge from the base
    `ItemTitle`. They are keyed by (StockItemId, Source, SubSource), so you can
    set a different title per channel. Pass source="" and sub_source="" to set the
    default channel-title row.

    Note: this writes the channel-title overrides only — it does NOT change the
    base `ItemTitle` (use `create_or_update_inventory_item` for that). When
    correcting product data, update both so listings don't go stale. Read current
    values first with `get_inventory_item_titles`.

    For batches larger than 50 items this tool requires confirmed_count=<N>.

    Args:
        titles: List of dicts, each with:
            - sku (str):        Item SKU  [required]
            - title (str):      The title text  [required]
            - source (str):     Channel source (e.g. "AMAZON"). Defaults to "".
            - sub_source (str): Channel sub-source. Defaults to "".
        confirmed_count: For batches > 50, pass len(titles) here.
        dry_run: If True (default), returns a manifest without writing.
            Set to False to execute.

    Returns:
        A dict with:
          - dry_run:     whether this was a dry run
          - item_count:  number of title rows in the batch
          - manifest:    per-item preview (always present)
          - results:     per-item outcome (live run only)
          - created:     new rows created (live run only)
          - updated:     existing rows updated (live run only)
          - errors:      rows that failed (live run only)
    """
    # ── Injection check ───────────────────────────────────────────────────────
    for t in titles:
        _check_injection("title", t.get("title", ""))

    # ── Validate ──────────────────────────────────────────────────────────────
    for i, t in enumerate(titles):
        if not t.get("sku"):
            raise ValueError(f"titles[{i}] is missing 'sku'.")
        if t.get("title") is None:
            raise ValueError(f"titles[{i}] (SKU '{t.get('sku')}') is missing 'title'.")

    sku_cache: dict[str, str] = {}
    existing_titles: dict[str, list] = {}

    # ── Read existing + build manifest ────────────────────────────────────────
    manifest = []
    for t in titles:
        sku        = t["sku"].strip()
        source     = t.get("source", "")
        sub_source = t.get("sub_source", "")
        new_title  = t["title"]

        try:
            stock_item_id = _resolve_sku_to_id(sku, sku_cache)
        except ValueError as exc:
            manifest.append({
                "sku": sku, "source": source, "sub_source": sub_source,
                "title": new_title, "error": str(exc),
            })
            continue

        if stock_item_id not in existing_titles:
            rows = call_linnworks_get(
                "Inventory/GetInventoryItemTitles",
                params={"inventoryItemId": stock_item_id},
            )
            existing_titles[stock_item_id] = rows if isinstance(rows, list) else []

        match = next(
            (r for r in existing_titles[stock_item_id]
             if r.get("Source", "") == source and r.get("SubSource", "") == sub_source),
            None,
        )
        manifest.append({
            "sku":           sku,
            "stock_item_id": stock_item_id,
            "source":        source,
            "sub_source":    sub_source,
            "old_title":     match.get("Title") if match else None,
            "new_title":     new_title,
            "action":        "update" if match else "create",
            "pk_row_id":     match.get("pkRowId") if match else None,
        })

    # ── Write guard ───────────────────────────────────────────────────────────
    guard = _write_guard("set_inventory_item_titles", titles, confirmed_count, dry_run)
    if guard is not None:
        return {**guard, "manifest": manifest}

    if dry_run:
        return {
            "dry_run":    True,
            "item_count": len(titles),
            "manifest":   manifest,
            "message":    "Dry run — no titles changed. Set dry_run=False to execute.",
        }

    # ── Live execution ─────────────────────────────────────────────────────────
    to_create  = [m for m in manifest if m.get("action") == "create" and not m.get("error")]
    to_update  = [m for m in manifest if m.get("action") == "update" and not m.get("error")]
    error_rows = [m for m in manifest if m.get("error")]

    results = []
    created = 0
    updated = 0
    errors  = len(error_rows)

    if to_create:
        create_payload = [
            {
                "pkRowId":     str(uuid.uuid4()),  # Linnworks needs a client-supplied GUID
                "StockItemId": m["stock_item_id"],
                "Source":      m["source"],
                "SubSource":   m["sub_source"],
                "Title":       m["new_title"],
            }
            for m in to_create
        ]
        call_linnworks(
            "Inventory/CreateInventoryItemTitles",
            {"inventoryItemTitles": create_payload},
        )
        for m in to_create:
            created += 1
            results.append({
                "sku": m["sku"], "action": "created",
                "source": m["source"], "sub_source": m["sub_source"],
            })

    if to_update:
        update_payload = [
            {
                "StockItemId": m["stock_item_id"],
                "pkRowId":     m["pk_row_id"],
                "Source":      m["source"],
                "SubSource":   m["sub_source"],
                "Title":       m["new_title"],
            }
            for m in to_update
        ]
        call_linnworks(
            "Inventory/UpdateInventoryItemTitles",
            {"inventoryItemTitles": update_payload},
        )
        for m in to_update:
            updated += 1
            results.append({
                "sku": m["sku"], "action": "updated",
                "source": m["source"], "sub_source": m["sub_source"],
                "old_title": m["old_title"],
                "new_title": m["new_title"],
            })

    for m in error_rows:
        results.append({"sku": m["sku"], "action": "error", "error": m["error"]})

    return {
        "dry_run":    False,
        "item_count": len(titles),
        "created":    created,
        "updated":    updated,
        "errors":     errors,
        "results":    results,
        "manifest":   manifest,
    }


@mcp.tool()
def get_inventory_item_suppliers(sku: str) -> dict:
    """
    Read the purchasable supplier links for ONE inventory item — answers
    "which supplier(s) can this item be bought from, at what code and cost,
    and which is the primary/default?".

    Reads the StockItemSuppliers table (Inventory/GetStockSupplierStat). This
    is the per-item link table that purchase ordering and supplier reporting
    key off — `get_sales_by_supplier` attributes sales via the IsDefault row
    here. It is distinct from `get_suppliers` (the global supplier list, no
    per-item association) and from the flat `purchase_price` on the item.

    An item with zero rows is invisible to supplier-keyed exports and restock
    tooling — use `set_inventory_item_suppliers` to attach one.

    Args:
        sku: Exact SKU / ItemNumber of the item.

    Returns:
        A dict with:
          - sku, stock_item_id, title
          - supplier_count:   number of supplier links
          - default_supplier: name of the IsDefault row (None if none is default)
          - suppliers: rows with supplier, supplier_id, code, barcode,
            purchase_price, lead_time, min_order_qty, pack_size, currency,
            is_default
    """
    stock_item_id = _resolve_sku_to_id(sku)
    item = call_linnworks("Inventory/GetInventoryItem", {"sku": sku})
    rows = call_linnworks_get(
        "Inventory/GetStockSupplierStat", params={"inventoryItemId": stock_item_id}
    )
    rows = rows if isinstance(rows, list) else []
    suppliers = [
        {
            "supplier":       r.get("Supplier"),
            "supplier_id":    r.get("SupplierID"),
            "code":           r.get("Code"),
            "barcode":        r.get("SupplierBarcode"),
            "purchase_price": r.get("PurchasePrice"),
            "lead_time":      r.get("LeadTime"),
            "min_order_qty":  r.get("SupplierMinOrderQty"),
            "pack_size":      r.get("SupplierPackSize"),
            "currency":       r.get("SupplierCurrency"),
            "is_default":     bool(r.get("IsDefault")),
        }
        for r in rows
    ]
    default_row = next((s for s in suppliers if s["is_default"]), None)
    return {
        "sku":              sku,
        "stock_item_id":    stock_item_id,
        "title":            item.get("ItemTitle"),
        "supplier_count":   len(suppliers),
        "default_supplier": default_row["supplier"] if default_row else None,
        "suppliers":        suppliers,
    }


# Fields a caller may set on a supplier link, mapped to the Linnworks
# StockItemSupplierStat field they write. Used by set_inventory_item_suppliers.
_SUPPLIER_LINK_FIELDS = {
    "supplier_code": "Code",
    "cost":          "PurchasePrice",
    "is_default":    "IsDefault",
    "lead_time":     "LeadTime",
    "min_order_qty": "SupplierMinOrderQty",
    "pack_size":     "SupplierPackSize",
    "barcode":       "SupplierBarcode",
    "currency":      "SupplierCurrency",
}


@mcp.tool()
def set_inventory_item_suppliers(
    links: list[dict],
    confirmed_count: int | None = None,
    dry_run: bool = True,
) -> dict:
    """
    Add or update purchasable supplier links on inventory items — "this item
    can be bought from supplier X, under code Y, at cost Z".

    Upserts the StockItemSuppliers table keyed by (item, supplier): an existing
    link for that supplier is UPDATED (only the fields you pass change — the
    tool reads the current row first and carries every other field through,
    because Linnworks CLEARS any field omitted from an update); a new supplier
    gets a CREATED link. Existing links to other suppliers are never touched,
    except that Linnworks itself auto-flips the previous default row to
    IsDefault=false when a new default is written (live-confirmed).

    The supplier is matched by name (case-insensitive) against the configured
    supplier list (`get_suppliers`) — a supplier GUID is also accepted. An
    unknown supplier becomes a per-item error row; it never sinks the batch.

    Why this matters: purchase ordering and supplier reporting key off this
    table — `get_sales_by_supplier` attributes sales via the IsDefault link, so
    an item with no default supplier link is invisible to supplier-keyed
    exports and restock tooling. Give an item's primary supplier
    is_default=True.

    For batches larger than 50 links this tool requires confirmed_count=<N>.

    Args:
        links: List of dicts, each with:
            - sku (str):            Item SKU  [required]
            - supplier (str):       Supplier name (case-insensitive) or GUID  [required]
            - supplier_code (str):  The supplier's own SKU/code for this item
            - cost (float):         Purchase price from this supplier
            - is_default (bool):    Make this the primary supplier (see above)
            - lead_time (int):      Lead time in days
            - min_order_qty (int):  Minimum order quantity
            - pack_size (int):      Supplier pack size
            - barcode (str):        Supplier barcode
            - currency (str):       Supplier currency (e.g. "GBP")
          Only sku and supplier are required; on update, omitted fields keep
          their current values.
        confirmed_count: For batches > 50, pass len(links) here.
        dry_run: If True (default), returns the manifest without writing.

    Returns:
        A dict with:
          - dry_run, item_count
          - manifest: per-link preview (action create/update, old vs new values)
          - results:  per-link outcome incl. read-back verification (live only)
          - created / updated / errors counts (live only)
    """
    # ── Validate + injection check ────────────────────────────────────────────
    for i, l in enumerate(links):
        if not l.get("sku"):
            raise ValueError(f"links[{i}] is missing 'sku'.")
        if not l.get("supplier"):
            raise ValueError(f"links[{i}] (SKU '{l.get('sku')}') is missing 'supplier'.")
        for field in ("supplier", "supplier_code", "barcode", "currency"):
            _check_injection(field, str(l.get(field) or ""))

    # ── Resolve supplier names → GUIDs (one catalogue fetch) ──────────────────
    supplier_rows = get_suppliers().get("suppliers", [])
    by_name = {(s.get("name") or "").strip().lower(): s for s in supplier_rows}
    by_id   = {(s.get("supplier_id") or "").lower(): s for s in supplier_rows}

    sku_cache: dict[str, str] = {}
    existing_stats: dict[str, list] = {}

    # ── Read existing + build manifest ────────────────────────────────────────
    manifest = []
    for l in links:
        sku = l["sku"].strip()
        sup_input = str(l["supplier"]).strip()
        sup = by_name.get(sup_input.lower()) or by_id.get(sup_input.lower())
        if sup is None:
            manifest.append({
                "sku": sku, "supplier": sup_input,
                "error": (
                    f"supplier '{sup_input}' not found in the configured supplier "
                    f"list — see get_suppliers. Known: "
                    f"{sorted(s.get('name') or '' for s in supplier_rows)}"
                ),
            })
            continue

        try:
            stock_item_id = _resolve_sku_to_id(sku, sku_cache)
        except ValueError as exc:
            manifest.append({"sku": sku, "supplier": sup["name"], "error": str(exc)})
            continue

        if stock_item_id not in existing_stats:
            rows = call_linnworks_get(
                "Inventory/GetStockSupplierStat",
                params={"inventoryItemId": stock_item_id},
            )
            existing_stats[stock_item_id] = rows if isinstance(rows, list) else []

        match = next(
            (r for r in existing_stats[stock_item_id]
             if (r.get("SupplierID") or "").lower() == (sup["supplier_id"] or "").lower()),
            None,
        )

        changes = {
            api_field: l[user_field]
            for user_field, api_field in _SUPPLIER_LINK_FIELDS.items()
            if user_field in l and l[user_field] is not None
        }
        manifest.append({
            "sku":           sku,
            "stock_item_id": stock_item_id,
            "supplier":      sup["name"],
            "supplier_id":   sup["supplier_id"],
            "action":        "update" if match else "create",
            "old":           {
                "code":           match.get("Code"),
                "purchase_price": match.get("PurchasePrice"),
                "is_default":     match.get("IsDefault"),
            } if match else None,
            "changes":       changes,
            "_existing_row": match,
        })

    # ── Write guard ───────────────────────────────────────────────────────────
    public_manifest = [{k: v for k, v in m.items() if k != "_existing_row"} for m in manifest]
    guard = _write_guard("set_inventory_item_suppliers", links, confirmed_count, dry_run)
    if guard is not None:
        return {**guard, "manifest": public_manifest}

    if dry_run:
        return {
            "dry_run":    True,
            "item_count": len(links),
            "manifest":   public_manifest,
            "message":    "Dry run — no supplier links changed. Set dry_run=False to execute.",
        }

    # ── Live execution (Create/Update return 204 No Content → void calls) ─────
    results = []
    created = updated = errors = 0
    touched_ids: dict[str, str] = {}  # stock_item_id -> sku (for read-back)

    for m in manifest:
        if m.get("error"):
            errors += 1
            results.append({"sku": m["sku"], "supplier": m.get("supplier"),
                            "action": "error", "error": m["error"]})
            continue

        if m["action"] == "update":
            # Carry the FULL existing row and overlay only the requested changes —
            # Linnworks clears any field omitted from UpdateStockSupplierStat.
            row = dict(m["_existing_row"])
            row.pop("StockItemIntId", None)
            row.update(m["changes"])
            row["StockItemId"] = m["stock_item_id"]
            row["SupplierID"]  = m["supplier_id"]
            endpoint = "Inventory/UpdateStockSupplierStat"
        else:
            row = {
                "StockItemId": m["stock_item_id"],
                "SupplierID":  m["supplier_id"],
                "Supplier":    m["supplier"],
                **m["changes"],
            }
            endpoint = "Inventory/CreateStockSupplierStat"

        try:
            call_linnworks_void(endpoint, {"itemSuppliers": [row]})
        except RuntimeError as exc:
            errors += 1
            results.append({"sku": m["sku"], "supplier": m["supplier"],
                            "action": "error", "error": str(exc)})
            continue

        touched_ids[m["stock_item_id"]] = m["sku"]
        if m["action"] == "update":
            updated += 1
        else:
            created += 1
        results.append({
            "sku": m["sku"], "supplier": m["supplier"], "action": f"{m['action']}d",
        })

    # ── Read back after write ─────────────────────────────────────────────────
    read_back = {}
    for sid, sku in touched_ids.items():
        rows = call_linnworks_get(
            "Inventory/GetStockSupplierStat", params={"inventoryItemId": sid}
        )
        read_back[sku] = [
            {"supplier": r.get("Supplier"), "code": r.get("Code"),
             "purchase_price": r.get("PurchasePrice"), "is_default": r.get("IsDefault")}
            for r in (rows if isinstance(rows, list) else [])
        ]

    return {
        "dry_run":    False,
        "item_count": len(links),
        "created":    created,
        "updated":    updated,
        "errors":     errors,
        "results":    results,
        "read_back":  read_back,
        "manifest":   public_manifest,
    }


@mcp.tool()
def add_inventory_item_images(
    images: list[dict],
    confirmed_count: int | None = None,
    dry_run: bool = True,
) -> dict:
    """
    Add images to Linnworks inventory items by URL.

    Each entry attaches one image URL to a stock item.  Images are additive —
    existing images are not removed.  Use is_main=True to set the uploaded
    image as the item's primary image.

    For batches larger than 100 items this tool requires confirmed_count=<N>.

    Args:
        images: List of dicts, each with:
            - sku (str):        Item SKU  [required]
            - image_url (str):  Publicly accessible URL of the image  [required]
            - is_main (bool):   Set as the main product image. Defaults to False.
        confirmed_count: For batches > 100, pass len(images) here.
        dry_run: If True (default), returns a manifest without writing.
            Set to False to execute.

    Returns:
        A dict with:
          - dry_run:     whether this was a dry run
          - item_count:  number of image rows in the batch
          - manifest:    per-item preview (always present)
          - results:     per-item outcome (live run only)
          - added:       images successfully added (live run only)
          - errors:      images that failed (live run only)
    """
    # ── Injection check ───────────────────────────────────────────────────────
    for img in images:
        _check_injection("image_url", img.get("image_url", ""))

    # ── Validate ──────────────────────────────────────────────────────────────
    for i, img in enumerate(images):
        if not img.get("sku"):
            raise ValueError(f"images[{i}] is missing 'sku'.")
        if not img.get("image_url"):
            raise ValueError(f"images[{i}] (SKU '{img.get('sku')}') is missing 'image_url'.")

    sku_cache: dict[str, str] = {}

    # ── Build manifest ────────────────────────────────────────────────────────
    manifest = []
    for img in images:
        sku = img["sku"].strip()
        try:
            stock_item_id = _resolve_sku_to_id(sku, sku_cache)
            manifest.append({
                "sku":           sku,
                "stock_item_id": stock_item_id,
                "image_url":     img["image_url"],
                "is_main":       img.get("is_main", False),
            })
        except ValueError as exc:
            manifest.append({
                "sku": sku, "image_url": img.get("image_url"),
                "is_main": img.get("is_main", False), "error": str(exc),
            })

    # ── Write guard ───────────────────────────────────────────────────────────
    guard = _write_guard("add_inventory_item_images", images, confirmed_count, dry_run)
    if guard is not None:
        return {**guard, "manifest": manifest}

    if dry_run:
        return {
            "dry_run":    True,
            "item_count": len(images),
            "manifest":   manifest,
            "message":    "Dry run — no images added. Set dry_run=False to execute.",
        }

    # ── Live execution (one call per image — no bulk endpoint) ────────────────
    results = []
    added  = 0
    errors = 0

    for m in manifest:
        if m.get("error"):
            errors += 1
            results.append({"sku": m["sku"], "action": "error", "error": m["error"]})
            continue
        try:
            call_linnworks(
                "Inventory/AddImageToInventoryItem",
                {
                    "request": {
                        "StockItemId": m["stock_item_id"],
                        "ImageUrl":    m["image_url"],
                        "IsMain":      m["is_main"],
                    }
                },
            )
            added += 1
            results.append({
                "sku": m["sku"], "action": "added",
                "image_url": m["image_url"], "is_main": m["is_main"],
            })
        except Exception as exc:
            errors += 1
            results.append({"sku": m["sku"], "action": "error", "error": str(exc)})

    return {
        "dry_run":    False,
        "item_count": len(images),
        "added":      added,
        "errors":     errors,
        "results":    results,
        "manifest":   manifest,
    }


@mcp.tool()
def create_variation_group(
    group_name: str,
    parent_sku: str,
    child_skus: list[str],
    dry_run: bool = True,
) -> dict:
    """
    Create a variation group in Linnworks, linking a parent item to its children.

    A variation group connects items that are variants of the same product
    (e.g. a T-shirt in different sizes/colours).  The parent item becomes the
    variation template; child SKUs are the individual sellable variants.

    All SKUs must already exist in Linnworks before creating the group.
    If a group with the same name already exists this tool reports it and
    does not create a duplicate.

    Args:
        group_name: The name for the variation group.  [required]
        parent_sku: SKU of the item that will be the variation parent.  [required]
        child_skus: List of SKUs that are children (variants) of the parent.
            The parent SKU should NOT be included here.  [required]
        dry_run: If True (default), validates all SKUs and shows what would be
            created without writing. Set to False to execute.

    Returns:
        A dict with:
          - dry_run:          whether this was a dry run
          - group_name:       the requested group name
          - parent_sku:       the parent SKU
          - child_skus:       the child SKUs
          - parent_id:        resolved parent StockItemId
          - child_ids:        resolved child StockItemIds
          - status:           "dry_run", "created", "already_exists", or "error"
          - message:          human-readable outcome
    """
    _check_injection("group_name", group_name)

    # ── Resolve all SKUs ──────────────────────────────────────────────────────
    sku_cache: dict[str, str] = {}
    try:
        parent_id = _resolve_sku_to_id(parent_sku, sku_cache)
    except ValueError as exc:
        return {
            "dry_run": dry_run, "group_name": group_name,
            "parent_sku": parent_sku, "status": "error",
            "message": f"Parent SKU resolution failed: {exc}",
        }

    child_ids = []
    child_errors = []
    for sku in child_skus:
        try:
            child_ids.append(_resolve_sku_to_id(sku, sku_cache))
        except ValueError as exc:
            child_errors.append({"sku": sku, "error": str(exc)})

    if child_errors:
        return {
            "dry_run": dry_run, "group_name": group_name,
            "parent_sku": parent_sku, "child_skus": child_skus,
            "status": "error",
            "message": f"{len(child_errors)} child SKU(s) could not be resolved.",
            "child_errors": child_errors,
        }

    # ── Check if group already exists ─────────────────────────────────────────
    try:
        existing_group = call_linnworks_get(
            "Stock/GetVariationGroupByName",
            params={"variationGroupName": group_name},
        )
        if existing_group and existing_group.get("VariationGroupName"):
            return {
                "dry_run":    dry_run,
                "group_name": group_name,
                "parent_sku": parent_sku,
                "child_skus": child_skus,
                "status":     "already_exists",
                "message": (
                    f"A variation group named '{group_name}' already exists "
                    f"in Linnworks. No new group was created."
                ),
                "existing_group": existing_group,
            }
    except RuntimeError:
        pass  # 404 or similar means the group does not exist — proceed

    base = {
        "dry_run":    dry_run,
        "group_name": group_name,
        "parent_sku": parent_sku,
        "child_skus": child_skus,
        "parent_id":  parent_id,
        "child_ids":  child_ids,
    }

    if dry_run:
        return {
            **base,
            "status":  "dry_run",
            "message": (
                f"Dry run — would create variation group '{group_name}' "
                f"with parent '{parent_sku}' and {len(child_skus)} child(ren). "
                f"Set dry_run=False to create."
            ),
        }

    # ── Create the group ──────────────────────────────────────────────────────
    call_linnworks(
        "Stock/CreateVariationGroup",
        {
            "template": {
                "VariationGroupName": group_name,
                "ParentSKU":          parent_sku,
                "ParentStockItemId":  parent_id,
                "VariationItemIds":   child_ids,
            }
        },
    )

    # Read back to confirm
    try:
        confirmed = call_linnworks_get(
            "Stock/GetVariationGroupByName",
            params={"variationGroupName": group_name},
        )
    except RuntimeError:
        confirmed = None

    return {
        **base,
        "status":         "created",
        "confirmed_group": confirmed,
        "message": (
            f"Variation group '{group_name}' created with parent '{parent_sku}' "
            f"and {len(child_ids)} child item(s)."
        ),
    }


@mcp.tool()
def delete_inventory_item(
    skus: list[str],
    confirmed_count: int | None = None,
    dry_run: bool = True,
) -> dict:
    """
    PERMANENTLY delete one or more inventory items from Linnworks by SKU.

    ⚠️  IRREVERSIBLE.  Deleted items CANNOT be restored via the API — their
    stock levels, prices, descriptions, extended properties, images, and
    listing links are all destroyed.  The dry-run manifest and the staging
    gate are the main safety nets.  Always run dry_run=True first and read
    the manifest carefully before setting dry_run=False.

    Deleting a composite parent, a variation parent, or an item that is on an
    active channel listing may have side effects — any Linnworks error is
    surfaced verbatim per item.

    For batches larger than 10 items this tool enters a staging mode: it
    returns a manifest of exactly what would be destroyed and asks you to
    confirm with confirmed_count=<N> before executing.

    Uses Inventory/DeleteInventoryItems (payload sent UNWRAPPED — empty 2xx
    body on success; confirmed live 15 Jun 2026).

    Args:
        skus: List of exact SKUs / ItemNumbers to delete.  Each is resolved to
            its StockItemId before deletion.  A SKU that does not resolve is
            reported as an error row and does NOT abort the rest of the batch.
        confirmed_count: For batches > 10, pass len(skus) here after reviewing
            the manifest to confirm the destruction.
        dry_run: If True (default), returns the manifest of what would be
            deleted without writing. Set to False to execute.

    Returns:
        A dict with:
          - dry_run:     whether this was a dry run
          - item_count:  number of SKUs in the batch
          - manifest:    per-item preview (sku, stock_item_id, title,
                         current_stock, resolved/error) — always present
          - results:     per-item outcome with deleted flag (live run only)
          - deleted:     count of items confirmed gone (live run only)
          - errors:      count of SKUs that failed to resolve or delete
    """
    if not skus:
        raise ValueError("skus must contain at least one SKU.")

    # ── Read-before-write: resolve each SKU + capture title and stock ─────────
    sku_cache: dict[str, str] = {}
    manifest: list[dict] = []

    for sku in skus:
        sku = (sku or "").strip()
        if not sku:
            manifest.append({
                "sku": sku, "stock_item_id": None, "title": None,
                "current_stock": None, "resolved": False,
                "error": "empty SKU",
            })
            continue
        try:
            item = call_linnworks("Inventory/GetInventoryItem", {"sku": sku})
        except RuntimeError as exc:
            manifest.append({
                "sku": sku, "stock_item_id": None, "title": None,
                "current_stock": None, "resolved": False,
                "error": f"not found: {exc}",
            })
            continue

        stock_item_id = item.get("StockItemId")
        if not stock_item_id:
            manifest.append({
                "sku": sku, "stock_item_id": None,
                "title": item.get("ItemTitle"),
                "current_stock": None, "resolved": False,
                "error": "found but returned no StockItemId",
            })
            continue

        sku_cache[sku] = stock_item_id

        # Total current stock across all locations (best-effort, informational)
        current_stock: int | None = None
        try:
            levels_resp = call_linnworks(
                "Stock/GetStockLevel_Batch",
                {"request": {"StockItemIds": [stock_item_id]}},
            )
            rows = levels_resp if isinstance(levels_resp, list) else (
                levels_resp.get("StockItemLevels") or []
            )
            total = 0
            for row_group in rows:
                for row in (row_group if isinstance(row_group, list) else [row_group]):
                    lvl = row.get("StockLevel")
                    if isinstance(lvl, (int, float)):
                        total += lvl
            current_stock = total
        except (ValueError, RuntimeError):
            current_stock = None

        manifest.append({
            "sku":           sku,
            "stock_item_id": stock_item_id,
            "title":         item.get("ItemTitle"),
            "current_stock": current_stock,
            "resolved":      True,
        })

    resolved_rows = [m for m in manifest if m.get("resolved")]
    resolve_errors = [m for m in manifest if not m.get("resolved")]

    # ── Write guard (threshold 10) ────────────────────────────────────────────
    guard = _write_guard("delete_inventory_item", skus, confirmed_count, dry_run)
    if guard is not None:
        return {**guard, "manifest": manifest}

    if dry_run:
        return {
            "dry_run":    True,
            "item_count": len(skus),
            "manifest":   manifest,
            "message": (
                f"Dry run — nothing deleted. {len(resolved_rows)} item(s) would be "
                f"PERMANENTLY deleted, {len(resolve_errors)} could not be resolved. "
                "Review the manifest, then set dry_run=False to execute. "
                "This cannot be undone."
            ),
        }

    if not resolved_rows:
        return {
            "dry_run":    False,
            "item_count": len(skus),
            "deleted":    0,
            "errors":     len(resolve_errors),
            "results":    [],
            "manifest":   manifest,
            "message":    "No SKUs resolved to a StockItemId; nothing was deleted.",
        }

    # ── Live execution ─────────────────────────────────────────────────────────
    ids_to_delete = [m["stock_item_id"] for m in resolved_rows]
    delete_error: str | None = None
    try:
        call_linnworks(
            "Inventory/DeleteInventoryItems",
            {"inventoryItemIds": ids_to_delete},
        )
    except RuntimeError as exc:
        # Surface the Linnworks error verbatim; read-back below still runs so we
        # can report which items (if any) actually went.
        delete_error = str(exc)

    # ── Read-back: probe each resolved SKU; gone => deleted ───────────────────
    results = []
    deleted = 0
    errors = len(resolve_errors)
    for m in resolved_rows:
        sku = m["sku"]
        gone = False
        probe_note = None
        try:
            call_linnworks("Inventory/GetInventoryItem", {"sku": sku})
            gone = False  # still exists
        except RuntimeError:
            gone = True   # not found => deleted
        if gone:
            deleted += 1
        else:
            errors += 1
            probe_note = "still exists after delete call"
        results.append({
            "sku":           sku,
            "stock_item_id": m["stock_item_id"],
            "title":         m.get("title"),
            "deleted":       gone,
            **({"note": probe_note} if probe_note else {}),
        })

    for m in resolve_errors:
        results.append({
            "sku":           m["sku"],
            "stock_item_id": None,
            "deleted":       False,
            "error":         m.get("error"),
        })

    out = {
        "dry_run":    False,
        "item_count": len(skus),
        "deleted":    deleted,
        "errors":     errors,
        "results":    results,
        "manifest":   manifest,
    }
    if delete_error is not None:
        out["delete_error"] = delete_error
        out["message"] = (
            "Linnworks returned an error on DeleteInventoryItems (surfaced in "
            "delete_error). Per-item read-back results show what actually got deleted."
        )
    return out


# ---------- Inventory categories ----------
#
# Linnworks category CRUD. Categories are a flat list (StructureCategoryId is 0
# for every row in this tenant — the tree/"structure" feature is unused). Each
# category is {CategoryId (guid), CategoryName, StructureCategoryId,
# ProductCategoryId}. The zero-GUID "Default" category is built in and must
# never be renamed or deleted.
#
# Endpoints (all live-confirmed 7 Jul 2026):
#   Inventory/GetCategories       GET  -> [LinnworksCategory]
#   Inventory/CreateCategory      POST {"categoryName": "..."} -> LinnworksCategory
#                                       (Linnworks mints the CategoryId server-side —
#                                        no client GUID needed, unlike the sub-entity
#                                        Create* endpoints)
#   Inventory/UpdateCategory      POST {"category": {full record}} -> {} on success
#                                       (carry ALL fields — omitted fields are cleared;
#                                        empty 2xx body)
#   Inventory/DeleteCategoryById  POST {"categoryId": "guid"} -> {} on success
#                                       (empty 2xx; a non-empty category's items are
#                                        reassigned to Default). Returns HTTP 400
#                                       "Category is in use" if the category still has
#                                       ARCHIVED items or channel references — see below.
#
# "Empty" here means "no ACTIVE inventory item references the category". There is
# NO per-category count endpoint (GetInventoryItemsCount returns only a global
# total, no category filter), so emptiness is derived by sweeping the whole
# catalogue via Stock/GetStockItems (keyWord="") and tallying CategoryId. That
# sweep is ~161 pages for ~32k items (~2 min); it throttles under the 150/min
# rate limit and backs off on HTTP 429. The scan always runs to completion — a
# partial scan could misreport a used category as empty and cause a wrongful
# delete.
#
# ⚠️  ARCHIVED-ITEM BLIND SPOT (confirmed live 7 Jul 2026): the sweep endpoint
# Stock/GetStockItems only returns ACTIVE stock — archived items are invisible to
# it (and there is NO endpoint that lists/counts archived items per category:
# GetStockItemsFull is also active-only, GetInventoryItemsCount has no category
# filter, and no GetArchivedItems endpoint exists). On this tenant 32,125 items
# are active but 92,816 exist including archived (~60k archived). So a category
# holding ONLY archived items reads as "0 items"/empty here, yet Linnworks refuses
# to delete it with HTTP 400 "Category is in use". That server-side guard is the
# AUTHORITATIVE emptiness check — it sees archived items and channel references the
# sweep cannot. The delete tools therefore treat a "Category is in use" 400 as an
# expected `skipped_in_use` outcome (NOT a failure), and the active-item sweep is
# only a cheap first-pass filter, not the final word. Use _is_category_in_use().

_DEFAULT_CATEGORY_ID = "00000000-0000-0000-0000-000000000000"


def _is_category_in_use(err: str) -> bool:
    """
    True if a DeleteCategoryById error is Linnworks' "Category is in use" 400.

    This is the authoritative "not actually empty" signal: it fires when a
    category still holds ARCHIVED items (invisible to the active-item sweep) or is
    referenced by a channel/listing configuration. Treated as an expected skip,
    not a delete failure. See the archived-item blind-spot note above.
    """
    return "category is in use" in (err or "").lower()


def _fetch_categories() -> list[dict]:
    """Return the raw LinnworksCategory list (one GET)."""
    cats = call_linnworks_get("Inventory/GetCategories")
    return cats if isinstance(cats, list) else []


def _format_category(c: dict, item_count: int | None = None) -> dict:
    """Normalise a LinnworksCategory into the MCP-facing shape."""
    out = {
        "category_id":         c.get("CategoryId"),
        "category_name":       c.get("CategoryName"),
        "product_category_id": c.get("ProductCategoryId"),
        "is_default":          (c.get("CategoryId") or "").lower() == _DEFAULT_CATEGORY_ID,
    }
    if item_count is not None:
        out["item_count"] = item_count
        out["is_empty"] = item_count == 0
    return out


def _count_items_per_category() -> dict:
    """
    Sweep the entire inventory and tally how many items reference each category.

    Returns {"counts": {category_id_lower: n}, "total_items": int, "pages": int}.

    Linnworks has no per-category count endpoint, so this paginates
    Stock/GetStockItems (keyWord="") across the whole catalogue and always runs
    to completion (no page cap). Throttled to stay under the 150/min rate limit,
    with a bounded 429 backoff. Expect ~2 minutes for a large catalogue.

    ⚠️  ACTIVE ITEMS ONLY. Stock/GetStockItems does not return ARCHIVED items
    (and no endpoint counts archived items per category — see the note above
    _is_category_in_use). So a count of 0 here means "no ACTIVE items", NOT
    "truly empty" — a category of only archived items reads as 0 and Linnworks
    will still refuse to delete it ("Category is in use"). This is a cheap
    first-pass filter; the delete endpoint's own guard is the final word.
    """
    counts: dict[str, int] = {}
    total = 0
    pages = 0
    page = 1
    while True:
        resp = None
        for _ in range(6):
            try:
                resp = call_linnworks_get(
                    "Stock/GetStockItems",
                    {"keyWord": "", "entriesPerPage": 200, "pageNumber": page},
                )
                break
            except RuntimeError as exc:
                if "429" in str(exc) or "quota" in str(exc).lower():
                    time.sleep(15)  # rate-limit backoff, then retry the same page
                    continue
                raise
        if resp is None:
            raise RuntimeError(
                f"Category sweep aborted: repeated rate-limit (429) on page {page}. "
                "Emptiness could not be determined; no categories were changed."
            )
        data = resp.get("Data") or []
        pages += 1
        for it in data:
            cid = (it.get("CategoryId") or "").lower()
            counts[cid] = counts.get(cid, 0) + 1
            total += 1
        total_pages = resp.get("TotalPages") or 1
        if page >= total_pages or not data:
            break
        page += 1
        time.sleep(0.45)  # proactive throttle to stay under 150/min
    return {"counts": counts, "total_items": total, "pages": pages}


@mcp.tool()
def get_categories(with_counts: bool = False) -> dict:
    """
    List all Linnworks inventory categories.

    By default this makes ONE fast API call and returns the category list. Pass
    with_counts=True to also report how many inventory items are in each
    category and flag the empty ones — but that triggers a FULL catalogue sweep
    (~2 minutes for a large catalogue) because Linnworks has no per-category
    count endpoint. Use with_counts=True when you want to find empty categories
    to clean up; leave it False for a quick name → id lookup.

    ⚠️  item_count / is_empty count ACTIVE items only — the sweep cannot see
    archived items (no Linnworks endpoint counts archived items per category).
    A category holding only archived stock shows item_count 0 / is_empty True
    here, yet Linnworks will refuse to delete it ("Category is in use"). Treat
    is_empty as "has no active items", not "safe to delete".

    Args:
        with_counts: If True, sweep the whole catalogue and add item_count +
            is_empty to every category (slow). Default False.

    Returns:
        - category_count: total number of categories
        - with_counts: echo of the flag
        - categories: list of {category_id, category_name, product_category_id,
            is_default[, item_count, is_empty]} sorted by name
        - (with_counts only) total_items, empty_count, used_count
    """
    cats = _fetch_categories()
    counts = None
    swept = None
    if with_counts:
        swept = _count_items_per_category()
        counts = swept["counts"]

    rows = []
    for c in cats:
        n = None if counts is None else counts.get((c.get("CategoryId") or "").lower(), 0)
        rows.append(_format_category(c, n))
    rows.sort(key=lambda r: (r.get("category_name") or "").lower())

    out = {
        "category_count": len(rows),
        "with_counts":    with_counts,
        "categories":     rows,
    }
    if with_counts:
        empty = [r for r in rows if r.get("is_empty")]
        out["total_items"] = swept["total_items"]
        out["empty_count"] = len(empty)
        out["used_count"]  = len(rows) - len(empty)
    return out


@mcp.tool()
def create_category(name: str, dry_run: bool = True) -> dict:
    """
    Create a new inventory category.

    Single-category create. Checks for an existing category with the same name
    (case-insensitive) first and refuses to create a duplicate. dry_run=True by
    default — preview only; set dry_run=False to actually create.

    Uses Inventory/CreateCategory (Linnworks mints the CategoryId server-side).

    Args:
        name: The category name to create.
        dry_run: If True (default), preview only. Set False to create.

    Returns:
        A dict with created flag, category_id (live run only), and any
        duplicate note.
    """
    name = (name or "").strip()
    if not name:
        raise ValueError("name must be a non-empty category name.")
    _check_injection("name", name)

    existing = _fetch_categories()
    dup = next(
        (c for c in existing
         if (c.get("CategoryName") or "").strip().lower() == name.lower()),
        None,
    )
    if dup:
        return {
            "created":              False,
            "dry_run":              dry_run,
            "category_name":        name,
            "existing_category_id": dup.get("CategoryId"),
            "message": (
                f"A category named '{dup.get('CategoryName')}' already exists "
                "— not creating a duplicate."
            ),
        }

    if dry_run:
        return {
            "created":       False,
            "dry_run":       True,
            "category_name": name,
            "message":       f"Dry run — would create category '{name}'. Set dry_run=False to create.",
        }

    resp = call_linnworks("Inventory/CreateCategory", {"categoryName": name})
    return {
        "created":       True,
        "dry_run":       False,
        "category_id":   resp.get("CategoryId"),
        "category_name": resp.get("CategoryName", name),
        "message":       f"Created category '{name}'.",
    }


@mcp.tool()
def rename_category(category_id: str, new_name: str, dry_run: bool = True) -> dict:
    """
    Rename an existing inventory category.

    Reads the current category first (for a before/after diff), then updates its
    name while carrying every other field through unchanged — Inventory/Update
    Category clears any field you omit, so the full record must be re-sent.
    Refuses to rename the built-in Default category, and refuses a name already
    used by another category. dry_run=True by default; reads back after writing.

    Uses Inventory/UpdateCategory.

    Args:
        category_id: The CategoryId (GUID) to rename — from get_categories.
        new_name: The new category name.
        dry_run: If True (default), preview only. Set False to apply.
    """
    category_id = (category_id or "").strip()
    new_name = (new_name or "").strip()
    if not category_id:
        raise ValueError("category_id is required.")
    if not new_name:
        raise ValueError("new_name must be a non-empty category name.")
    _check_injection("new_name", new_name)
    if category_id.lower() == _DEFAULT_CATEGORY_ID:
        raise ValueError("The built-in Default category cannot be renamed.")

    cats = _fetch_categories()
    rec = next(
        (c for c in cats if (c.get("CategoryId") or "").lower() == category_id.lower()),
        None,
    )
    if rec is None:
        return {
            "updated":     False,
            "dry_run":     dry_run,
            "category_id": category_id,
            "message":     f"No category found with id {category_id}.",
        }

    old_name = rec.get("CategoryName")
    dup = next(
        (c for c in cats
         if (c.get("CategoryName") or "").strip().lower() == new_name.lower()
         and (c.get("CategoryId") or "").lower() != category_id.lower()),
        None,
    )
    if dup:
        return {
            "updated":     False,
            "dry_run":     dry_run,
            "category_id": category_id,
            "message":     f"Another category is already named '{dup.get('CategoryName')}'.",
        }

    if old_name == new_name:
        return {
            "updated":     False,
            "dry_run":     dry_run,
            "category_id": category_id,
            "message":     f"Category is already named '{new_name}' — nothing to do.",
        }

    if dry_run:
        return {
            "updated":     False,
            "dry_run":     True,
            "category_id": category_id,
            "before":      {"category_name": old_name},
            "after":       {"category_name": new_name},
            "message":     f"Dry run — would rename '{old_name}' → '{new_name}'. Set dry_run=False to apply.",
        }

    updated = dict(rec)
    updated["CategoryName"] = new_name
    call_linnworks("Inventory/UpdateCategory", {"category": updated})

    # Read-back
    cats2 = _fetch_categories()
    rec2 = next(
        (c for c in cats2 if (c.get("CategoryId") or "").lower() == category_id.lower()),
        None,
    )
    now = rec2.get("CategoryName") if rec2 else None
    ok = now == new_name
    return {
        "updated":     ok,
        "dry_run":     False,
        "category_id": category_id,
        "before":      {"category_name": old_name},
        "after":       {"category_name": now},
        "message": (
            f"Renamed '{old_name}' → '{now}'." if ok
            else "Update sent, but read-back did not confirm the new name."
        ),
    }


@mcp.tool()
def delete_categories(
    category_ids: list[str],
    force: bool = False,
    confirmed_count: int | None = None,
    dry_run: bool = True,
) -> dict:
    """
    PERMANENTLY delete specific inventory categories by CategoryId.

    ⚠️  Deleting a category that still has items reassigns those items to the
    Default category — it does NOT delete the items, but it does change their
    categorisation. By default this tool REFUSES to delete a non-empty category
    and reports it as blocked; pass force=True to delete non-empty categories
    anyway (their items move to Default). The built-in Default category can
    never be deleted.

    Emptiness is checked via a full catalogue sweep (~2 min) UNLESS force=True
    (which deletes regardless, so it skips the sweep). Use this tool when you
    have specific category ids to remove; to clean up ALL empty categories at
    once, use delete_empty_categories instead.

    ⚠️  The sweep counts ACTIVE items only. A category holding only ARCHIVED
    items looks empty (item_count 0) but Linnworks refuses to delete it
    ("Category is in use"). Such categories come back as `skipped_in_use` (not
    an error); the response reports `deleted` and `skipped_in_use` counts.
    force=True does NOT override this — it only skips the sweep; the server-side
    in-use guard still applies. Unarchive/reassign the items first to delete.

    Staging: batches over 10 categories require a confirmed_count echo-back
    after you review the manifest. dry_run=True by default.

    Uses Inventory/DeleteCategoryById (one call per category).

    Args:
        category_ids: List of CategoryId GUIDs to delete (from get_categories).
        force: If True, delete even non-empty categories (their items → Default)
            and skip the emptiness sweep. Default False (empty-only, safe).
        confirmed_count: For batches > 10, pass len(category_ids) after review.
        dry_run: If True (default), preview only. Set False to execute.

    Returns:
        A dict with manifest (per-id: name, item_count, status of
        deletable/blocked/error) and, on a live run, per-id delete results.
    """
    if not category_ids:
        raise ValueError("category_ids must contain at least one CategoryId.")

    cats = _fetch_categories()
    by_id = {(c.get("CategoryId") or "").lower(): c for c in cats}

    counts = None
    if not force:
        counts = _count_items_per_category()["counts"]

    manifest: list[dict] = []
    for cid in category_ids:
        cid_norm = (cid or "").strip()
        key = cid_norm.lower()
        rec = by_id.get(key)
        if not cid_norm:
            manifest.append({"category_id": cid, "status": "error", "reason": "empty id"})
            continue
        if key == _DEFAULT_CATEGORY_ID:
            manifest.append({
                "category_id": cid_norm, "category_name": rec.get("CategoryName") if rec else "Default",
                "status": "blocked", "reason": "the built-in Default category cannot be deleted",
            })
            continue
        if rec is None:
            manifest.append({"category_id": cid_norm, "status": "error",
                             "reason": "no category found with this id"})
            continue

        row = {
            "category_id":   cid_norm,
            "category_name": rec.get("CategoryName"),
        }
        if counts is not None:
            n = counts.get(key, 0)
            row["item_count"] = n
            if n > 0:
                row["status"] = "blocked"
                row["reason"] = f"category has {n} item(s); pass force=True to delete (items → Default)"
            else:
                row["status"] = "deletable"
        else:
            # force=True — no sweep; we still note the reassignment risk
            row["status"] = "deletable"
            row["note"] = "force=True — will delete even if non-empty (any items → Default)"
        manifest.append(row)

    deletable = [m for m in manifest if m.get("status") == "deletable"]

    # ── Write guard (threshold 10, on the input list) ──────────────────────────
    guard = _write_guard("delete_categories", category_ids, confirmed_count, dry_run)
    if guard is not None:
        return {**guard, "force": force, "manifest": manifest}

    if dry_run:
        blocked = [m for m in manifest if m.get("status") == "blocked"]
        errors = [m for m in manifest if m.get("status") == "error"]
        return {
            "dry_run":    True,
            "force":      force,
            "item_count": len(category_ids),
            "manifest":   manifest,
            "message": (
                f"Dry run — nothing deleted. {len(deletable)} category(ies) would be "
                f"deleted, {len(blocked)} blocked, {len(errors)} error(s). Review the "
                "manifest, then set dry_run=False to execute."
            ),
        }

    # ── Live execution ─────────────────────────────────────────────────────────
    results = []
    deleted = 0
    skipped_in_use = 0
    for m in deletable:
        cid = m["category_id"]
        err = None
        try:
            call_linnworks("Inventory/DeleteCategoryById", {"categoryId": cid})
        except RuntimeError as exc:
            err = str(exc)
        if err and _is_category_in_use(err):
            # Not really empty — holds archived items or channel references the
            # active-item sweep can't see. Expected skip, not a failure.
            results.append({
                **m, "status": "skipped_in_use",
                "reason": (
                    "Linnworks reports this category is still in use, so it was NOT "
                    "deleted. It almost certainly holds ARCHIVED items (invisible to "
                    "the active-item sweep) or is referenced by a channel/listing "
                    "config. Unarchive/reassign those items (or clear the reference) "
                    "before it can be deleted."
                ),
                "detail": err,
            })
            skipped_in_use += 1
        elif err:
            results.append({**m, "delete_error": err})
        else:
            results.append({**m, "deleted": True})
            deleted += 1

    # Read-back once — confirm the ids we believe we deleted are actually gone
    cats2 = _fetch_categories()
    still = {(c.get("CategoryId") or "").lower() for c in cats2}
    for r in results:
        if r.get("deleted") is True:
            r["deleted"] = (r["category_id"] or "").lower() not in still

    non_deletable = [m for m in manifest if m.get("status") != "deletable"]
    msg = f"Deleted {deleted} of {len(deletable)} deletable category(ies)."
    if skipped_in_use:
        msg += (
            f" {skipped_in_use} skipped — still in use (archived items or channel "
            "references; not truly empty)."
        )
    return {
        "dry_run":        False,
        "force":          force,
        "item_count":     len(category_ids),
        "deleted":        deleted,
        "skipped_in_use": skipped_in_use,
        "results":        results + non_deletable,
        "manifest":       manifest,
        "message":        msg,
    }


@mcp.tool()
def delete_empty_categories(
    confirmed_count: int | None = None,
    dry_run: bool = True,
) -> dict:
    """
    Find and PERMANENTLY delete every inventory category that has no items.

    This is the category-cleanup tool. It sweeps the entire catalogue (~2 min)
    to count items per category, identifies the ones with zero ACTIVE items,
    shows you a manifest, and — on a confirmed live run — deletes them. The
    built-in Default category is always excluded even if it is empty.

    ⚠️  "Empty" here = zero ACTIVE items. The sweep CANNOT see archived items,
    and no Linnworks endpoint counts archived items per category. So a category
    that holds only archived stock is flagged empty here but Linnworks will
    REFUSE to delete it ("Category is in use"). The tool handles this cleanly:
    such categories come back as `skipped_in_use` (not an error), and the
    response reports both `deleted` and `skipped_in_use` counts. To actually
    remove one, unarchive/reassign its items (or clear its channel reference)
    first. This means the empty_count is an UPPER BOUND on what will delete.

    This can surface a LOT of categories (archived ranges, junk from spreadsheet
    imports, seasonal lines you may want to keep) — so ALWAYS review the manifest
    first. If more than 10 categories are empty, you must re-call with
    confirmed_count=<N> to proceed, and the sweep re-runs on that call to
    re-verify emptiness. To delete only a hand-picked subset, copy their ids into
    delete_categories instead.

    dry_run=True by default. Uses Stock/GetStockItems (sweep) +
    Inventory/DeleteCategoryById (one call per empty category).

    Args:
        confirmed_count: For >10 empty categories, pass the empty_count reported
            in the manifest to confirm the deletion.
        dry_run: If True (default), preview only. Set False to execute.

    Returns:
        A dict with empty_count, manifest (each empty category's id + name), and,
        on a live run, per-category delete results.
    """
    swept = _count_items_per_category()
    counts = swept["counts"]
    cats = _fetch_categories()

    empty = [
        c for c in cats
        if (c.get("CategoryId") or "").lower() != _DEFAULT_CATEGORY_ID
        and counts.get((c.get("CategoryId") or "").lower(), 0) == 0
    ]
    empty.sort(key=lambda c: (c.get("CategoryName") or "").lower())
    manifest = [
        {"category_id": c.get("CategoryId"), "category_name": c.get("CategoryName"), "item_count": 0}
        for c in empty
    ]

    # ── Write guard (threshold 10, on the discovered empty list) ───────────────
    guard = _write_guard("delete_empty_categories", empty, confirmed_count, dry_run)
    if guard is not None:
        return {
            **guard,
            "empty_count":  len(empty),
            "total_items":  swept["total_items"],
            "manifest":     manifest,
        }

    if dry_run:
        return {
            "dry_run":     True,
            "empty_count": len(empty),
            "total_items": swept["total_items"],
            "manifest":    manifest,
            "message": (
                f"Dry run — nothing deleted. {len(empty)} empty category(ies) found "
                f"(Default excluded). Review the manifest, then set dry_run=False to "
                "delete them."
            ),
        }

    # ── Live execution ─────────────────────────────────────────────────────────
    results = []
    deleted = 0
    skipped_in_use = 0
    for c in empty:
        cid = c.get("CategoryId")
        err = None
        try:
            call_linnworks("Inventory/DeleteCategoryById", {"categoryId": cid})
        except RuntimeError as exc:
            err = str(exc)
        row = {"category_id": cid, "category_name": c.get("CategoryName")}
        if err and _is_category_in_use(err):
            # "Empty" by the active-item sweep, but Linnworks blocks it — the
            # category holds archived items or channel references. Expected skip.
            row["status"] = "skipped_in_use"
            row["reason"] = (
                "Not truly empty — Linnworks reports it is still in use (ARCHIVED "
                "items, invisible to the active-item sweep, or a channel/listing "
                "reference). Not deleted."
            )
            row["detail"] = err
            skipped_in_use += 1
        elif err:
            row["delete_error"] = err
        else:
            row["deleted"] = True
            deleted += 1
        results.append(row)

    # Read-back once — confirm the ids we believe we deleted are actually gone
    cats2 = _fetch_categories()
    still = {(c.get("CategoryId") or "").lower() for c in cats2}
    for r in results:
        if r.get("deleted") is True:
            r["deleted"] = (r["category_id"] or "").lower() not in still

    msg = f"Deleted {deleted} of {len(empty)} category(ies) reported empty by the sweep."
    if skipped_in_use:
        msg += (
            f" {skipped_in_use} skipped — still in use (archived items or channel "
            "references; the active-item sweep can't see those)."
        )
    return {
        "dry_run":        False,
        "empty_count":    len(empty),
        "deleted":        deleted,
        "skipped_in_use": skipped_in_use,
        "results":        results,
        "manifest":       manifest,
        "message":        msg,
    }


# ---------- Listings — Generic Listing Tool (GLT) ----------
#
# The GLT lets you list EXISTING Linnworks inventory to a sales channel by
# applying a saved "configurator" (the listing template recipe). v1 is Shopify
# only — the most forgiving channel (no strict variation-theme / product-type
# validation), so the smallest, lowest-risk path to build and prove.
#
# Channel identity gotcha (cracked live 18 Jun 2026): GLT identifies the Shopify
# channel by ChannelType="Shopify" + ChannelName="SHOPIFY" — the uppercase
# *Source* string, NOT the per-store SubSource ("SWH Shopify", "Venom
# Skateboards", …). Passing a SubSource as ChannelName returns HTTP 400
# "Channel types mismatch on channel factory creation: Shopify - Shopify."
# The individual stores are distinguished by each configurator's ChannelId /
# SubSource instead (18=SWH Shopify, 21=Venom Skateboards, 26=Icarus Eyewear,
# 29=Lobster Eyewear, 34=The Warehouse Group B2B in this tenant).

GLT_SHOPIFY_CHANNEL_TYPE = "Shopify"
# GLT ChannelName for Shopify = the Source string, NOT a SubSource store name.
GLT_SHOPIFY_CHANNEL_NAME = "SHOPIFY"
# Extended property read per item to decide which configurator to apply.
SHOPIFY_CONFIGURATOR_PROPERTY = "Shopify Configurator"
_ZERO_GUID = "00000000-0000-0000-0000-000000000000"


def _glt_field(info: dict, key: str):
    """Unwrap a GLT ConfiguratorsInfo field.

    Each field comes wrapped as {"Type": "...", "Value": <x>, "Errors": [...]}.
    Returns the inner Value, or the raw field if it isn't wrapped.
    """
    f = info.get(key)
    if isinstance(f, dict) and "Value" in f:
        return f.get("Value")
    return f


def _fetch_shopify_configurators() -> list[dict]:
    """Fetch all Shopify GLT configurators for this tenant.

    Calls GenericListings/GetConfiguratorsInfoPaged with ChannelType=Shopify,
    ChannelName="SHOPIFY". Returns a flat list of normalized dicts:
    {id, name, channel_id, sub_source, show_in_inventory}.

    Confirmed live 18 Jun 2026 — 67 configurators in this tenant. A single page
    of 1000 covers it; tenants with >1000 configurators would need pagination.
    """
    resp = call_linnworks(
        "GenericListings/GetConfiguratorsInfoPaged",
        {"request": {
            "ChannelType": GLT_SHOPIFY_CHANNEL_TYPE,
            "ChannelName": GLT_SHOPIFY_CHANNEL_NAME,
            "PaginationParameters": {"PageNumber": 1, "EntriesPerPage": 1000},
        }},
    )
    infos = resp.get("ConfiguratorsInfo") if isinstance(resp, dict) else None
    out: list[dict] = []
    for it in (infos or []):
        info = it.get("Info") if isinstance(it, dict) and isinstance(it.get("Info"), dict) else it
        out.append({
            "id":                _glt_field(info, "Id"),
            "name":              _glt_field(info, "Name"),
            "channel_id":        _glt_field(info, "ChannelId"),
            "sub_source":        _glt_field(info, "SubSource"),
            "show_in_inventory": _glt_field(info, "IsShowInInventory"),
        })
    return out


def _norm_conf_name(name: str | None) -> str:
    """Normalize a configurator name for case/space-insensitive matching."""
    return (name or "").strip().lower()


# ---------- Channel listings (read) ----------
#
# The read-side companion to list_to_shopify (issue #18). Answers "is this SKU
# already listed, and on which channel/store?" by reading the channel-SKU link
# table (Inventory/GetInventoryItemChannelSKUs). A non-empty row set for a stock
# item means it is mapped/listed on that Source+SubSource — exactly what you need
# to dedupe a brand's in-stock items before a bulk list_to_shopify run, instead
# of guessing from barcodes. Confirmed live 18 Jun 2026 against this tenant
# (SHOPIFY per store, EBAY, AMAZON per region, Mirakl MP, MAGENTO all observed).


def _format_channel_sku_row(r: dict) -> dict:
    """Normalize one StockItemChannelSKU row into our flat shape.

    `update_status` carries the channel sync state ("", "Confirmed",
    "Notification", or a verbose error blob). Pathologically long error blobs
    are truncated to keep bulk responses readable — the row id and reference id
    are preserved so the full status can still be inspected in the Linnworks UI.
    """
    status = r.get("UpdateStatus")
    if isinstance(status, str) and len(status) > 280:
        status = status[:280] + " …(truncated)"
    return {
        "source":               r.get("Source"),       # e.g. SHOPIFY, EBAY, AMAZON, Mirakl MP
        "sub_source":           r.get("SubSource"),     # store/region, e.g. "SWH Shopify", "EBAY0"
        "channel_sku":          r.get("SKU"),
        "channel_reference_id": r.get("ChannelReferenceId"),
        "update_status":        status,
        "listed_quantity":      r.get("ListedQuantity"),
        "max_listed_quantity":  r.get("MaxListedQuantity"),
        "last_update":          r.get("LastUpdate"),
        "ignore_sync":          r.get("IgnoreSync"),
        "is_multi_location":    r.get("IsMultiLocation"),
        "channel_sku_row_id":   r.get("ChannelSKURowId"),
    }


def _fetch_channel_skus_for_ids(stock_item_ids: list[str]) -> dict[str, list]:
    """Batch-fetch channel-SKU link rows for many stock items.

    Returns {stock_item_id (lowercased): [raw StockItemChannelSKU rows]}.
    Wraps Inventory/BatchGetInventoryItemChannelSKUs (POST, payload sent
    UNWRAPPED as {"inventoryItemIds": [...]}), chunked at 200 to stay well under
    the endpoint's 150/min rate limit. Confirmed live 18 Jun 2026.
    """
    out: dict[str, list] = {}
    ids = [i for i in stock_item_ids if i]
    for i in range(0, len(ids), 200):
        chunk = ids[i:i + 200]
        resp = call_linnworks(
            "Inventory/BatchGetInventoryItemChannelSKUs",
            {"inventoryItemIds": chunk},
        )
        for entry in (resp if isinstance(resp, list) else []):
            sid = entry.get("StockItemId")
            if sid:
                out[sid.lower()] = entry.get("ChannelSkus") or []
    return out


@mcp.tool()
def get_channel_listings(sku: str) -> dict:
    """
    Read the existing channel listings for ONE inventory item — answers
    "is this SKU already listed, and on which channel/store?".

    Reads the Linnworks channel-SKU link table
    (Inventory/GetInventoryItemChannelSKUs) for the item. Each row is a live
    mapping between this stock item and a sales-channel listing, so a non-empty
    result means the item is already listed/linked on that channel. Covers every
    channel the item is mapped to — Shopify (per store), eBay, Amazon (per
    region), Mirakl, Magento, etc.

    This is the read-side companion to `list_to_shopify`: use it to confirm
    whether a SKU is already live before (re)listing, instead of guessing from
    the barcode. To check many SKUs at once, use `get_channel_listings_bulk`.

    Args:
        sku: The exact SKU / ItemNumber to look up.

    Returns:
        A dict with:
          - sku, stock_item_id, title
          - is_listed:     True if the item has any channel-SKU mapping
          - listing_count: number of channel listings
          - channels:      distinct Source values (e.g. ["AMAZON", "SHOPIFY"])
          - sub_sources:   distinct store/region names (e.g. ["SWH Shopify"])
          - listings:      per-channel rows (source, sub_source, channel_sku,
                           channel_reference_id, update_status, listed_quantity,
                           max_listed_quantity, last_update, ignore_sync,
                           is_multi_location, channel_sku_row_id)

    Note: Linnworks does not return a ready-made listing URL. `channel_sku` and
    `channel_reference_id` identify the listing on the channel (for Shopify the
    reference is a product:variant:inventory id triple).
    """
    try:
        item = call_linnworks("Inventory/GetInventoryItem", {"sku": sku})
    except RuntimeError:
        return {"error": f"No inventory item found for SKU '{sku}'", "sku": sku}

    stock_item_id = item.get("StockItemId")
    if not stock_item_id:
        return {"error": f"Item found for SKU '{sku}' but StockItemId was missing", "sku": sku}

    rows = call_linnworks_get(
        "Inventory/GetInventoryItemChannelSKUs",
        {"inventoryItemId": stock_item_id},
    )
    listings = [_format_channel_sku_row(r) for r in (rows if isinstance(rows, list) else [])]

    return {
        "sku":           sku,
        "stock_item_id": stock_item_id,
        "title":         item.get("ItemTitle"),
        "is_listed":     len(listings) > 0,
        "listing_count": len(listings),
        "channels":      sorted({l["source"] for l in listings if l.get("source")}),
        "sub_sources":   sorted({l["sub_source"] for l in listings if l.get("sub_source")}),
        "listings":      listings,
    }


@mcp.tool()
def get_channel_listings_bulk(skus: list[str]) -> dict:
    """
    Read the existing channel listings for MANY inventory items at once — the
    batch dedupe companion to `list_to_shopify`.

    For each SKU, resolves it to its StockItemId and reads the channel-SKU link
    table via Inventory/BatchGetInventoryItemChannelSKUs (one batched call per
    200 items). Use this before a bulk listing run to skip SKUs that are already
    live on a channel/store, rather than relying on the barcode heuristic.

    Args:
        skus: List of exact SKUs / ItemNumbers to check.

    Returns:
        A dict with:
          - item_count:     number of SKUs requested
          - resolved_count: how many resolved to a stock item
          - listed_count / unlisted_count: split of resolved items by whether
            they have any channel listing
          - results: per-SKU rows (sku, stock_item_id, title, is_listed,
            listing_count, channels, sub_sources, listings) — same listing shape
            as get_channel_listings
          - unresolved: per-SKU error rows for SKUs that were not found
    """
    if not skus:
        raise ValueError("skus must contain at least one SKU.")

    resolved: list[tuple[str, str]] = []
    titles: dict[str, str] = {}
    unresolved: list[dict] = []

    for raw in skus:
        s = (raw or "").strip()
        if not s:
            unresolved.append({"sku": raw, "error": "empty SKU"})
            continue
        try:
            item = call_linnworks("Inventory/GetInventoryItem", {"sku": s})
        except RuntimeError as exc:
            unresolved.append({"sku": s, "error": f"not found: {exc}"})
            continue
        sid = item.get("StockItemId")
        if not sid:
            unresolved.append({"sku": s, "error": "found but returned no StockItemId"})
            continue
        resolved.append((s, sid))
        titles[sid.lower()] = item.get("ItemTitle")

    by_id = _fetch_channel_skus_for_ids([sid for _, sid in resolved])

    results: list[dict] = []
    listed_count = 0
    for s, sid in resolved:
        listings = [_format_channel_sku_row(r) for r in by_id.get(sid.lower(), [])]
        if listings:
            listed_count += 1
        results.append({
            "sku":           s,
            "stock_item_id": sid,
            "title":         titles.get(sid.lower()),
            "is_listed":     len(listings) > 0,
            "listing_count": len(listings),
            "channels":      sorted({l["source"] for l in listings if l.get("source")}),
            "sub_sources":   sorted({l["sub_source"] for l in listings if l.get("sub_source")}),
            "listings":      listings,
        })

    return {
        "item_count":     len(skus),
        "resolved_count": len(resolved),
        "listed_count":   listed_count,
        "unlisted_count": len(resolved) - listed_count,
        "results":        results,
        "unresolved":     unresolved,
    }


def _format_image_row(r: dict) -> dict:
    """Normalize one image record into our flat shape.

    Handles both endpoint shapes: the single GET (StockItemImage) returns the
    thumbnail under `Source`, while the bulk POST (GetImagesInBulkResponseImage)
    returns it under `FullSourceThumbnail`. Both return the full-size image under
    `FullSource`. Confirmed live 18 Jun 2026.
    """
    return {
        "image_id":      r.get("pkRowId"),
        "is_main":       bool(r.get("IsMain")),
        "sort_order":    r.get("SortOrder"),
        "full_url":      r.get("FullSource"),
        "thumbnail_url": r.get("Source") or r.get("FullSourceThumbnail"),
    }


def _fetch_images_for_ids(stock_item_ids: list[str]) -> dict[str, list]:
    """Batch-fetch image rows for many stock items.

    Returns {stock_item_id (lowercased): [raw image rows]}.
    Wraps Inventory/GetImagesInBulk (POST, payload WRAPPED as
    {"request": {"StockItemIds": [...]}} — sending it unwrapped returns HTTP 400
    "request is empty"), chunked at 200. The bulk response carries `StockItemId`
    on each image (but not `SKU`), so callers map images back to their SKU via the
    resolved id. Confirmed live 18 Jun 2026.
    """
    out: dict[str, list] = {}
    ids = [i for i in stock_item_ids if i]
    for i in range(0, len(ids), 200):
        chunk = ids[i:i + 200]
        resp = call_linnworks(
            "Inventory/GetImagesInBulk",
            {"request": {"StockItemIds": chunk}},
        )
        imgs = resp.get("Images") if isinstance(resp, dict) else None
        for img in (imgs if isinstance(imgs, list) else []):
            sid = img.get("StockItemId")
            if sid:
                out.setdefault(sid.lower(), []).append(img)
    return out


@mcp.tool()
def get_inventory_item_images(sku: str) -> dict:
    """
    Read the images attached to ONE inventory item — answers "does this SKU have
    a product image, how many, and is a main image set?".

    Reads the Linnworks image table (Inventory/GetInventoryItemImages) for the
    item. This is the read-side companion to `add_inventory_item_images` (the
    write tool) and the third piece of the safe-bulk-listing workflow alongside
    `get_channel_listings` (is it already listed?) and `list_to_shopify`.

    Use it as a pre-listing image gate: an item with no image — or one with
    pictures but no main image set — is not ready to go live on a channel.
    To check many SKUs at once, use `get_inventory_item_images_bulk`.

    Args:
        sku: The exact SKU / ItemNumber to look up.

    Returns:
        A dict with:
          - sku, stock_item_id, title
          - has_image:      True if the item has at least one image
          - image_count:    number of images
          - has_main_image: True if any image is flagged as the main image —
                            spot items that have a picture but no main set
          - images:         per-image rows (image_id, is_main, sort_order,
                            full_url, thumbnail_url), sorted by sort_order
    """
    try:
        item = call_linnworks("Inventory/GetInventoryItem", {"sku": sku})
    except RuntimeError:
        return {"error": f"No inventory item found for SKU '{sku}'", "sku": sku}

    stock_item_id = item.get("StockItemId")
    if not stock_item_id:
        return {"error": f"Item found for SKU '{sku}' but StockItemId was missing", "sku": sku}

    rows = call_linnworks_get(
        "Inventory/GetInventoryItemImages",
        {"inventoryItemId": stock_item_id},
    )
    images = [_format_image_row(r) for r in (rows if isinstance(rows, list) else [])]
    images.sort(key=lambda im: (im["sort_order"] if im["sort_order"] is not None else 0))

    return {
        "sku":            sku,
        "stock_item_id":  stock_item_id,
        "title":          item.get("ItemTitle"),
        "has_image":      len(images) > 0,
        "image_count":    len(images),
        "has_main_image": any(im["is_main"] for im in images),
        "images":         images,
    }


@mcp.tool()
def get_inventory_item_images_bulk(skus: list[str]) -> dict:
    """
    Read the images for MANY inventory items at once — the batch pre-listing
    image gate, mirroring `get_channel_listings_bulk`.

    For each SKU, resolves it to its StockItemId (which also yields the title and
    flags SKUs that don't exist), then batch-reads the image table via
    Inventory/GetImagesInBulk (one batched call per 200 items). An item that
    exists but has zero images is reported with has_image=False (a real item to
    fix), distinct from an `unresolved` SKU (not found in the catalogue).

    Use this before a bulk listing run to skip/flag SKUs with no image, so you
    only list the genuinely ready ones.

    Args:
        skus: List of exact SKUs / ItemNumbers to check.

    Returns:
        A dict with:
          - item_count:        number of SKUs requested
          - resolved_count:    how many resolved to a stock item
          - with_image_count / without_image_count: split of resolved items by
            whether they have any image
          - results: per-SKU rows (sku, stock_item_id, title, has_image,
            image_count, has_main_image, images) — same image shape as
            get_inventory_item_images
          - unresolved: per-SKU error rows for SKUs that were not found
    """
    if not skus:
        raise ValueError("skus must contain at least one SKU.")

    resolved: list[tuple[str, str]] = []
    titles: dict[str, str] = {}
    unresolved: list[dict] = []

    for raw in skus:
        s = (raw or "").strip()
        if not s:
            unresolved.append({"sku": raw, "error": "empty SKU"})
            continue
        try:
            item = call_linnworks("Inventory/GetInventoryItem", {"sku": s})
        except RuntimeError as exc:
            unresolved.append({"sku": s, "error": f"not found: {exc}"})
            continue
        sid = item.get("StockItemId")
        if not sid:
            unresolved.append({"sku": s, "error": "found but returned no StockItemId"})
            continue
        resolved.append((s, sid))
        titles[sid.lower()] = item.get("ItemTitle")

    by_id = _fetch_images_for_ids([sid for _, sid in resolved])

    results: list[dict] = []
    with_image = 0
    for s, sid in resolved:
        raw_imgs = by_id.get(sid.lower(), [])
        images = [_format_image_row(r) for r in raw_imgs]
        images.sort(key=lambda im: (im["sort_order"] if im["sort_order"] is not None else 0))
        if images:
            with_image += 1
        results.append({
            "sku":            s,
            "stock_item_id":  sid,
            "title":          titles.get(sid.lower()),
            "has_image":      len(images) > 0,
            "image_count":    len(images),
            "has_main_image": any(im["is_main"] for im in images),
            "images":         images,
        })

    return {
        "item_count":         len(skus),
        "resolved_count":     len(resolved),
        "with_image_count":   with_image,
        "without_image_count": len(resolved) - with_image,
        "results":            results,
        "unresolved":         unresolved,
    }


def _fetch_raw_images(stock_item_id: str) -> list[dict]:
    """Fetch one item's RAW image rows (Inventory/GetInventoryItemImages).

    The image write tools need the untouched `StockItemImage` records — not the
    trimmed shape `_format_image_row` produces — because `Inventory/UpdateImages`
    overwrites the whole row and clears any field omitted from it (the same
    nulls-clear behaviour as UpdateInventoryItem / UpdateStockSupplierStat).
    Carrying the raw row through is what preserves the checksum fields.
    """
    rows = call_linnworks_get(
        "Inventory/GetInventoryItemImages",
        {"inventoryItemId": stock_item_id},
    )
    return rows if isinstance(rows, list) else []


def _image_simple_row(raw: dict, stock_item_id: str) -> dict:
    """Build a StockItemImageSimple payload row from a raw image record.

    Carries every field the model defines so an update never blanks one. Note
    the GET returns the checksum as `CheckSumValue` while the Simple model that
    UpdateImages consumes spells it `ChecksumValue` — both are populated from
    whichever the read supplied.
    """
    checksum = raw.get("ChecksumValue") or raw.get("CheckSumValue")
    return {
        "pkRowId":        raw.get("pkRowId"),
        "IsMain":         bool(raw.get("IsMain")),
        "SortOrder":      raw.get("SortOrder"),
        "ChecksumValue":  checksum,
        "RawChecksum":    raw.get("RawChecksum"),
        "StockItemId":    stock_item_id,
        "StockItemIntId": raw.get("StockItemIntId"),
    }


@mcp.tool()
def delete_inventory_item_images(
    sku: str,
    image_ids: list[str],
    confirmed_count: int | None = None,
    dry_run: bool = True,
) -> dict:
    """
    Delete one or more images from an inventory item — the corrective counterpart
    to `add_inventory_item_images`.

    Use this when an image on an item is wrong: a bad colourway, a watermarked
    source, or the same picture added twice. Until now the only fix was editing
    the item by hand in the Linnworks UI.

    IRREVERSIBLE — a deleted image row is gone; re-adding means calling
    `add_inventory_item_images` with the original URL again. Batches of more than
    10 images require confirmed_count=len(image_ids).

    Read-before-write: the item's current images are read first, so the manifest
    shows exactly which image (with its URL and main/sort flags) each id refers
    to. An id that isn't on the item is reported in `unresolved` and skipped —
    it never blocks the ids that are valid. After the write the images are read
    back to confirm each one is gone.

    Args:
        sku: The exact SKU / ItemNumber whose images should be deleted.
        image_ids: Image ids to remove (the `image_id` field returned by
            `get_inventory_item_images` — call that first to pick them).
        confirmed_count: For batches > 10, pass len(image_ids) here.
        dry_run: If True (default), returns the manifest without deleting.
            Set to False to execute.

    Returns:
        A dict with:
          - sku, stock_item_id, dry_run, item_count
          - manifest:        per-id preview of the image that would be deleted
          - unresolved:      ids not present on this item (skipped)
          - deleted:         ids confirmed gone by the read-back (live run only)
          - still_present:   ids the read-back still finds (live run only)
          - remaining_count: images left on the item after the delete (live run)
          - images:          the item's remaining images (live run only)
    """
    if not image_ids:
        raise ValueError("image_ids is empty — nothing to delete.")

    try:
        item = call_linnworks("Inventory/GetInventoryItem", {"sku": sku})
    except RuntimeError:
        return {"error": f"No inventory item found for SKU '{sku}'", "sku": sku}

    stock_item_id = item.get("StockItemId")
    if not stock_item_id:
        return {"error": f"Item found for SKU '{sku}' but StockItemId was missing", "sku": sku}

    # ── Read before write ─────────────────────────────────────────────────────
    current = {
        (r.get("pkRowId") or "").lower(): r
        for r in _fetch_raw_images(stock_item_id)
    }

    manifest, unresolved, to_delete = [], [], []
    for img_id in image_ids:
        raw = current.get((img_id or "").strip().lower())
        if raw is None:
            unresolved.append({
                "image_id": img_id,
                "error":    f"Image id '{img_id}' is not on SKU '{sku}' — skipped.",
            })
            continue
        to_delete.append(raw.get("pkRowId"))
        manifest.append({
            **_format_image_row(raw),
            "action": "delete",
        })

    if not to_delete:
        return {
            "sku":            sku,
            "stock_item_id":  stock_item_id,
            "dry_run":        dry_run,
            "item_count":     len(image_ids),
            "manifest":       manifest,
            "unresolved":     unresolved,
            "message":        "None of the given image ids are on this item — nothing to delete.",
        }

    # ── Write guard ───────────────────────────────────────────────────────────
    guard = _write_guard("delete_inventory_item_images", to_delete, confirmed_count, dry_run)
    if guard is not None:
        return {**guard, "sku": sku, "manifest": manifest, "unresolved": unresolved}

    if dry_run:
        return {
            "sku":           sku,
            "stock_item_id": stock_item_id,
            "dry_run":       True,
            "item_count":    len(to_delete),
            "manifest":      manifest,
            "unresolved":    unresolved,
            "message":       (
                f"Dry run — no images deleted. {len(to_delete)} image(s) would be "
                f"removed from '{sku}'. Set dry_run=False to execute."
            ),
        }

    # ── Live execution ────────────────────────────────────────────────────────
    # DeleteInventoryItemImageBulk (not DeleteImagesFromInventoryItem, which
    # keys off image URLs) — this one takes image ids, matching what
    # get_inventory_item_images hands back.
    resp = call_linnworks(
        "Inventory/DeleteInventoryItemImageBulk",
        {"request": [{
            "InventoryItemId": stock_item_id,
            "ItemNumber":      sku,
            "ImageIds":        to_delete,
        }]},
    )

    # ── Read back ─────────────────────────────────────────────────────────────
    remaining = _fetch_raw_images(stock_item_id)
    remaining_ids = {(r.get("pkRowId") or "").lower() for r in remaining}
    deleted       = [i for i in to_delete if (i or "").lower() not in remaining_ids]
    still_present = [i for i in to_delete if (i or "").lower() in remaining_ids]

    images = [_format_image_row(r) for r in remaining]
    images.sort(key=lambda im: (im["sort_order"] if im["sort_order"] is not None else 0))

    return {
        "sku":             sku,
        "stock_item_id":   stock_item_id,
        "dry_run":         False,
        "item_count":      len(to_delete),
        "manifest":        manifest,
        "unresolved":      unresolved,
        "deleted":         deleted,
        "still_present":   still_present,
        "remaining_count": len(images),
        "has_main_image":  any(im["is_main"] for im in images),
        "images":          images,
        "result_status":   resp.get("ResultStatus") if isinstance(resp, dict) else None,
    }


@mcp.tool()
def set_inventory_item_image_order(
    sku: str,
    image_ids: list[str] | None = None,
    main_image_id: str | None = None,
    confirmed_count: int | None = None,
    dry_run: bool = True,
) -> dict:
    """
    Reorder an inventory item's images and/or set which one is the main image.

    This matters because the main image is the photo channels show as the
    storefront hero — after `add_inventory_item_images` the ordering isn't
    controllable, so a secondary shot can end up as the listing's front photo.

    ⚠️ THE MAIN IMAGE IS ALWAYS FIRST — Linnworks pins it to sort_order 0 and you
    cannot override that (live-proven 17 Jul 2026). The final order is always:

        [main image] + [your requested order, with the main removed]

    So to change which photo leads, pass `main_image_id` — reordering alone can
    never put a non-main image first. `image_ids` only controls the order of the
    images BEHIND the main one. If the first id you pass isn't the main image,
    the write still succeeds but the main is forced ahead of it; the manifest
    shows the true predicted order and sets `main_forced_first`, so a dry run
    never promises an order Linnworks won't deliver.

    Pass whichever you need (at least one):

      - image_ids:     desired order. May be PARTIAL — any image you don't name
                       keeps its existing relative order and follows the ones you
                       did name.
      - main_image_id: the image to make the hero. Linnworks clears the main flag
                       on the others itself.

    Changing the images does NOT push them to a live listing — follow with
    `refresh_channel_listing` for that, and note the standing warning that a
    green refresh does not by itself prove the storefront photo changed. Read the
    live listing back.

    Args:
        sku: The exact SKU / ItemNumber whose images should be reordered.
        image_ids: Desired image order (may be partial). Omit to leave order alone.
        main_image_id: Image id to make the main/hero image. Omit to leave it alone.
        confirmed_count: For reorders touching > 25 images, pass the number of
            images on the item.
        dry_run: If True (default), returns the manifest without writing.
            Set to False to execute.

    Returns:
        A dict with:
          - sku, stock_item_id, dry_run
          - manifest:          per-image before/after (sort_order, is_main), in
                               the predicted final order
          - main_forced_first: True if the main image was pulled ahead of the
                               first id you asked for
          - unresolved:        ids not present on this item (skipped)
          - images:            the item's images in their new order (live run only)
          - has_main_image:    whether a main image is set (live run only)
    """
    if not image_ids and not main_image_id:
        raise ValueError(
            "Pass image_ids (to reorder), main_image_id (to set the main image), or both."
        )

    try:
        item = call_linnworks("Inventory/GetInventoryItem", {"sku": sku})
    except RuntimeError:
        return {"error": f"No inventory item found for SKU '{sku}'", "sku": sku}

    stock_item_id = item.get("StockItemId")
    if not stock_item_id:
        return {"error": f"Item found for SKU '{sku}' but StockItemId was missing", "sku": sku}

    # ── Read before write ─────────────────────────────────────────────────────
    raw_rows = _fetch_raw_images(stock_item_id)
    if not raw_rows:
        return {
            "sku":           sku,
            "stock_item_id": stock_item_id,
            "dry_run":       dry_run,
            "message":       f"SKU '{sku}' has no images — nothing to reorder.",
            "images":        [],
        }

    by_id = {(r.get("pkRowId") or "").lower(): r for r in raw_rows}
    unresolved = []

    for label, given in (("image_ids", image_ids or []), ("main_image_id", [main_image_id] if main_image_id else [])):
        for img_id in given:
            if (img_id or "").strip().lower() not in by_id:
                unresolved.append({
                    "image_id": img_id,
                    "field":    label,
                    "error":    f"Image id '{img_id}' is not on SKU '{sku}' — skipped.",
                })

    if main_image_id and any(u["field"] == "main_image_id" for u in unresolved):
        return {
            "sku":           sku,
            "stock_item_id": stock_item_id,
            "dry_run":       dry_run,
            "unresolved":    unresolved,
            "error":         (
                f"main_image_id '{main_image_id}' is not an image on SKU '{sku}' — "
                "refusing to write. Call get_inventory_item_images to list valid ids."
            ),
        }

    # ── Work out the order Linnworks will ACTUALLY produce ────────────────────
    # Named ids lead in the order given; everything else keeps its existing
    # relative order behind them. A partial list is therefore a promotion, not a
    # destructive re-sort of the images the caller didn't mention.
    existing_sorted = sorted(
        raw_rows,
        key=lambda r: (r.get("SortOrder") if r.get("SortOrder") is not None else 0),
    )
    named = [
        by_id[(i or "").strip().lower()]
        for i in (image_ids or [])
        if (i or "").strip().lower() in by_id
    ]
    named_ids = {(r.get("pkRowId") or "").lower() for r in named}
    requested = named + [
        r for r in existing_sorted if (r.get("pkRowId") or "").lower() not in named_ids
    ]

    # The effective main (new one if being set, else the current one) is pinned to
    # position 0 by the server no matter where the caller put it — so predict that
    # rather than promise an order Linnworks will override.
    main_key = (main_image_id or "").strip().lower() or next(
        ((r.get("pkRowId") or "").lower() for r in raw_rows if r.get("IsMain")), ""
    )
    main_row = by_id.get(main_key)
    new_order = (
        [main_row] + [r for r in requested if (r.get("pkRowId") or "").lower() != main_key]
        if main_row else requested
    )
    main_forced_first = bool(
        main_row and requested and (requested[0].get("pkRowId") or "").lower() != main_key
    )

    manifest = []
    for position, raw in enumerate(new_order):
        img_id      = raw.get("pkRowId")
        old_sort    = raw.get("SortOrder")
        old_is_main = bool(raw.get("IsMain"))
        new_is_main = (img_id or "").lower() == main_key
        manifest.append({
            "image_id":       img_id,
            "full_url":       raw.get("FullSource"),
            "old_sort_order": old_sort,
            "new_sort_order": position,
            "old_is_main":    old_is_main,
            "new_is_main":    new_is_main,
            "changed":        old_sort != position or new_is_main != old_is_main,
        })

    main_changing = bool(main_row) and not main_row.get("IsMain")
    order_changing = [m["image_id"] for m in manifest] != [
        r.get("pkRowId") for r in existing_sorted
    ] or any(m["old_sort_order"] != m["new_sort_order"] for m in manifest)

    warning = None
    if main_forced_first:
        warning = (
            f"Image '{requested[0].get('pkRowId')}' was requested first, but Linnworks "
            f"pins the MAIN image to position 0 — it will lead instead. Pass "
            f"main_image_id to change which image is the hero."
        )

    if not order_changing and not main_changing:
        return {
            "sku":               sku,
            "stock_item_id":     stock_item_id,
            "dry_run":           dry_run,
            "manifest":          manifest,
            "unresolved":        unresolved,
            "main_forced_first": main_forced_first,
            "warning":           warning,
            "message":           "Images are already in the requested order — nothing to change.",
        }

    # ── Write guard ───────────────────────────────────────────────────────────
    guard = _write_guard(
        "set_inventory_item_image_order", new_order, confirmed_count, dry_run
    )
    if guard is not None:
        return {**guard, "sku": sku, "manifest": manifest, "unresolved": unresolved}

    if dry_run:
        return {
            "sku":               sku,
            "stock_item_id":     stock_item_id,
            "dry_run":           True,
            "manifest":          manifest,
            "unresolved":        unresolved,
            "main_forced_first": main_forced_first,
            "warning":           warning,
            "message":           (
                "Dry run — nothing written. Images would be ordered as shown in the "
                "manifest"
                + (f" and the main image set to '{main_image_id}'" if main_changing else "")
                + ". Set dry_run=False to execute."
            ),
        }

    # ── Live execution ────────────────────────────────────────────────────────
    # Set the main image FIRST, so the sort write that follows agrees with the
    # server's "main is position 0" rule instead of fighting it. Then submit the
    # FULL set main-first — a full, main-first payload is honoured exactly, while
    # a partial one gets re-normalised (live-proven 17 Jul 2026). Each row carries
    # every field: UpdateImages clears anything omitted. Both calls return 204.
    if main_changing:
        call_linnworks_void(
            "Inventory/SetInventoryItemImageAsMain",
            {"inventoryItemId": stock_item_id, "mainImageId": main_row.get("pkRowId")},
        )

    if order_changing:
        rows = []
        for position, raw in enumerate(new_order):
            row = _image_simple_row(raw, stock_item_id)
            row["SortOrder"] = position
            row["IsMain"] = (raw.get("pkRowId") or "").lower() == main_key
            rows.append(row)
        call_linnworks_void("Inventory/UpdateImages", {"images": rows})

    # ── Read back ─────────────────────────────────────────────────────────────
    images = [_format_image_row(r) for r in _fetch_raw_images(stock_item_id)]
    images.sort(key=lambda im: (im["sort_order"] if im["sort_order"] is not None else 0))

    return {
        "sku":               sku,
        "stock_item_id":     stock_item_id,
        "dry_run":           False,
        "reordered_count":   len(new_order) if order_changing else 0,
        "main_image_set":    main_row.get("pkRowId") if main_changing else None,
        "main_forced_first": main_forced_first,
        "warning":           warning,
        "manifest":          manifest,
        "unresolved":        unresolved,
        "order_matches_plan": [im["image_id"] for im in images] == [m["image_id"] for m in manifest],
        "has_main_image":    any(im["is_main"] for im in images),
        "images":            images,
    }


@mcp.tool()
def list_to_shopify(
    skus: list[str],
    configurator: str | None = None,
    default_configurator: str | None = None,
    sub_source: str | None = None,
    confirmed_count: int | None = None,
    dry_run: bool = True,
) -> dict:
    """
    List EXISTING Linnworks inventory items to Shopify via the Generic Listing
    Tool (GLT) — the API equivalent of the UI flow "select items → apply
    configurator → create listings". Shopify only (v1).

    Per-item configurator selection is data-driven, so a mixed batch auto-routes
    each product to the right listing recipe:
      1. `configurator` override (if given) — forces this configurator for ALL skus
      2. else the item's "Shopify Configurator" extended property value
      3. else `default_configurator` (the batch fallback)
    The chosen name is validated against the live Shopify configurator catalogue
    (GenericListings/GetConfiguratorsInfoPaged). A name that doesn't match a real
    configurator becomes a per-item error row in `unresolved` — reported, not
    fatal — so one typo or one unfilled property never sinks the whole batch.

    The same configurator name can exist on more than one Shopify store (e.g. a
    deck configurator on both "SWH Shopify" and "Venom Skateboards"). A name that
    matches configurators on multiple stores is ambiguous and is reported as such;
    pass `sub_source` (e.g. "SWH Shopify") to scope the batch to one store.

    Flow per configurator group (live run only):
      GenericListings/CreateTemplates  → returns the created template ids
      GenericListings/ProcessTemplates → pushes those templates live to Shopify
    Items are grouped by their resolved configurator so each group is one
    CreateTemplates + one ProcessTemplates call.

    ⚠️  A live run (dry_run=False) creates REAL Shopify listings — customer-facing
    and not trivially undone. Always run dry_run=True first and read the plan.
    The read/selection path (configurator catalogue, SKU + extended-property
    resolution) is live-confirmed; the write path (CreateTemplates /
    ProcessTemplates) is built to the OpenAPI spec but NOT yet live-exercised in
    this tenant — start with a single SKU.

    For batches larger than 25 SKUs this tool stages: it returns the plan and asks
    you to confirm with confirmed_count=<N> before executing.

    Args:
        skus: Exact SKUs / ItemNumbers to list. Each is resolved to its
            StockItemId and its "Shopify Configurator" extended property is read.
        configurator: Optional override — force this configurator name for every
            SKU, ignoring per-item extended properties.
        default_configurator: Optional fallback configurator name for any item
            whose "Shopify Configurator" property is blank (only consulted when
            `configurator` is not given).
        sub_source: Optional Shopify store name to scope to (e.g. "SWH Shopify",
            "Venom Skateboards"). Disambiguates configurator names shared across
            stores and restricts which configurators count as valid.
        confirmed_count: For batches > 25 SKUs, pass len(skus) after reviewing
            the plan to confirm the write.
        dry_run: If True (default), returns the plan without creating any listing.
            Set to False to create and push the Shopify listings.

    Returns:
        A dict with:
          - dry_run, item_count
          - configurator_catalogue_count: configurators available for matching
          - available_sub_sources: Shopify store names present in the catalogue
          - plan: per-SKU resolution for items that resolved cleanly (sku,
            stock_item_id, title, configurator, configurator_id, channel_id,
            sub_source, decision)
          - groups: items grouped by resolved configurator — what each
            CreateTemplates call would cover
          - unresolved: per-SKU error rows (not found / no configurator decided /
            name not in catalogue / ambiguous across stores)
          - results: per-group outcome with created template ids and process
            status (live run only)
    """
    if not skus:
        raise ValueError("skus must contain at least one SKU.")

    # Injection check on the free-text selection args (defensive — these are
    # matched against a fixed catalogue, so an injected string just won't match,
    # but the framework says check every free-text write parameter).
    _check_injection("configurator", configurator or "")
    _check_injection("default_configurator", default_configurator or "")
    _check_injection("sub_source", sub_source or "")

    # ── Fetch the live Shopify configurator catalogue (read-before-write) ──────
    catalogue = _fetch_shopify_configurators()
    available_sub_sources = sorted(
        {c["sub_source"] for c in catalogue if c.get("sub_source")}
    )

    # Optional store scope. Validate up front so a typo'd sub_source fails loudly
    # rather than silently matching nothing.
    scope = catalogue
    if sub_source:
        scope = [c for c in catalogue if _norm_conf_name(c.get("sub_source")) == _norm_conf_name(sub_source)]
        if not scope:
            return {
                "error": (
                    f"sub_source '{sub_source}' is not a Shopify store in this tenant. "
                    f"Available: {available_sub_sources}"
                ),
                "available_sub_sources": available_sub_sources,
            }

    # name → [configurators] (within the chosen scope)
    by_name: dict[str, list[dict]] = {}
    for c in scope:
        by_name.setdefault(_norm_conf_name(c.get("name")), []).append(c)

    # ── Resolve each SKU → GUID, decide configurator, validate ────────────────
    sku_cache: dict[str, str] = {}
    plan: list[dict] = []
    unresolved: list[dict] = []

    for raw_sku in skus:
        sku = (raw_sku or "").strip()
        if not sku:
            unresolved.append({"sku": raw_sku, "error": "empty SKU"})
            continue

        # Resolve identity (StockItemId + title)
        try:
            item = call_linnworks("Inventory/GetInventoryItem", {"sku": sku})
        except RuntimeError as exc:
            unresolved.append({"sku": sku, "error": f"not found: {exc}"})
            continue
        stock_item_id = item.get("StockItemId")
        if not stock_item_id:
            unresolved.append({"sku": sku, "error": "found but returned no StockItemId"})
            continue
        sku_cache[sku] = stock_item_id
        title = item.get("ItemTitle")

        # Decide which configurator name applies + where the decision came from
        decision: str
        conf_name: str | None
        if configurator:
            conf_name, decision = configurator, "override"
        else:
            prop_value = None
            try:
                props = call_linnworks(
                    "Inventory/GetInventoryItemExtendedProperties",
                    {"inventoryItemId": stock_item_id},
                )
                for p in (props if isinstance(props, list) else []):
                    if _norm_conf_name(p.get("ProperyName")) == _norm_conf_name(SHOPIFY_CONFIGURATOR_PROPERTY):
                        val = (p.get("PropertyValue") or "").strip()
                        if val:
                            prop_value = val
                        break
            except RuntimeError:
                prop_value = None  # treat property read failure as "blank"

            if prop_value:
                conf_name, decision = prop_value, "extended_property"
            elif default_configurator:
                conf_name, decision = default_configurator, "default"
            else:
                unresolved.append({
                    "sku": sku, "stock_item_id": stock_item_id, "title": title,
                    "error": (
                        f"no configurator: item has no '{SHOPIFY_CONFIGURATOR_PROPERTY}' "
                        "extended property and no default_configurator was given"
                    ),
                })
                continue

        # Validate the chosen name against the catalogue
        matches = by_name.get(_norm_conf_name(conf_name), [])
        if not matches:
            unresolved.append({
                "sku": sku, "stock_item_id": stock_item_id, "title": title,
                "configurator": conf_name, "decision": decision,
                "error": (
                    f"configurator '{conf_name}' not found in the Shopify catalogue"
                    + (f" for store '{sub_source}'" if sub_source else "")
                    + " — check spelling/casing against available configurators"
                ),
            })
            continue
        if len(matches) > 1:
            unresolved.append({
                "sku": sku, "stock_item_id": stock_item_id, "title": title,
                "configurator": conf_name, "decision": decision,
                "error": (
                    f"ambiguous: '{conf_name}' exists on "
                    f"{len(matches)} stores ({[m['sub_source'] for m in matches]}) — "
                    "pass sub_source to disambiguate"
                ),
            })
            continue

        chosen = matches[0]
        plan.append({
            "sku":             sku,
            "stock_item_id":   stock_item_id,
            "title":           title,
            "configurator":    chosen.get("name"),
            "configurator_id": chosen.get("id"),
            "channel_id":      chosen.get("channel_id"),
            "sub_source":      chosen.get("sub_source"),
            "decision":        decision,
        })

    # ── Dedupe: drop items already listed on their target Shopify store ───────
    # Read-before-write via the channel-SKU link table (see get_channel_listings,
    # issue #18). Any resolved item that already has a SHOPIFY channel-SKU on its
    # target store (the configurator's sub_source) is moved out of the listing
    # plan into `already_listed` and is NOT sent to CreateTemplates — so a bulk
    # run can't silently create a duplicate customer-facing Shopify listing.
    # Runs on both dry runs and live runs. A lookup failure degrades safely:
    # dedupe is skipped and a warning is surfaced rather than blocking the listing.
    already_listed: list[dict] = []
    dedupe_warning: str | None = None
    if plan:
        try:
            channel_map = _fetch_channel_skus_for_ids([r["stock_item_id"] for r in plan])
            still_to_list: list[dict] = []
            for row in plan:
                existing = [
                    _format_channel_sku_row(cr)
                    for cr in channel_map.get((row["stock_item_id"] or "").lower(), [])
                    if _norm_conf_name(cr.get("Source")) == _norm_conf_name(GLT_SHOPIFY_CHANNEL_NAME)
                    and _norm_conf_name(cr.get("SubSource")) == _norm_conf_name(row["sub_source"])
                ]
                if existing:
                    already_listed.append({**row, "existing_listings": existing})
                else:
                    still_to_list.append(row)
            plan = still_to_list
        except RuntimeError as exc:
            dedupe_warning = (
                f"could not check existing channel listings ({exc}); proceeding "
                "WITHOUT dedupe — verify nothing is double-listed"
            )

    # ── Group resolved items by configurator (what each CreateTemplates covers) ─
    groups: dict[int, dict] = {}
    for row in plan:
        cid = row["configurator_id"]
        g = groups.setdefault(cid, {
            "configurator":    row["configurator"],
            "configurator_id": cid,
            "channel_id":      row["channel_id"],
            "sub_source":      row["sub_source"],
            "skus":            [],
            "inventory_item_ids": [],
        })
        g["skus"].append(row["sku"])
        g["inventory_item_ids"].append(row["stock_item_id"])
    group_list = list(groups.values())

    base_out = {
        "item_count":                    len(skus),
        "configurator_catalogue_count":  len(catalogue),
        "available_sub_sources":         available_sub_sources,
        "plan":                          plan,
        "groups":                        [
            {k: v for k, v in g.items() if k != "inventory_item_ids"}
            for g in group_list
        ],
        "already_listed":                already_listed,
        "unresolved":                    unresolved,
    }
    if dedupe_warning:
        base_out["dedupe_warning"] = dedupe_warning

    # ── Write guard (threshold 25) ────────────────────────────────────────────
    guard = _write_guard("list_to_shopify", skus, confirmed_count, dry_run)
    if guard is not None:
        return {**guard, **base_out}

    if dry_run:
        return {
            "dry_run": True,
            **base_out,
            "message": (
                f"Dry run — nothing listed. {len(plan)} SKU(s) across "
                f"{len(group_list)} configurator group(s) would be listed to Shopify; "
                f"{len(already_listed)} SKU(s) already live on their target store and "
                f"skipped (see already_listed); "
                f"{len(unresolved)} SKU(s) could not be resolved (see unresolved). "
                "Review the plan, then set dry_run=False to create and push the "
                "listings. A live run creates real customer-facing Shopify listings."
            ),
        }

    if not group_list:
        msg = "No SKUs resolved to a configurator; nothing was listed."
        if already_listed:
            msg = (
                f"Nothing listed — all resolved SKU(s) were already live on their "
                f"target store ({len(already_listed)} skipped, see already_listed)."
            )
        return {
            "dry_run": False,
            **base_out,
            "results": [],
            "message": msg,
        }

    # ── Live execution: per group, CreateTemplates then ProcessTemplates ───────
    results: list[dict] = []
    for g in group_list:
        ids = g["inventory_item_ids"]
        result = {
            "configurator":    g["configurator"],
            "configurator_id": g["configurator_id"],
            "channel_id":      g["channel_id"],
            "sub_source":      g["sub_source"],
            "sku_count":       len(ids),
            "created_template_ids": [],
            "processed":       False,
        }
        try:
            create_resp = call_linnworks(
                "GenericListings/CreateTemplates",
                {"request": {
                    "ChannelType": GLT_SHOPIFY_CHANNEL_TYPE,
                    "ChannelName": GLT_SHOPIFY_CHANNEL_NAME,
                    "Parameters": {
                        "SelectedRegions":  [],
                        "Token":            _ZERO_GUID,
                        "InventoryItemIds": ids,
                        "ChannelId":        g["channel_id"],
                    },
                    "PaginationParameters": {"PageNumber": 1, "EntriesPerPage": max(len(ids), 1)},
                    "ConfiguratorId": g["configurator_id"],
                }},
            )
        except RuntimeError as exc:
            result["error"] = f"CreateTemplates failed: {exc}"
            results.append(result)
            continue

        created_ids = []
        if isinstance(create_resp, dict):
            created_ids = create_resp.get("AllCreatedIds") or []
        result["created_template_ids"] = created_ids

        if not created_ids:
            result["error"] = (
                "CreateTemplates returned no template ids (AllCreatedIds empty) — "
                "nothing to process. Inspect TemplatesInfo in the Linnworks UI."
            )
            results.append(result)
            continue

        try:
            call_linnworks(
                "GenericListings/ProcessTemplates",
                {"request": {
                    "ChannelType": GLT_SHOPIFY_CHANNEL_TYPE,
                    "ChannelName": GLT_SHOPIFY_CHANNEL_NAME,
                    "TemplateRequests": [
                        {"TemplateId": tid, "Action": "Create"} for tid in created_ids
                    ],
                    "ClientContext": {"Activity": "list_to_shopify", "Source": "linnworks-mcp"},
                }},
            )
            result["processed"] = True
        except RuntimeError as exc:
            result["error"] = (
                f"templates created (ids {created_ids}) but ProcessTemplates failed: {exc}"
            )
        results.append(result)

    listed = sum(1 for r in results if r.get("processed"))
    return {
        "dry_run":    False,
        **base_out,
        "results":    results,
        "message": (
            f"{listed}/{len(results)} configurator group(s) listed and pushed to "
            "Shopify. Verify the new listings in the Linnworks GLT / your Shopify "
            "admin — there is no clean API read-back for newly created listings. "
            "Any per-group error is surfaced in results[].error."
        ),
    }


# GLT actions that re-push an EXISTING listing (used by refresh_channel_listing).
# The full enum on TemplateToProcess also includes Create/Delete/NotAllowed,
# which are not "refresh" actions.
_GLT_PROCESS_ACTIONS = {
    "Create", "Update", "Relist", "Delete", "Revise", "ChannelSpecific", "NotAllowed",
}
# Auto-mode: which of the template's NextSuggestedAction values count as a valid
# "push my pending changes to the live listing" action.
_GLT_REFRESH_ACTIONS = {"Update", "Revise", "Relist", "ChannelSpecific"}


@mcp.tool()
def refresh_channel_listing(
    skus: list[str],
    sub_source: str = "SWH Shopify",
    action: str | None = None,
    confirmed_count: int | None = None,
    dry_run: bool = True,
) -> dict:
    """
    Re-push / revise EXISTING Shopify listings so updated item data — extended
    properties, title, price, description, etc. — propagates to the live channel.
    Shopify only (v1).

    This is the revise counterpart to `list_to_shopify`: that tool CREATES new
    listings; this one REVISES listings that already exist. It never creates a
    listing — if a SKU isn't already live on the target store it's reported in
    `unresolved` (use `list_to_shopify` to create it).

    Flow per SKU (read-before-write):
      1. Resolve SKU → StockItemId + title.
      2. Confirm the item has a SHOPIFY channel-SKU mapping on `sub_source` (the
         channel-SKU link table — see get_channel_listings). Not listed →
         unresolved.
      3. GenericListings/OpenTemplatesByInventory → open the item's EXISTING GLT
         template for that store (this OPENS the existing template, it does NOT
         create a new one — so it can't duplicate the listing).
      4. (live run) GenericListings/ProcessTemplates with the revise action →
         pushes the current item data to the live Shopify listing.

    Variation groups (issue #26): GLT variation listings hang off the variation
    PARENT — the parent holds the template (and usually no channel-SKU row),
    the children hold the channel-SKU mappings (and no templates). Both
    directions are handled automatically:
      - Passing CHILD SKUs: a child with no template of its own falls back to
        its variation parent's template (`via_variation_parent: true` in the
        plan). Several children of one group dedupe to ONE parent-template push
        (`covers_skus` lists the inputs it covers). Revising the parent
        template pushes ALL variants of the multi-variant Shopify product.
      - Passing the PARENT SKU: it counts as listed when any of its children is
        mapped on the store (`listed_via_children` in the plan).
    This is how a Shopify "Compare at price" (compare_at_price, mapped from the
    `special_price` extended property) is pushed for variant listings: update
    `special_price` on the children, then refresh — the parent-template revise
    carries the new compare-at to the live listing.

    Action selection (per template, data-driven):
      - action=None (default, "auto"): use the template's own
        `NextSuggestedAction` when GLT marks it allowed — this is the action the
        GLT engine itself computed for pushing the pending change (typically
        "Update" for Shopify) — otherwise fall back to "Revise".
      - action="Revise"/"Update"/"Relist"/…: force that GLT action for every item.
    Templates GLT marks as locked, or where neither the suggested action nor
    Revise is allowed, are reported in `unresolved` rather than force-pushed.

    ⚠️  A live run (dry_run=False) changes REAL customer-facing Shopify listings.

    ⚠️⚠️  STALE-SNAPSHOT HAZARD (live-proven 14 Jul 2026): ProcessTemplates
    Update pushes the template's STORED field snapshot, NOT the item's current
    data. A template whose fields were last built months ago (see
    `LastModificationTime` on the open template) will push those old values —
    a live run on tpl 39076 overwrote the current £89.95 selling price with
    the template's stale £79.95 and left the stale compare-at in place, and
    the storefront had to be repaired. GLT's `NextSuggestedAction: "Update"`
    does NOT mean the template body is fresh, and `action="Revise"` behaves
    IDENTICALLY to "Update" (both push the stored snapshot — live-proven).
    No public API refreshes a variation template's fields (#27): only
    live-run a template you know is fresh (the UI listing screen rebuilds
    fields on open; the API path does not), and read the live listing back
    immediately after every push.

    For batches larger than 25 SKUs this tool stages: it returns the plan and asks
    you to confirm with confirmed_count=<N> before executing.

    Args:
        skus: Exact SKUs / ItemNumbers whose Shopify listings to refresh.
        sub_source: Shopify store name (default "SWH Shopify"). Scopes both the
            "is it listed?" check and which store's template is opened/revised.
        action: Optional GLT action override (e.g. "Revise", "Update"). Default
            None = auto (use the template's NextSuggestedAction, else "Revise").
        confirmed_count: For batches > 25 SKUs, pass len(skus) after reviewing
            the plan to confirm the write.
        dry_run: If True (default), returns the plan without pushing anything.
            Set to False to push the revisions to Shopify.

    Returns:
        A dict with:
          - dry_run, item_count, target_sub_source, target_channel_id,
            available_sub_sources
          - plan: per-TEMPLATE rows that would be revised (sku, stock_item_id,
            title, template_id, configurator_id, active_listing_id, status,
            action, next_suggested_action, is_allowed_to_revise, covers_skus;
            plus via_variation_parent / listed_via_children where a variation
            group was resolved). Deduped: inputs sharing one template = one row.
          - unresolved: per-SKU error rows (not found / not listed on the store /
            no template / locked / no allowed revise action)
          - results: per-SKU push outcome (live run only)
    """
    if not skus:
        raise ValueError("skus must contain at least one SKU.")

    _check_injection("sub_source", sub_source or "")
    _check_injection("action", action or "")

    if action is not None and action not in _GLT_PROCESS_ACTIONS:
        raise ValueError(
            f"action '{action}' is not a valid GLT action. Valid: {sorted(_GLT_PROCESS_ACTIONS)}"
        )

    # ── Resolve target store ChannelId from the configurator catalogue ────────
    catalogue = _fetch_shopify_configurators()
    available_sub_sources = sorted({c["sub_source"] for c in catalogue if c.get("sub_source")})
    ss_to_channel: dict[str, int] = {}
    for c in catalogue:
        ss, cid = c.get("sub_source"), c.get("channel_id")
        if ss and cid is not None:
            ss_to_channel.setdefault(_norm_conf_name(ss), cid)
    target_channel_id = ss_to_channel.get(_norm_conf_name(sub_source))
    if target_channel_id is None:
        return {
            "error": (
                f"sub_source '{sub_source}' is not a Shopify store in this tenant. "
                f"Available: {available_sub_sources}"
            ),
            "available_sub_sources": available_sub_sources,
        }

    # ── Resolve each SKU + confirm it's listed on the target store ────────────
    # Variation blind spot (issue #26): GLT variation listings hang off the
    # variation PARENT — the parent holds the template but usually has NO
    # channel-SKU row of its own, while the children hold the channel-SKU
    # mappings but NO templates. Both directions are handled below:
    #   - a parent SKU counts as "listed" when any of its CHILDREN is mapped
    #     on the target store;
    #   - a child SKU with no template of its own falls back to its PARENT's
    #     template (see the plan build).
    # _variation_cache memoises child->parent lookups per tool call; a confirmed
    # group also seeds every sibling, so a batch of N children in one group
    # costs ONE SearchVariationGroups sweep, not N.
    resolved: list[dict] = []
    unresolved: list[dict] = []
    _variation_cache: dict[str, dict | None] = {}

    def _parent_of_child(child_sku: str, child_sid: str) -> dict | None:
        """Child SKU -> {'parent_sku','parent_sid'} via the variation table, or None."""
        key = child_sku.strip().lower()
        if key in _variation_cache:
            return _variation_cache[key]
        rel = _resolve_variation(child_sku, child_sid)
        entry = None
        if rel.get("role") == "child" and rel.get("parent_stock_item_id"):
            entry = {
                "parent_sku": rel.get("parent_sku"),
                "parent_sid": rel.get("parent_stock_item_id"),
            }
            for sib in rel.get("siblings", []):
                if sib.get("sku"):
                    _variation_cache[sib["sku"].strip().lower()] = entry
        _variation_cache[key] = entry
        return entry

    def _rows_on_store(rows: list) -> list:
        return [
            r for r in (rows if isinstance(rows, list) else [])
            if _norm_conf_name(r.get("Source")) == _norm_conf_name(GLT_SHOPIFY_CHANNEL_NAME)
            and _norm_conf_name(r.get("SubSource")) == _norm_conf_name(sub_source)
        ]

    for raw in skus:
        sku = (raw or "").strip()
        if not sku:
            unresolved.append({"sku": raw, "error": "empty SKU"})
            continue
        try:
            item = call_linnworks("Inventory/GetInventoryItem", {"sku": sku})
        except RuntimeError as exc:
            unresolved.append({"sku": sku, "error": f"not found: {exc}"})
            continue
        sid = item.get("StockItemId")
        if not sid:
            unresolved.append({"sku": sku, "error": "found but returned no StockItemId"})
            continue
        title = item.get("ItemTitle")
        try:
            rows = call_linnworks_get(
                "Inventory/GetInventoryItemChannelSKUs", {"inventoryItemId": sid}
            )
        except RuntimeError as exc:
            unresolved.append({
                "sku": sku, "stock_item_id": sid, "title": title,
                "error": f"could not read channel listings: {exc}",
            })
            continue
        if _rows_on_store(rows):
            resolved.append({"sku": sku, "stock_item_id": sid, "title": title})
            continue

        # Not mapped itself — a variation PARENT is still revisable when its
        # children carry the store mapping (the template lives on the parent).
        rel = _resolve_variation(sku, sid)
        if rel.get("role") == "parent" and rel.get("children"):
            child_map = _fetch_channel_skus_for_ids(
                [c["stock_item_id"] for c in rel["children"] if c.get("stock_item_id")]
            )
            listed_children = [
                c["sku"] for c in rel["children"]
                if _rows_on_store(child_map.get((c.get("stock_item_id") or "").lower(), []))
            ]
            if listed_children:
                resolved.append({
                    "sku": sku, "stock_item_id": sid, "title": title,
                    "listed_via_children": listed_children,
                })
                continue

        unresolved.append({
            "sku": sku, "stock_item_id": sid, "title": title,
            "error": (
                f"not listed on Shopify store '{sub_source}' — "
                "use list_to_shopify to create it first"
            ),
        })

    # ── Open the existing GLT templates for the resolved items (read) ──────────
    templates_by_sid: dict[str, dict] = {}
    ids = [r["stock_item_id"] for r in resolved]
    for i in range(0, len(ids), 200):
        chunk = ids[i:i + 200]
        resp = call_linnworks(
            "GenericListings/OpenTemplatesByInventory",
            {"request": {
                "ChannelType": GLT_SHOPIFY_CHANNEL_TYPE,
                "ChannelName": GLT_SHOPIFY_CHANNEL_NAME,
                "Parameters": {
                    "SelectedRegions":  [],
                    "Token":            _ZERO_GUID,
                    "InventoryItemIds": chunk,
                    "ChannelId":        target_channel_id,
                },
                "PaginationParameters": {"PageNumber": 1, "EntriesPerPage": max(len(chunk), 1)},
            }},
        )
        for t in (resp.get("TemplatesInfo") if isinstance(resp, dict) else None) or []:
            tsid = t.get("StockItemId")
            if tsid:
                templates_by_sid[tsid.lower()] = t

    # ── Variation-child fallback: no own template → use the PARENT's template ──
    # A child mapped on the store but with no template of its own inherits its
    # variation parent's template (the multi-variant Shopify product is managed
    # there). Revising the parent template pushes ALL variants of the listing.
    child_fallback: dict[str, dict] = {}  # input sku (lower) -> {parent_sku, parent_sid}
    parent_sids_to_open: list[str] = []
    for r in resolved:
        if r["stock_item_id"].lower() in templates_by_sid or "listed_via_children" in r:
            continue
        entry = _parent_of_child(r["sku"], r["stock_item_id"])
        if entry and entry.get("parent_sid"):
            child_fallback[r["sku"].strip().lower()] = entry
            if entry["parent_sid"].lower() not in templates_by_sid:
                parent_sids_to_open.append(entry["parent_sid"])
    parent_sids_to_open = list(dict.fromkeys(parent_sids_to_open))
    for i in range(0, len(parent_sids_to_open), 200):
        chunk = parent_sids_to_open[i:i + 200]
        resp = call_linnworks(
            "GenericListings/OpenTemplatesByInventory",
            {"request": {
                "ChannelType": GLT_SHOPIFY_CHANNEL_TYPE,
                "ChannelName": GLT_SHOPIFY_CHANNEL_NAME,
                "Parameters": {
                    "SelectedRegions":  [],
                    "Token":            _ZERO_GUID,
                    "InventoryItemIds": chunk,
                    "ChannelId":        target_channel_id,
                },
                "PaginationParameters": {"PageNumber": 1, "EntriesPerPage": max(len(chunk), 1)},
            }},
        )
        for t in (resp.get("TemplatesInfo") if isinstance(resp, dict) else None) or []:
            tsid = t.get("StockItemId")
            if tsid:
                templates_by_sid[tsid.lower()] = t

    # ── Build the plan, deciding the push action per template ─────────────────
    # Deduped by template: several input SKUs (e.g. all children of one
    # variation group) resolving to the SAME template become ONE push, with the
    # covered inputs listed in covers_skus.
    plan: list[dict] = []
    plan_by_template: dict = {}
    for r in resolved:
        t = templates_by_sid.get(r["stock_item_id"].lower())
        via_parent = None
        if not t:
            via_parent = child_fallback.get(r["sku"].strip().lower())
            if via_parent and via_parent.get("parent_sid"):
                t = templates_by_sid.get(via_parent["parent_sid"].lower())
        if not t:
            unresolved.append({
                **r,
                "error": (
                    "listed on the channel but no GLT template could be opened "
                    "for it (checked the item and its variation parent)"
                ),
            })
            continue

        existing = plan_by_template.get(t.get("Id"))
        if existing is not None:
            existing["covers_skus"].append(r["sku"])
            continue

        if t.get("IsLocked"):
            unresolved.append({
                **r, "template_id": t.get("Id"),
                "error": "GLT template is locked — cannot revise right now",
            })
            continue

        next_action  = t.get("NextSuggestedAction")
        next_allowed = bool(t.get("IsNextSuggestedActionAllowed"))
        can_revise   = bool(t.get("IsAllowedToRevise"))

        if action is not None:
            chosen_action = action
        elif next_allowed and next_action in _GLT_REFRESH_ACTIONS:
            chosen_action = next_action
        elif can_revise:
            chosen_action = "Revise"
        else:
            unresolved.append({
                **r, "template_id": t.get("Id"), "next_suggested_action": next_action,
                "error": "GLT does not allow a revise/update on this template (no allowed push action)",
            })
            continue

        info = t.get("Info") if isinstance(t.get("Info"), dict) else {}
        row = {
            "sku":                   via_parent["parent_sku"] if via_parent else r["sku"],
            "stock_item_id":         via_parent["parent_sid"] if via_parent else r["stock_item_id"],
            "title":                 r["title"],
            "template_id":           t.get("Id"),
            "configurator_id":       t.get("ConfiguratorId"),
            "active_listing_id":     _glt_field(info, "ActiveListingId"),
            "status":                _glt_field(info, "Status"),
            "action":                chosen_action,
            "next_suggested_action": next_action,
            "is_allowed_to_revise":  can_revise,
            "covers_skus":           [r["sku"]],
        }
        if via_parent:
            row["via_variation_parent"] = True
        if "listed_via_children" in r:
            row["listed_via_children"] = r["listed_via_children"]
        plan.append(row)
        plan_by_template[t.get("Id")] = row

    base_out = {
        "item_count":            len(skus),
        "target_sub_source":     sub_source,
        "target_channel_id":     target_channel_id,
        "available_sub_sources": available_sub_sources,
        "plan":                  plan,
        "unresolved":            unresolved,
    }

    # ── Write guard (threshold 25) ────────────────────────────────────────────
    guard = _write_guard("refresh_channel_listing", skus, confirmed_count, dry_run)
    if guard is not None:
        return {**guard, **base_out}

    if dry_run:
        return {
            "dry_run": True,
            **base_out,
            "message": (
                f"Dry run — nothing pushed. {len(plan)} listing(s) on '{sub_source}' would be "
                f"revised; {len(unresolved)} SKU(s) could not be revised (see unresolved). "
                "Review the plan, then set dry_run=False to push the revisions. A live run "
                "changes real customer-facing Shopify listings."
            ),
        }

    if not plan:
        return {
            "dry_run": False,
            **base_out,
            "results": [],
            "message": "Nothing to revise — no SKU resolved to an existing, revisable Shopify template.",
        }

    # ── Live execution: ProcessTemplates per template (Revise/Update push) ─────
    results: list[dict] = []
    pushed = 0
    for row in plan:
        res = {
            "sku":         row["sku"],
            "template_id": row["template_id"],
            "action":      row["action"],
            "processed":   False,
        }
        try:
            call_linnworks(
                "GenericListings/ProcessTemplates",
                {"request": {
                    "ChannelType": GLT_SHOPIFY_CHANNEL_TYPE,
                    "ChannelName": GLT_SHOPIFY_CHANNEL_NAME,
                    "TemplateRequests": [
                        {"TemplateId": row["template_id"], "Action": row["action"]}
                    ],
                    "ClientContext": {"Activity": "refresh_channel_listing", "Source": "linnworks-mcp"},
                }},
            )
            res["processed"] = True
            pushed += 1
        except RuntimeError as exc:
            res["error"] = f"ProcessTemplates ({row['action']}) failed: {exc}"
        results.append(res)

    return {
        "dry_run": False,
        **base_out,
        "results": results,
        "message": (
            f"{pushed}/{len(plan)} Shopify listing(s) on '{sub_source}' revised and pushed. "
            "ProcessTemplates returns no body, so success is inferred from a 2xx — READ THE LIVE "
            "LISTING BACK NOW: the push sends the template's STORED field snapshot, which can be "
            "stale and overwrite current prices/content (live-proven 14 Jul 2026). Per-item "
            "errors are in results[].error."
        ),
    }


# ---------- Unpublish / take down a channel listing (write) ----------
#
# The destructive counterpart to list_to_shopify (creates) and
# refresh_channel_listing (revises). Where those keep a listing alive, this ENDS
# it — the GLT "Delete" action against the item's existing template retires the
# live Shopify listing so it stops selling. Built for the duplicate-item cleanup
# in issue #22: after a SKU-scheme migration leaves an orphaned Linnworks item
# still live-listed on Shopify at a stale quantity, this takes that listing down
# in bulk instead of doing it by hand in the Shopify admin, SKU by SKU.
#
# Same read-before-write selection path as refresh_channel_listing (resolve →
# confirm the SHOPIFY+sub_source channel-SKU mapping → OpenTemplatesByInventory
# opens the EXISTING template — never creates one), but it forces Action="Delete"
# and reads the channel-SKU table back afterwards to confirm the listing is gone.


@mcp.tool()
def unpublish_channel_listing(
    skus: list[str],
    sub_source: str = "SWH Shopify",
    confirmed_count: int | None = None,
    dry_run: bool = True,
) -> dict:
    """
    Take an EXISTING Shopify listing DOWN — unpublish/retire the storefront
    listing so it stops selling. Shopify only (v1).

    This is the destructive counterpart to `list_to_shopify` (which CREATES
    listings) and `refresh_channel_listing` (which REVISES them). It ends the
    live listing via the GLT "Delete" action against the item's existing
    template. Use it to retire an orphaned/duplicate listing — e.g. after a
    SKU-scheme migration leaves a stale Linnworks item still live on Shopify at a
    frozen quantity, silently able to oversell.

    ⚠️  DESTRUCTIVE and customer-facing. A live run removes a REAL Shopify
    listing (with its reviews, ranking and URL). Point it only at the orphan you
    mean to retire — NOT the good listing you want to keep. Re-listing later is
    possible via `list_to_shopify`, but the original listing's channel history
    (reviews / SEO) is not recoverable.

    Flow per SKU (read-before-write):
      1. Resolve SKU → StockItemId + title.
      2. Confirm the item has a SHOPIFY channel-SKU mapping on `sub_source`
         (the channel-SKU link table — see get_channel_listings), and capture
         its current channel_reference_id + listed_quantity for the manifest.
         Not listed on that store → unresolved.
      3. GenericListings/OpenTemplatesByInventory → open the item's EXISTING GLT
         template for that store (OPENS the existing template — it does NOT
         create one). Locked templates → unresolved.
      4. (live run) GenericListings/ProcessTemplates with Action="Delete" →
         ends the live Shopify listing.
      5. (live run) Re-read the channel-SKU table to confirm the SHOPIFY row on
         `sub_source` is gone / no longer listed (`taken_down` per SKU).

    Live safety: the read/selection path (channel check + OpenTemplatesByInventory)
    is live-confirmed; the ProcessTemplates Delete push is built to the OpenAPI
    spec but NOT yet live-exercised in this tenant — it retires a real
    customer-facing listing, so start with a single SKU on a throwaway/orphan
    item you are certain about.

    Staging: the threshold is 10 (the tightest tier, shared with
    delete_inventory_item). For batches > 10 SKUs this returns the plan + manifest
    and asks you to confirm with confirmed_count=<N> before executing.

    Args:
        skus: Exact SKUs / ItemNumbers whose Shopify listings to take down.
        sub_source: Shopify store name (default "SWH Shopify"). Scopes both the
            "is it listed?" check and which store's listing is deleted.
        confirmed_count: For batches > 10 SKUs, pass len(skus) after reviewing the
            plan to confirm the write.
        dry_run: If True (default), returns the plan without taking anything down.
            Set to False to delete the listings on Shopify.

    Returns:
        A dict with:
          - dry_run, item_count, target_sub_source, target_channel_id,
            available_sub_sources
          - plan: per-SKU rows that would be taken down (sku, stock_item_id,
            title, template_id, configurator_id, active_listing_id, status,
            channel_reference_id, listed_quantity, action)
          - unresolved: per-SKU error rows (not found / not listed on the store /
            no template / locked)
          - results: per-SKU outcome (live run only — processed, taken_down,
            still_listed, error)
    """
    if not skus:
        raise ValueError("skus must contain at least one SKU.")

    _check_injection("sub_source", sub_source or "")

    # ── Resolve target store ChannelId from the configurator catalogue ────────
    catalogue = _fetch_shopify_configurators()
    available_sub_sources = sorted({c["sub_source"] for c in catalogue if c.get("sub_source")})
    ss_to_channel: dict[str, int] = {}
    for c in catalogue:
        ss, cid = c.get("sub_source"), c.get("channel_id")
        if ss and cid is not None:
            ss_to_channel.setdefault(_norm_conf_name(ss), cid)
    target_channel_id = ss_to_channel.get(_norm_conf_name(sub_source))
    if target_channel_id is None:
        return {
            "error": (
                f"sub_source '{sub_source}' is not a Shopify store in this tenant. "
                f"Available: {available_sub_sources}"
            ),
            "available_sub_sources": available_sub_sources,
        }

    # ── Resolve each SKU + confirm it's listed on the target store ────────────
    resolved: list[dict] = []
    unresolved: list[dict] = []
    for raw in skus:
        sku = (raw or "").strip()
        if not sku:
            unresolved.append({"sku": raw, "error": "empty SKU"})
            continue
        try:
            item = call_linnworks("Inventory/GetInventoryItem", {"sku": sku})
        except RuntimeError as exc:
            unresolved.append({"sku": sku, "error": f"not found: {exc}"})
            continue
        sid = item.get("StockItemId")
        if not sid:
            unresolved.append({"sku": sku, "error": "found but returned no StockItemId"})
            continue
        title = item.get("ItemTitle")
        try:
            rows = call_linnworks_get(
                "Inventory/GetInventoryItemChannelSKUs", {"inventoryItemId": sid}
            )
        except RuntimeError as exc:
            unresolved.append({
                "sku": sku, "stock_item_id": sid, "title": title,
                "error": f"could not read channel listings: {exc}",
            })
            continue
        on_store = [
            r for r in (rows if isinstance(rows, list) else [])
            if _norm_conf_name(r.get("Source")) == _norm_conf_name(GLT_SHOPIFY_CHANNEL_NAME)
            and _norm_conf_name(r.get("SubSource")) == _norm_conf_name(sub_source)
        ]
        if not on_store:
            unresolved.append({
                "sku": sku, "stock_item_id": sid, "title": title,
                "error": f"not listed on Shopify store '{sub_source}' — nothing to take down",
            })
            continue
        # Capture the current listing identity for the manifest / confirmation.
        row0 = on_store[0]
        resolved.append({
            "sku": sku, "stock_item_id": sid, "title": title,
            "channel_reference_id": row0.get("ChannelReferenceId"),
            "listed_quantity":      row0.get("ListedQuantity"),
        })

    # ── Open the existing GLT templates for the resolved items (read) ──────────
    templates_by_sid: dict[str, dict] = {}
    ids = [r["stock_item_id"] for r in resolved]
    for i in range(0, len(ids), 200):
        chunk = ids[i:i + 200]
        resp = call_linnworks(
            "GenericListings/OpenTemplatesByInventory",
            {"request": {
                "ChannelType": GLT_SHOPIFY_CHANNEL_TYPE,
                "ChannelName": GLT_SHOPIFY_CHANNEL_NAME,
                "Parameters": {
                    "SelectedRegions":  [],
                    "Token":            _ZERO_GUID,
                    "InventoryItemIds": chunk,
                    "ChannelId":        target_channel_id,
                },
                "PaginationParameters": {"PageNumber": 1, "EntriesPerPage": max(len(chunk), 1)},
            }},
        )
        for t in (resp.get("TemplatesInfo") if isinstance(resp, dict) else None) or []:
            tsid = t.get("StockItemId")
            if tsid:
                templates_by_sid[tsid.lower()] = t

    # ── Build the take-down plan ───────────────────────────────────────────────
    plan: list[dict] = []
    for r in resolved:
        t = templates_by_sid.get(r["stock_item_id"].lower())
        if not t:
            unresolved.append({
                **r,
                "error": "listed on the channel but no GLT template could be opened for it",
            })
            continue
        if t.get("IsLocked"):
            unresolved.append({
                **r, "template_id": t.get("Id"),
                "error": "GLT template is locked — cannot take it down right now",
            })
            continue

        info = t.get("Info") if isinstance(t.get("Info"), dict) else {}
        plan.append({
            "sku":                  r["sku"],
            "stock_item_id":        r["stock_item_id"],
            "title":                r["title"],
            "template_id":          t.get("Id"),
            "configurator_id":      t.get("ConfiguratorId"),
            "active_listing_id":    _glt_field(info, "ActiveListingId"),
            "status":               _glt_field(info, "Status"),
            "channel_reference_id": r["channel_reference_id"],
            "listed_quantity":      r["listed_quantity"],
            "action":               "Delete",
        })

    base_out = {
        "item_count":            len(skus),
        "target_sub_source":     sub_source,
        "target_channel_id":     target_channel_id,
        "available_sub_sources": available_sub_sources,
        "plan":                  plan,
        "unresolved":            unresolved,
    }

    # ── Write guard (threshold 10) ─────────────────────────────────────────────
    guard = _write_guard("unpublish_channel_listing", skus, confirmed_count, dry_run)
    if guard is not None:
        return {**guard, **base_out}

    if dry_run:
        return {
            "dry_run": True,
            **base_out,
            "message": (
                f"Dry run — nothing taken down. {len(plan)} listing(s) on '{sub_source}' would be "
                f"DELETED (unpublished from Shopify); {len(unresolved)} SKU(s) could not be taken "
                "down (see unresolved). Review the plan — confirm each channel_reference_id / "
                "listed_quantity is the orphan you mean to retire, NOT a listing you want to keep — "
                "then set dry_run=False. A live run removes real customer-facing Shopify listings."
            ),
        }

    if not plan:
        return {
            "dry_run": False,
            **base_out,
            "results": [],
            "message": "Nothing to take down — no SKU resolved to an existing Shopify template.",
        }

    # ── Live execution: ProcessTemplates Delete, then read-back per item ───────
    results: list[dict] = []
    taken = 0
    for row in plan:
        res = {
            "sku":         row["sku"],
            "template_id": row["template_id"],
            "action":      "Delete",
            "processed":   False,
            "taken_down":  None,
        }
        try:
            call_linnworks(
                "GenericListings/ProcessTemplates",
                {"request": {
                    "ChannelType": GLT_SHOPIFY_CHANNEL_TYPE,
                    "ChannelName": GLT_SHOPIFY_CHANNEL_NAME,
                    "TemplateRequests": [
                        {"TemplateId": row["template_id"], "Action": "Delete"}
                    ],
                    "ClientContext": {"Activity": "unpublish_channel_listing", "Source": "linnworks-mcp"},
                }},
            )
            res["processed"] = True
        except RuntimeError as exc:
            res["error"] = f"ProcessTemplates (Delete) failed: {exc}"
            results.append(res)
            continue

        # Read-back: is the SHOPIFY row on this store gone / no longer listed?
        try:
            rows = call_linnworks_get(
                "Inventory/GetInventoryItemChannelSKUs",
                {"inventoryItemId": row["stock_item_id"]},
            )
            still = [
                r for r in (rows if isinstance(rows, list) else [])
                if _norm_conf_name(r.get("Source")) == _norm_conf_name(GLT_SHOPIFY_CHANNEL_NAME)
                and _norm_conf_name(r.get("SubSource")) == _norm_conf_name(sub_source)
            ]
            res["still_listed"] = len(still) > 0
            res["taken_down"] = len(still) == 0
            if still:
                taken += 0  # left up; sync may lag — surface but don't count as done
            else:
                taken += 1
        except RuntimeError as exc:
            res["readback_error"] = f"could not confirm take-down: {exc}"
        results.append(res)

    return {
        "dry_run": False,
        **base_out,
        "results": results,
        "message": (
            f"{taken}/{len(plan)} Shopify listing(s) on '{sub_source}' confirmed taken down. "
            "ProcessTemplates returns no body, so success is inferred from a 2xx plus a channel-SKU "
            "read-back; the channel may lag, so a row that is still_listed=true may simply not have "
            "synced yet — re-check with get_channel_listings, and verify in your Shopify admin. "
            "Per-item errors are in results[].error."
        ),
    }


@mcp.tool()
def delist_all_shopify_listings(
    skus: list[str],
    confirmed_count: int | None = None,
    dry_run: bool = True,
) -> dict:
    """
    Take down EVERY Shopify listing for each given item, across ALL Shopify
    stores it is listed on — the convenience wrapper over unpublish_channel_listing
    (which handles one store at a time).

    Built for archived-item cleanup: after you UNARCHIVE items in the Linnworks UI
    (they must be ACTIVE — an archived SKU cannot be resolved, so this tool can't
    see it), run this to retire all their Shopify storefront listings in one pass,
    then re-archive with archive_inventory_items.

    Per SKU it reads the channel-SKU link table, finds every distinct SHOPIFY
    store the item is listed on, and delegates each (SKU, store) to the proven
    unpublish_channel_listing (GLT ProcessTemplates Delete). It does NOT touch the
    base item, stock, or non-Shopify channels.

    ⚠️  SHOPIFY ONLY. Any listing on a non-Shopify channel (Mirakl, eBay, Amazon,
    Magento…) is reported under `skipped_channels` and left UP — the GLT delete
    path cannot retire it. So "all listings gone" is only true for Shopify stores.

    ⚠️  DESTRUCTIVE and customer-facing — removes real Shopify listings (reviews,
    ranking, URL not recoverable). dry_run=True by default; staging threshold 10
    on the number of (SKU × store) take-downs.

    Args:
        skus: Exact SKUs / ItemNumbers (must be ACTIVE / resolvable) whose Shopify
            listings should all be taken down.
        confirmed_count: For > 10 planned take-downs, pass the take_down_count from
            the dry-run manifest to confirm.
        dry_run: If True (default), preview only. Set False to execute.

    Returns:
        dict with per-SKU discovery (shopify_stores, skipped_channels), a combined
        `plan` of (sku, store) take-downs, `unresolved`, and — on a live run —
        per-(sku, store) `results` (processed / taken_down / still_listed / error).
    """
    if not skus:
        raise ValueError("skus must contain at least one SKU.")

    catalogue = _fetch_shopify_configurators()
    shopify_stores = {_norm_conf_name(c["sub_source"]) for c in catalogue if c.get("sub_source")}

    # ── Discover, per SKU, which Shopify stores (and which non-Shopify channels) ─
    discovery: list[dict] = []
    unresolved: list[dict] = []
    work: list[tuple[str, str]] = []   # (sku, actual store sub_source)
    for raw in skus:
        sku = (raw or "").strip()
        if not sku:
            unresolved.append({"sku": raw, "error": "empty SKU"})
            continue
        try:
            item = call_linnworks("Inventory/GetInventoryItem", {"sku": sku})
        except RuntimeError as exc:
            unresolved.append({
                "sku": sku,
                "error": (
                    f"not found / not resolvable: {exc}. If this item is ARCHIVED, "
                    "unarchive it in Linnworks first — archived SKUs cannot be resolved."
                ),
            })
            continue
        sid = item.get("StockItemId")
        if not sid:
            unresolved.append({"sku": sku, "error": "found but returned no StockItemId"})
            continue
        try:
            rows = call_linnworks_get(
                "Inventory/GetInventoryItemChannelSKUs", {"inventoryItemId": sid}
            )
        except RuntimeError as exc:
            unresolved.append({"sku": sku, "stock_item_id": sid,
                               "error": f"could not read channel listings: {exc}"})
            continue
        rows = rows if isinstance(rows, list) else []
        stores_here: list[str] = []
        skipped: list[dict] = []
        seen_stores: set[str] = set()
        for r in rows:
            src, ss = r.get("Source"), r.get("SubSource")
            if _norm_conf_name(src) == _norm_conf_name(GLT_SHOPIFY_CHANNEL_NAME):
                key = _norm_conf_name(ss)
                if key in shopify_stores and key not in seen_stores:
                    seen_stores.add(key)
                    stores_here.append(ss)
                    work.append((sku, ss))
            else:
                skipped.append({"source": src, "sub_source": ss,
                                "channel_reference_id": r.get("ChannelReferenceId")})
        discovery.append({
            "sku": sku, "stock_item_id": sid, "title": item.get("ItemTitle"),
            "shopify_stores": stores_here, "skipped_channels": skipped,
        })

    # ── Build the combined plan by dry-running the proven tool per (sku, store) ──
    plan: list[dict] = []
    for sku, store in work:
        sub = unpublish_channel_listing(skus=[sku], sub_source=store, dry_run=True)
        for p in sub.get("plan", []):
            plan.append({**p, "sub_source": store})
        for u in sub.get("unresolved", []):
            unresolved.append({**u, "sub_source": store})

    base_out = {
        "item_count":       len(skus),
        "shopify_stores_in_tenant": sorted(shopify_stores),
        "discovery":        discovery,
        "plan":             plan,
        "unresolved":       unresolved,
        "skipped_channels": [
            {"sku": d["sku"], **s} for d in discovery for s in d["skipped_channels"]
        ],
    }

    # ── Write guard (threshold 10) on the number of take-downs ─────────────────
    guard = _write_guard("delist_all_shopify_listings", plan, confirmed_count, dry_run)
    if guard is not None:
        return {**guard, **base_out}

    if dry_run:
        return {
            "dry_run": True,
            **base_out,
            "take_down_count": len(plan),
            "message": (
                f"Dry run — nothing taken down. {len(plan)} Shopify listing(s) across "
                f"{len({(p['sku'], p['sub_source']) for p in plan})} (item×store) would be DELETED; "
                f"{len(base_out['skipped_channels'])} non-Shopify listing(s) can't be taken down "
                "(see skipped_channels) and will stay up. Review the plan, then set dry_run=False."
            ),
        }

    # ── Live execution: delegate each (sku, store) to the proven tool ──────────
    results: list[dict] = []
    for sku, store in work:
        sub = unpublish_channel_listing(skus=[sku], sub_source=store, dry_run=False)
        for r in sub.get("results", []):
            results.append({**r, "sub_source": store})
    taken = sum(1 for r in results if r.get("taken_down"))
    return {
        "dry_run": False,
        **base_out,
        "results": results,
        "message": (
            f"{taken}/{len(results)} Shopify listing(s) confirmed taken down across all stores. "
            f"{len(base_out['skipped_channels'])} non-Shopify listing(s) left up (see "
            "skipped_channels). Channel sync may lag — re-check with get_channel_listings."
        ),
    }


@mcp.tool()
def archive_inventory_items(
    skus: list[str],
    confirmed_count: int | None = None,
    dry_run: bool = True,
) -> dict:
    """
    ARCHIVE inventory items (the Linnworks "archive" action) by SKU.

    Archiving hides an item from the active catalogue. It is REVERSIBLE via
    unarchive_inventory_items (or the Linnworks UI). Typically the final step of
    archived-item cleanup: after you unarchive items to delist them
    (delist_all_shopify_listings), re-archive them here.

    Items must be ACTIVE to resolve by SKU (an already-archived SKU won't resolve
    → reported unresolved). Read-before-write resolves each SKU → StockItemId and
    captures the title; read-back confirms the SKU no longer resolves (i.e. it is
    archived). Uses Inventory/ArchiveInventoryItems (one batched call).

    ⚠️  Archiving an item can affect its channel behaviour (a stale listing may be
    left frozen — see the archived-item listing gotcha). Delist first, then archive.

    Args:
        skus: Exact SKUs / ItemNumbers to archive (must be ACTIVE).
        confirmed_count: For > 25 items, pass len(skus) after reviewing the manifest.
        dry_run: If True (default), preview only. Set False to archive.

    Returns:
        dict with manifest (per-SKU sku, stock_item_id, title, status) plus
        unresolved rows, and — on a live run — per-SKU `archived` read-back.
    """
    return _set_archive_state(skus, archive=True,
                              confirmed_count=confirmed_count, dry_run=dry_run)


@mcp.tool()
def unarchive_inventory_items(
    stock_item_ids: list[str],
    confirmed_count: int | None = None,
    dry_run: bool = True,
) -> dict:
    """
    UNARCHIVE (restore to active) inventory items by StockItemId GUID.

    ⚠️  Takes StockItemId GUIDs, NOT SKUs — an archived item CANNOT be resolved
    from its SKU (GetInventoryItem fails, and no SKU→id resolver sees archived
    items), so the only handle is the GUID. Source the GUIDs from a Linnworks
    export of the Archived view that includes the "Stock Item ID" column. There is
    no API to enumerate archived items.

    Restoring is REVERSIBLE via archive_inventory_items. Once unarchived, an item
    becomes active and resolves by SKU again — so you can then delist it
    (delist_all_shopify_listings) and re-archive it. Uses
    Inventory/UnarchiveInventoryItems (one batched call). Read-back confirms each
    id now resolves as an active item.

    Args:
        stock_item_ids: StockItemId GUIDs to unarchive.
        confirmed_count: For > 25 items, pass len(stock_item_ids) after review.
        dry_run: If True (default), preview only. Set False to unarchive.

    Returns:
        dict with manifest (per-id stock_item_id) plus, on a live run, per-id
        `active` read-back.
    """
    return _set_archive_state(stock_item_ids, archive=False,
                              confirmed_count=confirmed_count, dry_run=dry_run)


def _set_archive_state(
    items: list[str],
    archive: bool,
    confirmed_count: int | None,
    dry_run: bool,
) -> dict:
    """
    Shared archive/unarchive worker. archive=True takes SKUs (resolves → GUID);
    archive=False takes StockItemId GUIDs directly (archived SKUs can't resolve).
    """
    op = "archive_inventory_items" if archive else "unarchive_inventory_items"
    endpoint = ("Inventory/ArchiveInventoryItems" if archive
                else "Inventory/UnarchiveInventoryItems")
    if not items:
        raise ValueError("The list must contain at least one item.")

    # ── Resolve to StockItemId GUIDs + build manifest ─────────────────────────
    manifest: list[dict] = []
    unresolved: list[dict] = []
    ids: list[str] = []
    cache: dict = {}
    for raw in items:
        val = (raw or "").strip()
        if not val:
            unresolved.append({"input": raw, "error": "empty value"})
            continue
        if archive:
            # SKU → GUID (item must be active to resolve)
            try:
                item = call_linnworks("Inventory/GetInventoryItem", {"sku": val})
            except RuntimeError as exc:
                unresolved.append({"sku": val, "error": (
                    f"not resolvable: {exc}. Already archived items cannot be "
                    "resolved by SKU.")})
                continue
            sid = item.get("StockItemId")
            if not sid:
                unresolved.append({"sku": val, "error": "no StockItemId returned"})
                continue
            manifest.append({"sku": val, "stock_item_id": sid,
                             "title": item.get("ItemTitle")})
            ids.append(sid)
        else:
            # GUID passed directly
            manifest.append({"stock_item_id": val})
            ids.append(val)

    base_out = {"item_count": len(items), "manifest": manifest, "unresolved": unresolved}

    guard = _write_guard(op, items, confirmed_count, dry_run)
    if guard is not None:
        return {**guard, **base_out}

    if dry_run:
        verb = "archived" if archive else "unarchived"
        return {
            "dry_run": True, **base_out,
            "message": (
                f"Dry run — nothing changed. {len(ids)} item(s) would be {verb}; "
                f"{len(unresolved)} unresolved. Set dry_run=False to execute."
            ),
        }

    if not ids:
        return {"dry_run": False, **base_out,
                "message": "Nothing to do — no items resolved."}

    # ── Execute (one batched call) ─────────────────────────────────────────────
    call_linnworks(endpoint, {"parameters": {
        "InventoryItemIds": ids, "SelectedRegions": [], "Token": _ZERO_GUID,
    }})

    # ── Read-back: archived items stop resolving by SKU; unarchived start ──────
    results: list[dict] = []
    for m in manifest:
        row = dict(m)
        if archive and m.get("sku"):
            try:
                call_linnworks("Inventory/GetInventoryItem", {"sku": m["sku"]})
                row["archived"] = False   # still resolves → not archived
            except RuntimeError:
                row["archived"] = True    # no longer resolves → archived
        else:
            try:
                gi = call_linnworks("Inventory/GetInventoryItem",
                                    {"stockItemId": m["stock_item_id"]})
                row["active"] = bool(gi.get("StockItemId"))
            except RuntimeError:
                row["active"] = None      # couldn't confirm via GUID lookup
        results.append(row)

    verb = "archived" if archive else "unarchived"
    confirmed = sum(1 for r in results if (r.get("archived") if archive else r.get("active")))
    return {
        "dry_run": False, **base_out, "results": results,
        "message": (
            f"{verb.capitalize()} {len(ids)} item(s); {confirmed} confirmed by read-back. "
            + ("Archived SKUs no longer resolve." if archive
               else "Unarchived items now resolve as active.")
        ),
    }


# ---------- Entrypoint ----------

def main() -> None:
    # Offline smoke test — list every registered MCP tool WITHOUT credentials:
    #     python server.py --list-tools
    # Use this in the build loop: it confirms the module imports cleanly and the
    # new tool actually registered (catches decorator typos, duplicate names, and
    # import-time errors that `py_compile` alone misses). No network call.
    if "--list-tools" in sys.argv:
        import asyncio

        tools = asyncio.run(mcp.list_tools())
        for t in sorted(tools, key=lambda x: x.name):
            print(t.name)
        print(f"\n{len(tools)} tools registered")
        sys.exit(0)

    # Sanity-check credentials without launching the MCP server:
    #     python server.py --check-auth
    if "--check-auth" in sys.argv:
        _require_credentials()
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
    _require_credentials()
    mcp.run()


if __name__ == "__main__":
    main()
