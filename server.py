"""
Linnworks MCP Server — Phase 1 (local stdio)

A single-tenant MCP server that exposes Linnworks data to Claude Desktop.
Phase 1 = stdio transport, your machine only, no OAuth, no hosting.

Run via Claude Desktop after registering this script in claude_desktop_config.json.
See README.md for setup instructions.
"""
from __future__ import annotations

# Keep in sync with pyproject.toml [project] version on every release.
__version__ = "1.48.2"

import json
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

# Payment/order-status enum used by Orders/ChangeStatus. This is the nStatus
# enum documented in the ChangeStatus endpoint description (orders.json) and is
# DISTINCT from the GetOrdersLowFidelity display codes in _ORDER_STATUS_LABELS
# above — do not conflate the two. GetOrdersById's GeneralInfo.Status uses THIS
# enum (a live PAID order reads back as 1), so it is what set_order_status reads
# and writes for the paid/unpaid actions.
_PAYMENT_STATUS_LABELS: dict[int, str] = {
    0: "UNPAID",
    1: "PAID",
    2: "RETURN",
    3: "PENDING",
    4: "RESEND",
}

# Order-status actions supported by set_order_status → (endpoint, value).
#   lock/unlock  → Orders/LockOrder   (lockOrder boolean)
#   paid/unpaid  → Orders/ChangeStatus (status int, per _PAYMENT_STATUS_LABELS)
_ORDER_STATUS_ACTIONS: dict[str, tuple[str, object]] = {
    "lock":   ("LockOrder", True),
    "unlock": ("LockOrder", False),
    "paid":   ("ChangeStatus", 1),
    "unpaid": ("ChangeStatus", 0),
}

# Actions users may ask for that the public Linnworks API does NOT expose.
# Park/unpark exist only as a RulesEngine action (ChangeOrderParkStatus) with no
# standalone endpoint — GeneralInfo.IsParked is READ-only via the API.
_UNSUPPORTED_STATUS_ACTIONS: set[str] = {"park", "unpark"}

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


# ---------- Rate limiting (issue #34) ----------
#
# Linnworks enforces per-endpoint per-minute quotas (150/min on most Inventory
# reads, 250/min on Stock/GetItemChangesHistory, …). Exceeding one returns
# HTTP 429 with a body like:
#
#   {"Message":"API calls quota exceeded! Maximum admitted 150 per Minute."}
#
# That is a TRANSIENT infrastructure failure, not a statement about the data.
# Before v1.37.0 nothing here retried it, and callers that wrapped errors into
# a per-item "not found" bucket laundered it into a factual claim about the
# catalogue — `get_channel_listings_bulk` reported 4,804 SKUs as "not found"
# when it had simply burned the quota, under-reporting live listings by 88%
# and, in a delist run, silently skipping 92 of 154 items while reporting
# success. Because the failure pointed at "this SKU doesn't exist", it pointed
# toward the destructive answer.
#
# Retrying here fixes every endpoint at once rather than one caller at a time.
# The quota is per MINUTE, so the backoff is sized to outlast a full window.

_RATE_LIMIT_BACKOFF = (5, 10, 20, 30)   # seconds; ~65s total, > one quota window


class RateLimitError(Exception):
    """
    Raised when a Linnworks call is still rate-limited after the full backoff.

    ⚠️  Deliberately does NOT subclass RuntimeError (changed in v1.40.0, issue #37).

    v1.37.0 made it a RuntimeError subclass so that "existing `except RuntimeError`
    handlers keep working". They did keep working — they kept working WRONGLY.
    ~40 handlers across this file bucket a RuntimeError as a per-item DATA failure
    ("SKU not found", `unresolved`), so a transient quota error was still being
    reported as "this SKU does not exist" — the exact defect #34 was meant to kill,
    and in the direction that points at the destructive answer. Live: 116 of 266
    SKUs in a listing run, and 53 of 191 in an ageing run, all of which resolved
    fine on retry.

    Inheriting from Exception makes that structurally impossible: a bare
    `except RuntimeError` cannot swallow it, so it propagates loudly instead of
    being mislabelled. Tools that should degrade gracefully catch it EXPLICITLY
    and report a `rate_limited` bucket with `complete: false`.

    It means "ask again later". It never means "no such record".
    """


def _is_rate_limited(response) -> bool:
    """True if a response is a Linnworks quota rejection."""
    if response.status_code == 429:
        return True
    # Belt and braces: the quota message has been observed on non-429 statuses.
    return "quota exceeded" in (response.text or "").lower()


def _rate_limit_pause(method_path: str, response, attempt: int) -> bool:
    """
    Sleep out a 429 before the next attempt. Returns False when retries are spent.

    Honours Retry-After when Linnworks sends it; otherwise uses the fixed
    backoff ladder, which is sized to outlast a one-minute quota window.
    """
    if attempt >= len(_RATE_LIMIT_BACKOFF):
        return False
    delay = _RATE_LIMIT_BACKOFF[attempt]
    retry_after = (response.headers or {}).get("Retry-After")
    if retry_after:
        try:
            delay = max(delay, int(float(retry_after)))
        except (TypeError, ValueError):
            pass
    time.sleep(delay)
    return True


def call_linnworks(method_path: str, payload: dict) -> dict:
    """
    POST a Linnworks API call with one automatic re-auth on token expiry and
    bounded retry/backoff on HTTP 429 (issue #34).
    """
    global _token, _server
    attempt = 0
    while True:
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

        if _is_rate_limited(response):
            if _rate_limit_pause(method_path, response, attempt):
                attempt += 1
                continue
            raise RateLimitError(
                f"Linnworks {method_path} rate-limited: HTTP {response.status_code} — "
                f"{response.text} (retried {len(_RATE_LIMIT_BACKOFF)}x). This is a quota "
                "failure, NOT a missing record — retry later or slow the batch down."
            )
        break

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
    purely via HTTP 204. Raises RuntimeError on any non-2xx response, and
    RateLimitError if still throttled after the full backoff (issue #34).
    """
    global _token, _server
    attempt = 0
    while True:
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

        if _is_rate_limited(response):
            if _rate_limit_pause(method_path, response, attempt):
                attempt += 1
                continue
            raise RateLimitError(
                f"Linnworks {method_path} rate-limited: HTTP {response.status_code} — "
                f"{response.text} (retried {len(_RATE_LIMIT_BACKOFF)}x)."
            )
        break

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

    Retries HTTP 429 with backoff and raises RateLimitError if still throttled
    after the full ladder (issue #34).
    """
    global _token, _server
    attempt = 0
    while True:
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

        if _is_rate_limited(response):
            if _rate_limit_pause(method_path, response, attempt):
                attempt += 1
                continue
            raise RateLimitError(
                f"Linnworks {method_path} rate-limited: HTTP {response.status_code} — "
                f"{response.text} (retried {len(_RATE_LIMIT_BACKOFF)}x)."
            )
        break

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
    "repair_channel_listing_images":  10,   # WRITES to live customer-facing product pages (Shopify Admin)
    "delist_all_shopify_listings":    10,   # TAKES DOWN every Shopify listing for an item — destructive
    "delist_all_channel_listings":    10,   # TAKES DOWN every GLT listing (all channels) — destructive
    "revise_ebay_listing_description": 10,  # WRITES to a live eBay listing; push is not yet live-proven
    "delete_categories":              10,   # IRREVERSIBLE — deletes categories (non-empty → items reassigned)
    "delete_empty_categories":        10,   # IRREVERSIBLE — bulk-deletes empty categories
    "archive_inventory_items":        25,   # hides items from channels; reversible via unarchive
    "unarchive_inventory_items":      25,   # restores items to active; reversible via archive
    "set_order_status":               25,   # lock/unlock/paid/unpaid — reversible order-state changes
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


# ---------------------------------------------------------------------------
# Bulk active-inventory enumeration (Stock/GetStockItemsFull)
# ---------------------------------------------------------------------------
#
# The "give me EVERYTHING" counterpart to search_inventory_items' keyword search,
# built for catalogue-cleanup sweeps: find every active item sitting at zero across
# ALL locations, then hand those to the delist/archive tools.
#
# Four behaviours of Stock/GetStockItemsFull were live-probed for this (5 Aug 2026,
# issue #32) and every one of them changes what the tool can promise:
#
#   • loadCompositeParents / loadVariationParents are INCLUDE flags, NOT
#     "load extra detail" flags. With both false — the payload previously recorded
#     in CLAUDE.md as the "full active-item enumerator" — composite and variation
#     parents are silently ABSENT from the results. Live: the vnm-catnip family
#     returns 8 items with both true and 1 with both false. So that documented
#     sweep was never the full catalogue. Both default to True here.
#   • dataRequirements is a STRING enum on this endpoint
#     ("StockLevels"/"Pricing"/"Supplier"/…), not the integer [1] that
#     GetStockItemsFullByIds wants for Suppliers[]. "StockLevels" populates a
#     PER-LOCATION StockLevels array — which is what makes "out of stock
#     EVERYWHERE" answerable in one sweep rather than one sweep per location.
#   • There is NO TotalEntries/TotalPages and NO top-level Quantity on this model.
#     Paging past the end returns HTTP 400 "No items found with given filter" —
#     an end-of-results signal, not a failure — and quantities must be derived by
#     summing the per-location rows. Cross-checked live against the GET endpoint:
#     Default 1 + Keen 6 == GET Quantity 7; InOrders == GET InOrder.
#   • There is NO IsCompositeParent field on this model (only IsVariationParent).
#     That flag lives on the Stock/GetStockItems GET rows instead, so it is filled
#     here only on request, from the cached composite index (see
#     flag_composite_parents below).
#
# ⚠️  ACTIVE ITEMS ONLY — like every other list/search endpoint, this never returns
# archived items (~32k active vs ~93k including archived on this tenant). "Every
# stock item" means every ACTIVE one.

_STOCK_LEVEL_SUM_FIELDS = {
    "quantity": "StockLevel",
    "available": "Available",
    "in_order":  "InOrders",
    "due":       "Due",
}


def _fetch_full_stock_page(
    page: int,
    per_page: int,
    include_composite_parents: bool,
    include_variation_parents: bool,
    data_requirements: list[str],
) -> list | None:
    """
    Fetch one page of Stock/GetStockItemsFull, with a 429 backoff.

    Returns the page's rows, or None when the page is past the end of the
    catalogue — Linnworks signals that with HTTP 400 "No items found with given
    filter" rather than an empty list, so auto-paging terminates on None instead
    of surfacing a spurious error.
    """
    payload = {
        "keyword": "",
        "loadCompositeParents": include_composite_parents,
        "loadVariationParents": include_variation_parents,
        "entriesPerPage": per_page,
        "pageNumber": page,
        "dataRequirements": data_requirements,
        "searchTypes": [],
    }
    for _ in range(6):
        try:
            resp = call_linnworks("Stock/GetStockItemsFull", payload)
            return resp if isinstance(resp, list) else []
        except RuntimeError as exc:
            msg = str(exc)
            if "No items found with given filter" in msg:
                return None  # past the last page — a clean end, not a failure
            if "429" in msg or "quota" in msg.lower():
                time.sleep(15)
                continue
            raise
    raise RuntimeError(
        f"Inventory sweep aborted: repeated rate-limit (429) on page {page}. "
        "No partial result is returned — a truncated sweep would under-report "
        "items and could wrongly clear stock as dead."
    )


def _format_full_stock_row(
    row: dict,
    location_id: str | None,
    stock_levels_loaded: bool,
) -> dict:
    """
    Map a Stock/GetStockItemsFull row to the MCP-facing shape.

    Quantities are DERIVED from the per-location StockLevels array (this model has
    no top-level Quantity): summed across every location, or taken from just one
    when location_id scopes the view.

    stock_levels_loaded must be passed rather than inferred from the row — the
    endpoint returns "StockLevels": [] BOTH when levels weren't requested and when
    an item genuinely has no stock rows, and those must not read the same. Levels
    unread give None ("not read"); levels read but empty give 0 (really no stock).
    Conflating them would let an unread figure pass a zero-stock cleanup filter.
    """
    levels = row.get("StockLevels") or []
    if location_id:
        wanted = location_id.strip().lower()
        levels = [
            l for l in levels
            if ((l.get("Location") or {}).get("StockLocationId") or "").lower() == wanted
        ]

    if not stock_levels_loaded:
        levels = []
        totals = {k: None for k in _STOCK_LEVEL_SUM_FIELDS}
    else:
        totals = {
            key: sum((l.get(field) or 0) for l in levels)
            for key, field in _STOCK_LEVEL_SUM_FIELDS.items()
        }

    return {
        "sku": row.get("ItemNumber"),
        "stock_item_id": row.get("StockItemId"),
        "title": row.get("ItemTitle"),
        "barcode": row.get("BarcodeNumber"),
        "category_name": row.get("CategoryName"),
        "category_id": row.get("CategoryId"),
        "purchase_price": row.get("PurchasePrice"),
        "quantity": totals["quantity"],
        "available": totals["available"],
        "in_order": totals["in_order"],
        "due": totals["due"],
        "is_variation_parent": row.get("IsVariationParent", False),
        # Not on this model — filled only when flag_composite_parents=True.
        "is_composite_parent": None,
        "locations": [
            {
                "location_id": (l.get("Location") or {}).get("StockLocationId"),
                "location_name": (l.get("Location") or {}).get("LocationName"),
                "stock_level": l.get("StockLevel"),
                "available": l.get("Available"),
                "in_order": l.get("InOrders"),
                "due": l.get("Due"),
                "minimum_level": l.get("MinimumLevel"),
            }
            for l in levels
        ],
    }


@mcp.tool()
def list_inventory_items(
    page: int = 1,
    per_page: int = 200,
    all_pages: bool = False,
    include_stock_levels: bool = True,
    location_id: str | None = None,
    zero_stock_only: bool = False,
    include_composite_parents: bool = True,
    include_variation_parents: bool = True,
    flag_composite_parents: bool = False,
    max_items: int = 5000,
) -> dict:
    """
    Enumerate ACTIVE inventory items with their stock levels — the bulk
    "give me everything" counterpart to search_inventory_items (keyword) and
    find_inventory_item (exact SKU).

    Built for the catalogue-cleanup sweep: "give me every active stock item with
    its current quantity, so I can find everything that's out of stock across all
    locations", then feed those SKUs to find_composite_parents (the archive gate),
    delist_all_channel_listings, and archive_inventory_items.

    PER-LOCATION STOCK is the point. With include_stock_levels=True (default) each
    item carries a `locations` breakdown, and `quantity`/`available`/`in_order` are
    the SUM across every location. That is what makes "out of stock EVERYWHERE"
    answerable — on this tenant stock sits at ~28 per-supplier locations as well as
    Default, so a Default-only zero is NOT dead stock. Pass `location_id` to scope
    the figures to one location instead.

    ⚠️ ACTIVE ITEMS ONLY. Like every Linnworks list/search endpoint, this never
    returns archived items (~32k active vs ~93k including archived here). "Every
    stock item" means every ACTIVE one; there is no endpoint that enumerates
    archived stock (only a UI/Data export).

    COST. One API call per 200 items. all_pages=True sweeps the whole catalogue —
    ~170 calls / ~90s on this tenant — so it is an AUTOPAGINATING tool: never run
    it in parallel with another one (get_top_skus, find_composite_parents, the
    category sweep …), they will rate-limit each other. It throttles under the
    150/min limit and backs off on 429.

    Args:
        page: 1-based page number (ignored when all_pages=True).
        per_page: Items per page (default 200, the Linnworks maximum).
        all_pages: Sweep the entire catalogue instead of one page. Expensive —
            see COST above. Pair with zero_stock_only / max_items to keep the
            response manageable.
        include_stock_levels: Load the per-location StockLevels (default True).
            Set False for a faster metadata-only listing — quantities then come
            back as None (meaning "not read", NOT zero).
        location_id: Scope quantities to one location's stock only (from
            get_locations; Default is the zero-GUID). Requires
            include_stock_levels. Items with no stock row at that location report
            zero and an empty `locations` list.
        zero_stock_only: Return only items whose quantity is zero in the scope
            being measured — i.e. across ALL locations by default, or at
            `location_id` when scoped. This is the cleanup-candidate filter.
            Requires include_stock_levels.
        include_composite_parents: Include composite/bundle parent items
            (default True). These are INCLUDE flags on the Linnworks side —
            setting this False makes bundle parents ABSENT from the results, which
            is what a cleanup sweep usually wants (a parent carries no stock of
            its own), but means the listing is no longer the full catalogue.
        include_variation_parents: Same, for variation-group parents (default True).
        flag_composite_parents: Fill each item's `is_composite_parent`. The
            endpoint behind this tool does NOT carry that field, so it is derived
            from the composite index used by find_composite_parents — which costs
            ~215 extra API calls to build the first time, then is cached for 15
            minutes. Leave False (the default) and `is_composite_parent` stays
            None, meaning "not determined". `is_variation_parent` is always real.
        max_items: Safety cap on how many item rows are RETURNED (default 5000).
            The sweep still completes and the counts still cover everything
            scanned — only the `items` detail is truncated, flagged by
            `truncated: true`.

    Returns:
        A dict with:
          - scope:          active_only / location / parent-flag settings in force
          - pages_fetched:  API pages read
          - scanned_count:  items examined across those pages
          - matched_count:  items passing zero_stock_only (== scanned when off)
          - count:          item rows actually returned
          - truncated:      True when max_items clipped the returned rows
          - complete:       True when the whole catalogue was swept (all_pages)
          - items:          sku, stock_item_id, title, barcode, category_name,
                            category_id, purchase_price, quantity, available,
                            in_order, due, is_variation_parent,
                            is_composite_parent, locations[]
    """
    per_page = max(1, min(per_page, 200))
    page = max(1, page)
    max_items = max(1, max_items)

    if location_id and not include_stock_levels:
        raise ValueError(
            "location_id requires include_stock_levels=True — quantities are "
            "derived from the per-location StockLevels array."
        )
    if zero_stock_only and not include_stock_levels:
        raise ValueError(
            "zero_stock_only requires include_stock_levels=True — with stock "
            "levels unread, quantity is None (not read) and cannot be tested "
            "for zero."
        )

    data_requirements = ["StockLevels"] if include_stock_levels else []

    items: list[dict] = []
    scanned = 0
    matched = 0
    pages_fetched = 0
    truncated = False
    current = page

    while True:
        rows = _fetch_full_stock_page(
            current, per_page,
            include_composite_parents, include_variation_parents,
            data_requirements,
        )
        if rows is None:  # past the last page — clean end, not an error
            break
        pages_fetched += 1
        scanned += len(rows)

        for row in rows:
            formatted = _format_full_stock_row(row, location_id, include_stock_levels)
            # Guarded above: zero_stock_only requires include_stock_levels, so
            # quantity here is a real number, never an unread None.
            if zero_stock_only and formatted["quantity"] != 0:
                continue
            matched += 1
            if len(items) < max_items:
                items.append(formatted)
            else:
                truncated = True

        if not all_pages or not rows or len(rows) < per_page:
            break
        current += 1
        time.sleep(0.42)  # stay under the 150/min rate limit

    if flag_composite_parents and items:
        parents = _get_composite_index().get("parents") or {}
        for it in items:
            sid = (it.get("stock_item_id") or "").lower()
            it["is_composite_parent"] = sid in parents

    return {
        "scope": {
            "active_only": True,
            "location_id": location_id,
            "stock_levels_loaded": include_stock_levels,
            "zero_stock_only": zero_stock_only,
            "includes_composite_parents": include_composite_parents,
            "includes_variation_parents": include_variation_parents,
            "composite_parent_flag_resolved": flag_composite_parents,
        },
        "page": None if all_pages else page,
        "per_page": per_page,
        "pages_fetched": pages_fetched,
        "scanned_count": scanned,
        "matched_count": matched,
        "count": len(items),
        "truncated": truncated,
        "complete": all_pages,
        "items": items,
    }


def _format_variation_member(m: dict) -> dict:
    """Map a Stock/GetVariationItems member row to the MCP-facing shape."""
    return {
        "sku": m.get("ItemNumber") or m.get("SKU"),
        "stock_item_id": m.get("pkStockItemId") or m.get("StockItemId"),
        "title": m.get("ItemTitle"),
    }


def _find_variation_group_by_name(name: str) -> dict | None:
    """
    Find a variation group by its exact name, returning the group row
    {VariationSKU, pkVariationItemId, VariationGroupName} or None.

    Stock/GetVariationGroupByName is UNRELIABLE on this tenant — it returns
    HTTP 200 `null` even for a group that demonstrably exists (live-confirmed
    28 Jul 2026, immediately after creating a group). So this searches
    Stock/SearchVariationGroups (searchType=VariationName, a SUBSTRING match)
    and confirms an EXACT case-insensitive name match to avoid false positives.
    """
    target = (name or "").strip().lower()
    if not target:
        return None
    page: int | None = 1
    pages_scanned = 0
    while page and pages_scanned < 10:
        res = call_linnworks_get(
            "Stock/SearchVariationGroups",
            {"searchType": "VariationName", "searchText": name,
             "pageNumber": page, "entriesPerPage": 100},
        )
        data = res.get("Data", []) if isinstance(res, dict) else []
        total_pages = res.get("TotalPages", 1) if isinstance(res, dict) else 1
        for row in data:
            if (row.get("VariationGroupName") or "").strip().lower() == target:
                return row
        pages_scanned += 1
        page = page + 1 if page < total_pages else None
    return None


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

    Reverse (component -> parent composites) is not resolved HERE, because there is
        still no endpoint that maps a component straight back to its parents — it can
        only be derived by enumerating every composite parent and inverting their
        component lists, which costs a full catalogue sweep (~105s / ~215 calls) and
        must not be paid per get_item_relationships call.

        It IS now available: use `find_composite_parents` (issue #31), which builds
        that index once and answers a whole batch of SKUs from it. So belongs_to
        stays empty here and reverse_lookup_supported stays False — meaning "not
        resolved by this call", NOT "impossible" (which is what the original issue
        #17 Open Q2 note claimed).
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
            "reverse_lookup_tool": "find_composite_parents",
        }

    return {
        "role": "none",
        "components": [],
        "belongs_to": [],
        "reverse_lookup_supported": False,
        "reverse_lookup_tool": "find_composite_parents",
        "note": ("Not a composite parent. Whether this SKU is a COMPONENT of "
                 "another composite is NOT resolved by this call (it needs a "
                 "catalogue-wide index) — use find_composite_parents for that, "
                 "especially before archiving/delisting/deleting the SKU."),
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
        bundles that contain it) is NOT resolved here — it needs a catalogue-wide
        index, so it lives in `find_composite_parents`. Use that one before
        archiving/delisting/deleting a SKU that might feed a live bundle.

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
              belongs_to[] (always empty here), reverse_lookup_supported (always
              False here), reverse_lookup_tool ("find_composite_parents")}
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


# ---------------------------------------------------------------------------
# Composite reverse lookup — component -> the composites that contain it
# ---------------------------------------------------------------------------
#
# Linnworks has NO component->parent endpoint, so the reverse direction is
# derived by enumerating every composite parent and inverting their component
# lists into a component -> parents index.
#
# Issue #17 (Open Q2) declared this infeasible because "Inventory/GetInventoryItems
# and Stock/GetStockItemsFull both 400". That blocker was stale (issue #31): the
# GET Stock/GetStockItems sweep already used by _count_items_per_category
# enumerates the whole active catalogue fine AND carries a per-row
# IsCompositeParent flag, and Inventory/GetInventoryItemsCompositionByIds resolves
# compositions in BULK. Two live findings from building this (5 Aug 2026):
#
#   • IsCompositeParent on Stock/GetStockItems is RELIABLE — unlike its sibling
#     IsVariationParent, which is not (see _resolve_variation). Cross-checked on a
#     200-item page: 96 flagged, 96 with components, zero disagreement either way.
#     That matters because a false negative here would silently mean "safe to
#     archive" for an item that still feeds a live bundle.
#   • GetInventoryItemsCompositionByIds caps at 100 ids per call (HTTP 400 "The
#     maximum items count for this call is 100" at 200) and OMITS items with no
#     compositions from the response map rather than returning empty lists.
#
# Measured on this tenant: 171 sweep pages + 44 composition calls = 215 API calls,
# ~105s, yielding 4343 composite parents over 34,023 active items and 2908 distinct
# components. That is why the index is built ONCE and cached for the process, and
# why the reverse lookup is a batch tool rather than a per-SKU one.
#
# ⚠️  ACTIVE ITEMS ONLY — the sweep endpoint never returns archived items (same
# blind spot documented above _is_category_in_use). An ARCHIVED composite parent is
# therefore invisible to the index. That is the safe direction for the archive-gate
# use case (an archived parent is not a live listing), but it does mean the index
# cannot tell you a component feeds an archived bundle.

_COMPOSITE_INDEX_TTL_SECONDS = 900  # 15 min — the index costs ~105s to build
_composite_index_cache: dict | None = None


def _build_composite_index() -> dict:
    """
    Build the component -> composite-parents index for the whole ACTIVE catalogue.

    Two phases:
      1. Sweep Stock/GetStockItems (keyWord="", 200/page) across every active item,
         keeping (a) a SKU -> StockItemId/title map for free resolution and
         (b) the StockItemIds flagged IsCompositeParent.
      2. Resolve those parents' component lists in bulk via
         Inventory/GetInventoryItemsCompositionByIds (chunked at 100 — its hard
         cap) and invert them into {component_id: [{parent_id, quantity}]}.

    Both phases throttle under their rate limits (150/min sweep, 250/min
    compositions) and back off on HTTP 429. The sweep always runs to completion —
    a partial index would under-report parents, i.e. wrongly clear a component for
    archiving, which is the exact failure this tool exists to prevent.

    Returns {"index", "parents", "sku_to_id", "built_at", "stats"}.
    """
    parents: dict[str, dict] = {}     # parent stock_item_id (lower) -> {sku, title}
    sku_to_id: dict[str, dict] = {}   # sku (lower) -> {stock_item_id, title}
    total_items = 0
    page = 1
    sweep_pages = 0

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
                    time.sleep(15)
                    continue
                raise
        if resp is None:
            raise RuntimeError(
                f"Composite index build aborted: repeated rate-limit (429) on sweep "
                f"page {page}. No partial index is returned — a partial sweep would "
                "under-report composite parents and could wrongly clear a component "
                "as safe to archive."
            )

        data = resp.get("Data") or []
        sweep_pages += 1
        for it in data:
            sid = it.get("StockItemId")
            if not sid:
                continue
            sid = sid.lower()
            total_items += 1
            sku = it.get("ItemNumber")
            title = it.get("ItemTitle")
            if sku:
                sku_to_id[sku.strip().lower()] = {"stock_item_id": sid, "title": title}
            if it.get("IsCompositeParent"):
                parents[sid] = {"sku": sku, "title": title}

        total_pages = resp.get("TotalPages") or 1
        if page >= total_pages or not data:
            break
        page += 1
        time.sleep(0.42)  # stay under the 150/min sweep limit

    # --- Phase 2: bulk-resolve each parent's components and invert ---
    index: dict[str, list] = {}
    parent_ids = list(parents)
    composition_calls = 0
    for i in range(0, len(parent_ids), 100):  # hard cap: 100 ids per call
        chunk = parent_ids[i:i + 100]
        resp = None
        for _ in range(6):
            try:
                resp = call_linnworks(
                    "Inventory/GetInventoryItemsCompositionByIds",
                    {"request": {"InventoryItemIds": chunk}},
                )
                break
            except RuntimeError as exc:
                if "429" in str(exc) or "quota" in str(exc).lower():
                    time.sleep(15)
                    continue
                raise
        if resp is None:
            raise RuntimeError(
                "Composite index build aborted: repeated rate-limit (429) resolving "
                "compositions. No partial index is returned."
            )
        composition_calls += 1
        # Items with no compositions are OMITTED from the map, not returned empty.
        for pid, comps in (resp.get("InventoryItemsCompositionByIds") or {}).items():
            pid = (pid or "").lower()
            for c in comps or []:
                cid = (c.get("LinkedStockItemId") or "").lower()
                if not cid:
                    continue
                index.setdefault(cid, []).append(
                    {"parent_id": pid, "quantity": c.get("Quantity")}
                )
        time.sleep(0.25)  # stay under the 250/min composition limit

    return {
        "index":     index,
        "parents":   parents,
        "sku_to_id": sku_to_id,
        "built_at":  time.time(),
        "stats": {
            "active_items":       total_items,
            "composite_parents":  len(parents),
            "indexed_components": len(index),
            "api_calls":          sweep_pages + composition_calls,
            "sweep_pages":        sweep_pages,
            "composition_calls":  composition_calls,
        },
    }


def _get_composite_index(rebuild: bool = False) -> dict:
    """
    Return the composite reverse index, building it if absent/stale/forced.

    Cached for the process for _COMPOSITE_INDEX_TTL_SECONDS so that one bulk
    screening run — the intended use — pays the ~105s build once. Pass
    rebuild=True when composites may have changed since the last build.
    """
    global _composite_index_cache
    cache = _composite_index_cache
    if (not rebuild and cache
            and (time.time() - cache["built_at"]) < _COMPOSITE_INDEX_TTL_SECONDS):
        return cache
    _composite_index_cache = _build_composite_index()
    return _composite_index_cache


@mcp.tool()
def find_composite_parents(
    skus: list[str],
    include_listing_status: bool = True,
    max_parents_listed: int = 25,
    rebuild_index: bool = False,
) -> dict:
    """
    For each SKU, find the composite/bundle parents that CONTAIN it — the reverse
    of get_item_relationships' forward parent -> components lookup.

    Answers the archive-safety question the rest of the toolset cannot:
    "before I archive/delist/delete this dead-looking SKU, is it a component of a
    bundle that is still live?" A component can read as dead stock on its own
    while still feeding a selling composite (pooled multipacks, kids' complete
    bundles, custom completes) — retiring it silently breaks the parent.

    Run this as a gate before archive_inventory_items, delete_inventory_item,
    delist_all_channel_listings, or any bulk operation that retires items.

    HOW IT WORKS / COST. Linnworks has no component -> parent endpoint, so this
    enumerates every composite parent in the ACTIVE catalogue and inverts their
    component lists into an index. On this tenant that is ~215 API calls / ~105
    seconds (34k items, ~4.3k composite parents). The index is built ONCE and
    cached for 15 minutes, so pass the whole batch of candidate SKUs in one call
    rather than calling per SKU. Do not run this in parallel with the other
    autopaginating tools.

    ⚠️  ACTIVE ITEMS ONLY. The sweep endpoint never returns archived items, so an
    ARCHIVED composite parent is invisible here. Safe for the archive-gate use
    (an archived parent is not selling), but it is not a complete history.

    Args:
        skus: Exact SKUs / ItemNumbers to screen (case-insensitive). Resolved from
            the sweep itself at no extra cost; anything not in the active
            catalogue falls back to a direct lookup and, failing that, is
            reported under `unresolved` (an archived SKU cannot be resolved by
            SKU at all — that is a Linnworks limitation).
        include_listing_status: Also report whether each parent is live on a sales
            channel (Inventory/BatchGetInventoryItemChannelSKUs, ~1 call per 200
            distinct parents). This is what turns "is a component" into "is a
            component of something still selling". Default True.
        max_parents_listed: Cap on how many parent rows are returned per SKU
            (default 25). Some components sit in thousands of composites, which
            would swamp the response. `parent_count` and `listed_parent_count`
            are always counted across ALL parents, never just the returned ones,
            so the safety verdict is never truncated — only the detail is.
        rebuild_index: Force a fresh index build instead of using the cached one.

    Returns:
        A dict with:
          - item_count, resolved_count
          - component_count: how many resolved SKUs are a component of anything
          - blocked_count: how many have at least one LISTED parent — the SKUs
            you must NOT retire (None-ish when include_listing_status=False)
          - results: per-SKU rows with sku, stock_item_id, title, is_component,
            parent_count, listed_parent_count, has_listed_parent,
            safe_to_retire (False if it feeds a live parent), parents[]
            (parent_sku, parent_stock_item_id, parent_title, quantity,
            parent_is_listed, parent_channels), parents_truncated
          - unresolved: rows for SKUs not found in the active catalogue
          - index: build/cache stats (built_at, age_seconds, from_cache, and the
            sweep totals) so a stale answer is visible rather than implicit
    """
    if not skus:
        raise ValueError("skus must contain at least one SKU.")

    was_cached = (
        not rebuild_index
        and _composite_index_cache is not None
        and (time.time() - _composite_index_cache["built_at"]) < _COMPOSITE_INDEX_TTL_SECONDS
    )
    idx = _get_composite_index(rebuild=rebuild_index)
    index, parents, sku_to_id = idx["index"], idx["parents"], idx["sku_to_id"]

    # --- Resolve the requested SKUs (free from the sweep; fall back on a miss) ---
    resolved: list[tuple[str, str, str | None]] = []  # (sku, stock_item_id, title)
    unresolved: list[dict] = []
    for raw in skus:
        s = (raw or "").strip()
        if not s:
            unresolved.append({"sku": raw, "error": "empty SKU"})
            continue
        hit = sku_to_id.get(s.lower())
        if hit:
            resolved.append((s, hit["stock_item_id"], hit["title"]))
            continue
        # Not in the swept active catalogue — could be a newer item than the
        # cached index, so try a direct lookup before giving up.
        try:
            item = call_linnworks("Inventory/GetInventoryItem", {"sku": s})
            sid = item.get("StockItemId")
            if sid:
                resolved.append((s, sid.lower(), item.get("ItemTitle")))
                continue
            unresolved.append({"sku": s, "error": "found but returned no StockItemId"})
        except RuntimeError:
            unresolved.append({
                "sku": s,
                "error": ("not found in the active catalogue — it may be archived "
                          "(archived SKUs cannot be resolved by SKU in Linnworks)"),
            })

    # --- Listing status for every DISTINCT parent across the whole batch ---
    listed_ids: set[str] = set()
    channels_by_parent: dict[str, list] = {}
    listing_error: str | None = None
    if include_listing_status:
        distinct_parents = sorted({
            p["parent_id"]
            for _, sid, _ in resolved
            for p in index.get(sid, [])
        })
        if distinct_parents:
            try:
                by_id = _fetch_channel_skus_for_ids(distinct_parents)
                for pid, rows in by_id.items():
                    if rows:
                        listed_ids.add(pid)
                        channels_by_parent[pid] = sorted(
                            {r.get("Source") for r in rows if r.get("Source")}
                        )
            except RuntimeError as exc:
                # Degrade safely: the component->parent answer still stands, we
                # just cannot say which parents are live.
                listing_error = str(exc)

    results: list[dict] = []
    component_count = 0
    blocked_count = 0
    for s, sid, title in resolved:
        links = index.get(sid, [])
        if links:
            component_count += 1

        rows = []
        listed_parent_count = 0
        for link in links:
            pid = link["parent_id"]
            meta = parents.get(pid, {})
            is_listed = (pid in listed_ids) if include_listing_status else None
            if is_listed:
                listed_parent_count += 1
            rows.append({
                "parent_sku":            meta.get("sku"),
                "parent_stock_item_id":  pid,
                "parent_title":          meta.get("title"),
                "quantity":              link.get("quantity"),
                "parent_is_listed":      is_listed,
                "parent_channels":       channels_by_parent.get(pid, []),
            })

        # Listed parents first so a truncated list still shows what blocks you.
        rows.sort(key=lambda r: (not r["parent_is_listed"], r["parent_sku"] or ""))
        has_listed = listed_parent_count > 0
        if has_listed:
            blocked_count += 1

        results.append({
            "sku":                 s,
            "stock_item_id":       sid,
            "title":               title,
            "is_component":        bool(links),
            "parent_count":        len(links),
            "listed_parent_count": listed_parent_count if include_listing_status else None,
            "has_listed_parent":   has_listed if include_listing_status else None,
            # Only a definite verdict when we actually checked the channels.
            "safe_to_retire":      (not has_listed) if include_listing_status else None,
            "parents":             rows[:max_parents_listed],
            "parents_truncated":   len(rows) > max_parents_listed,
        })

    out = {
        "item_count":      len(skus),
        "resolved_count":  len(resolved),
        "component_count": component_count,
        "blocked_count":   blocked_count if include_listing_status else None,
        "results":         results,
        "unresolved":      unresolved,
        "index": {
            "from_cache":  was_cached,
            "built_at":    datetime.fromtimestamp(idx["built_at"], timezone.utc)
                               .isoformat(),
            "age_seconds": round(time.time() - idx["built_at"]),
            "ttl_seconds": _COMPOSITE_INDEX_TTL_SECONDS,
            "scope":       "active items only — archived composite parents are invisible",
            **idx["stats"],
        },
    }
    if listing_error:
        out["listing_status_error"] = (
            f"Parent listing status could not be read ({listing_error}); "
            "parent_is_listed / safe_to_retire are unreliable for this run."
        )
    return out


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
    require_complete: bool = False,
    dry_run: bool = True,
) -> dict:
    """
    Update the delivery address on an open (unprocessed) Linnworks order.

    Handles BOTH kinds of address change:
      - amend a line or two — pass just those fields (everything else is kept);
      - replace the whole destination — pass every field, ideally with
        require_complete=True so a half-supplied address is refused.

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
        require_complete: Guard for whole-address changes. When True, the call is
            REFUSED unless you supply all five core fields — full_name, address1,
            town, postcode, country — non-blank. Use it whenever the destination
            itself is changing (a customer address correction, a redirect), so a
            partly-supplied address cannot silently merge with the old one and
            ship to a hybrid of the two: new street, old town. Leave False
            (default) when deliberately amending one or two lines.
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

    # ---------- Step 0: completeness guard (before any API call) ----------
    # Checks what the CALLER supplied, not the merged result: a merged address can
    # be "complete" and still be wrong (new street + old town). Supplying every
    # core field is the caller stating the whole destination explicitly.
    if require_complete:
        missing = sorted(
            param for param in ("full_name", "address1", "town", "postcode", "country")
            if not (user_provided[param] or "").strip()
        )
        if missing:
            return {
                "order_id": order_id,
                "status": "error",
                "error": (
                    "require_complete=True but these core address fields are missing "
                    f"or blank: {', '.join(missing)}. Supply the whole destination, "
                    "or set require_complete=False to amend individual lines."
                ),
                "missing_fields": missing,
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
def set_order_status(
    order_ids: list[str],
    action: str,
    confirmed_count: Optional[int] = None,
    dry_run: bool = True,
) -> dict:
    """
    Change the workflow status of one or more Linnworks orders.

    Supported actions (case-insensitive):
      - "lock"   → hold the order so it CANNOT be picked/dispatched (Orders/LockOrder)
      - "unlock" → release a held order back into normal processing
      - "paid"   → mark the order PAID   (Orders/ChangeStatus, status 1)
      - "unpaid" → mark the order UNPAID (Orders/ChangeStatus, status 0)

    The primary use case is HOLDING an order before it dispatches — e.g. a
    customer has requested cancellation in Shopify and you want to stop the
    pickwave until a human confirms. Use "lock" for that; a locked order stays
    in the open-orders queue but is barred from processing (padlock in the UI).

    ⚠️ Stock allocation side effect: LOCKING an order RELEASES the stock it had
    allocated back to available — so that stock can be re-allocated to (and sold
    on) other orders while this one is held. PARKING would KEEP the stock
    allocated, but parking is not reachable via the API (below). If you hold an
    order you intend to keep, be aware its stock is no longer ring-fenced.

    ⚠️ "park"/"unpark" are NOT supported — Linnworks exposes no public endpoint
    for parking (it exists only as a RulesEngine action; GeneralInfo.IsParked is
    read-only via the API). Requesting them returns an error suggesting "lock".
    Note parking is also the only state that KEEPS stock allocated (see above),
    so this API gap has a real operational cost, not just a naming one.

    ⚠️ Lock read-back limitation: the Linnworks API exposes no lock-status field
    on the order model, so this tool CANNOT verify a lock/unlock landed by
    reading it back — it reports the API's write response and asks you to confirm
    the padlock in the Linnworks UI. Paid/unpaid ARE read-back verified against
    GeneralInfo.Status.

    Read-before-write: every order is resolved and its current status, parked
    flag, processed flag, customer and reference captured into a manifest. An
    order id that can't be resolved becomes a resolve_error row and never aborts
    the batch. Bulk-capable: all resolvable orders are changed in a single API
    call (both endpoints take an id list). Staging threshold 25 (reversible).
    dry_run defaults to True.

    Args:
        order_ids: One or more order identifiers. Each may be a GUID pkOrderID
            or a numeric order number (e.g. "607046"); both are accepted and a
            single string is also accepted for convenience.
        action: One of "lock", "unlock", "paid", "unpaid".
        confirmed_count: For batches above the staging threshold, echo back the
            exact order count to execute (see the write-safety framework).
        dry_run: If True (default), returns the manifest without changing
            anything. Set to False to execute.

    Returns:
        A dict with dry_run, action, endpoint, order_count, resolved_count,
        the per-order manifest (dry run) or per-order read-back results (live),
        and resolve_errors for any unresolved ids.
    """
    action_norm = (action or "").strip().lower()

    if action_norm in _UNSUPPORTED_STATUS_ACTIONS:
        return {
            "error": (
                f"Action '{action_norm}' is not supported — the Linnworks public "
                f"API has no park/unpark endpoint (parking exists only as a "
                f"RulesEngine action; GeneralInfo.IsParked is read-only). To hold "
                f"an order from dispatch, use action='lock' instead."
            ),
            "action": action_norm,
            "supported_actions": list(_ORDER_STATUS_ACTIONS.keys()),
        }

    if action_norm not in _ORDER_STATUS_ACTIONS:
        return {
            "error": (
                f"Unknown action {action!r}. Supported actions: "
                f"{', '.join(_ORDER_STATUS_ACTIONS.keys())}."
            ),
            "supported_actions": list(_ORDER_STATUS_ACTIONS.keys()),
        }

    # Normalise order_ids → clean list (accept a bare string too).
    if isinstance(order_ids, str):
        order_ids = [order_ids]
    order_ids = [str(o).strip() for o in (order_ids or []) if str(o).strip()]
    if not order_ids:
        return {"error": "No order_ids provided.", "action": action_norm}

    endpoint, value = _ORDER_STATUS_ACTIONS[action_norm]

    # ---- Read-before-write: resolve every order, build the manifest. ----
    resolved: list[tuple[str, dict]] = []   # (guid, manifest_row)
    manifest: list[dict] = []
    resolve_errors: list[dict] = []
    guid_list: list[str] = []

    for oid in order_ids:
        try:
            guid, raw = _resolve_order_guid(oid)
        except RuntimeError as exc:
            resolve_errors.append({"order_id_input": oid, "error": str(exc)})
            manifest.append({"order_id_input": oid, "resolved": False, "error": str(exc)})
            continue

        fmt = _format_order_detail(raw)
        cur = fmt.get("status")
        row = {
            "order_id_input":       oid,
            "order_id":             guid,
            "num_order_id":         fmt.get("num_order_id"),
            "customer_name":        fmt.get("customer_name"),
            "reference_num":        fmt.get("reference_num"),
            "external_reference":   fmt.get("external_reference"),
            "source":               fmt.get("source"),
            "processed":            fmt.get("processed"),
            "is_parked":            fmt.get("is_parked"),
            "current_status":       cur,
            "current_status_label": _PAYMENT_STATUS_LABELS.get(cur, f"Unknown({cur})"),
            "action":               action_norm,
            "resolved":             True,
        }

        if endpoint == "LockOrder":
            row["intent"] = (
                "Lock (hold from picking/dispatch)" if value
                else "Unlock (release for processing)"
            )
            if fmt.get("processed"):
                row["warning"] = (
                    "Order is already processed/dispatched — locking has no "
                    "effect on a dispatched order."
                )
        else:  # ChangeStatus (paid/unpaid)
            row["new_status"] = value
            row["new_status_label"] = _PAYMENT_STATUS_LABELS.get(value)
            row["intent"] = f"Set payment status → {_PAYMENT_STATUS_LABELS.get(value)}"

        resolved.append((guid, row))
        guid_list.append(guid)
        manifest.append(row)

    # ---- Staging gate (based on the batch the caller passed). ----
    guard = _write_guard("set_order_status", order_ids, confirmed_count, dry_run)
    if guard is not None:
        return {
            **guard,
            "action": action_norm,
            "endpoint": f"Orders/{endpoint}",
            "manifest": manifest,
            "resolve_errors": resolve_errors,
        }

    if dry_run:
        return {
            "dry_run": True,
            "action": action_norm,
            "endpoint": f"Orders/{endpoint}",
            "order_count": len(order_ids),
            "resolved_count": len(guid_list),
            "manifest": manifest,
            "resolve_errors": resolve_errors,
            "message": "Set dry_run=False to execute this status change.",
        }

    if not guid_list:
        return {
            "dry_run": False,
            "action": action_norm,
            "endpoint": f"Orders/{endpoint}",
            "order_count": len(order_ids),
            "resolved_count": 0,
            "results": [],
            "resolve_errors": resolve_errors,
            "message": "No resolvable orders to act on.",
        }

    # ---- Execute: a single call handles the whole resolved id list. ----
    if endpoint == "LockOrder":
        payload = {"orderIds": guid_list, "lockOrder": bool(value)}
    else:
        payload = {"orderIds": guid_list, "status": int(value)}
    write_resp = call_linnworks(f"Orders/{endpoint}", payload)

    # ---- Read-back per order. ----
    results: list[dict] = []
    for guid, row in resolved:
        rb = {
            "order_id":     guid,
            "num_order_id": row["num_order_id"],
            "reference_num": row["reference_num"],
            "action":       action_norm,
        }
        try:
            _, raw2 = _resolve_order_guid(guid)
            fmt2 = _format_order_detail(raw2)
            if endpoint == "ChangeStatus":
                new = fmt2.get("status")
                rb["status_after"] = new
                rb["status_after_label"] = _PAYMENT_STATUS_LABELS.get(new, f"Unknown({new})")
                rb["changed"] = (new == value)
            else:  # LockOrder — no readable lock field on the order model
                rb["is_parked"] = fmt2.get("is_parked")
                rb["lock_readback"] = "unavailable"
                rb["note"] = (
                    "The Linnworks API exposes no lock-status field, so the lock "
                    "cannot be verified by read-back — confirm the padlock in the "
                    "Linnworks UI."
                )
        except RuntimeError as exc:
            rb["readback_error"] = str(exc)
        results.append(rb)

    return {
        "dry_run": False,
        "action": action_norm,
        "endpoint": f"Orders/{endpoint}",
        "order_count": len(order_ids),
        "resolved_count": len(guid_list),
        "linnworks_response": write_resp,
        "results": results,
        "resolve_errors": resolve_errors,
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


# ---------- Stock change history (issue #33) ----------
#
# Stock/GetItemChangesHistory is the audit trail behind a stock level. The public
# spec's StockItemChangeHistory model has NO ChangeSource field — the source is
# embedded in the free-text `Note`, so _classify_change_source() derives it. The
# patterns below were built from ~6,100 live rows across Default, FBA and supplier
# locations on this tenant (5 Aug 2026).
#
# Each entry: (canonical source, tuple of lowercase note prefixes/substrings).
# Order matters — first match wins, so specific patterns precede general ones.
_CHANGE_SOURCE_PATTERNS: list[tuple[str, tuple[str, ...]]] = [
    ("RETURN",       ("customer return for order",)),
    ("SCRAP",        ("scrapped by",)),
    ("SALE",         ("order ",)),
    ("PO_DELIVERY",  ("po delivered", "delivered")),
    ("STOCKTAKE",    ("stock count adjustment",)),
    ("ADJUSTMENT",   ("direct adjustment",)),
    ("FBA_SYNC",     ("fba sync", "no quantity returned from channel")),
    ("FILE_IMPORT",  ("imported from file", "import ")),
]

# Sources that are automated/system noise rather than genuine trading movement.
# Mirrors the ChangeSource exclusions used in this tenant's custom-SQL export
# ('Imported from file%', 'FBA Sync%', 'Import %') — see issue #33.
_AUTOMATED_CHANGE_SOURCES = ("FILE_IMPORT", "FBA_SYNC")

# Every canonical source this classifier can emit (used to validate filters).
_KNOWN_CHANGE_SOURCES = (
    "SALE", "RETURN", "PO_DELIVERY", "PO_BOOKING", "PO_DELETED", "PO_UPDATE",
    "STOCKTAKE", "ADJUSTMENT", "FBA_SYNC", "FILE_IMPORT", "SCRAP", "OTHER",
)


def _classify_change_source(note: str | None) -> str:
    """
    Derive a canonical change_source from a stock-change Note.

    Linnworks does NOT return a ChangeSource field on Stock/GetItemChangesHistory
    (despite the underlying StockChange table having one, which is why the custom-SQL
    export can filter on it). The source has to be recovered from the free-text note.

    Returns one of _KNOWN_CHANGE_SOURCES. Unrecognised notes → "OTHER" rather than
    being forced into a wrong bucket — the raw `note` is always returned alongside
    so a caller can reclassify.
    """
    n = (note or "").strip().lower()
    if not n:
        return "OTHER"

    # PO notes all start "PO" but mean different things; split them before the
    # general table so a "PO ... delivered" never falls through to another rule.
    if n.startswith("po "):
        if "delivered" in n:
            return "PO_DELIVERY"
        if "to open" in n or "due " in n:
            return "PO_BOOKING"
        if "deleted" in n:
            return "PO_DELETED"
        if "update" in n:
            return "PO_UPDATE"
        return "OTHER"

    for source, prefixes in _CHANGE_SOURCE_PATTERNS:
        for p in prefixes:
            if n.startswith(p) or p in n:
                return source
    return "OTHER"


def _fetch_change_history_rows(
    stock_item_id: str,
    location_id: str,
    max_pages: int,
    per_page: int = 200,
) -> tuple[list[dict], int, bool]:
    """
    Page Stock/GetItemChangesHistory for one (item, location), newest-first.

    Returns (rows, total_entries, truncated). Stops early once max_pages is hit —
    `truncated` then says the trailing history was NOT fully read, which the caller
    must surface (an out_of_stock_since derived from a truncated tail can only be
    a lower bound on how long the item has been at zero).

    ⚠️  locationId is REQUIRED despite the spec calling it optional ("If null then
    combined") — omitting it returns HTTP 400 "The request is invalid.". There is
    therefore no combined-across-locations mode; scan per location and merge.
    ⚠️  pageNumber=-1 ("all pages" per the spec) returns HTTP 400 "Value must be at
    least 1" — real paging is the only option.
    """
    rows: list[dict] = []
    total = 0
    page = 1
    while page <= max_pages:
        resp = call_linnworks_get(
            "Stock/GetItemChangesHistory",
            {
                "stockItemId": stock_item_id,
                "locationId": location_id,
                "entriesPerPage": per_page,
                "pageNumber": page,
            },
        )
        total = resp.get("TotalEntries") or 0
        batch = resp.get("Data") or []
        rows.extend(batch)
        # Past-the-end returns an empty Data array with a 200 (unlike
        # GetStockItemsFull, which 400s) — a clean stop signal.
        if len(batch) < per_page:
            return rows, total, False
        page += 1
    return rows, total, len(rows) < total


def _format_change_row(row: dict) -> dict:
    """Normalise one StockItemChangeHistory row into the tool's output shape."""
    note = row.get("Note")
    return {
        "date": row.get("Date"),
        "change_source": _classify_change_source(note),
        # `Level` is the level AFTER the change — verified live on 15 consecutive
        # row pairs (row.Level - row.ChangeQty == the next-older row's Level).
        "level_after": row.get("Level"),
        "quantity": row.get("ChangeQty"),
        "stock_value_after": row.get("StockValue"),
        "change_value": row.get("ChangeValue"),
        "note": note,
    }


def _summarise_change_history(
    rows: list[dict],
    current_level: int | None,
    truncated: bool,
    now: "datetime | None" = None,
) -> dict:
    """
    Derive the dead-stock summary from newest-first movement rows.

    out_of_stock_since is the date of the OLDEST row in the current unbroken
    trailing run of level_after == 0 — NOT simply the most recent row at zero.
    That distinction is the whole point: an item sat at zero for a year still
    receives automated rows (FBA sync, file imports) that write 0 again, and
    "most recent row where level == 0" would reset the dead-stock clock to today
    on exactly the items the cleanup job is looking for.

    Deriving it from the raw level series (rather than from source-filtered rows)
    is also why no filtering happens here: FBA-sync rows in this tenant are
    genuine decrements (live-observed walking an item 13 → 0), so excluding them
    from the series would produce a wrong date. Source filtering belongs to
    last_real_movement_date, which is what the noise actually distorts.
    """
    now = now or datetime.now(timezone.utc)

    def _parse(d: str | None):
        if not d:
            return None
        try:
            return datetime.fromisoformat(str(d).replace("Z", "+00:00"))
        except ValueError:
            return None

    def _first(pred):
        return next((r for r in rows if pred(r)), None)

    # Trailing zero run — walk from newest backwards while level_after == 0.
    out_of_stock_since = None
    zero_run_rows = 0
    zero_run_complete = True
    if rows and (rows[0].get("level_after") or 0) == 0:
        for r in rows:
            if (r.get("level_after") or 0) != 0:
                break
            out_of_stock_since = r.get("date")
            zero_run_rows += 1
        # The run reaching the end of a truncated read means the true transition
        # is older than anything we fetched.
        if zero_run_rows == len(rows) and truncated:
            zero_run_complete = False

    # If current stock disagrees with the newest history row, the history is not a
    # safe basis for a zero claim — say so rather than asserting a date.
    level_from_history = rows[0].get("level_after") if rows else None
    level_mismatch = (
        current_level is not None
        and level_from_history is not None
        and current_level != level_from_history
    )
    if current_level is not None and current_level > 0:
        out_of_stock_since = None

    days_out = None
    since_dt = _parse(out_of_stock_since)
    if since_dt is not None:
        days_out = (now - since_dt).days

    movements = [r for r in rows if (r.get("quantity") or 0) != 0]
    real_movements = [
        r for r in movements
        if r.get("change_source") not in _AUTOMATED_CHANGE_SOURCES
    ]
    last_sale = _first(lambda r: r.get("change_source") == "SALE")
    last_recv = _first(lambda r: r.get("change_source") in ("PO_DELIVERY", "RETURN"))

    return {
        "current_level": current_level,
        "level_from_history": level_from_history,
        "out_of_stock_since": out_of_stock_since,
        "days_out_of_stock": days_out,
        "out_of_stock_since_is_lower_bound": bool(out_of_stock_since) and not zero_run_complete,
        "last_movement_date": movements[0]["date"] if movements else None,
        "last_real_movement_date": real_movements[0]["date"] if real_movements else None,
        "last_sale_date": last_sale["date"] if last_sale else None,
        "last_received_date": last_recv["date"] if last_recv else None,
        "movement_count": len(rows),
        "level_mismatch": level_mismatch,
    }


@mcp.tool()
def get_stock_change_history(
    skus: list[str] | str,
    location_id: str | None = None,
    all_locations: bool = False,
    include_movements: bool = True,
    max_movements: int = 50,
    change_sources: list[str] | None = None,
    exclude_change_sources: list[str] | None = None,
    max_pages: int = 10,
) -> dict:
    """
    Stock movement history for one or more SKUs, plus a derived dead-stock summary
    answering "when did this item actually go out of stock?".

    This is the audit trail behind a stock level — every change with its date,
    quantity, resulting level and source (sale, PO delivery, return, stocktake,
    manual adjustment, file import, FBA sync). Built for the catalogue-cleanup
    screen: find items that have sat at zero for ~3 months, then hand them to
    find_composite_parents → delist_all_channel_listings → archive_inventory_items.

    KEY DERIVED FIELD — `out_of_stock_since` is the date the item TRANSITIONED to
    zero: the oldest row in the current unbroken run of level_after == 0. It is
    deliberately NOT "the most recent row where level == 0", because an item that
    has been dead for a year still receives automated rows (FBA sync, nightly file
    imports) that rewrite 0 — which would reset the dead-stock clock to today on
    exactly the items you are hunting. `days_out_of_stock` follows from it.

    ⚠️  PER LOCATION, NOT COMBINED. Linnworks requires a locationId here (the
    spec's "if null then combined" is wrong — it 400s), so history is always
    location-scoped. `all_locations=True` scans every location and returns a
    per-location breakdown plus `zero_at_all_locations_since` (the LATEST of the
    per-location zero dates, set only when every location is currently zero).
    Quantities are NOT summed across locations: most non-Default locations in this
    tenant are virtual dropship supplier mirrors showing the same availability, so
    a combined figure would be meaningless.

    ⚠️  change_source is DERIVED from the free-text note, not returned by Linnworks
    — the API has no ChangeSource field even though the underlying table does. The
    raw `note` is always included so you can reclassify anything landing in "OTHER".

    Filtering (`change_sources` / `exclude_change_sources`) applies to the returned
    `movements` list and to `last_real_movement_date`. It never applies to the
    level series behind `out_of_stock_since`, because automated rows here are real
    decrements (FBA sync live-observed walking an item 13 → 0) and dropping them
    would produce a wrong date.

    Cost: one call per (SKU, location) page. `all_locations=True` costs ~29 calls
    per SKU on this tenant — keep those batches small. Rate limit 250/min.

    Args:
        skus: One SKU or a list of SKUs (exact ItemNumbers; ACTIVE items only —
            archived SKUs cannot be resolved).
        location_id: StockLocationId GUID to scope to. Defaults to the "Default"
            location (owned stock). Ignored when all_locations=True.
        all_locations: If True, scan every stock location and return a per-location
            breakdown plus zero_at_all_locations_since. Costs ~29 calls per SKU.
        include_movements: If True (default), return the movement rows. Set False
            for a summary-only bulk screen.
        max_movements: Cap on returned movement rows per SKU/location (default 50).
            Truncates detail only — every summary figure is computed across all
            rows read.
        change_sources: Only include movements with these sources. One or more of:
            SALE, RETURN, PO_DELIVERY, PO_BOOKING, PO_DELETED, PO_UPDATE,
            STOCKTAKE, ADJUSTMENT, FBA_SYNC, FILE_IMPORT, SCRAP, OTHER.
        exclude_change_sources: Drop movements with these sources. Applied after
            change_sources.
        max_pages: Max history pages (200 rows each) to read per SKU/location.
            Default 10 (2,000 rows). If the trailing zero run reaches the end of a
            truncated read, out_of_stock_since_is_lower_bound is set True.

    Returns:
        dict with per-SKU results carrying the summary fields (out_of_stock_since,
        days_out_of_stock, last_sale_date, last_received_date, …), optional
        movements, and — with all_locations — a `locations` breakdown. Unresolvable
        SKUs land in `unresolved` and never abort the batch.
    """
    if isinstance(skus, str):
        skus = [skus]
    if not skus:
        return {"error": "No SKUs supplied.", "count": 0, "results": []}

    for name, sel in (("change_sources", change_sources),
                      ("exclude_change_sources", exclude_change_sources)):
        if sel:
            bad = [s for s in sel if s.upper() not in _KNOWN_CHANGE_SOURCES]
            if bad:
                raise ValueError(
                    f"Unknown {name}: {bad}. Valid sources: {', '.join(_KNOWN_CHANGE_SOURCES)}"
                )

    include_set = {s.upper() for s in change_sources} if change_sources else None
    exclude_set = {s.upper() for s in exclude_change_sources} if exclude_change_sources else set()
    max_pages = max(1, min(int(max_pages or 1), 100))

    if all_locations:
        loc_rows = call_linnworks_get("Inventory/GetStockLocations") or []
        targets = [
            (l.get("StockLocationId"), l.get("LocationName"))
            for l in loc_rows
            if l.get("StockLocationId")
        ]
    else:
        lid = location_id or DEFAULT_LOCATION_ID
        targets = [(lid, None)]

    def _apply_filter(rows: list[dict]) -> list[dict]:
        out = rows
        if include_set is not None:
            out = [r for r in out if r["change_source"] in include_set]
        if exclude_set:
            out = [r for r in out if r["change_source"] not in exclude_set]
        return out

    results, unresolved, rate_limited = [], [], []
    id_cache: dict = {}
    calls = 0

    for sku in skus:
        try:
            stock_item_id = _resolve_sku_to_id(sku, id_cache)
        except RateLimitError as exc:
            # A quota failure is NOT a missing item, and the archived hint below
            # would assert a cause that didn't happen (issue #37).
            rate_limited.append({"sku": sku, "error": str(exc)})
            continue
        except Exception as exc:
            unresolved.append({
                "sku": sku,
                "error": str(exc),
                "hint": "Archived items cannot be resolved by SKU — unarchive first.",
            })
            continue

        # Current levels are authoritative — history can be empty for an item that
        # genuinely holds stock (live-confirmed: RS-102201 has 0 history rows at
        # Default while holding stock at Rock Solid), so never infer the level from
        # the history alone.
        levels_by_loc: dict[str, int] = {}
        try:
            batch = call_linnworks(
                "Stock/GetStockLevel_Batch", {"request": {"StockItemIds": [stock_item_id]}}
            )
            calls += 1
            item_row = next(
                (r for r in (batch if isinstance(batch, list) else [])
                 if r.get("pkStockItemId") == stock_item_id),
                None,
            )
            for l in (item_row or {}).get("StockItemLevels") or []:
                loc = (l.get("Location") or {}).get("StockLocationId")
                if loc:
                    levels_by_loc[loc] = l.get("StockLevel") or 0
        except RateLimitError as exc:
            # Do NOT swallow this. Current level is what suppresses or produces
            # out_of_stock_since, and the cleanup chain archives/delists on that
            # date — silently treating "we were throttled" as "no level data"
            # would let a stocked item read as dead (issue #37).
            rate_limited.append({
                "sku": sku,
                "error": f"stock levels unavailable: {exc}",
                "note": "ageing skipped — a missing level would fake an out-of-stock date",
            })
            continue
        except Exception:
            levels_by_loc = {}

        per_location = []
        for lid, lname in targets:
            rows_raw, total, truncated = _fetch_change_history_rows(
                stock_item_id, lid, max_pages
            )
            calls += max(1, -(-len(rows_raw) // 200))
            rows = [_format_change_row(r) for r in rows_raw]
            summary = _summarise_change_history(
                rows, levels_by_loc.get(lid), truncated
            )
            entry = {
                "location_id": lid,
                "location_name": lname,
                "total_entries": total,
                "rows_read": len(rows),
                "history_truncated": truncated,
                **summary,
            }
            if include_movements:
                shown = _apply_filter(rows)
                entry["movements"] = shown[:max_movements]
                entry["movements_truncated"] = len(shown) > max_movements
                entry["movements_matching_filter"] = len(shown)
            per_location.append(entry)

        record: dict = {"sku": sku, "stock_item_id": stock_item_id}

        if all_locations:
            active = [p for p in per_location if p["total_entries"] or p["current_level"]]
            all_zero = all((p["current_level"] or 0) == 0 for p in per_location)
            zero_dates = [p["out_of_stock_since"] for p in active if p["out_of_stock_since"]]
            never_stocked = all_zero and not zero_dates
            record.update({
                "scope": "all_locations",
                "zero_at_all_locations": all_zero,
                # The LATEST per-location zero date: every location has been at
                # zero only since the last one of them hit zero.
                "zero_at_all_locations_since": max(zero_dates) if (all_zero and zero_dates) else None,
                "never_stocked_anywhere": never_stocked,
                "locations_with_history": len([p for p in per_location if p["total_entries"]]),
                "locations": [p for p in per_location if p["total_entries"] or (p["current_level"] or 0)],
            })
        else:
            record.update({"scope": "location", **per_location[0]})

        results.append(record)

    return {
        "count": len(results),
        "unresolved_count": len(unresolved),
        "rate_limited_count": len(rate_limited),
        "complete": not rate_limited,
        "scope": "all_locations" if all_locations else "location",
        "change_source_filter": {
            "include": sorted(include_set) if include_set else None,
            "exclude": sorted(exclude_set) if exclude_set else None,
            "note": (
                "Filters apply to `movements` and last_real_movement_date only — never "
                "to the level series behind out_of_stock_since."
            ),
        },
        "automated_sources": list(_AUTOMATED_CHANGE_SOURCES),
        "api_calls": calls,
        "results": results,
        "unresolved": unresolved,
        # Transient quota failures — retry these; they are NOT missing items.
        "rate_limited": rate_limited,
    }


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
        except RateLimitError:
            # A quota failure here silently drops suppliers from the map, which
            # shows up downstream as revenue landing in the "No Supplier" bucket —
            # an under-attribution that looks like real data. Fail loudly instead
            # of quietly skewing a supplier report (issue #37).
            raise
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

    ⚠️  A rate-limited call is NOT a missing SKU. RateLimitError is re-raised
    unchanged rather than being folded into the "not found" ValueError — the
    old behaviour turned a transient quota failure into a factual claim that
    the item didn't exist, which callers then acted on (issue #34).

    Args:
        sku:   The exact SKU / item number.
        cache: Optional dict for within-call deduplication.  If provided,
               already-resolved SKUs are returned from cache without an API call.
    """
    if cache is not None and sku in cache:
        return cache[sku]
    try:
        item = call_linnworks("Inventory/GetInventoryItem", {"sku": sku})
    except RateLimitError:
        raise
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
    Create a variation group in Linnworks, linking a NEW variation parent to
    its existing child items.

    A variation group connects items that are variants of the same product
    (e.g. a T-shirt in different sizes/colours). The parent is the variation
    template that carries the group; the child SKUs are the individual sellable
    variants.

    ⚠️ The `parent_sku` is a NEW variation SKU that Linnworks MINTS as it
    creates the group — it must NOT already exist as a normal stock item.
    (Live-confirmed 28 Jul 2026: passing an existing item's SKU returns HTTP 400
    "Chosen variation SKU already exists as a normal stock item.") The child
    SKUs, by contrast, MUST already exist. To add more children to a group that
    already exists, use `add_variation_group_items` instead.

    Args:
        group_name: The name / title for the variation group.  [required]
        parent_sku: The NEW variation-parent SKU to create (must not already
            exist as a stock item or variation).  [required]
        child_skus: List of EXISTING SKUs that are children (variants) of the
            parent. The parent SKU is not included here.  [required]
        dry_run: If True (default), validates the parent is free + children
            exist and shows what would be created without writing. Set to False
            to execute.

    Returns:
        A dict with:
          - dry_run:          whether this was a dry run
          - group_name:       the requested group name
          - parent_sku:       the new parent variation SKU
          - child_skus:       the child SKUs
          - pk_variation_item_id: the minted group id (live run)
          - child_ids:        resolved child StockItemIds
          - status:           "dry_run", "created", "already_exists", or "error"
          - message:          human-readable outcome
    """
    _check_injection("group_name", group_name)

    # ── The parent SKU must be a NEW variation SKU, not an existing item ───────
    try:
        parent_state = call_linnworks_get(
            "Stock/CheckVariationParentSKUExists", {"parentSKU": parent_sku}
        )
    except RuntimeError:
        parent_state = None
    if parent_state and str(parent_state).strip() != "NotExists":
        hint = (
            "it is already a variation parent — use add_variation_group_items "
            "to add children to it"
            if str(parent_state).strip() == "AlreadyVariation"
            else "it already exists as a normal stock item; the variation "
                 "parent SKU must be a brand-new SKU"
        )
        return {
            "dry_run": dry_run, "group_name": group_name,
            "parent_sku": parent_sku, "child_skus": child_skus,
            "status": "error",
            "message": f"Cannot use '{parent_sku}' as the variation parent — {hint}.",
            "parent_sku_state": str(parent_state).strip(),
        }

    # ── Resolve child SKUs (they MUST already exist) ──────────────────────────
    sku_cache: dict[str, str] = {}
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

    # ── Check if a group with this name already exists ────────────────────────
    existing_group = _find_variation_group_by_name(group_name)
    if existing_group:
        return {
            "dry_run":    dry_run,
            "group_name": group_name,
            "parent_sku": parent_sku,
            "child_skus": child_skus,
            "status":     "already_exists",
            "message": (
                f"A variation group named '{group_name}' already exists "
                f"in Linnworks (parent '{existing_group.get('VariationSKU')}'). "
                f"No new group was created — use add_variation_group_items to "
                f"add children to it."
            ),
            "existing_group": existing_group,
        }

    base = {
        "dry_run":    dry_run,
        "group_name": group_name,
        "parent_sku": parent_sku,
        "child_skus": child_skus,
        "child_ids":  child_ids,
    }

    if dry_run:
        return {
            **base,
            "status":  "dry_run",
            "message": (
                f"Dry run — would create variation group '{group_name}' with a "
                f"new parent '{parent_sku}' and {len(child_skus)} existing "
                f"child(ren). Set dry_run=False to create."
            ),
        }

    # ── Create the group (Linnworks mints the parent StockItemId; pass the ────
    #    zero-GUID and read the minted id back from the response) ──────────────
    created = call_linnworks(
        "Stock/CreateVariationGroup",
        {
            "template": {
                "VariationGroupName": group_name,
                "ParentSKU":          parent_sku,
                "ParentStockItemId":  "00000000-0000-0000-0000-000000000000",
                "VariationItemIds":   child_ids,
            }
        },
    )
    pk = created.get("pkVariationItemId") if isinstance(created, dict) else None

    # Read back the members to confirm
    members = []
    if pk:
        members = call_linnworks_get(
            "Stock/GetVariationItems", {"pkVariationItemId": pk}
        ) or []

    return {
        **base,
        "status":              "created",
        "pk_variation_item_id": pk,
        "confirmed_group":     created,
        "member_count":        len(members),
        "message": (
            f"Variation group '{group_name}' created with new parent "
            f"'{parent_sku}' and {len(child_ids)} child item(s)."
        ),
    }


@mcp.tool()
def add_variation_group_items(
    child_skus: list[str],
    parent_sku: str | None = None,
    group_name: str | None = None,
    dry_run: bool = True,
) -> dict:
    """
    Add one or more child SKUs to an EXISTING variation group.

    The counterpart to `create_variation_group`, which is create-only: it
    no-ops the moment a group with that name exists and has no code path that
    would ever attach a new child. Use THIS tool to link an extra size/colour
    into a group that already exists.

    Why this is needed: supplier feeds add new variants over time (e.g. a
    product launches S/M/L, the supplier adds XS weeks later). The CSV import
    creates the new child item but does not reliably re-link it into an
    already-live group — it stays orphaned (an item, not a member) until
    someone opens the product in the Linnworks UI and revises the group. This
    tool is the API path to do that link. Detect the gap first with
    `get_item_relationships` (compare the group's live children against the
    expected set), then close it here.

    Identify the group by EITHER `parent_sku` OR `group_name` (at least one is
    required). If both are given they must resolve to the same group or the
    call errors. The group id used by Linnworks is the PARENT item's
    StockItemId (== the group's `pkVariationItemId`, confirmed issue #17).

    Idempotent: only child SKUs not already in the group are added. SKUs
    already linked are reported under `already_present` and skipped, so it is
    safe to re-run with the full expected child set. A child SKU that cannot be
    resolved becomes a row in `child_errors` and does NOT abort the others.

    Args:
        child_skus: SKUs to add as children of the group. The parent SKU should
            not be included. [required]
        parent_sku: SKU of the variation parent identifying the group.
        group_name: Name of the variation group identifying it (alternative or
            complement to parent_sku).
        dry_run: If True (default), resolves SKUs, diffs against the current
            members and shows what would be added without writing. Set to False
            to execute.

    Returns:
        A dict with:
          - dry_run:               whether this was a dry run
          - status:                "dry_run", "added", "no_op", or "error"
          - group_name:            the resolved group name
          - parent_sku:            the resolved parent SKU
          - pk_variation_item_id:  the group id (parent StockItemId)
          - current_members:       SKUs already in the group before the write
          - to_add:                child SKUs that would be / were added
          - already_present:       requested SKUs already in the group (skipped)
          - child_errors:          [{sku, error}] for SKUs that failed to resolve
          - added:                 (live run) read-back per SKU {sku, added: bool}
          - message:               human-readable outcome
    """
    if group_name:
        _check_injection("group_name", group_name)

    if not parent_sku and not group_name:
        return {
            "dry_run": dry_run, "status": "error",
            "message": "Provide at least one of parent_sku or group_name to "
                       "identify the existing variation group.",
        }

    sku_cache: dict[str, str] = {}

    # ── Resolve the target group → pk_variation_item_id + parent_sku + name ────
    pk_variation_item_id: str | None = None
    resolved_parent_sku: str | None = None
    resolved_group_name: str | None = None

    if parent_sku:
        try:
            parent_id = _resolve_sku_to_id(parent_sku, sku_cache)
        except ValueError as exc:
            return {
                "dry_run": dry_run, "status": "error",
                "parent_sku": parent_sku, "group_name": group_name,
                "message": f"Parent SKU resolution failed: {exc}",
            }
        group = call_linnworks_get(
            "Stock/GetVariationGroupByParentId", {"pkStockItemId": parent_id}
        )
        if not group or not group.get("pkVariationItemId"):
            return {
                "dry_run": dry_run, "status": "error",
                "parent_sku": parent_sku, "group_name": group_name,
                "message": (
                    f"'{parent_sku}' is not a variation parent (no existing "
                    f"group hangs off it). Use create_variation_group to make a "
                    f"new group, or pass the correct parent SKU."
                ),
            }
        pk_variation_item_id = group.get("pkVariationItemId")
        resolved_parent_sku = parent_sku
        resolved_group_name = group.get("VariationGroupName")

        # If a group_name was ALSO supplied, it must match the parent's group.
        if group_name and (resolved_group_name or "").strip().lower() != group_name.strip().lower():
            return {
                "dry_run": dry_run, "status": "error",
                "parent_sku": parent_sku, "group_name": group_name,
                "message": (
                    f"parent_sku '{parent_sku}' belongs to group "
                    f"'{resolved_group_name}', which does not match the "
                    f"group_name '{group_name}' you supplied."
                ),
            }
    else:
        # Identify by name only (GetVariationGroupByName is unreliable — use
        # the SearchVariationGroups-backed exact-match helper).
        existing = _find_variation_group_by_name(group_name)
        if not existing or not existing.get("pkVariationItemId"):
            return {
                "dry_run": dry_run, "status": "error",
                "group_name": group_name,
                "message": (
                    f"No variation group named '{group_name}' exists in "
                    f"Linnworks. Use create_variation_group to make it."
                ),
            }
        pk_variation_item_id = existing.get("pkVariationItemId")
        resolved_parent_sku = existing.get("VariationSKU")
        resolved_group_name = existing.get("VariationGroupName")

    # ── Read current members (read-before-write) ──────────────────────────────
    members = call_linnworks_get(
        "Stock/GetVariationItems", {"pkVariationItemId": pk_variation_item_id}
    ) or []
    current_member_skus = [
        (m.get("ItemNumber") or m.get("SKU") or "") for m in members
    ]
    current_lower = {s.strip().lower() for s in current_member_skus if s}

    # ── Diff requested children against current members ───────────────────────
    to_add: list[str] = []
    to_add_ids: list[str] = []
    already_present: list[str] = []
    child_errors: list[dict] = []
    seen_in_request: set[str] = set()

    for sku in child_skus:
        key = (sku or "").strip().lower()
        if not key or key in seen_in_request:
            continue  # skip blanks and duplicates within the request
        seen_in_request.add(key)
        if key in current_lower:
            already_present.append(sku)
            continue
        try:
            to_add_ids.append(_resolve_sku_to_id(sku, sku_cache))
            to_add.append(sku)
        except ValueError as exc:
            child_errors.append({"sku": sku, "error": str(exc)})

    base = {
        "dry_run":              dry_run,
        "group_name":           resolved_group_name,
        "parent_sku":           resolved_parent_sku,
        "pk_variation_item_id": pk_variation_item_id,
        "current_members":      current_member_skus,
        "to_add":               to_add,
        "already_present":      already_present,
        "child_errors":         child_errors,
    }

    # ── Nothing to add ────────────────────────────────────────────────────────
    if not to_add:
        msg_bits = []
        if already_present:
            msg_bits.append(f"{len(already_present)} already in the group")
        if child_errors:
            msg_bits.append(f"{len(child_errors)} could not be resolved")
        detail = "; ".join(msg_bits) if msg_bits else "no valid children supplied"
        return {
            **base,
            "status": "no_op" if not child_errors else "error",
            "message": (
                f"No children to add to '{resolved_group_name}' ({detail})."
            ),
        }

    # ── Dry run ───────────────────────────────────────────────────────────────
    if dry_run:
        return {
            **base,
            "status": "dry_run",
            "message": (
                f"Dry run — would add {len(to_add)} child(ren) to variation "
                f"group '{resolved_group_name}' "
                f"({len(already_present)} already present, "
                f"{len(child_errors)} unresolved). Set dry_run=False to add."
            ),
        }

    # ── Add the missing children ──────────────────────────────────────────────
    call_linnworks(
        "Stock/AddVariationItems",
        {
            "pkVariationItemId": pk_variation_item_id,
            "pkStockItemIds":    to_add_ids,
        },
    )

    # ── Read back to confirm each requested add actually landed ───────────────
    after = call_linnworks_get(
        "Stock/GetVariationItems", {"pkVariationItemId": pk_variation_item_id}
    ) or []
    after_lower = {
        (m.get("ItemNumber") or m.get("SKU") or "").strip().lower()
        for m in after
    }
    added = [
        {"sku": sku, "added": (sku or "").strip().lower() in after_lower}
        for sku in to_add
    ]
    added_ok = sum(1 for a in added if a["added"])

    return {
        **base,
        "status":          "added",
        "added":           added,
        "member_count_after": len(after),
        "message": (
            f"Added {added_ok}/{len(to_add)} child(ren) to variation group "
            f"'{resolved_group_name}'. "
            f"{len(already_present)} were already present"
            + (f", {len(child_errors)} could not be resolved" if child_errors else "")
            + "."
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

# ── GLT channel registry (issue #30) ──────────────────────────────────────────
#
# The GLT is the ONLY listing-management surface in the public API, and it only
# covers the channels in its ChannelType enum. `channel_name` is the uppercase
# *Source* string (same convention as Shopify's "SHOPIFY"), and `source` is what
# that channel's rows carry in the channel-SKU link table — they match for every
# channel confirmed live here, but they are kept separate because they are
# conceptually different fields.
#
# Live-probed on this tenant (5 Aug 2026, GetConfiguratorsInfoPaged per type):
#   Shopify 67 configurators (per-store ChannelId: 18 SWH / 21 Venom / 26 Icarus
#                             / 29 Lobster / 34 TWG B2B)
#   Amazon  10 configurators (ONE account — ChannelId 2, SubSource
#                             "The Warehouse Group")
#   TikTok   5 configurators (ChannelId 30, SubSource "SKATEWAREHOUSE_UK")
#   Magento  0 · Walmart 0  → valid ChannelTypes, but nothing GLT-managed here.
#   eBay / Etsy → HTTP 400 "Invalid parameter request" (NOT GLT channels)
#   External → 400 "Failed to create a channel"; CDiscount_OBSOLETE → 400 null ref
#
# ⚠️  AMAZON REGION SHAPE — the one structural difference from Shopify.
# Shopify is 1:1 (each store = its own ChannelId AND its own channel-SKU
# SubSource). Amazon is 1:many: a single account (ChannelId 2, SubSource
# "The Warehouse Group") fronts SEVEN regional SubSources in the channel-SKU
# table — "The Warehouse Group", "… - Germany", "… - France", "… - Italy",
# "… - Spain", "… - Netherlands", "… - Sweden". So a regional sub_source has NO
# configurator of its own and must resolve to the account's ChannelId — see
# _resolve_glt_target()'s account-prefix fallback.
# "revise_attempted" (issue #45) is the tri-state companion to "revise_proven":
# a channel can be (a) never attempted (revise_attempted=False, revise_proven=
# False — TikTok/Magento/Walmart), (b) attempted live and shown ineffective
# (revise_attempted=True, revise_proven=False — Amazon, see below), or
# (c) attempted and proven (revise_attempted=True, revise_proven=True —
# Shopify). Without this field "not proven" reads as "untried", which was
# false for Amazon after 24-25 Aug 2026 and made the tool's own warnings less
# accurate than the evidence already sitting in CLAUDE.md.
GLT_CHANNELS: dict[str, dict] = {
    "shopify": {"channel_type": "Shopify", "channel_name": "SHOPIFY", "source": "SHOPIFY",
                "delete_proven": True, "revise_proven": True, "revise_attempted": True},
    # Amazon Delete LIVE-PROVEN 5 Aug 2026 on MOB-GRP-3080 (template 31703, ASIN
    # B0B311TFG3): ProcessTemplates Delete → 2xx, the AMAZON channel-SKU row and
    # the template both disappeared, eBay/Shopify rows and stock untouched. NB it
    # succeeded even though the template read NextSuggestedAction:"NotAllowed" —
    # that flag describes the SUGGESTED action, it does not gate a forced Delete.
    # ⚠️  Revise/Update (refresh_channel_listing) has been FIRED LIVE ON AMAZON
    # TWICE (issue #45, 24-25 Aug 2026) and is NOT merely untried — it is TRIED
    # AND SHOWN INEFFECTIVE:
    #  • vnm_bushings_con_80a.FBA (template 32064, 24 Aug ~20:10Z) — title push;
    #    the observable itself turned out invalid (a variation child's displayed
    #    title is parent/contribution-driven, so it can't prove a revise either
    #    way) but the push was accepted (processed:true) and polled unchanged
    #    for 12h regardless.
    #  • vnm_artdeck_8.0 (template 32239, ASIN B08QW9NJ2Y, 25 Aug ~08:12Z) — a
    #    price push on an MFN standalone listing, a seller-feed-controlled field
    #    immune to the variation/contribution merging that invalidated the first
    #    proof. ProcessTemplates accepted it (processed:true) and the offer price
    #    NEVER MOVED off its pre-push value across an hour of 3-minute polling.
    #  No Linnworks-side error surfaced on any readable surface (template status
    #  stayed "Listed", LastModificationTime didn't advance — no rebuild), and
    #  Seller Central carries no feed log for either push (Linnworks submits
    #  under its own SP-API app). See CLAUDE.md's v1.47.1 entry and issue #45 for
    #  the full write-up. `revise_proven` stays False — accepted-with-no-effect
    #  is not proof of a working route, and flipping it would need a push that
    #  demonstrably DID land. The still-open question is whether the defect is
    #  in this tool's API call or in Linnworks' own Amazon integration; that
    #  needs a manual GLT-UI revise on the same listing and/or a Linnworks
    #  support ticket, both external to this repo (see the issue's post-merge
    #  verification steps). Amazon also still carries hundreds of templates of
    #  unknown age with no API route to rebuild a stale one (#27) — if the route
    #  is ever fixed, the same bulk call would revert live content the way the
    #  Shopify catnip push did (v1.27.1); the current inertness is not safety.
    "amazon":  {"channel_type": "Amazon",  "channel_name": "AMAZON",  "source": "AMAZON",
                "delete_proven": True, "revise_proven": False, "revise_attempted": True},
    # TikTok Delete LIVE-PROVEN 7 Aug 2026 on ven-20-black-raw-core-complete-7.5
    # (template 30006, configurator 112, listing 1729486505080953421): the TIKTOK
    # channel-SKU row vanished, OpenTemplatesByInventory on ChannelId 30 went 1 → 0,
    # and the Amazon/eBay/2× Shopify rows plus stock were untouched. Two notes:
    #  • The template read Status "Errors while updating" and NextSuggestedAction
    #    "Update"; a forced Delete worked anyway — same lesson as Amazon's
    #    "NotAllowed", these fields do not gate a Delete.
    #  • The item is a variation CHILD that held its OWN template. On TikTok the
    #    template hangs off the child, NOT off the variation parent as on Shopify
    #    (#26) — so a TikTok child usually needs no whole-group gate at all. The
    #    gate still fires correctly for the children that genuinely have no
    #    template (live-confirmed on vnm-triplepads-yellowblack-jnr).
    # Revise/Update has NEVER been attempted live on TikTok (issue #45 tried
    # Amazon only) — genuinely untried, not merely unproven; do not conflate
    # with Amazon's tried-and-ineffective state above.
    "tiktok":  {"channel_type": "TikTok",  "channel_name": "TIKTOK",  "source": "TIKTOK",
                "delete_proven": True, "revise_proven": False, "revise_attempted": False},
    "magento": {"channel_type": "Magento", "channel_name": "MAGENTO", "source": "MAGENTO",
                "delete_proven": False, "revise_proven": False, "revise_attempted": False},
    "walmart": {"channel_type": "Walmart", "channel_name": "WALMART", "source": "WALMART",
                "delete_proven": False, "revise_proven": False, "revise_attempted": False},
}

# Channels seen in this tenant's channel-SKU table that the GLT cannot touch at
# all — no configurators, no GLT templates. They can only be CREATED, DELETED or
# RELISTED in that channel's own admin, so "delisted everywhere" is never true
# for them and they are always reported under `skipped_channels`.
#
# ⚠️  eBay is a partial exception (issue #43, 25 Aug 2026): it has NO GLT
# templates (still true — GetConfiguratorsInfoPaged 400s for it, see above), but
# it DOES have its own dedicated, non-GLT Listings/ family
# (GeteBayConfigurators / GeteBayTemplates / ProcesseBayListings — see
# `revise_ebay_listing_description`) that can REVISE an existing listing's
# description. That is the one eBay write this server supports; create/end/
# relist remain out of reach here. Mirakl/Etsy/CDiscount are unaffected by this
# — still fully manual, not re-probed by issue #43.
NON_GLT_SOURCES = ("EBAY", "MIRAKL MP", "ETSY", "CDISCOUNT")


def _proven_delete_channels() -> str:
    """Comma-joined list of channels whose Delete is live-proven, for warnings.

    Derived from GLT_CHANNELS so a warning can never go stale the way the
    hard-coded "only Shopify is" did — it survived both the Amazon (v1.32.0) and
    TikTok (v1.42.0) proofs while claiming neither had happened.
    """
    proven = sorted(c["channel_type"] for c in GLT_CHANNELS.values() if c["delete_proven"])
    return ", ".join(proven) if proven else "none"


def _proven_revise_channels() -> str:
    """Comma-joined list of channels whose Revise/Update is live-proven, for warnings.

    Derived from GLT_CHANNELS (issue #42) for the same reason as
    `_proven_delete_channels()`: a hard-coded "only Shopify is" string cannot be
    trusted to stay true once someone proves another channel — that mistake has
    already happened once in this codebase (see that function's docstring).
    """
    proven = sorted(c["channel_type"] for c in GLT_CHANNELS.values() if c["revise_proven"])
    return ", ".join(proven) if proven else "none"


# eBay's own (non-GLT) revise capability (issue #43) — see
# `revise_ebay_listing_description` and the NON_GLT_SOURCES note above. Kept as
# a registry, like GLT_CHANNELS, rather than a bare bool, so a warning message
# can always be *derived* rather than hard-coded (the mistake this codebase has
# already made and fixed twice for GLT_CHANNELS — see `_proven_delete_channels`).
#
# Issue #47 (26 Aug 2026): the live push was fired for the first time and
# turned out to be ACCEPTED by Linnworks but not observed to reach the
# channel — a distinct fact from `revise_proven`, which only answers "has a
# full round-trip been confirmed?". `push_observed_state` /
# `push_observed_reason` record that distinction as DATA, not prose, so every
# message DERIVES it instead of a hand-typed string drifting out of sync (the
# same twice-fixed lesson as `_proven_delete_channels`/`_proven_revise_channels`
# above). `push_observed_reason` is the ONLY place its literal text may be
# written — every caller below interpolates it via `_ebay_push_observation_
# reason()`, never retypes it. This is subordinate evidence, not a second
# proof flag: `revise_proven` alone remains the single gate a caller trusts.
#
# #45 conflict decision (recorded here, not acted on elsewhere): Amazon's GLT
# Revise is in the identical situation — accepted, no observable channel
# effect, `revise_proven` False on evidence rather than caution. Rather than
# extend `GLT_CHANNELS` in THIS eBay-only change (out of scope — see the
# brief's Conflicts section), these two field names are deliberately generic,
# not eBay-specific, so issue #45 can add the SAME two keys to each
# `GLT_CHANNELS` entry VERBATIM and the two registries stay one shape.
# `GLT_CHANNELS` itself is untouched by this change.
EBAY_CHANNELS: dict[str, dict] = {
    "ebay": {
        "source": "EBAY",
        "revise_proven": False,
        "push_observed_state": "accepted_but_not_processed",
        "push_observed_reason": (
            "A live push was submitted against a real eBay listing on 26 Aug 2026 "
            "(ven-grip-cleaner, item 286493672322) and accepted by Linnworks (2xx), "
            "but across a two-hour polling window on the description-frame URL no "
            "channel-side effect was observed; the identical edit pushed from the "
            "Linnworks listing UI landed within minutes. This records what was "
            "observed on that one listing at that time -- it is not a proven rule "
            "about the API path. revise_proven remains the single gate and stays "
            "False until a fresh proof lands."
        ),
    },
}

# The route that DOES work today, appended to every message that also carries
# the observation reason above (issue #47, AC3) — kept as its own constant
# rather than folded into `push_observed_reason` so the registry's reason
# field stays a pure description of what was observed, not instructions.
EBAY_UI_WORKAROUND_NOTE = (
    "The route that works today for propagating an eBay description edit is "
    "the Linnworks listing UI -- review this tool's dry-run manifest first to "
    "see what would be sent, then make the same edit there."
)


def _ebay_push_observation_reason() -> str:
    """The single read site for `push_observed_reason` (issue #47) -- every
    message below interpolates this, never retypes the literal text, so a
    correction to the registry entry is a one-line change everywhere it's
    surfaced (the same discipline `_proven_delete_channels` established)."""
    return EBAY_CHANNELS["ebay"]["push_observed_reason"]


def _proven_ebay_revise_channels() -> str:
    """Comma-joined list of eBay accounts whose description-revise is live-proven."""
    proven = sorted(c["source"] for c in EBAY_CHANNELS.values() if c["revise_proven"])
    return ", ".join(proven) if proven else "none"


def _resolve_glt_channel(channel: str) -> dict:
    """Map a channel name ("Shopify", "amazon", "AMAZON"…) to its GLT identity.

    Returns {key, channel_type, channel_name, source, delete_proven}.
    Raises ValueError naming the supported channels for anything else — notably
    eBay / Etsy / Mirakl, which are not GLT channels at all. This is a GLT-only
    refusal — eBay has its OWN separate revise route outside the GLT, see
    `revise_ebay_listing_description` (issue #43).
    """
    key = _norm_conf_name(channel).replace(" ", "")
    entry = GLT_CHANNELS.get(key)
    if entry is None:
        raise ValueError(
            f"'{channel}' is not a GLT-managed channel. Supported: "
            f"{sorted(c['channel_type'] for c in GLT_CHANNELS.values())}. "
            f"Channels like {', '.join(NON_GLT_SOURCES)} have no GLT templates, so they cannot "
            "be revised or deleted through this GLT machinery — they must be managed through "
            "that channel's own admin, or (eBay only) through "
            "revise_ebay_listing_description for description revises."
        )
    return {"key": key, **entry}


def _glt_field(info: dict, key: str):
    """Unwrap a GLT ConfiguratorsInfo field.

    Each field comes wrapped as {"Type": "...", "Value": <x>, "Errors": [...]}.
    Returns the inner Value, or the raw field if it isn't wrapped.
    """
    f = info.get(key)
    if isinstance(f, dict) and "Value" in f:
        return f.get("Value")
    return f


def _fetch_glt_configurators(channel: str = "Shopify") -> list[dict]:
    """Fetch all GLT configurators for one channel in this tenant.

    Calls GenericListings/GetConfiguratorsInfoPaged with the channel's
    ChannelType + ChannelName (the uppercase Source string). Returns a flat list
    of normalized dicts: {id, name, channel_id, sub_source, show_in_inventory}.

    Confirmed live — Shopify 67 (18 Jun 2026), Amazon 10 / TikTok 5 / Magento 0 /
    Walmart 0 (5 Aug 2026, issue #30). A single page of 1000 covers it; tenants
    with >1000 configurators would need pagination.
    """
    ch = _resolve_glt_channel(channel)
    resp = call_linnworks(
        "GenericListings/GetConfiguratorsInfoPaged",
        {"request": {
            "ChannelType": ch["channel_type"],
            "ChannelName": ch["channel_name"],
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


def _fetch_shopify_configurators() -> list[dict]:
    """Shopify-scoped alias of _fetch_glt_configurators (list_to_shopify /
    refresh_channel_listing are Shopify-only by design)."""
    return _fetch_glt_configurators("Shopify")


def _norm_conf_name(name: str | None) -> str:
    """Normalize a configurator name for case/space-insensitive matching."""
    return (name or "").strip().lower()


def _glt_channel_id_for(
    ss_to_channel: dict[str, int], sub_source: str
) -> tuple[int | None, str | None]:
    """Pick the ChannelId to open templates with for one sub_source.

    `ss_to_channel` maps normalized configurator SubSource → ChannelId. Returns
    (channel_id, resolution) where resolution is "exact" or "account-prefix", or
    (None, None) if the sub_source doesn't belong to this channel.

    The prefix rule exists for Amazon: the channel-SKU table carries regional
    sub-sources ("The Warehouse Group - Germany") that have no configurator of
    their own and must resolve to their account ("The Warehouse Group",
    ChannelId 2). It is anchored to this channel's own account names, so a
    sub_source belonging to a different channel still resolves to nothing.
    """
    want = _norm_conf_name(sub_source)
    cid = ss_to_channel.get(want)
    if cid is not None:
        return cid, "exact"
    # Longest account prefix wins, so nested account names can't cross-match.
    best: tuple[int, str] | None = None
    for acct, acct_cid in ss_to_channel.items():
        if acct and want.startswith(acct) and len(want) > len(acct):
            if best is None or len(acct) > len(best[1]):
                best = (acct_cid, acct)
    if best is not None:
        return best[0], "account-prefix"
    return None, None


def _resolve_glt_target(channel: str, sub_source: str) -> dict:
    """Resolve (channel, sub_source) → the ChannelId to open templates with.

    Shopify is 1:1 — every store has its own configurator SubSource + ChannelId,
    so an exact match is expected. Amazon is 1:many — ONE account configurator
    ("The Warehouse Group", ChannelId 2) fronts several regional channel-SKU
    SubSources ("The Warehouse Group - Germany", …), which have no configurator
    of their own. So an exact miss falls back to the account whose SubSource the
    requested one is a prefix of, and the result records how it resolved.

    Returns {ok, channel, channel_id, resolution, available_sub_sources, error}.
    `resolution` is "exact" or "account-prefix" (or None on failure). A stray
    sub_source from a DIFFERENT channel never resolves — the prefix fallback is
    anchored to this channel's own account names, not to "there is only one id".
    """
    ch = _resolve_glt_channel(channel)
    catalogue = _fetch_glt_configurators(ch["channel_type"])
    available = sorted({c["sub_source"] for c in catalogue if c.get("sub_source")})

    ss_to_channel: dict[str, int] = {}
    for c in catalogue:
        ss, cid = c.get("sub_source"), c.get("channel_id")
        if ss and cid is not None:
            ss_to_channel.setdefault(_norm_conf_name(ss), cid)

    base = {"channel": ch, "available_sub_sources": available}
    cid, resolution = _glt_channel_id_for(ss_to_channel, sub_source)
    if cid is not None:
        return {**base, "ok": True, "channel_id": cid, "resolution": resolution}

    if not available:
        err = (
            f"{ch['channel_type']} has no GLT configurators in this tenant — nothing on "
            f"{ch['channel_type']} is GLT-managed here, so its listings cannot be taken down "
            "via the API."
        )
    else:
        err = (
            f"sub_source '{sub_source}' is not a known {ch['channel_type']} account/store in "
            f"this tenant. Available: {available}"
        )
    return {**base, "ok": False, "channel_id": None, "resolution": None, "error": err}


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


def _resolve_bulk_inputs(
    skus: list[str] | None,
    stock_item_ids: list[str] | None,
) -> tuple[list[tuple[str, str]], dict[str, str], list[dict], list[dict]]:
    """
    Shared front-end for the bulk read tools: turn SKUs and/or StockItemIds into
    (resolved, titles, unresolved, rate_limited).

    Two things this fixes (issue #34):

    1. **StockItemIds skip resolution entirely.** The batched read underneath is
       one call per 200 items; the per-SKU `GetInventoryItem` loop in front of it
       was the real bottleneck, self-limiting these tools to ~150 SKUs/min. Any
       caller chaining from `list_inventory_items` / `find_composite_parents`
       already holds the ids — passing them turns thousands of calls into a
       handful.
    2. **A rate-limited SKU is NOT an unresolved SKU.** 429s go to their own
       `rate_limited` bucket. `unresolved` keeps its original meaning: the
       catalogue says no.
    """
    resolved: list[tuple[str, str]] = []
    titles: dict[str, str] = {}
    unresolved: list[dict] = []
    rate_limited: list[dict] = []

    for raw in (stock_item_ids or []):
        sid = (raw or "").strip()
        if not sid:
            unresolved.append({"stock_item_id": raw, "error": "empty stock_item_id"})
            continue
        resolved.append((sid, sid))   # no SKU known; the id doubles as the label

    for raw in (skus or []):
        s = (raw or "").strip()
        if not s:
            unresolved.append({"sku": raw, "error": "empty SKU"})
            continue
        try:
            item = call_linnworks("Inventory/GetInventoryItem", {"sku": s})
        except RateLimitError as exc:
            # Transient quota failure — say so. Never "not found".
            rate_limited.append({"sku": s, "error": str(exc)})
            continue
        except RuntimeError as exc:
            unresolved.append({"sku": s, "error": f"not found: {exc}"})
            continue
        sid = item.get("StockItemId")
        if not sid:
            unresolved.append({"sku": s, "error": "found but returned no StockItemId"})
            continue
        resolved.append((s, sid))
        titles[sid.lower()] = item.get("ItemTitle")

    return resolved, titles, unresolved, rate_limited


@mcp.tool()
def get_channel_listings_bulk(
    skus: list[str] | None = None,
    stock_item_ids: list[str] | None = None,
) -> dict:
    """
    Read the existing channel listings for MANY inventory items at once — the
    batch dedupe companion to `list_to_shopify`.

    Reads the channel-SKU link table via Inventory/BatchGetInventoryItemChannelSKUs
    (one batched call per 200 items). Use before a bulk listing run to skip SKUs
    already live on a channel/store, or before a delist/archive sweep to find what
    is still live.

    ⚠️  PREFER `stock_item_ids` FOR LARGE BATCHES. Resolving SKUs costs one
    `GetInventoryItem` call each against a 150/min quota, so a few thousand SKUs
    will spend minutes in backoff; the batched read itself is ~1 call per 200.
    Anything chaining from `list_inventory_items` or `find_composite_parents`
    already has the ids. Measured: 5,391 items took 187s (and was wrong) by SKU
    vs 15.6s / 27 calls by id.

    ⚠️  `unresolved` means the catalogue has no such SKU. Rate-limited lookups go
    to a SEPARATE `rate_limited` list — they are transient and should be retried,
    NOT treated as missing items. Conflating the two previously under-reported
    live listings by 88% (issue #34).

    Args:
        skus: Exact SKUs / ItemNumbers to check.
        stock_item_ids: StockItemId GUIDs to check (skips resolution — faster and
            quota-free). Either or both may be given.

    Returns:
        A dict with:
          - item_count:     number of inputs requested
          - resolved_count: how many resolved to a stock item
          - listed_count / unlisted_count: split of resolved items by whether
            they have any channel listing
          - results: per-item rows (sku, stock_item_id, title, is_listed,
            listing_count, channels, sub_sources, listings) — same listing shape
            as get_channel_listings
          - unresolved: rows for inputs the catalogue does not have
          - rate_limited: rows that hit the quota; re-run these
          - complete: False when anything was rate-limited (the answer is partial)
    """
    if not skus and not stock_item_ids:
        raise ValueError("Provide at least one of skus or stock_item_ids.")

    resolved, titles, unresolved, rate_limited = _resolve_bulk_inputs(skus, stock_item_ids)

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
        "item_count":     len(skus or []) + len(stock_item_ids or []),
        "resolved_count": len(resolved),
        "listed_count":   listed_count,
        "unlisted_count": len(resolved) - listed_count,
        "results":        results,
        "unresolved":     unresolved,
        "rate_limited":   rate_limited,
        "complete":       not rate_limited,
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
def get_inventory_item_images_bulk(
    skus: list[str] | None = None,
    stock_item_ids: list[str] | None = None,
) -> dict:
    """
    Read the images for MANY inventory items at once — the batch pre-listing
    image gate, mirroring `get_channel_listings_bulk`.

    Batch-reads the image table via Inventory/GetImagesInBulk (one batched call
    per 200 items). An item that exists but has zero images is reported with
    has_image=False (a real item to fix), distinct from an `unresolved` input
    (not in the catalogue).

    Use this before a bulk listing run to skip/flag SKUs with no image, so you
    only list the genuinely ready ones.

    ⚠️  PREFER `stock_item_ids` FOR LARGE BATCHES — resolving SKUs costs one
    `GetInventoryItem` call each against a 150/min quota, while the batched read
    is ~1 call per 200. See `get_channel_listings_bulk` (issue #34).

    ⚠️  `unresolved` means the catalogue has no such SKU; rate-limited lookups go
    to a separate `rate_limited` list and should be retried, never read as
    missing items.

    Args:
        skus: Exact SKUs / ItemNumbers to check.
        stock_item_ids: StockItemId GUIDs to check (skips resolution). Either or
            both may be given.

    Returns:
        A dict with:
          - item_count:        number of inputs requested
          - resolved_count:    how many resolved to a stock item
          - with_image_count / without_image_count: split of resolved items by
            whether they have any image
          - results: per-item rows (sku, stock_item_id, title, has_image,
            image_count, has_main_image, images) — same image shape as
            get_inventory_item_images
          - unresolved: rows for inputs the catalogue does not have
          - rate_limited: rows that hit the quota; re-run these
          - complete: False when anything was rate-limited (the answer is partial)
    """
    if not skus and not stock_item_ids:
        raise ValueError("Provide at least one of skus or stock_item_ids.")

    resolved, titles, unresolved, rate_limited = _resolve_bulk_inputs(skus, stock_item_ids)

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
        "item_count":         len(skus or []) + len(stock_item_ids or []),
        "resolved_count":     len(resolved),
        "with_image_count":   with_image,
        "without_image_count": len(resolved) - with_image,
        "results":            results,
        "unresolved":         unresolved,
        "rate_limited":       rate_limited,
        "complete":           not rate_limited,
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


def _norm_title(title: str | None) -> str:
    """
    Normalise an ItemTitle for product-level duplicate matching (issue #38).

    Lowercase + collapse whitespace only. Deliberately NOT fuzzy: exact match on
    the normalised title caught all 177 duplicate pairs in the 6 Aug 2026
    incident, and anything looser would start blocking legitimately distinct
    variants (sizes, colours) whose titles differ by a word or two.
    """
    if not title:
        return ""
    return " ".join(str(title).split()).casefold()


@mcp.tool()
def list_to_shopify(
    skus: list[str],
    configurator: str | None = None,
    default_configurator: str | None = None,
    sub_source: str | None = None,
    allow_duplicate_titles: bool = False,
    known_listed_titles: list[str] | None = None,
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
          - already_listed: SKUs whose OWN item is already live on the target
            store (item-level dedupe) — excluded from CreateTemplates
          - possible_duplicates: SKUs whose TITLE matches a product already live
            on the target store under a DIFFERENT SKU (product-level dedupe,
            issue #38) — excluded unless allow_duplicate_titles=True
          - unresolved: per-SKU error rows (not found / no configurator decided /
            name not in catalogue / ambiguous across stores)
          - results: per-group outcome with created template ids and process
            status (live run only)

    ⚠️  TWO dedupe layers, because one wasn't enough:
      1. ITEM-level — is THIS StockItemId already listed on the target store?
      2. PRODUCT-level — is a DIFFERENT SKU with the same title already live?

    Layer 2 exists because layer 1 worked perfectly and still produced 177
    duplicate Shopify products on 6 Aug 2026: a category carried the same fins
    under an old word-suffix SKU scheme and a new code scheme, so the new SKUs
    were genuinely unlisted while the products were already for sale. Titles are
    compared normalised (lowercase, whitespace collapsed) and matched exactly.

    The comparison set is the batch's own `already_listed` rows — free, and the
    right answer when you pass a whole category. If your batch contains ONLY the
    new SKUs, pass `known_listed_titles` (e.g. titles from `list_inventory_items`
    for that category) or layer 2 has nothing to compare against.

    Args (beyond the selection args above):
        allow_duplicate_titles: If True, title-matched items are listed anyway
            and merely reported. Default False (they are excluded).
        known_listed_titles: Extra titles known to be live on the target store,
            for when the batch doesn't itself contain the already-listed SKUs.
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
    rate_limited: list[dict] = []

    for raw_sku in skus:
        sku = (raw_sku or "").strip()
        if not sku:
            unresolved.append({"sku": raw_sku, "error": "empty SKU"})
            continue

        # Resolve identity (StockItemId + title)
        try:
            item = call_linnworks("Inventory/GetInventoryItem", {"sku": sku})
        except RateLimitError as exc:
            # Quota failure — transient, and NOT a missing SKU (issue #37).
            rate_limited.append({"sku": sku, "error": str(exc)})
            continue
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

    # ── Product-level duplicate guard (issue #38) ─────────────────────────────
    # The dedupe above is ITEM-level: it asks "is THIS StockItemId already listed
    # on the target store?". It cannot see that a DIFFERENT SKU for the same
    # physical product is already live, because the two SKUs are separate
    # Linnworks items with separate StockItemIds.
    #
    # That is not hypothetical. On 6 Aug 2026 a bulk run over the Surfboard Fins
    # category created 177 duplicate product pairs on SWH Shopify: the category
    # carried the same fins under an old word-suffix SKU scheme and a new code
    # scheme, and 184 of the 265 newly created listings had a BYTE-IDENTICAL
    # ItemTitle to a listing already live on the same store. The item-level check
    # was working correctly and still produced the wrong outcome.
    #
    # So: compare normalised titles against everything known to be live on the
    # same store, and refuse the match by default. Sources of "known live":
    #   - the already_listed rows from this very batch (free — the common case,
    #     since callers usually pass a whole category), and
    #   - optional `known_listed_titles` supplied by the caller, for when the
    #     batch contains only the new SKUs and the old ones aren't in it.
    possible_duplicates: list[dict] = []
    if plan:
        live_titles: dict[str, list[str]] = {}
        for row in already_listed:
            key = _norm_title(row.get("title"))
            if key:
                live_titles.setdefault(key, []).append(row.get("sku"))
        for t in (known_listed_titles or []):
            key = _norm_title(t)
            if key:
                live_titles.setdefault(key, [])

        if live_titles:
            keep: list[dict] = []
            for row in plan:
                key = _norm_title(row.get("title"))
                match = live_titles.get(key) if key else None
                if match is None:
                    keep.append(row)
                    continue
                dup = {
                    **row,
                    "duplicate_of_skus": [s for s in match if s],
                    "matched_on": "normalised ItemTitle",
                    "reason": (
                        "a product with an identical title is already live on "
                        f"'{row['sub_source']}' under a different SKU — listing this would "
                        "create a second Shopify product for the same physical item"
                    ),
                }
                if allow_duplicate_titles:
                    dup["listed_anyway"] = True
                    keep.append(row)
                else:
                    dup["listed_anyway"] = False
                possible_duplicates.append(dup)
            if not allow_duplicate_titles:
                plan = keep

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
        "possible_duplicates":           possible_duplicates,
        "unresolved":                    unresolved,
        "rate_limited":                  rate_limited,
        "complete":                      not rate_limited,
    }
    if dedupe_warning:
        base_out["dedupe_warning"] = dedupe_warning
    if possible_duplicates and not allow_duplicate_titles:
        base_out["duplicate_warning"] = (
            f"{len(possible_duplicates)} SKU(s) share a title with a product already live on "
            "the target store under a DIFFERENT SKU and were EXCLUDED — see possible_duplicates. "
            "This is the SKU-migration case that created 177 duplicate Shopify products on "
            "6 Aug 2026. Pass allow_duplicate_titles=True only if you are sure they are "
            "genuinely different products."
        )

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
                f"{len(possible_duplicates)} SKU(s) share a title with a product already live "
                f"under a different SKU "
                f"({'listed anyway — allow_duplicate_titles=True' if allow_duplicate_titles else 'EXCLUDED'}, "
                "see possible_duplicates); "
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


# ── Pre-flight staleness check for a GLT template (issue #40) ─────────────────
#
# ProcessTemplates pushes the template's STORED snapshot, not the item's current
# data (#27). A push carrying a stale snapshot returns the same empty 2xx as a
# real revise, so `processed: true` has meant "accepted", never "changed
# anything" — live-proven 18 Aug 2026, when two BLT templates pushed cleanly and
# left Shopify byte-for-byte unchanged because the stored image URL had since
# been deleted.
#
# WHAT OpenTemplatesByInventory ACTUALLY EXPOSES (live-probed 19 Aug 2026 on
# tpl 52731 / 39076). Info carries real VALUES for Title, Price and
# LastModificationTime, but Images / Attributes / MetaFields / Description come
# back as Type:"Action" SUMMARIES — a count ("1", "21", "42") or the literal
# "Filled". So the snapshot can only be compared on:
#
#     title  ·  price (non-variation only)  ·  image COUNT
#
# and never on image content/URL, description body, attribute or metafield
# values. Three traps, each of which yields a WRONG verdict if ignored:
#
#   1. TITLE MUST BE COMPARED AGAINST THE EFFECTIVE CHANNEL TITLE, not the base
#      ItemTitle. The #40 items carry a channel-title override ("Zero Megadeath
#      …") that differs from the base title ("Zero Megadeth …"), and the template
#      correctly holds the override — comparing against the base would flag every
#      override-carrying item as stale.
#   2. A VARIATION TEMPLATE REPORTS Price 0.0 (prices live per-variant in
#      Variations). Live: tpl 39076 -> 0.0 against a real 79.95 channel price.
#      Comparing it marks every variation group stale, so price is skipped there.
#   3. MATCHING FIELDS DO NOT MEAN THE PUSH WILL CHANGE ANYTHING. The proven #40
#      no-op had title, price AND image count all matching while the image URL
#      had changed underneath. This is why nothing here returns `will_change`:
#      the honest output is `comparable_fields_match` plus an explicit
#      `undetectable_fields` list. Assuming a match means "fresh" would repeat
#      the v1.42.1 mistake of trusting a stored field that merely looks live.
#
# NextSuggestedAction is NOT a freshness signal either: it read "Update" with
# IsNextSuggestedActionAllowed true on 24/24 sampled listed Shopify templates,
# including the ones whose snapshots were seven weeks old. It is effectively a
# constant for a listed template.
#
# The load-bearing signal is therefore LastModificationTime — the snapshot build
# time. It is free (already on the opened template) and it is what would have
# caught #40: the images were corrected on 18 Aug, the snapshot was built 12 Aug.
# It also correctly dates the known-stale catnip template to 2026-02-16, matching
# the v1.27.1 incident.

_GLT_UNDETECTABLE_FIELDS = [
    "image content/URL (the template exposes only a COUNT)",
    "description body (exposed only as \"Filled\")",
    "attributes (exposed only as a count)",
    "metafields (exposed only as a count)",
]


def _glt_snapshot_age_days(last_modified: str | None) -> int | None:
    """Whole days between the template snapshot's build time and now (UTC)."""
    if not last_modified:
        return None
    raw = str(last_modified).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0, (datetime.now(timezone.utc) - dt).days)


def _effective_channel_value(rows, sub_source: str, key: str,
                             channel_source: str = GLT_SHOPIFY_CHANNEL_NAME, fallback=None):
    """
    The value the channel actually uses: the <channel_source>/<sub_source>
    override row if one exists, else `fallback` (the item-level base value).

    `channel_source` is the channel-SKU Source string (e.g. "SHOPIFY", "AMAZON")
    — hard-coding SHOPIFY here (issue #42) meant an Amazon title/price override
    was invisible, so the staleness check compared a template against the base
    item value instead of what the Amazon listing actually shows.

    Returns (value, source) where source is "channel_override" or "base".
    """
    for r in rows if isinstance(rows, list) else []:
        if (_norm_conf_name(r.get("Source")) == _norm_conf_name(channel_source)
                and _norm_conf_name(r.get("SubSource")) == _norm_conf_name(sub_source)):
            val = r.get(key)
            if val is not None:
                return val, "channel_override"
    return fallback, "base"


def _refresh_staleness_message(check_staleness: bool, stale_rows: list, no_diff_rows: list,
                               unchecked_rows: list, live: bool = False) -> str:
    """One sentence summarising the pre-flight staleness verdict (issue #40)."""
    if not check_staleness:
        return ("Staleness NOT checked (check_staleness=False) — the snapshot may disagree "
                "with the item. ")
    bits = []
    if stale_rows:
        verb = "pushed a STALE snapshot" if live else "would push a STALE snapshot"
        names = ", ".join(
            f"{r['sku']} ({'/'.join(r['staleness']['stale_fields'])})" for r in stale_rows[:5])
        more = f" +{len(stale_rows) - 5} more" if len(stale_rows) > 5 else ""
        bits.append(f"⚠️ {len(stale_rows)} template(s) {verb}: {names}{more} — the stored value "
                    "overwrites the current one; rebuild the template in the Linnworks GLT UI.")
    if no_diff_rows:
        bits.append(f"{len(no_diff_rows)} template(s) show NO DETECTABLE difference — which may "
                    "mean a silent no-op, NOT that the push worked: image URLs, description, "
                    "attributes and metafields are invisible to this check (issue #40).")
    if unchecked_rows:
        bits.append(f"{len(unchecked_rows)} template(s) could NOT be checked (read failed) — "
                    "treat their freshness as unknown.")
    return (" ".join(bits) + " ") if bits else ""


def _glt_template_staleness(template: dict, stock_item_id: str, base_title: str | None,
                            base_price, sub_source: str,
                            channel_source: str = GLT_SHOPIFY_CHANNEL_NAME) -> dict:
    """
    Compare a GLT template's STORED snapshot against the item's CURRENT values.

    Costs three GETs per template (titles / prices / images). Never raises: a
    failed read yields checked=False with comparable_fields_match=None, because
    "we could not tell" must never collapse into "it matches" (issue #37 —
    a quota error laundered into a factual verdict).

    `channel_source` (issue #42) is the channel-SKU Source string the title/price
    override lookup is scoped to — pass the channel actually being refreshed
    (e.g. "AMAZON"), not the Shopify default, or an Amazon channel-title override
    is invisible and the check compares against the base item value instead.

    The per-variant-price skip below (a variation template reports Price 0.0) is
    confirmed on SHOPIFY only. Whether an Amazon variation family's template
    hangs off the parent or each child — and so whether "IsVariation" even means
    the same thing there — is unestablished (see refresh_channel_listing's
    channel docstring); the same skip-rather-than-compare behaviour is applied
    regardless of channel, which is the conservative choice either way.
    """
    info = template.get("Info") if isinstance(template.get("Info"), dict) else {}
    last_mod = _glt_field(info, "LastModificationTime")
    out: dict = {
        "checked": True,
        "template_last_modified": last_mod,
        "snapshot_age_days": _glt_snapshot_age_days(last_mod),
        "compared": {},
        "stale_fields": [],
        "skipped_comparisons": [],
        "comparable_fields_match": None,
        "undetectable_fields": list(_GLT_UNDETECTABLE_FIELDS),
    }

    try:
        title_rows = call_linnworks_get(
            "Inventory/GetInventoryItemTitles", {"inventoryItemId": stock_item_id})
        price_rows = call_linnworks_get(
            "Inventory/GetInventoryItemPrices", {"inventoryItemId": stock_item_id})
        images = call_linnworks_get(
            "Inventory/GetInventoryItemImages", {"inventoryItemId": stock_item_id})
    except (RateLimitError, RuntimeError) as exc:
        out["checked"] = False
        out["error"] = f"could not read current item values: {exc}"
        out["warning"] = (
            "Staleness UNKNOWN — the current-value read failed, so this is not a "
            "statement that the snapshot is fresh."
        )
        return out

    # ── title: compare against the EFFECTIVE channel title (trap 1) ──────────
    tpl_title = _glt_field(info, "Title")
    cur_title, title_src = _effective_channel_value(
        title_rows, sub_source, "Title", channel_source, fallback=base_title)
    if tpl_title is not None and cur_title is not None:
        match = str(tpl_title).strip() == str(cur_title).strip()
        out["compared"]["title"] = {
            "template": tpl_title, "item": cur_title,
            "item_value_from": title_src, "match": match,
        }
        if not match:
            out["stale_fields"].append("title")

    # ── price: NOT comparable on a variation template (trap 2) ───────────────
    if template.get("IsVariation"):
        out["skipped_comparisons"].append(
            "price (variation template — the snapshot reports 0.0 because prices "
            "live per-variant, so a comparison would be a false positive)"
        )
        out["undetectable_fields"].append("per-variant prices on a variation template")
    else:
        tpl_price = _glt_field(info, "Price")
        cur_price, price_src = _effective_channel_value(
            price_rows, sub_source, "Price", channel_source, fallback=base_price)
        if tpl_price is not None and cur_price is not None:
            try:
                match = abs(float(tpl_price) - float(cur_price)) < 0.005
            except (TypeError, ValueError):
                match = str(tpl_price) == str(cur_price)
            out["compared"]["price"] = {
                "template": tpl_price, "item": cur_price,
                "item_value_from": price_src, "match": match,
            }
            if not match:
                out["stale_fields"].append("price")

    # ── images: COUNT only — matching counts prove nothing (trap 3) ──────────
    tpl_images = _glt_field(info, "Images")
    cur_count = len(images) if isinstance(images, list) else None
    if tpl_images is not None and cur_count is not None:
        try:
            tpl_count = int(str(tpl_images).strip())
        except (TypeError, ValueError):
            tpl_count = None
        if tpl_count is not None:
            match = tpl_count == cur_count
            out["compared"]["image_count"] = {
                "template": tpl_count, "item": cur_count, "match": match,
                "note": ("COUNT ONLY — the template does not expose image URLs, so an "
                         "equal count does NOT mean the same images (this is exactly "
                         "how issue #40 went undetected)"),
            }
            if not match:
                out["stale_fields"].append("image_count")

    out["comparable_fields_match"] = not out["stale_fields"]

    age = out["snapshot_age_days"]
    age_txt = f"built {age} day(s) ago" if age is not None else "build time unknown"
    if out["stale_fields"]:
        out["warning"] = (
            f"STALE — the snapshot ({age_txt}) disagrees with the item on "
            f"{', '.join(out['stale_fields'])}. Pushing it will send the STORED value "
            "and overwrite the current one on the live listing. Rebuild the template "
            "by opening the listing in the Linnworks GLT UI first."
        )
    else:
        out["warning"] = (
            f"No DETECTABLE difference on the comparable fields, but the snapshot was "
            f"{age_txt} and this is NOT a guarantee the push will change anything: "
            "image URLs, description body, attributes and metafields are invisible "
            "here. Issue #40 was a silent no-op with every comparable field matching. "
            "Read the live listing back after pushing."
        )
    return out


@mcp.tool()
def refresh_channel_listing(
    skus: list[str],
    sub_source: str = "SWH Shopify",
    channel: str = "Shopify",
    action: str | None = None,
    check_staleness: bool = True,
    confirmed_count: int | None = None,
    dry_run: bool = True,
) -> dict:
    """
    Re-push / revise EXISTING listings on any GLT-managed channel so updated
    item data — extended properties, title, price, description, etc. —
    propagates to the live channel. Shopify (default), Amazon, TikTok (also
    Magento/Walmart where a tenant lists through the GLT).

    Generalised beyond Shopify (issue #42) by reusing the channel registry and
    target resolver already live-proven for `unpublish_channel_listing`
    (issue #30): channel identity comes from `GLT_CHANNELS` / `_resolve_glt_target`,
    never from a hard-coded string, so a non-GLT channel (eBay, Etsy, Mirakl,
    CDiscount) raises a clear ValueError naming the channels that ARE supported
    instead of silently doing nothing.

    ⚠️  `ProcessTemplates` Revise/Update is live-proven on SHOPIFY only
    (v1.27.1, 14 Jul 2026). On AMAZON it has been FIRED LIVE TWICE and
    PRODUCED NO OBSERVABLE CHANGE on either listing (issue #45, 24-25 Aug
    2026, templates 32064 + 32239) — tried and shown ineffective, not merely
    untried (see `GLT_CHANNELS["amazon"]["revise_attempted"]`). On TIKTOK it
    has never been attempted live at all — only the Delete action has been
    fired live there (`unpublish_channel_listing`). Each plan row and the
    response carry `revise_proven` (proof) and the registry also carries
    `revise_attempted` (whether a live push was ever tried) — an unproven
    channel is warned about in every message, worded differently depending on
    which state it's in — see `channel` below for what the eventual
    single-listing live proof needs to check.

    This is the revise counterpart to `list_to_shopify`: that tool CREATES new
    listings; this one REVISES listings that already exist. It never creates a
    listing — if a SKU isn't already live on the target store it's reported in
    `unresolved` (use `list_to_shopify` to create it).

    Flow per SKU (read-before-write):
      1. Resolve SKU → StockItemId + title.
      2. Confirm the item has a channel-SKU mapping for this channel's Source on
         `sub_source` (the channel-SKU link table — see get_channel_listings).
         Not listed → unresolved.
      3. GenericListings/OpenTemplatesByInventory → open the item's EXISTING GLT
         template(s) for that store/account (this OPENS existing templates, it
         does NOT create any — so it can't duplicate the listing). An item can
         have SEVERAL templates on one channel (live-observed on Amazon: a
         merchant and an ".FBA" template on one StockItemId) — ALL of them are
         planned and pushed, one plan row each, because reviving only one would
         leave the other stale.
      4. (live run) GenericListings/ProcessTemplates with the revise action, per
         template → pushes the current item data to the live listing.

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
        `NextSuggestedAction` when GLT marks it allowed, otherwise fall back to
        "Revise".
      - action="Revise"/"Update"/"Relist"/…: force that GLT action for every item.
    Templates GLT marks as locked, or where neither the suggested action nor
    Revise is allowed, are reported in `unresolved` rather than force-pushed.
    ⚠️ `NextSuggestedAction` is NOT evidence that a change is pending: it read
    "Update", allowed, on 24/24 sampled listed Shopify templates (19 Aug 2026),
    including ones whose snapshots were seven weeks old. For a listed Shopify
    template it is effectively a constant — use `staleness` below, not this.

    ⚠️  A live run (dry_run=False) changes REAL customer-facing listings.
    Acceptance of the push is NOT proof of a change (see below) — read the
    listing data back (e.g. get_channel_listings), not the channel's own detail
    page: Amazon detail pages in particular can lag the catalogue by up to a day,
    so a green result plus an unchanged detail page proves nothing either way.

    ⚠️⚠️  A PUSH CAN BE A SILENT NO-OP (issue #40, live-proven 18 Aug 2026).
    ProcessTemplates returns an empty 2xx whether it changed the listing or not,
    so `processed: true` means the push was ACCEPTED — never that anything moved.
    Two BLT templates pushed "successfully" and left Shopify byte-for-byte
    unchanged, because the template's stored image URL pointed at a Linnworks
    image that had since been deleted. The no-op read as "fixed" and the wrong
    image stayed live.
    → `check_staleness=True` (default) now compares the template's STORED
      snapshot against the item's CURRENT values before pushing and reports
      `staleness` per plan row. ⚠️ It is a PARTIAL check by necessity: the
      template exposes real values only for Title, Price and
      LastModificationTime — Images/Attributes/MetaFields/Description come back
      as counts or the literal "Filled". So `comparable_fields_match: true` means
      "no difference I can SEE", NOT "this push will change something". The #40
      no-op had title, price AND image count all matching. There is deliberately
      no `will_change` field, and no post-push read-back: `LastUpdateTime`
      advances on every push (it advanced on the proven no-op), so nothing the
      API returns can confirm the channel actually changed. Read the storefront.

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
        skus: Exact SKUs / ItemNumbers whose listings to refresh.
        sub_source: Store / account / region name, scoping both the "is it
            listed?" check and which template(s) are opened/revised. Shopify:
            the store ("SWH Shopify" default, "Venom Skateboards", …). Amazon:
            the account ("The Warehouse Group") or a regional sub-source
            ("The Warehouse Group - Germany"), which resolves to the account's
            ChannelId (`sub_source_resolution: "account-prefix"` in the
            response). TikTok: "SKATEWAREHOUSE_UK".
        channel: GLT channel — "Shopify" (default), "Amazon", "TikTok",
            "Magento", "Walmart". A non-GLT channel (eBay, Etsy, Mirakl,
            CDiscount) raises a ValueError naming the supported channels.
            ⚠️ ONE Amazon variation family has been observed live (bushings,
            issue #45, 25 Aug 2026): every child SKU resolved its OWN template
            directly (TikTok's shape, not Shopify's) — the child→parent
            fallback never fired. That is one observation, not a rule — other
            Amazon variation families may still hang off the parent, the same
            as Shopify. The child→parent fallback described above (issue #26)
            runs unchanged regardless of channel, so on an unobserved Amazon
            variation SKU it could still open the correct parent template, the
            wrong one, or none at all. Treat any Amazon variation result as
            unverified until more families are observed.
        action: Optional GLT action override (e.g. "Revise", "Update"). Default
            None = auto (use the template's NextSuggestedAction, else "Revise").
        check_staleness: If True (default), compare each template's stored
            snapshot against the item's current title / price / image count
            before pushing (3 extra GETs per template) and report `staleness`
            on every plan row. The title/price comparison uses the override row
            for THIS channel (e.g. an Amazon title override), not Shopify's, so
            it doesn't misfire on exactly the channel-specific content this
            covers. Set False to skip the extra reads — the plan then says
            nothing about whether the snapshot is fresh.
        confirmed_count: For batches > 25 SKUs, pass len(skus) after reviewing
            the plan to confirm the write.
        dry_run: If True (default), returns the plan without pushing anything.
            Set to False to push the revisions to the channel. A dry run makes
            no ProcessTemplates call for any channel.

    Returns:
        A dict with:
          - dry_run, item_count, target_channel, target_source,
            target_sub_source, target_channel_id, sub_source_resolution,
            revise_proven, available_sub_sources
          - plan: one row PER TEMPLATE that would be revised (sku, covers_skus,
            stock_item_id, title, channel, sub_source, template_id,
            configurator_id, active_listing_id, status, action,
            next_suggested_action, is_allowed_to_revise, templates_on_item,
            revise_proven; plus via_variation_parent / listed_via_children where
            a variation group was resolved). Deduped by template id: inputs
            sharing one template = one row, but an item with several templates
            on one channel (e.g. Amazon merchant + FBA) produces one row PER
            template. Each row also carries `staleness` (when check_staleness):
            template_last_modified, snapshot_age_days, compared{title,price,
            image_count}, stale_fields[], comparable_fields_match,
            skipped_comparisons[], undetectable_fields[], warning.
          - stale_plan_count / no_detectable_change_count /
            staleness_unchecked_count / staleness_note
          - unresolved: per-SKU error rows (not found / not listed on the store /
            no template / locked / no allowed revise action)
          - results: per-template push outcome (live run only)

        The single-listing live proof this needs before `revise_proven` on a
        channel can be flipped to True (see the note in GLT_CHANNELS and
        CLAUDE.md): pick ONE low-risk listing on that channel, check its
        template's freshness first (LastModificationTime / `staleness` here),
        push it, then read the listing DATA back (get_channel_listings or the
        channel's own reporting) — never the detail page — to confirm the
        change actually landed before trusting the flag.
    """
    if not skus:
        raise ValueError("skus must contain at least one SKU.")

    _check_injection("sub_source", sub_source or "")
    _check_injection("channel", channel or "")
    _check_injection("action", action or "")

    if action is not None and action not in _GLT_PROCESS_ACTIONS:
        raise ValueError(
            f"action '{action}' is not a valid GLT action. Valid: {sorted(_GLT_PROCESS_ACTIONS)}"
        )

    # ── Resolve the target channel + ChannelId from the configurator catalogue ─
    # Shared with unpublish_channel_listing (issue #30/#42) — channel identity
    # comes from the registry, never a hard-coded string, and a non-GLT channel
    # (eBay/Etsy/Mirakl/CDiscount) raises here naming the supported channels.
    target = _resolve_glt_target(channel, sub_source)
    ch = target["channel"]
    channel_source = ch["source"]
    available_sub_sources = target["available_sub_sources"]
    if not target["ok"]:
        return {"error": target["error"], "available_sub_sources": available_sub_sources}
    target_channel_id = target["channel_id"]

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
    rate_limited: list[dict] = []
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
            if _norm_conf_name(r.get("Source")) == _norm_conf_name(channel_source)
            and _norm_conf_name(r.get("SubSource")) == _norm_conf_name(sub_source)
        ]

    for raw in skus:
        sku = (raw or "").strip()
        if not sku:
            unresolved.append({"sku": raw, "error": "empty SKU"})
            continue
        try:
            item = call_linnworks("Inventory/GetInventoryItem", {"sku": sku})
        except RateLimitError as exc:
            # Quota failure — transient, and NOT a missing SKU (issue #37).
            rate_limited.append({"sku": sku, "error": str(exc)})
            continue
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
            resolved.append({
                "sku": sku, "stock_item_id": sid, "title": title,
                "base_price": item.get("RetailPrice"),
            })
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
                    "base_price": item.get("RetailPrice"),
                    "listed_via_children": listed_children,
                })
                continue

        unresolved.append({
            "sku": sku, "stock_item_id": sid, "title": title,
            "error": (
                f"not listed on {ch['channel_type']} '{sub_source}' — "
                "use list_to_shopify to create it first"
            ),
        })

    # ── Open the existing GLT templates for the resolved items (read) ──────────
    # An item can have MORE THAN ONE template on a channel (live-observed on
    # Amazon: a merchant template and an ".FBA" template on one StockItemId), so
    # templates are collected as a LIST per item — keying by id would drop all
    # but the last and silently leave the other listing on the stale snapshot.
    templates_by_sid: dict[str, list[dict]] = {}
    ids = [r["stock_item_id"] for r in resolved]
    for i in range(0, len(ids), 200):
        chunk = ids[i:i + 200]
        resp = call_linnworks(
            "GenericListings/OpenTemplatesByInventory",
            {"request": {
                "ChannelType": ch["channel_type"],
                "ChannelName": ch["channel_name"],
                "Parameters": {
                    "SelectedRegions":  [],
                    "Token":            _ZERO_GUID,
                    "InventoryItemIds": chunk,
                    "ChannelId":        target_channel_id,
                },
                # One item can return several templates on one channel, so ask
                # for headroom rather than exactly len(chunk) entries.
                "PaginationParameters": {"PageNumber": 1, "EntriesPerPage": max(len(chunk) * 4, 10)},
            }},
        )
        for t in (resp.get("TemplatesInfo") if isinstance(resp, dict) else None) or []:
            tsid = t.get("StockItemId")
            if tsid:
                templates_by_sid.setdefault(tsid.lower(), []).append(t)

    # ── Variation-child fallback: no own template → use the PARENT's template ──
    # A child mapped on the store but with no template of its own inherits its
    # variation parent's template (the multi-variant listing is managed there).
    # Revising the parent template pushes ALL variants of the listing.
    child_fallback: dict[str, dict] = {}  # input sku (lower) -> {parent_sku, parent_sid}
    parent_sids_to_open: list[str] = []
    for r in resolved:
        if templates_by_sid.get(r["stock_item_id"].lower()) or "listed_via_children" in r:
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
                "ChannelType": ch["channel_type"],
                "ChannelName": ch["channel_name"],
                "Parameters": {
                    "SelectedRegions":  [],
                    "Token":            _ZERO_GUID,
                    "InventoryItemIds": chunk,
                    "ChannelId":        target_channel_id,
                },
                "PaginationParameters": {"PageNumber": 1, "EntriesPerPage": max(len(chunk) * 4, 10)},
            }},
        )
        for t in (resp.get("TemplatesInfo") if isinstance(resp, dict) else None) or []:
            tsid = t.get("StockItemId")
            if tsid:
                templates_by_sid.setdefault(tsid.lower(), []).append(t)

    # ── Build the plan, deciding the push action per template ─────────────────
    # One row PER TEMPLATE (issue #42): an item can have several templates on
    # one channel (live-observed on Amazon: a merchant + an ".FBA" template on
    # one StockItemId) — keying by stock item id, as this used to, would drop
    # all but the last and silently leave the other template on its stale
    # snapshot while reporting a clean refresh. Several input SKUs resolving to
    # the SAME template (e.g. all children of one variation group) still dedupe
    # to ONE push, with the covered inputs listed in covers_skus.
    plan: list[dict] = []
    plan_by_template: dict = {}
    base_info_by_sid: dict[str, dict] = {
        r["stock_item_id"].lower(): {"title": r.get("title"), "base_price": r.get("base_price")}
        for r in resolved
    }
    for r in resolved:
        templates = templates_by_sid.get(r["stock_item_id"].lower()) or []
        via_parent = None
        if not templates:
            via_parent = child_fallback.get(r["sku"].strip().lower())
            if via_parent and via_parent.get("parent_sid"):
                templates = templates_by_sid.get(via_parent["parent_sid"].lower()) or []
        if not templates:
            unresolved.append({
                **r,
                "error": (
                    "listed on the channel but no GLT template could be opened "
                    "for it (checked the item and its variation parent)"
                ),
            })
            continue

        for t in templates:
            existing = plan_by_template.get(t.get("Id"))
            if existing is not None:
                if r["sku"] not in existing["covers_skus"]:
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
                "channel":               ch["channel_type"],
                "sub_source":            sub_source,
                "templates_on_item":     len(templates),
                "revise_proven":         ch["revise_proven"],
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

            # Pre-flight staleness (issue #40): does the template's STORED
            # snapshot still agree with the item's current values? Costs 3 GETs
            # per template. A via-parent row is judged on the PARENT's values —
            # the template holds the parent's title, not the child's. The
            # title/price comparison is scoped to THIS channel's Source
            # (issue #42) — comparing against Shopify's override on an Amazon
            # refresh would misjudge the very thing being refreshed.
            if check_staleness:
                base_title, base_price = r.get("title"), r.get("base_price")
                if via_parent:
                    cached = base_info_by_sid.get(row["stock_item_id"].lower())
                    if cached is None:
                        try:
                            pitem = call_linnworks(
                                "Inventory/GetInventoryItem", {"sku": row["sku"]})
                            cached = {"title": pitem.get("ItemTitle"),
                                      "base_price": pitem.get("RetailPrice")}
                        except (RateLimitError, RuntimeError):
                            cached = {}
                        base_info_by_sid[row["stock_item_id"].lower()] = cached
                    base_title = cached.get("title")
                    base_price = cached.get("base_price")
                row["staleness"] = _glt_template_staleness(
                    t, row["stock_item_id"], base_title, base_price, sub_source,
                    channel_source)

            plan.append(row)
            plan_by_template[t.get("Id")] = row

    stale_rows       = [r for r in plan if r.get("staleness", {}).get("stale_fields")]
    unchecked_rows   = [r for r in plan if "staleness" in r and not r["staleness"].get("checked")]
    no_diff_rows     = [r for r in plan
                        if r.get("staleness", {}).get("comparable_fields_match") is True]

    base_out = {
        "item_count":            len(skus),
        "target_channel":        ch["channel_type"],
        "target_source":         channel_source,
        "target_sub_source":     sub_source,
        "target_channel_id":     target_channel_id,
        "sub_source_resolution": target["resolution"],
        "revise_proven":         ch["revise_proven"],
        "available_sub_sources": available_sub_sources,
        "plan":                  plan,
        "unresolved":            unresolved,
        "rate_limited":          rate_limited,
        "complete":              not rate_limited,
    }
    if check_staleness:
        base_out["staleness_checked"] = True
        base_out["stale_plan_count"] = len(stale_rows)
        base_out["no_detectable_change_count"] = len(no_diff_rows)
        base_out["staleness_unchecked_count"] = len(unchecked_rows)
        base_out["staleness_note"] = (
            "Staleness compares the template's STORED snapshot against the item's "
            "current values. Only title, price (non-variation) and image COUNT are "
            "comparable — image URLs, description body, attributes and metafields are "
            "not exposed, so 'no detectable change' is NOT a promise that the push "
            "will alter the listing (issue #40 was a silent no-op with every "
            "comparable field matching)."
        )
    else:
        base_out["staleness_checked"] = False

    # ── Write guard (threshold 25) ────────────────────────────────────────────
    guard = _write_guard("refresh_channel_listing", skus, confirmed_count, dry_run)
    if guard is not None:
        return {**guard, **base_out}

    # Name the channels actually proven rather than hard-coding one — the delete
    # side of this made exactly this mistake once already (see
    # _proven_delete_channels's docstring): a hard-coded "Shopify only" string
    # would still say that after Amazon or TikTok gets proven.
    #
    # Two distinct unproven states (issue #45): "never attempted" (TikTok/
    # Magento/Walmart — genuinely untried) and "attempted, no observable
    # effect" (Amazon — tried live twice and shown ineffective). Collapsing
    # both into one "not yet live-proven" message understated what is already
    # known about Amazon, so the wording is derived from `revise_attempted`
    # on the registry rather than hard-coded to either state.
    if ch["revise_proven"]:
        unproven_note = ""
    elif ch.get("revise_attempted"):
        unproven_note = (
            f" ⚠️  ProcessTemplates Revise/Update has been fired live on {ch['channel_type']} "
            "and produced no observable change on either listing it was tried against "
            f"(issue #45) — this channel is TRIED AND SHOWN INEFFECTIVE, not merely untried "
            f"(proven: {_proven_revise_channels()}). Do not trust a bulk push here without "
            "independently reading the listing data back afterwards."
        )
    else:
        unproven_note = (
            f" ⚠️  ProcessTemplates Revise/Update has NEVER been attempted live on "
            f"{ch['channel_type']} (proven: {_proven_revise_channels()}) — prove it on ONE "
            "low-risk listing (check the template's freshness first, then read the listing "
            "DATA back, not the detail page) before trusting a bulk run on this channel."
        )

    if dry_run:
        return {
            "dry_run": True,
            **base_out,
            "message": (
                f"Dry run — nothing pushed. {len(plan)} {ch['channel_type']} listing(s) on "
                f"'{sub_source}' would be revised; {len(unresolved)} SKU(s) could not be revised "
                "(see unresolved). "
                + _refresh_staleness_message(check_staleness, stale_rows, no_diff_rows,
                                             unchecked_rows) +
                "Review the plan, then set dry_run=False to push the revisions. A live run changes "
                f"real customer-facing listings.{unproven_note}"
            ),
        }

    if not plan:
        return {
            "dry_run": False,
            **base_out,
            "results": [],
            "message": (
                f"Nothing to revise — no SKU resolved to an existing, revisable {ch['channel_type']} "
                "template."
            ),
        }

    # ── Live execution: ProcessTemplates per template (Revise/Update push) ─────
    results: list[dict] = []
    pushed = 0
    for row in plan:
        res = {
            "sku":         row["sku"],
            "covers_skus": list(row.get("covers_skus") or [row["sku"]]),
            "channel":     ch["channel_type"],
            "sub_source":  sub_source,
            "template_id": row["template_id"],
            "action":      row["action"],
            "processed":   False,
        }
        st = row.get("staleness")
        if st is not None:
            res["stale_fields"] = st.get("stale_fields")
            res["comparable_fields_match"] = st.get("comparable_fields_match")
            res["snapshot_age_days"] = st.get("snapshot_age_days")
        try:
            call_linnworks(
                "GenericListings/ProcessTemplates",
                {"request": {
                    "ChannelType": ch["channel_type"],
                    "ChannelName": ch["channel_name"],
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

    # Acceptance of the push is NOT proof of a change (issue #40/#42): ONE 2xx
    # whether the channel changed or not, and a detail page — Amazon's
    # especially — can lag the catalogue by up to a day, so an unchanged detail
    # page proves nothing either way. Read the listing DATA back.
    readback_note = (
        "ProcessTemplates returns no body, so `processed: true` means the push was ACCEPTED, "
        "never that the listing changed — this is NOT proof of a change. READ THE LISTING DATA "
        "BACK NOW (e.g. get_channel_listings), not the channel's detail/storefront page — "
    )
    if ch["channel_type"] == "Amazon":
        readback_note += "Amazon detail pages in particular can lag the catalogue by up to a day. "
    else:
        readback_note += "a green result plus an unchanged detail page proves nothing either way. "

    return {
        "dry_run": False,
        **base_out,
        "results": results,
        "message": (
            f"{pushed}/{len(plan)} {ch['channel_type']} listing(s) on '{sub_source}' revised and "
            "pushed. "
            + _refresh_staleness_message(check_staleness, stale_rows, no_diff_rows,
                                         unchecked_rows, live=True) +
            readback_note +
            "The push sends the template's STORED field snapshot, which can be stale and overwrite "
            "current prices/content (live-proven on Shopify 14 Jul 2026) or change nothing at all "
            f"(issue #40). Per-item errors are in results[].error.{unproven_note}"
        ),
    }


# ---------- Unpublish / take down a channel listing (write) ----------
#
# The destructive counterpart to list_to_shopify (creates) and
# refresh_channel_listing (revises). Where those keep a listing alive, this ENDS
# it — the GLT "Delete" action against the item's existing template retires the
# live listing so it stops selling. Built for the duplicate-item cleanup in issue
# #22: after a SKU-scheme migration leaves an orphaned Linnworks item still
# live-listed at a stale quantity, this takes that listing down in bulk instead
# of doing it by hand in the channel's admin, SKU by SKU.
#
# Same read-before-write selection path as refresh_channel_listing (resolve →
# confirm the Source+sub_source channel-SKU mapping → OpenTemplatesByInventory
# opens the EXISTING template — never creates one), but it forces Action="Delete"
# and reads the channel-SKU table back afterwards to confirm the listing is gone.
#
# GENERALISED BEYOND SHOPIFY (issue #30). The primitive is channel-agnostic — only
# ChannelType/ChannelName and the channel-SKU Source change — so `channel` now
# selects any GLT channel (see GLT_CHANNELS). Two live-probed differences from
# Shopify that the code has to handle, both surfaced 5 Aug 2026 on Amazon:
#
#   1. ONE ITEM CAN HAVE SEVERAL TEMPLATES ON ONE CHANNEL. vnm_bearings_gold
#      returns TWO Amazon templates (32115 = the ".FBA" channel SKU, 32381 = the
#      merchant one) for a single StockItemId — Shopify returns one per store.
#      Keying templates by StockItemId (as this tool used to) silently dropped all
#      but the last, so a "successful" take-down would leave the other listing
#      live. Templates are now collected as a LIST per item and every one of them
#      is planned and processed.
#   2. Info.ActiveListingId is not a product id on Amazon — it is the channel SKU
#      ("vnm_bearings_gold.FBA"). It is still the right identity to show in the
#      manifest, just don't read it as a Shopify-style numeric product id.
#
# Read-back is done ONCE PER ITEM after all of its templates are processed —
# with multiple templates feeding one channel-SKU row set, a per-template
# read-back would report still_listed for a row a later template still owns.


@mcp.tool()
def unpublish_channel_listing(
    skus: list[str],
    sub_source: str = "SWH Shopify",
    channel: str = "Shopify",
    allow_variation_parent_takedown: bool = True,
    also_retiring_skus: list[str] | None = None,
    confirmed_count: int | None = None,
    dry_run: bool = True,
) -> dict:
    """
    Take an EXISTING channel listing DOWN — unpublish/retire the listing so it
    stops selling. Works on any GLT-managed channel: Shopify, Amazon, TikTok
    (also Magento/Walmart where a tenant lists through the GLT).

    This is the destructive counterpart to `list_to_shopify` (which CREATES
    listings) and `refresh_channel_listing` (which REVISES them). It ends the
    live listing via the GLT "Delete" action against the item's existing
    template. Use it to retire an orphaned/duplicate listing — e.g. after a
    SKU-scheme migration leaves a stale Linnworks item still live at a frozen
    quantity, silently able to oversell — or as the take-down step of a
    dead-product cleanup (delist → wait for channel sync → archive).

    ⚠️  DESTRUCTIVE and customer-facing. A live run removes a REAL listing (with
    its reviews, ranking and URL). Point it only at the orphan you mean to
    retire — NOT the good listing you want to keep. Re-listing later is possible,
    but the original listing's channel history (reviews / SEO) is not recoverable.

    ⚠️  AMAZON / TIKTOK ARE NOT YET LIVE-PROVEN. `ProcessTemplates` Delete is
    live-proven on SHOPIFY only (v1.25.0). The Amazon read/selection path is
    live-probed and the delete payload is identical bar ChannelType/ChannelName,
    but the delete SEMANTICS on Amazon are unverified — prove it on ONE throwaway
    Amazon listing before any bulk run, exactly as Shopify was proven. Each plan
    row carries `delete_proven` so an unproven channel is never silently assumed.

    ⚠️  eBay, Etsy, Mirakl and CDiscount are NOT GLT channels — they have no
    templates, so this tool cannot END a listing on any of them; that must be
    done in the channel's own admin. Passing them raises a ValueError. (eBay
    DOES have its own separate, non-GLT route for REVISING a listing's
    description — see `revise_ebay_listing_description`, issue #43 — but that
    is not this tool and does not change what's said here.)

    VARIATION GROUPS (issue #35). A variation CHILD holds the channel-SKU rows
    but no template of its own — the template lives on the variation PARENT and
    serves EVERY variant of the listing. So a dead size/colour can only be
    retired by deleting the parent's template, which also ends its siblings.
    That is done here ONLY when it costs nothing: when every other member of the
    group is either already un-listed on this store, or is itself being retired
    in the same operation (`skus` + `also_retiring_skus`). Otherwise the SKU is
    blocked with `blocked_reason="variation_child_live_siblings"`, naming the
    parent and the live siblings, so the reason is actionable instead of opaque.
    "Live" means "still has a channel-SKU row on this store" — NOT "has stock":
    a sibling listed at zero stock is still a customer-facing listing nobody
    asked to remove. Passing a PARENT SKU directly is an explicit instruction to
    retire the whole group and IS allowed, with the affected children named in
    the plan row.

    Flow per SKU (read-before-write):
      1. Resolve SKU → StockItemId + title.
      2. Confirm the item has a channel-SKU mapping for this channel's Source on
         `sub_source` (see get_channel_listings), and capture its current
         channel_reference_id + listed_quantity for the manifest. A variation
         PARENT counts as listed when any of its children is mapped. Not listed
         on that store/account → unresolved.
      3. GenericListings/OpenTemplatesByInventory → open the item's EXISTING GLT
         template(s) (OPENS existing templates — it does NOT create any). An item
         can have SEVERAL templates on one channel (live-observed on Amazon: a
         merchant and an ".FBA" template on one item) — ALL of them are planned,
         because deleting one would leave the other live. Locked → unresolved.
         No template of its own → the variation-parent fallback above.
      4. (live run) GenericListings/ProcessTemplates with Action="Delete" per
         template → ends the live listing.
      5. (live run) Verify on TWO surfaces: re-open each template by id to prove
         it is gone, AND re-read the channel-SKU rows of every item whose listing
         this template served (the children, for a parent take-down).

    Staging: the threshold is 10 (the tightest tier, shared with
    delete_inventory_item). For batches > 10 SKUs this returns the plan + manifest
    and asks you to confirm with confirmed_count=<N> before executing.

    Args:
        skus: Exact SKUs / ItemNumbers whose listings to take down.
        sub_source: Store / account / region name, scoping both the "is it
            listed?" check and which listing is deleted. Shopify: the store
            ("SWH Shopify" default, "Venom Skateboards", …). Amazon: the account
            ("The Warehouse Group") or a regional sub-source
            ("The Warehouse Group - Germany"), which resolves to the account's
            ChannelId. TikTok: "SKATEWAREHOUSE_UK".
        channel: GLT channel — "Shopify" (default), "Amazon", "TikTok",
            "Magento", "Walmart".
        allow_variation_parent_takedown: If True (default), a variation child may
            be retired via its parent's template when — and only when — no other
            group member would lose a listing (see above). Set False to refuse
            every parent-level take-down; children are then blocked with
            `variation_child_parent_takedown_disabled` rather than silently
            mislabelled.
        also_retiring_skus: Extra SKUs known to be going away in this same
            operation. They are treated as already-retired when judging whether a
            variation parent is safe to take down — needed when a group is split
            across chunks, or when a caller delegates one SKU at a time (as
            delist_all_channel_listings does). They are NOT themselves delisted.
        confirmed_count: For batches > 10 SKUs, pass len(skus) after reviewing the
            plan to confirm the write.
        dry_run: If True (default), returns the plan without taking anything down.
            Set to False to delete the listings on the channel.

    Returns:
        A dict with:
          - dry_run, item_count, target_channel, target_source, target_sub_source,
            target_channel_id, sub_source_resolution, delete_proven,
            available_sub_sources
          - plan: one row PER TEMPLATE to be deleted (sku, covers_skus,
            stock_item_id, template_stock_item_id, listing_sids, title,
            template_id, configurator_id, active_listing_id, status,
            channel_reference_id, listed_quantity, next_suggested_action, action,
            plus via_variation_parent / variation_parent_sku /
            variation_group_name / group_member_skus on a parent take-down)
          - unresolved: per-SKU rows, each with a `blocked_reason` code —
            not_found · channel_read_failed · not_listed · no_glt_template ·
            variation_child_live_siblings · variation_parent_has_no_template ·
            variation_child_parent_takedown_disabled · template_locked
          - blocked_summary / blocked_count / retirable_sku_count — so a small
            plan against a large request cannot read as success
          - results: per-template outcome (live run only — processed, taken_down,
            still_listed, outcome, error)
    """
    if not skus:
        raise ValueError("skus must contain at least one SKU.")

    _check_injection("sub_source", sub_source or "")
    _check_injection("channel", channel or "")

    # ── Resolve the target channel + ChannelId from the configurator catalogue ─
    target = _resolve_glt_target(channel, sub_source)   # raises on a non-GLT channel
    ch = target["channel"]
    available_sub_sources = target["available_sub_sources"]
    if not target["ok"]:
        return {"error": target["error"], "available_sub_sources": available_sub_sources}
    target_channel_id = target["channel_id"]
    channel_source = ch["source"]

    # SKUs whose listings are going away in this same operation. Their liveness
    # must NOT block a variation-parent take-down — the caller is retiring them
    # too. `skus` itself is included, so handing a whole group in one batch just
    # works; `also_retiring_skus` covers a chunked run and the one-SKU-at-a-time
    # delegation in delist_all_channel_listings.
    retiring: set[str] = {(s or "").strip().lower() for s in skus if (s or "").strip()}
    for s in (also_retiring_skus or []):
        _check_injection("also_retiring_skus", s or "")
        if (s or "").strip():
            retiring.add(s.strip().lower())

    def _rows_on_store(rows) -> list:
        return [
            r for r in (rows if isinstance(rows, list) else [])
            if _norm_conf_name(r.get("Source")) == _norm_conf_name(channel_source)
            and _norm_conf_name(r.get("SubSource")) == _norm_conf_name(sub_source)
        ]

    # Memoised child -> group lookup. A confirmed group seeds every one of its
    # members, so N children of one group cost ONE SearchVariationGroups sweep.
    _variation_cache: dict[str, dict | None] = {}

    def _group_of_child(child_sku: str, child_sid: str) -> dict | None:
        """Child SKU -> {'parent_sku','parent_sid','group_name','members'} or None."""
        key = child_sku.strip().lower()
        if key in _variation_cache:
            return _variation_cache[key]
        try:
            rel = _resolve_variation(child_sku, child_sid)
        except RuntimeError:
            _variation_cache[key] = None
            return None
        entry = None
        if rel.get("role") == "child" and rel.get("parent_stock_item_id"):
            # The group's full membership, from this child's point of view: it
            # plus its siblings. Identical from any member's point of view, which
            # is what makes the seeding below correct.
            members = [{"sku": child_sku, "stock_item_id": child_sid}] + [
                {"sku": s.get("sku"), "stock_item_id": s.get("stock_item_id")}
                for s in rel.get("siblings", []) if s.get("sku")
            ]
            entry = {
                "parent_sku": rel.get("parent_sku"),
                "parent_sid": rel.get("parent_stock_item_id"),
                "group_name": rel.get("group_name"),
                "members":    members,
            }
            for m in members:
                _variation_cache[(m["sku"] or "").strip().lower()] = entry
        _variation_cache[key] = entry
        return entry

    # ── Resolve each SKU + confirm it's listed on the target store ────────────
    resolved: list[dict] = []
    unresolved: list[dict] = []
    rate_limited: list[dict] = []
    for raw in skus:
        sku = (raw or "").strip()
        if not sku:
            unresolved.append({"sku": raw, "blocked_reason": "empty_sku", "error": "empty SKU"})
            continue
        try:
            item = call_linnworks("Inventory/GetInventoryItem", {"sku": sku})
        except RateLimitError as exc:
            # Quota failure — transient, and NOT a missing SKU (issue #37).
            rate_limited.append({"sku": sku, "error": str(exc)})
            continue
        except RuntimeError as exc:
            unresolved.append({"sku": sku, "blocked_reason": "not_found",
                               "error": f"not found: {exc}"})
            continue
        sid = item.get("StockItemId")
        if not sid:
            unresolved.append({"sku": sku, "blocked_reason": "not_found",
                               "error": "found but returned no StockItemId"})
            continue
        title = item.get("ItemTitle")
        try:
            rows = call_linnworks_get(
                "Inventory/GetInventoryItemChannelSKUs", {"inventoryItemId": sid}
            )
        except RuntimeError as exc:
            unresolved.append({
                "sku": sku, "stock_item_id": sid, "title": title,
                "blocked_reason": "channel_read_failed",
                "error": f"could not read channel listings: {exc}",
            })
            continue
        on_store = _rows_on_store(rows)
        if on_store:
            # Capture the current listing identity for the manifest / confirmation.
            row0 = on_store[0]
            resolved.append({
                "sku": sku, "stock_item_id": sid, "title": title,
                "channel_reference_id": row0.get("ChannelReferenceId"),
                "listed_quantity":      row0.get("ListedQuantity"),
                "channel_row_count":    len(on_store),
                # Whose channel-SKU rows must disappear for this to count as
                # taken down. For a parent take-down these are the CHILDREN —
                # the parent has no rows of its own, so checking the parent
                # would report "gone" without anything having happened.
                "listing_sids":         [sid],
            })
            continue

        # Not mapped itself — but a variation PARENT owns the whole group's
        # listing while carrying no channel-SKU row. Passing the parent IS an
        # explicit instruction to retire every variant, so it is allowed; the
        # blast radius is named on the plan row.
        try:
            rel = _resolve_variation(sku, sid)
        except RuntimeError:
            rel = {"role": "none"}
        if rel.get("role") == "parent" and rel.get("children"):
            child_map = _fetch_channel_skus_for_ids(
                [c["stock_item_id"] for c in rel["children"] if c.get("stock_item_id")]
            )
            listed_children = [
                c for c in rel["children"]
                if _rows_on_store(child_map.get((c.get("stock_item_id") or "").lower(), []))
            ]
            if listed_children:
                resolved.append({
                    "sku": sku, "stock_item_id": sid, "title": title,
                    "channel_reference_id": None,
                    "listed_quantity":      None,
                    "channel_row_count":    0,
                    "listing_sids":         [c["stock_item_id"] for c in listed_children],
                    "is_variation_parent":  True,
                    "variation_group_name": rel.get("group_name"),
                    "listed_via_children":  [c["sku"] for c in listed_children],
                })
                continue

        unresolved.append({
            "sku": sku, "stock_item_id": sid, "title": title,
            "blocked_reason": "not_listed",
            "error": (
                f"not listed on {ch['channel_type']} '{sub_source}' — nothing to take down"
            ),
        })

    # ── Open the existing GLT templates for the resolved items (read) ──────────
    # An item can have MORE THAN ONE template on a channel (live-observed on
    # Amazon: a merchant template and an ".FBA" template on one StockItemId), so
    # templates are collected as a LIST per item — keying by id would drop all
    # but the last and leave the other listing live after a "successful" run.
    templates_by_sid: dict[str, list[dict]] = {}
    ids = [r["stock_item_id"] for r in resolved]
    for i in range(0, len(ids), 200):
        chunk = ids[i:i + 200]
        resp = call_linnworks(
            "GenericListings/OpenTemplatesByInventory",
            {"request": {
                "ChannelType": ch["channel_type"],
                "ChannelName": ch["channel_name"],
                "Parameters": {
                    "SelectedRegions":  [],
                    "Token":            _ZERO_GUID,
                    "InventoryItemIds": chunk,
                    "ChannelId":        target_channel_id,
                },
                # One item can return several templates, so ask for headroom
                # rather than exactly len(chunk) entries.
                "PaginationParameters": {"PageNumber": 1, "EntriesPerPage": max(len(chunk) * 4, 10)},
            }},
        )
        for t in (resp.get("TemplatesInfo") if isinstance(resp, dict) else None) or []:
            tsid = t.get("StockItemId")
            if tsid:
                templates_by_sid.setdefault(tsid.lower(), []).append(t)

    # ── Variation-child classification + parent fallback (issue #35) ───────────
    #
    # A dead variation CHILD has channel-SKU rows but NO template of its own —
    # the template hangs off the parent and serves every variant (issue #26).
    # The take-down deliberately refused to follow it, because deleting that
    # template ends the siblings too. In the first live cleanup run that made 28
    # of 37 blocked SKUs unretirable, while the obvious workaround (delist the
    # parent) would have killed live listings in 19 of 21 groups.
    #
    # Following the parent is safe exactly when nothing else is lost: every OTHER
    # group member is already un-listed on this store, or is itself being retired
    # in this operation. Then the parent's template serves ONLY listings the
    # caller asked to end. Liveness is judged on the channel-SKU row, not on
    # stock — a sibling listed at zero stock is still a live listing.
    #
    # Classification runs even when allow_variation_parent_takedown is False, so
    # a blocked child says WHY instead of collapsing into "no template".
    child_fallback: dict[str, dict] = {}     # input sku (lower) -> group entry
    variation_block: dict[str, dict] = {}    # input sku (lower) -> block detail
    _group_live_cache: dict[str, list[str]] = {}
    parent_sids_to_open: list[str] = []
    for r in resolved:
        if templates_by_sid.get(r["stock_item_id"].lower()) or r.get("is_variation_parent"):
            continue
        grp = _group_of_child(r["sku"], r["stock_item_id"])
        if not grp or not grp.get("parent_sid"):
            continue                          # not a variation child at all
        psid = grp["parent_sid"].lower()
        if psid in _group_live_cache:
            live_outside = _group_live_cache[psid]
        else:
            # Every member not being retired here. The child itself is always in
            # `retiring` (it came from `skus`), so excluding it is automatic —
            # which is also why this is cacheable per group rather than per child.
            outside = [m for m in grp["members"]
                       if (m["sku"] or "").strip().lower() not in retiring]
            sib_map = _fetch_channel_skus_for_ids(
                [m["stock_item_id"] for m in outside if m.get("stock_item_id")]
            ) if outside else {}
            live_outside = [
                m["sku"] for m in outside
                if _rows_on_store(sib_map.get((m.get("stock_item_id") or "").lower(), []))
            ]
            _group_live_cache[psid] = live_outside

        key = r["sku"].strip().lower()
        if live_outside:
            variation_block[key] = {
                "blocked_reason":     "variation_child_live_siblings",
                "parent_sku":         grp["parent_sku"],
                "group_name":         grp["group_name"],
                "live_siblings":      live_outside,
            }
        elif not allow_variation_parent_takedown:
            variation_block[key] = {
                "blocked_reason":     "variation_child_parent_takedown_disabled",
                "parent_sku":         grp["parent_sku"],
                "group_name":         grp["group_name"],
                "live_siblings":      [],
            }
        else:
            child_fallback[key] = grp
            if psid not in templates_by_sid:
                parent_sids_to_open.append(grp["parent_sid"])

    parent_sids_to_open = list(dict.fromkeys(parent_sids_to_open))
    for i in range(0, len(parent_sids_to_open), 200):
        chunk = parent_sids_to_open[i:i + 200]
        resp = call_linnworks(
            "GenericListings/OpenTemplatesByInventory",
            {"request": {
                "ChannelType": ch["channel_type"],
                "ChannelName": ch["channel_name"],
                "Parameters": {
                    "SelectedRegions":  [],
                    "Token":            _ZERO_GUID,
                    "InventoryItemIds": chunk,
                    "ChannelId":        target_channel_id,
                },
                "PaginationParameters": {"PageNumber": 1, "EntriesPerPage": max(len(chunk) * 4, 10)},
            }},
        )
        for t in (resp.get("TemplatesInfo") if isinstance(resp, dict) else None) or []:
            tsid = t.get("StockItemId")
            if tsid:
                templates_by_sid.setdefault(tsid.lower(), []).append(t)

    # ── Build the take-down plan (one row per TEMPLATE) ────────────────────────
    # Deduped by template id: several children of one variation group resolve to
    # the SAME parent template, and that is ONE delete, not N. covers_skus names
    # every input SKU whose listing the delete ends.
    plan: list[dict] = []
    plan_by_template: dict = {}
    for r in resolved:
        key = r["sku"].strip().lower()
        templates = templates_by_sid.get(r["stock_item_id"].lower()) or []
        via = None
        if not templates:
            via = child_fallback.get(key)
            if via:
                templates = templates_by_sid.get(via["parent_sid"].lower()) or []
                if not templates:
                    unresolved.append({
                        **r,
                        "blocked_reason":       "variation_parent_has_no_template",
                        "variation_parent_sku": via["parent_sku"],
                        "variation_group_name": via["group_name"],
                        "error": (
                            f"variation child — its parent '{via['parent_sku']}' has no GLT "
                            "template either, so nothing here can reach this listing. End it in "
                            f"the {ch['channel_type']} admin."
                        ),
                    })
                    continue

        if not templates:
            blocked = variation_block.get(key)
            if blocked and blocked["blocked_reason"] == "variation_child_live_siblings":
                sibs = blocked["live_siblings"]
                unresolved.append({
                    **r,
                    "blocked_reason":       blocked["blocked_reason"],
                    "variation_parent_sku": blocked["parent_sku"],
                    "variation_group_name": blocked["group_name"],
                    "live_sibling_count":   len(sibs),
                    "live_siblings":        sibs[:25],
                    "error": (
                        f"variation child — its listing is owned by parent "
                        f"'{blocked['parent_sku']}', whose template still serves {len(sibs)} live "
                        "sibling listing(s). Deleting it would take those down too. Remove this "
                        f"variant in the {ch['channel_type']} admin, or retire the whole group "
                        "(pass every member, or list them in also_retiring_skus)."
                    ),
                })
            elif blocked:
                unresolved.append({
                    **r,
                    "blocked_reason":       blocked["blocked_reason"],
                    "variation_parent_sku": blocked["parent_sku"],
                    "variation_group_name": blocked["group_name"],
                    "error": (
                        f"variation child — the whole group is retirable via parent "
                        f"'{blocked['parent_sku']}', but allow_variation_parent_takedown=False. "
                        "Set it True to take the parent template down."
                    ),
                })
            else:
                unresolved.append({
                    **r,
                    "blocked_reason": "no_glt_template",
                    "error": (
                        "listed on the channel but NO GLT template exists for it (checked the "
                        "item and its variation parent) — it was most likely listed outside the "
                        f"GLT, so nothing here can reach it. End it in the {ch['channel_type']} "
                        "admin."
                    ),
                })
            continue

        for t in templates:
            tid = t.get("Id")
            existing = plan_by_template.get(tid)
            if existing is not None:
                if r["sku"] not in existing["covers_skus"]:
                    existing["covers_skus"].append(r["sku"])
                for s in (r.get("listing_sids") or []):
                    if s not in existing["listing_sids"]:
                        existing["listing_sids"].append(s)
                continue

            if t.get("IsLocked"):
                unresolved.append({
                    **r, "template_id": tid,
                    "blocked_reason": "template_locked",
                    "error": "GLT template is locked — cannot take it down right now",
                })
                continue

            info = t.get("Info") if isinstance(t.get("Info"), dict) else {}
            row = {
                "sku":                   r["sku"],
                "covers_skus":           [r["sku"]],
                "stock_item_id":         r["stock_item_id"],
                # Whose template this is — the parent's on a fallback. The
                # per-template read-back re-opens THIS item.
                "template_stock_item_id": via["parent_sid"] if via else r["stock_item_id"],
                # Whose channel-SKU rows must vanish for this to count as gone.
                "listing_sids":          list(r.get("listing_sids") or []),
                "title":                 r["title"],
                "channel":               ch["channel_type"],
                "sub_source":            sub_source,
                "template_id":           tid,
                "configurator_id":       t.get("ConfiguratorId"),
                # On Amazon this is the channel SKU ("…​.FBA"), not a product id.
                "active_listing_id":     _glt_field(info, "ActiveListingId"),
                "status":                _glt_field(info, "Status"),
                "channel_reference_id":  r["channel_reference_id"],
                "listed_quantity":       r["listed_quantity"],
                "next_suggested_action": t.get("NextSuggestedAction"),
                "templates_on_item":     len(templates),
                "delete_proven":         ch["delete_proven"],
                "action":                "Delete",
            }
            if via:
                row.update({
                    "via_variation_parent": True,
                    "variation_parent_sku": via["parent_sku"],
                    "variation_group_name": via["group_name"],
                    "group_member_skus":    [m["sku"] for m in via["members"]],
                    "warning": (
                        f"Whole-group take-down: this deletes the template on variation parent "
                        f"'{via['parent_sku']}', which ends the listing for ALL "
                        f"{len(via['members'])} member(s) of '{via['group_name']}'. Allowed "
                        "because no member outside this operation still has a listing on "
                        f"'{sub_source}'."
                    ),
                })
            elif r.get("is_variation_parent"):
                kids = r.get("listed_via_children") or []
                row.update({
                    "is_variation_parent":  True,
                    "variation_group_name": r.get("variation_group_name"),
                    "listed_via_children":  kids,
                    "warning": (
                        f"Variation PARENT: deleting this template ends the listing for all "
                        f"{len(kids)} currently-listed child variant(s) — {', '.join(kids[:10])}"
                        f"{' …' if len(kids) > 10 else ''}. Nothing checks whether you meant to "
                        "keep any of them; pass the individual children instead if you did."
                    ),
                })
            plan.append(row)
            plan_by_template[tid] = row

    blocked_summary: dict[str, int] = {}
    for u in unresolved:
        code = u.get("blocked_reason") or "unknown"
        blocked_summary[code] = blocked_summary.get(code, 0) + 1

    base_out = {
        "item_count":            len(skus),
        "target_channel":        ch["channel_type"],
        "target_source":         channel_source,
        "target_sub_source":     sub_source,
        "target_channel_id":     target_channel_id,
        "sub_source_resolution": target["resolution"],
        "delete_proven":         ch["delete_proven"],
        "available_sub_sources": available_sub_sources,
        "plan":                  plan,
        "unresolved":            unresolved,
        "blocked_summary":       blocked_summary,
        "blocked_count":         len(unresolved),
        "retirable_sku_count":   len({s for row in plan for s in row["covers_skus"]}),
        "rate_limited":          rate_limited,
        "complete":              not rate_limited,
    }

    # ── Write guard (threshold 10) ─────────────────────────────────────────────
    guard = _write_guard("unpublish_channel_listing", skus, confirmed_count, dry_run)
    if guard is not None:
        return {**guard, **base_out}

    # Name the channels actually proven rather than hard-coding one — this string
    # said "only Shopify is" for the whole of the Amazon and TikTok proofs, which
    # is exactly the kind of stale in-code claim assumption #10 warns about.
    unproven_note = (
        ""
        if ch["delete_proven"]
        else (
            f" ⚠️  ProcessTemplates Delete is NOT yet live-proven on {ch['channel_type']} "
            f"(proven: {_proven_delete_channels()}) — prove it on ONE throwaway listing "
            "before any bulk run."
        )
    )

    # A small plan against a large request must never read as success (issue #35):
    # spell out how many SKUs are unretirable and WHY, in the message itself.
    blocked_note = (
        ""
        if not unresolved
        else (
            f" ⚠️  {len(unresolved)} of {len(skus)} requested SKU(s) CANNOT be taken down: "
            + ", ".join(f"{n}× {code}" for code, n in
                        sorted(blocked_summary.items(), key=lambda kv: -kv[1]))
            + ". See unresolved[] — each row carries blocked_reason and what to do instead "
              "(variation_child_live_siblings and no_glt_template need the channel's own admin)."
        )
    )
    group_note = (
        ""
        if not any(p.get("via_variation_parent") for p in plan)
        else (
            f" {sum(1 for p in plan if p.get('via_variation_parent'))} of these are WHOLE-GROUP "
            "take-downs via a variation parent — allowed only because no member outside this "
            "operation is still listed; check plan[].group_member_skus."
        )
    )

    if dry_run:
        return {
            "dry_run": True,
            **base_out,
            "message": (
                f"Dry run — nothing taken down. {len(plan)} {ch['channel_type']} template(s) on "
                f"'{sub_source}' would be DELETED (listing ended), covering "
                f"{base_out['retirable_sku_count']} of {len(skus)} requested SKU(s). Review the "
                "plan — confirm each channel_reference_id / listed_quantity is the orphan you "
                "mean to retire, NOT a listing you want to keep — then set dry_run=False. A live "
                f"run removes real customer-facing listings.{group_note}{blocked_note}"
                f"{unproven_note}"
            ),
        }

    if not plan:
        return {
            "dry_run": False,
            **base_out,
            "results": [],
            "message": (
                f"Nothing to take down — none of the {len(skus)} requested SKU(s) resolved to a "
                f"deletable {ch['channel_type']} template.{blocked_note}"
            ),
        }

    # ── Live execution: ProcessTemplates Delete per template ───────────────────
    # Read-back is deferred until every template for an item has been processed —
    # several templates can feed one item's channel-SKU rows (Amazon merchant +
    # FBA), so a per-template read-back would report still_listed for rows the
    # next template is about to remove.
    results: list[dict] = []
    for row in plan:
        res = {
            "sku":         row["sku"],
            "covers_skus": list(row.get("covers_skus") or [row["sku"]]),
            "channel":     ch["channel_type"],
            "sub_source":  sub_source,
            "template_id": row["template_id"],
            "via_variation_parent": bool(row.get("via_variation_parent")),
            "action":      "Delete",
            "processed":   False,
            "taken_down":  None,
        }
        try:
            call_linnworks(
                "GenericListings/ProcessTemplates",
                {"request": {
                    "ChannelType": ch["channel_type"],
                    "ChannelName": ch["channel_name"],
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

    # ── Read-back: TWO surfaces, per item AND per template (issue #36) ─────────
    #
    # The channel-SKU check alone is a PER-ITEM signal, and the first successful
    # delete empties that table for the whole item — so on a multi-template item
    # it reported taken_down:true for templates that were never deleted. Live
    # proof (6 Aug 2026): 304035-000-825 and 304037-000-850 each had two
    # templates; one of each survived with Linnworks returning
    # Status "Not deleted", and the tool called all four a success.
    #
    # So each template is now checked individually by re-opening the item's
    # templates and asking whether THAT template id is gone, while the
    # channel-SKU read still answers the separate question "is the listing gone".
    # The two can legitimately disagree: an orphaned-but-undeletable template
    # over an already-dead listing is a real, observed state.
    #
    # Which item to read is now TWO different questions, because a variation
    # parent take-down splits them (issue #35): the TEMPLATE belongs to the
    # parent, but the channel-SKU rows belong to the CHILDREN — the parent has
    # none. Reading the parent's rows would return zero and score a clean
    # take_down without anything having happened, which is the #36 defect wearing
    # a different hat. So templates are re-opened on template_stock_item_id and
    # listings are re-read on every id in listing_sids.
    remaining: dict = {}
    template_readback_error: dict[str, str] = {}
    for tsid in dict.fromkeys(row["template_stock_item_id"] for row in plan):
        try:
            resp = call_linnworks(
                "GenericListings/OpenTemplatesByInventory",
                {"request": {
                    "ChannelType": ch["channel_type"],
                    "ChannelName": ch["channel_name"],
                    "Parameters": {
                        "SelectedRegions": [],
                        "Token": _ZERO_GUID,
                        "InventoryItemIds": [tsid],
                        "ChannelId": target_channel_id,
                    },
                    "PaginationParameters": {"PageNumber": 1, "EntriesPerPage": 20},
                }},
            )
            for t in (resp.get("TemplatesInfo") if isinstance(resp, dict) else None) or []:
                info = t.get("Info") if isinstance(t.get("Info"), dict) else {}
                remaining[t.get("Id")] = _glt_field(info, "Status")
        except RuntimeError as exc:
            template_readback_error[tsid] = str(exc)

    listing_counts: dict[str, int] = {}
    listing_errors: dict[str, str] = {}
    for lsid in dict.fromkeys(s for row in plan for s in (row.get("listing_sids") or [])):
        try:
            rows = call_linnworks_get(
                "Inventory/GetInventoryItemChannelSKUs", {"inventoryItemId": lsid},
            )
            listing_counts[lsid] = len(_rows_on_store(rows))
        except RuntimeError as exc:
            listing_errors[lsid] = str(exc)

    taken = 0
    for row, r in zip(plan, results):
        lsids = row.get("listing_sids") or []
        errs = [listing_errors[s] for s in lsids if s in listing_errors]
        if errs:
            r["readback_error"] = f"could not confirm take-down: {errs[0]}"
        # Only trust a count that covers EVERY item this template served.
        still_count = (
            sum(listing_counts[s] for s in lsids)
            if lsids and all(s in listing_counts for s in lsids)
            else None
        )
        if still_count is not None:
            r["still_listed"] = still_count > 0

        terr = template_readback_error.get(row["template_stock_item_id"])
        if terr:
            r["template_readback_error"] = terr
            r["taken_down"] = None
            r["outcome"] = "unconfirmed"
            continue

        tid = r["template_id"]
        template_gone = tid not in remaining
        r["template_deleted"] = template_gone
        r["template_status_after"] = remaining.get(tid)

        if still_count is None:
            # The listing side could not be read — never score a take-down on
            # half the evidence.
            r["taken_down"] = None
            r["outcome"] = "unconfirmed"
        elif template_gone and still_count == 0:
            r["taken_down"] = True
            r["outcome"] = "taken_down"
            taken += 1
        elif template_gone and still_count:
            # Template removed but a channel-SKU row survives — usually
            # channel sync lag, sometimes another template still owns it.
            r["taken_down"] = False
            r["outcome"] = "template_deleted_listing_row_remains"
        elif not template_gone and still_count == 0:
            # ⚠️ The case that used to be silently reported as success.
            r["taken_down"] = False
            r["outcome"] = "listing_gone_template_orphaned"
            r["warning"] = (
                f"Template {tid} still exists (status "
                f"{r['template_status_after']!r}) even though the channel-SKU row is gone. "
                "The listing itself appears to be down — verify in the channel's admin. "
                "Repeated Delete calls on such a template return 2xx and change nothing."
            )
        else:
            r["taken_down"] = False
            r["outcome"] = "delete_failed"
            r["warning"] = (
                f"Template {tid} was NOT deleted (status "
                f"{r['template_status_after']!r}) and the listing row is still present — "
                "the listing may still be live. Check the channel's admin."
            )

    orphaned = sum(1 for r in results if r.get("outcome") == "listing_gone_template_orphaned")
    failed = sum(1 for r in results if r.get("outcome") == "delete_failed")
    extra = ""
    if orphaned:
        extra += (
            f" {orphaned} template(s) survived the Delete but their listing row is gone "
            "(orphaned template — listing appears down; see results[].warning)."
        )
    if failed:
        extra += (
            f" ⚠️ {failed} template(s) were NOT deleted AND still have a live listing row — "
            "these may still be selling."
        )

    return {
        "dry_run": False,
        **base_out,
        "results": results,
        "taken_down_count": taken,
        "orphaned_template_count": orphaned,
        "delete_failed_count": failed,
        "message": (
            f"{taken}/{len(plan)} {ch['channel_type']} template(s) on '{sub_source}' confirmed "
            "taken down. Each template is verified individually (re-opened by id) AND against the "
            "channel-SKU table, because a per-item check alone reports success for templates that "
            "were never deleted on multi-template items (issue #36). On a variation-parent "
            "take-down the listing side is read on the CHILDREN, since the parent has no "
            "channel-SKU rows of its own (issue #35). The channel may lag, so a still_listed=true "
            "row may simply not have synced — re-check with get_channel_listings and verify in the "
            f"channel's own admin. Per-item errors are in results[].error.{extra}{blocked_note}"
            f"{unproven_note}"
        ),
    }


@mcp.tool()
def delist_all_channel_listings(
    skus: list[str],
    channels: list[str] | None = None,
    allow_variation_parent_takedown: bool = True,
    confirmed_count: int | None = None,
    dry_run: bool = True,
) -> dict:
    """
    Take down EVERY GLT-manageable listing for each given item, across ALL
    channels and stores/accounts it is listed on — the fan-out wrapper over
    unpublish_channel_listing (which handles one channel+store at a time).

    Built for dead-product cleanup ("retire this product everywhere, then archive
    it") and archived-item cleanup: UNARCHIVE items in the Linnworks UI first
    (they must be ACTIVE — an archived SKU cannot be resolved), run this to retire
    their listings, let the channels sync, then re-archive with
    archive_inventory_items.

    Per SKU it reads the channel-SKU link table, groups every listing by
    (channel, store/account), and delegates each group to unpublish_channel_listing
    (GLT ProcessTemplates Delete). It does NOT touch the base item or its stock.

    ⚠️  "EVERYWHERE" IS NEVER LITERALLY TRUE. eBay, Etsy, Mirakl and CDiscount are
    not GLT channels — no templates — so this tool cannot END their listings;
    they are reported under `skipped_channels` and LEFT UP for manual take-down
    in that channel's own admin. Same for any channel with no configurators in
    this tenant (Magento/Walmart here). (eBay has a separate non-GLT route for
    REVISING a description — see `revise_ebay_listing_description` — which does
    not help end a listing.)

    ⚠️  ONLY SHOPIFY DELETES ARE LIVE-PROVEN. Amazon/TikTok rows carry
    delete_proven=false — prove the Delete on ONE throwaway listing per channel
    before trusting a bulk run.

    ⚠️  AMAZON REGIONS SHARE ONE ACCOUNT. Amazon's regional sub-sources
    ("The Warehouse Group - Germany", "- Spain", …) all hang off one account
    (ChannelId 2) and one set of templates, so they are collapsed into a single
    take-down per (item, account) with the regions listed under `covers_sub_sources`
    — deleting once per region would just re-delete the same templates. After a
    live run the tool re-reads each item's rows for that channel and reports any
    sub-source still present under `still_listed_sub_sources`.

    ⚠️  DEAD VARIANTS INSIDE LIVE PRODUCTS ARE THE COMMON CASE, and most of them
    are NOT retirable here. A variation child's listing is owned by its parent's
    template, so it can only be taken down by ending the whole group — done
    automatically when every other member is dead or also in this batch, and
    refused (with `blocked_reason="variation_child_live_siblings"`) when a
    sibling is still listed. Sizes and colours are exactly what goes dead in a
    catalogue, so expect a large `blocked_summary`; those variants need removing
    on the channel side. See `unresolved[].blocked_reason` for the per-SKU reason
    rather than reading the take-down count as a completion rate.

    ⚠️  DESTRUCTIVE and customer-facing — removes real listings (reviews, ranking,
    URL not recoverable). dry_run=True by default; staging threshold 10 on the
    number of planned template deletions.

    Args:
        skus: Exact SKUs / ItemNumbers (must be ACTIVE / resolvable) to delist.
            The whole list is treated as one operation, so a variation group
            handed in fully here is retirable even though each SKU is processed
            separately.
        channels: GLT channels to act on, e.g. ["Shopify", "Amazon"]. Default
            (None) = every GLT channel that has configurators in this tenant.
            Pass ["Shopify"] to keep to the live-proven path.
        allow_variation_parent_takedown: Passed through to
            unpublish_channel_listing. True (default) allows a whole-group
            take-down via the variation parent when no member outside this batch
            would lose a listing; False refuses every parent-level take-down.
        confirmed_count: For > 10 planned take-downs, pass the take_down_count from
            the dry-run manifest to confirm.
        dry_run: If True (default), preview only. Set False to execute.

    Returns:
        dict with `glt_channels` (what is actionable in this tenant), per-SKU
        `discovery` (targets + skipped_channels), a combined `plan` of template
        take-downs, `unresolved` (each row carrying `blocked_reason`),
        `blocked_summary`, and — on a live run — per-template `results` plus
        `still_listed_sub_sources`.
    """
    if not skus:
        raise ValueError("skus must contain at least one SKU.")

    # ── Which GLT channels are actionable in this tenant? ─────────────────────
    if channels is None:
        wanted = list(GLT_CHANNELS.keys())
    else:
        wanted = []
        for c in channels:
            _check_injection("channels", c or "")
            wanted.append(_resolve_glt_channel(c)["key"])   # raises on non-GLT

    # One catalogue fetch per channel; a channel with zero configurators is not
    # GLT-managed here and everything on it must be skipped, not attempted.
    channel_state: dict[str, dict] = {}
    for key in dict.fromkeys(wanted):
        entry = GLT_CHANNELS[key]
        try:
            cat = _fetch_glt_configurators(entry["channel_type"])
        except (RuntimeError, ValueError):
            cat = []
        ss_to_channel: dict[str, int] = {}
        for c in cat:
            ss, cid = c.get("sub_source"), c.get("channel_id")
            if ss and cid is not None:
                ss_to_channel.setdefault(_norm_conf_name(ss), cid)
        channel_state[_norm_conf_name(entry["source"])] = {
            **entry, "key": key,
            "ss_to_channel": ss_to_channel,
            "accounts": sorted({c["sub_source"] for c in cat if c.get("sub_source")}),
        }

    # ── Discover, per SKU, what is actionable and what must be skipped ────────
    discovery: list[dict] = []
    unresolved: list[dict] = []
    work: list[dict] = []    # {sku, channel_type, sub_source}
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

        # Collapse to one take-down per (channel, resolved ChannelId): Amazon's
        # regional sub-sources share one account and one template set.
        targets: dict[tuple[str, int], dict] = {}
        skipped: list[dict] = []
        for r in rows:
            src, ss = r.get("Source"), r.get("SubSource")
            state = channel_state.get(_norm_conf_name(src))
            if state is None:
                skipped.append({
                    "source": src, "sub_source": ss,
                    "channel_reference_id": r.get("ChannelReferenceId"),
                    "reason": (
                        "not a GLT channel — no template, so it cannot be ended here; end it in "
                        "the channel's own admin"
                        if _norm_conf_name(src) in {_norm_conf_name(s) for s in NON_GLT_SOURCES}
                        else "channel not selected / not GLT-manageable in this tenant"
                    ),
                })
                continue
            cid, resolution = _glt_channel_id_for(state["ss_to_channel"], ss or "")
            if cid is None:
                skipped.append({
                    "source": src, "sub_source": ss,
                    "channel_reference_id": r.get("ChannelReferenceId"),
                    "reason": (
                        f"no {state['channel_type']} GLT configurator matches this sub-source "
                        f"(accounts here: {state['accounts'] or 'none'})"
                    ),
                })
                continue
            k = (state["channel_type"], cid)
            t = targets.setdefault(k, {
                "channel": state["channel_type"],
                "channel_id": cid,
                "sub_source": ss,                 # representative (delete target)
                "sub_source_resolution": resolution,
                "covers_sub_sources": [],
                "delete_proven": state["delete_proven"],
            })
            if ss and ss not in t["covers_sub_sources"]:
                t["covers_sub_sources"].append(ss)
            # Prefer an exactly-matching account as the representative, so the
            # delegated call resolves without relying on the prefix fallback.
            if resolution == "exact" and t["sub_source_resolution"] != "exact":
                t["sub_source"] = ss
                t["sub_source_resolution"] = "exact"

        for t in targets.values():
            work.append({"sku": sku, "channel_type": t["channel"], "sub_source": t["sub_source"]})
        discovery.append({
            "sku": sku, "stock_item_id": sid, "title": item.get("ItemTitle"),
            "targets": list(targets.values()), "skipped_channels": skipped,
        })

    # ── Build the combined plan by dry-running the single-channel tool ────────
    # Delegation is batched PER TARGET (channel × account), not per SKU. A whole
    # variation group shares ONE parent template, so N children delegated
    # separately would queue N deletes of the same template — the first would
    # succeed and the rest would come back as "no template" (issue #35). Handing
    # the delegate every SKU for a target lets it dedupe by template and report
    # one take-down covering all of them.
    #
    # `also_retiring_skus` carries the REST of the batch on top, so a group whose
    # members land in different targets still counts them all as going away.
    batch_skus = [(s or "").strip() for s in skus if (s or "").strip()]
    by_target: dict[tuple, dict] = {}
    for w in work:
        k = (w["channel_type"], _norm_conf_name(w["sub_source"]))
        t = by_target.setdefault(k, {"channel_type": w["channel_type"],
                                     "sub_source": w["sub_source"], "skus": []})
        if w["sku"] not in t["skus"]:
            t["skus"].append(w["sku"])
    targets_work = list(by_target.values())

    plan: list[dict] = []
    for w in targets_work:
        sub = unpublish_channel_listing(
            skus=w["skus"], sub_source=w["sub_source"], channel=w["channel_type"],
            allow_variation_parent_takedown=allow_variation_parent_takedown,
            also_retiring_skus=batch_skus,
            # The fan-out's own _write_guard below is the real staging gate; the
            # delegate must not stage a batch the caller has already confirmed.
            confirmed_count=len(w["skus"]), dry_run=True,
        )
        if sub.get("error"):
            unresolved.append({**w, "error": sub["error"]})
            continue
        plan.extend(sub.get("plan", []))
        for u in sub.get("unresolved", []):
            unresolved.append({**u, "channel": w["channel_type"], "sub_source": w["sub_source"]})

    glt_channels = [
        {"channel": s["channel_type"], "accounts": s["accounts"],
         "actionable": bool(s["accounts"]), "delete_proven": s["delete_proven"]}
        for s in channel_state.values()
    ]
    blocked_summary: dict[str, int] = {}
    for u in unresolved:
        code = u.get("blocked_reason") or "unknown"
        blocked_summary[code] = blocked_summary.get(code, 0) + 1

    base_out = {
        "item_count":       len(skus),
        "glt_channels":     glt_channels,
        "discovery":        discovery,
        "plan":             plan,
        "unresolved":       unresolved,
        "blocked_summary":  blocked_summary,
        "blocked_count":    len(unresolved),
        "retirable_sku_count": len({s for p in plan for s in (p.get("covers_skus") or [p["sku"]])}),
        "skipped_channels": [
            {"sku": d["sku"], **s} for d in discovery for s in d["skipped_channels"]
        ],
    }

    # ── Write guard (threshold 10) on the number of take-downs ─────────────────
    guard = _write_guard("delist_all_channel_listings", plan, confirmed_count, dry_run)
    if guard is not None:
        return {**guard, **base_out}

    unproven = sorted({p["channel"] for p in plan if not p.get("delete_proven")})
    unproven_note = (
        f" ⚠️  {', '.join(unproven)} Delete is NOT live-proven — prove it on one throwaway "
        "listing per channel first."
        if unproven else ""
    )

    # Never let the take-down count read as a completion rate (issue #35): on real
    # dead stock most SKUs are dead VARIANTS of live products and are not
    # retirable through Linnworks at all.
    blocked_note = (
        ""
        if not unresolved
        else (
            f" ⚠️  {base_out['blocked_count']} SKU-listing(s) CANNOT be taken down: "
            + ", ".join(f"{n}× {code}" for code, n in
                        sorted(blocked_summary.items(), key=lambda kv: -kv[1]))
            + ". See unresolved[].blocked_reason — variation_child_live_siblings and "
              "no_glt_template both need removing on the channel side, not here."
        )
    )
    group_note = (
        ""
        if not any(p.get("via_variation_parent") for p in plan)
        else (
            f" {sum(1 for p in plan if p.get('via_variation_parent'))} row(s) are WHOLE-GROUP "
            "take-downs via a variation parent — every member is dead or in this batch; check "
            "plan[].group_member_skus."
        )
    )

    if dry_run:
        return {
            "dry_run": True,
            **base_out,
            "take_down_count": len(plan),
            "message": (
                f"Dry run — nothing taken down. {len(plan)} template(s) would be DELETED, "
                f"covering {base_out['retirable_sku_count']} of {len(skus)} requested SKU(s); "
                f"{len(base_out['skipped_channels'])} listing(s) on non-GLT channels stay up "
                "(see skipped_channels). Review the plan, then set "
                f"dry_run=False.{group_note}{blocked_note}{unproven_note}"
            ),
        }

    # ── Live execution: delegate each (channel, account) target ───────────────
    results: list[dict] = []
    for w in targets_work:
        sub = unpublish_channel_listing(
            skus=w["skus"], sub_source=w["sub_source"], channel=w["channel_type"],
            allow_variation_parent_takedown=allow_variation_parent_takedown,
            also_retiring_skus=batch_skus,
            confirmed_count=len(w["skus"]), dry_run=False,
        )
        results.extend(sub.get("results", []))

    # ── Honest read-back: which sub-sources are STILL listed per (sku, channel)?
    # The delegated read-back only sees its own representative sub-source; Amazon
    # regions have to be re-checked explicitly.
    still_listed: list[dict] = []
    for d in discovery:
        if not d["targets"]:
            continue
        try:
            rows = call_linnworks_get(
                "Inventory/GetInventoryItemChannelSKUs", {"inventoryItemId": d["stock_item_id"]}
            )
        except RuntimeError as exc:
            still_listed.append({"sku": d["sku"], "readback_error": str(exc)})
            continue
        rows = rows if isinstance(rows, list) else []
        for t in d["targets"]:
            src = GLT_CHANNELS[_norm_conf_name(t["channel"])]["source"]
            remaining = sorted({
                r.get("SubSource") for r in rows
                if _norm_conf_name(r.get("Source")) == _norm_conf_name(src)
                and r.get("SubSource") in t["covers_sub_sources"]
            })
            if remaining:
                still_listed.append({
                    "sku": d["sku"], "channel": t["channel"], "sub_sources": remaining,
                })

    taken = sum(1 for r in results if r.get("taken_down"))
    orphaned = sum(1 for r in results if r.get("outcome") == "listing_gone_template_orphaned")
    failed = sum(1 for r in results if r.get("outcome") == "delete_failed")
    # Never let a per-template failure hide behind the aggregate count (issue #36).
    extra = ""
    if orphaned:
        extra += (f" {orphaned} orphaned template(s): the Delete did not remove them but their "
                  "listing row is gone — listing appears down, template needs manual tidying.")
    if failed:
        extra += (f" ⚠️ {failed} template(s) were NOT deleted and still have a live listing row — "
                  "these may still be selling; check the channel's admin.")
    return {
        "dry_run": False,
        **base_out,
        "results": results,
        "still_listed_sub_sources": still_listed,
        "taken_down_count": taken,
        "orphaned_template_count": orphaned,
        "delete_failed_count": failed,
        "message": (
            f"{taken}/{len(results)} template(s) confirmed taken down across all selected "
            f"channels. {len(base_out['skipped_channels'])} listing(s) on non-GLT channels were "
            "left up (see skipped_channels) — those need ending in the channel's own admin. "
            "still_listed_sub_sources lists any store/region whose channel-SKU row survived; "
            "channel sync can lag, so re-check with get_channel_listings before concluding the "
            f"take-down failed.{extra}{group_note}{blocked_note}{unproven_note}"
        ),
    }


@mcp.tool()
def delist_all_shopify_listings(
    skus: list[str],
    allow_variation_parent_takedown: bool = True,
    confirmed_count: int | None = None,
    dry_run: bool = True,
) -> dict:
    """
    Take down every SHOPIFY listing for each given item, across all Shopify stores
    it is listed on — the Shopify-scoped, live-proven slice of
    delist_all_channel_listings.

    Identical behaviour and safety posture, with `channels` pinned to Shopify:
    non-Shopify listings (Amazon, TikTok, eBay, Mirakl…) are reported under
    `skipped_channels` and LEFT UP. Use `delist_all_channel_listings` to also
    retire Amazon/TikTok (GLT) listings — noting those deletes are not yet
    live-proven.

    ⚠️  DESTRUCTIVE and customer-facing. dry_run=True by default; staging
    threshold 10 on the number of planned take-downs.

    Args:
        skus: Exact SKUs / ItemNumbers (must be ACTIVE / resolvable).
        allow_variation_parent_takedown: True (default) allows a whole-group
            take-down via the variation parent when no member outside this batch
            is still listed; False refuses every parent-level take-down. See
            delist_all_channel_listings.
        confirmed_count: For > 10 planned take-downs, pass the take_down_count
            from the dry-run manifest to confirm.
        dry_run: If True (default), preview only. Set False to execute.
    """
    return delist_all_channel_listings(
        skus=skus, channels=["Shopify"],
        allow_variation_parent_takedown=allow_variation_parent_takedown,
        confirmed_count=confirmed_count, dry_run=dry_run,
    )


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


# ---------- eBay listing description revise (issue #43) ----------
#
# eBay is not a GLT channel (NON_GLT_SOURCES) — no configurator, no template,
# refresh_channel_listing/unpublish_channel_listing both refuse it by design.
# But Linnworks DOES expose a SEPARATE, dedicated (non-GLT) Listings/ API
# family for eBay specifically — live-probed 25 Aug 2026:
#
#   GET  Listings/GeteBayConfigurators   -> every EBAY configurator, incl.
#                                           AssociatedTemplates count (no body)
#   POST Listings/GeteBayTemplates       -> paged EbayListing templates for
#                                           one configurator (or by
#                                           TemplateIds); body {"parameters":
#                                           {...}} — see gotchas below
#   POST Listings/ProcesseBayListings    -> push an updated EbayListing back
#                                           (204 No Content); fired live
#                                           26 Aug 2026 (issue #47) — ACCEPTED
#                                           (204) but not observed to reach
#                                           the channel on this tenant; see
#                                           the wrapper note below and
#                                           EBAY_CHANNELS["ebay"] above
#
# ⚠️  GeteBayTemplates REQUIRES either a non-zero ConfigId or a non-empty
# TemplateIds — both confirmed live (Source/SubSource alone, or an empty/zero
# parameters object, returns HTTP 400 "Parameter 'parameters' has invalid
# value"). `InventoryItemIds` is documented in the GetTemplatesParameters
# schema but has NO server-side effect (confirmed live: passing a real
# StockItemId alongside a real ConfigId returns the exact same first page as
# omitting it). So there is NO endpoint that answers "which eBay template
# covers SKU/listing X" directly — the only route is to sweep every EBAY
# configurator's templates and match on ListingIds, the same situation
# find_composite_parents (issue #31) hit for composite parents.
#
# ⚠️  eBay's variation shape (live-probed): ONE template = ONE eBay listing.
# For a multi-variation listing the template's top-level SKU is a group-style
# identifier (may not even be a real sellable SKU) and the real child SKUs
# live in the template's Variations[] array, each with its own
# StockItemId/SKU. ListingIds (usually one entry, the eBay item id) is on the
# TEMPLATE — shared by every variation it covers (live-confirmed on
# vnm-triplepads-yellowblack: adult + junior variants, one template, one
# ListingId). This is why dedupe below is done on the CHANNEL-SKU table's
# ChannelReferenceId, which is confirmed live to equal ListingIds[0]
# (vnm_art_cruiser_deck: ChannelReferenceId "287277885799" ==
# GeteBayTemplates ListingIds[0]) — the channel-SKU table already IS the
# per-SKU -> listing-id index, for free, with no eBay-API sweep needed for
# planning.
#
# ⚠️  Sub-source naming mismatch (live-confirmed, same tenant, same SKU,
# 25 Aug 2026): the channel-SKU table's SubSource is "EBAY0" (matches the
# Listings API's AccountId/SubsourceName), but Inventory/
# GetInventoryItemDescriptions' EBAY row uses "EBAY0_UK" — a DIFFERENT
# string. An exact `_effective_channel_value` match against the channel-SKU
# store id would silently miss the real per-channel description override and
# fall back to the (usually absent) default row every time. `store` and
# `sub_source` are NOT interchangeable strings on this API family — matching
# the wrong one silently resolves nothing, exactly the trap this issue named.
# `_ebay_effective_description` below tries the exact match first (the same
# rule used elsewhere in the server), then a case-insensitive PREFIX match —
# the same shape as the Amazon account-prefix precedent in
# `_resolve_glt_target`.
#
# ⚠️  Seller design-template preservation: across ~1,600 live templates read
# while probing this (9 configurators with AssociatedTemplates > 0, every
# template with a populated Description), NONE contained wrapper markers such
# as "Check out our eBay reviews" or "Need Help Deciding?" — Description reads
# as inner content only, byte-identical to the item's own eBay-channel
# description row (vnm_art_cruiser_deck: identical string on both). So
# sending ONLY the Description field — Title, Attributes, Categories, Price
# and everything else on the template are read and carried through UNCHANGED,
# matching the "nulls clear omitted fields" convention this codebase has hit
# on every other Linnworks update endpoint.
#
# Issue #47 (26 Aug 2026) confirmed WHY the wrapper never appears in
# Description: it is composed by the SELLER'S DESIGN TEMPLATE AT PUSH TIME,
# around whatever inner content it is given — Linnworks never stores it. On
# `ven-grip-cleaner` (item 286493672322, in the "EVRi - Single Image"
# configurator — ~150 of this tenant's templates, NOT re-checked across the
# other 32 configurators), a UI-INITIATED push of the same edited description
# — made through the Linnworks listing UI, not this tool's API path — landed
# with BOTH furniture blocks intact. That is UI-initiated evidence about the
# wrapper only. It is NOT evidence about the API path: the API-submitted push
# on that same listing was accepted (204) but was never observed to reach the
# channel at all across a two-hour poll (see
# EBAY_CHANNELS["ebay"]["push_observed_reason"] and CLAUDE.md's v1.48.1
# entry), so it produced no observation one way or the other about what an
# API-initiated push would do to the wrapper. Do not read this as "wrapper
# preservation proven for this tool" — that has not been shown, and the one
# UI observation is scoped to the "EVRi - Single Image" configurator, not all
# 33 on this tenant.
#
# ⚠️  STALE-SNAPSHOT FAMILY (issue #43's own trap list, echoing v1.27.1 —
# ProcessTemplates Update on Shopify pushed a 5-month-stale price and reverted
# a live listing). The push here sends the FULL stored EbayListing template
# back, Description swapped — Title/Price/Categories/Attributes/everything
# else on it is whatever Linnworks last stored, not necessarily current. So
# the template is now resolved (and its Title/Price shown) during PLANNING —
# including on a dry run, not just before a live push — so a human reviewing
# the manifest can see the stored Title/Price before confirming, and title is
# additionally compared against the item's own current base title (free —
# already resolved) so an obviously stale title is flagged rather than only
# shown. Price is shown but NOT independently re-fetched against a "current"
# value here (that would cost another read per plan row); compare the shown
# stored_price against the live listing/Linnworks yourself before confirming
# a batch. A cheap, single-configurator re-sweep after a live push reports
# whether the STORED template's Description now matches what was sent — a
# read-back of Linnworks' own record, not proof the live eBay listing changed
# (that remains unconfirmable — see the verification-surface note above).
#
# ⚠️  A CHANNEL-SKU ROW PROVES A MAPPING, NOT A LIVE LISTING (issue #43's own
# trap list, echoing v1.42.1 — TikTok rows with LastUpdate 0001-01-01 were
# mistaken for live channel state). A non-empty EBAY channel-SKU row here
# only means Linnworks once recorded a mapping; `LastUpdate` of the null date
# 0001-01-01T00:00:00 (or missing) means that mapping has never been
# confirmed by the channel. Each plan row now carries `never_synced_skus` and
# a warning naming them — the listing may not actually be live even though it
# resolved a listing id (see CLAUDE.md assumption #13).
#
# ⚠️  EBAY-SIDE REFUSAL (issue #43's own trap list). eBay can refuse or only
# partially apply a revise on a listing with active bids, a recent sale, or
# certain category restrictions — that comes back as a per-listing CONDITION,
# not a transport error, so `ProcesseBayListings` still returns its ordinary
# 204 and looks like success unless it is read explicitly. The post-push
# read-back template already carries `Status`/`ErrorMessage`, so each live
# result row surfaces them verbatim as `post_push_status` /
# `post_push_error_message` rather than reading them and discarding them —
# this tool does not interpret or block on them, it only reports them.
EBAY_DESCRIPTION_FRAME_URL_TEMPLATE = "https://vi.vipr.ebaydesc.com/itmdesc/{item_id}"
EBAY_NEVER_SYNCED_DATE_PREFIX = "0001-01-01"


def _ebay_effective_description(raw_rows, store: str, default_description):
    """
    The eBay channel description Linnworks would use for `store`, with the
    same fallback shape as `_effective_channel_value` — plus a prefix-match
    fallback for the "EBAY0" vs "EBAY0_UK" sub-source naming mismatch
    (issue #43; see the module note above). Returns (value, source) where
    source is "channel_override" (exact match, the same rule used elsewhere),
    "channel_override_prefix" (eBay-specific fallback), or "base".
    """
    value, source = _effective_channel_value(
        raw_rows, store, "Description", channel_source="EBAY", fallback=None)
    if value:
        return value, source
    store_norm = _norm_conf_name(store)
    if store_norm:
        for r in (raw_rows if isinstance(raw_rows, list) else []):
            if (_norm_conf_name(r.get("Source")) == "ebay"
                    and _norm_conf_name(r.get("SubSource")).startswith(store_norm)):
                val = r.get("Description")
                if val:
                    return val, "channel_override_prefix"
    return default_description, "base"


def _is_never_synced_channel_sku(last_update) -> bool:
    """
    True when a channel-SKU row's LastUpdate is the null date (0001-01-01) —
    or missing entirely — meaning the mapping has never been confirmed by
    the channel (v1.42.1: TikTok rows in this exact state were mistaken for
    live channel state). A row like this proves a MAPPING exists, not that a
    listing is actually live. Missing/falsy is treated as never-synced too —
    "unknown" must not read as "confirmed live".
    """
    if not last_update:
        return True
    return str(last_update).startswith(EBAY_NEVER_SYNCED_DATE_PREFIX)


def _sweep_ebay_config_for_listing(config_id, listing_id: str) -> dict | None:
    """Page Listings/GeteBayTemplates for ONE known ConfigId until a template
    whose ListingIds contains `listing_id` is found, or the pages run out.

    A RateLimitError (quota exhausted) is deliberately NOT caught here — it
    PROPAGATES to the caller. This tool's heaviest quota consumer is this
    sweep (no by-listing-id lookup exists on this API — see the module note
    above), and folding a 429 into the same `None` this function returns for
    a genuine "not found" is exactly the #34/#37 mistake this repo has fixed
    twice elsewhere (assumption #12): a partial run would silently read as a
    complete one, and a quota exhaustion would be reported as "no template
    covers this listing" (QA round 3, issue #43). A `RuntimeError` (a real
    400 from a bad request) still returns None — that IS a data-shaped
    failure, not a quota one."""
    if not config_id:
        return None
    page = 1
    while True:
        try:
            resp = call_linnworks("Listings/GeteBayTemplates", {
                "parameters": {
                    "ConfigId": config_id,
                    "PageNumber": page,
                    "EntriesPerPage": 200,
                    "TemplatesType": "Both",
                }
            })
        except RuntimeError:
            return None
        items = resp.get("Items") if isinstance(resp, dict) else None
        if not items:
            return None
        for it in items:
            if listing_id in (it.get("ListingIds") or []):
                return it
        total = resp.get("TotalItems") or 0
        if page * 200 >= total:
            return None
        page += 1


def _find_ebay_template_for_listing(listing_id: str, config_id: str | None = None) -> dict | None:
    """
    Locate the eBay template serving `listing_id`.

    There is no direct by-listing-id, by-SKU or by-inventory-item lookup on
    Listings/GeteBayTemplates (see the module note above) — ConfigId or
    TemplateIds is mandatory and InventoryItemIds is a no-op server-side.

    If `config_id` is already known (e.g. re-checking a template just pushed,
    whose ConfigId was already read), this sweeps ONLY that one configurator —
    cheap, used for the post-push read-back. Otherwise it walks every EBAY
    configurator that has AssociatedTemplates > 0 (from GeteBayConfigurators),
    largest first, until a match is found. Returns the raw EbayListing dict,
    or None if nothing covers it.

    Costs are bounded by this tenant's eBay footprint (live-probed: ~1,600
    templates across 9 populated configurators) but are NOT capped here beyond
    that. Called during PLANNING (including on a dry run — issue #43's own
    stale-snapshot trap requires the stored Title/Price to be shown in the
    manifest before a write is confirmed, not just before a live push) and
    once more, config-scoped, after a live push as a read-back. The live push
    itself was fired for the first time 26 Aug 2026 (issue #47) — accepted
    (204) but not observed to reach the channel; see
    EBAY_CHANNELS["ebay"]["push_observed_reason"].

    Raises `RateLimitError` (does NOT catch/swallow it) when the quota is
    exhausted, at either the configurator list or the template sweep — the
    caller must bucket that separately from "no template found" (QA round 3).
    A `RuntimeError` still degrades to None, same as the sweep helper.
    """
    if config_id:
        return _sweep_ebay_config_for_listing(config_id, listing_id)
    try:
        configs = call_linnworks_get("Listings/GeteBayConfigurators")
    except RuntimeError:
        return None
    if not isinstance(configs, list):
        return None
    populated = sorted(
        (c for c in configs if isinstance(c, dict) and c.get("AssociatedTemplates")),
        key=lambda c: -(c.get("AssociatedTemplates") or 0),
    )
    for cfg in populated:
        found = _sweep_ebay_config_for_listing(cfg.get("pkConfigId"), listing_id)
        if found is not None:
            return found
    return None


def _ebay_template_staleness(template: dict, current_title) -> dict:
    """
    Surface an eBay template's STORED Title/Price and, where possible, flag
    them as stale against the item's CURRENT value — the same stale-snapshot
    hazard v1.27.1 hit on Shopify (ProcessTemplates Update silently pushed a
    5-month-old price). A revise here sends the WHOLE stored template back
    with only Description swapped, so Title/Price/etc. go live exactly as
    stored — "whatever gets pushed must be shown", per the brief's own trap.

    Title is compared against `current_title` (the item's base ItemTitle,
    already resolved for free — no extra read). Price is shown but NOT
    independently re-compared here (that would cost another read per plan
    row, and a multi-variation template's top-level Price is meaningless
    per-variant anyway) — review the shown `stored_price` yourself. Never
    raises; a missing comparable value is reported as "not compared", never
    silently as "matches".
    """
    stored_title = template.get("Title")
    stored_price = template.get("Price")
    out: dict = {
        "stored_title": stored_title,
        "stored_price": stored_price,
        "title_stale": None,
    }
    if stored_title is not None and current_title is not None:
        out["title_stale"] = str(stored_title).strip() != str(current_title).strip()
    if out["title_stale"]:
        out["warning"] = (
            f"STALE SNAPSHOT — the stored template title ('{stored_title}') disagrees "
            f"with the item's current title ('{current_title}'). Pushing this template "
            "re-sends the STORED title/price and will overwrite the current one on the "
            "live eBay listing (the same hazard the Shopify GLT push hit in v1.27.1) — "
            "review stored_price above too before confirming."
        )
    else:
        out["warning"] = (
            "Title shows no detectable difference, but stored_price is NOT independently "
            "re-checked here — review it against the live listing before confirming."
        )
    return out


@mcp.tool()
def revise_ebay_listing_description(
    skus: list[str],
    store: str = "EBAY0",
    confirmed_count: int | None = None,
    dry_run: bool = True,
) -> dict:
    """
    Revise an EXISTING eBay listing's description to match Linnworks' current
    eBay-channel description for the item(s) — the first eBay write capability
    in this server (issue #43).

    ⚠️  eBay is NOT a GLT channel (see NON_GLT_SOURCES) — refresh_channel_listing
    and unpublish_channel_listing both refuse it. This tool uses a SEPARATE,
    dedicated (non-GLT) Linnworks API family instead: Listings/GeteBayConfigurators
    + Listings/GeteBayTemplates (read) and Listings/ProcesseBayListings (write).
    Confirmed live 25 Aug 2026 — see CLAUDE.md's confirmed-endpoints table for
    the exact request/response evidence. It does NOT change what
    refresh_channel_listing/unpublish_channel_listing can do, and it cannot
    create, end or relist a listing — revise of an EXISTING listing's
    description only.

    ⚠️  ACCEPTED BUT NOT OBSERVED TO PROCESS — revise_proven still False.
    Listings/ProcesseBayListings WAS fired live on 26 Aug 2026 (issue #47)
    against a real listing (ven-grip-cleaner, item 286493672322): Linnworks
    ACCEPTED the push (204), but across a two-hour poll of the
    description-frame URL no channel-side effect was observed, while the
    identical edit pushed from the Linnworks listing UI landed within
    minutes. See EBAY_CHANNELS["ebay"]["push_observed_reason"] for the full
    observation and CLAUDE.md's v1.48.1 entry for the write-up. A
    `processed`/`unconfirmed` result here means Linnworks ACCEPTED the push,
    never that the listing changed. EBAY_CHANNELS["ebay"]["revise_proven"]
    stays False on that EVIDENCE, not on caution, until a fresh proof lands
    (see CLAUDE.md's post-merge verification steps). Until then, the route
    that works today for propagating a description edit is the Linnworks
    listing UI — review this tool's dry-run manifest first, then make the
    same edit there.

    ⚠️  STALE-SNAPSHOT FAMILY (v1.27.1 echo). The push sends the FULL stored
    eBay template back with only Description swapped — Title, Price and
    everything else on it goes live exactly as Linnworks last stored it, not
    necessarily as it is now. So every plan row resolves the template (even
    on a dry run) and shows `staleness.stored_title` / `staleness.stored_price`
    plus a `title_stale` flag (title compared against the item's own current
    title for free; price is shown but not independently re-fetched) —
    "whatever gets pushed must be shown in the manifest", per the trap this
    issue itself names. A live push also re-checks the template afterwards
    (`post_push_description_matches` in each result row) — a read-back of
    Linnworks' own stored record, not proof the live eBay listing changed.

    ⚠️  EBAY-SIDE REFUSAL. eBay can refuse or only partially apply a revise —
    active bids, a recent sale, or certain category restrictions all come back
    as a per-listing CONDITION, not a transport error, so `ProcesseBayListings`
    still returns its ordinary 204 and this tool still reports `unconfirmed`.
    The post-push read-back template's `Status`/`ErrorMessage` fields are the
    only place that condition is visible to us, so each result row carries
    them verbatim as `post_push_status` / `post_push_error_message` (None when
    the read-back itself failed) — read them before treating an `unconfirmed`
    row as good news; this tool does NOT interpret them or block on them.

    ⚠️  A CHANNEL-SKU ROW PROVES A MAPPING, NOT A LIVE LISTING. Judging "is it
    listed" from a non-empty channel-SKU row alone repeats the v1.42.1 TikTok
    incident (rows with LastUpdate 0001-01-01 mistaken for live channel
    state). Each plan row carries `never_synced_skus` — covered SKUs whose
    eBay channel-SKU row has a null/missing LastUpdate, i.e. this mapping has
    never been confirmed by the channel — with a `never_synced_warning` when
    non-empty. These SKUs are still planned (not auto-blocked), because a
    never-synced mapping is a caution for review, not proof the listing is
    dead.

    ⚠️  VERIFICATION SURFACE. Do NOT judge success from the eBay item page —
    the description sits in a cross-origin frame there and a fresh push can
    render as unchanged even when it landed. Check the description-frame URL
    directly (https://vi.vipr.ebaydesc.com/itmdesc/<item_id> — see
    EBAY_DESCRIPTION_FRAME_URL_TEMPLATE), which serves the description content
    on its own.

    ⚠️  MULTI-VARIATION LISTINGS. One eBay item id can cover many Linnworks
    SKUs (eBay variations). SKUs are deduped to ONE plan row per eBay listing
    id (the channel-SKU table's ChannelReferenceId) — a naive per-SKU loop
    would revise the SAME listing once per covered SKU, burning revise quota
    and making any failure impossible to attribute to one push.

    ⚠️  SELLER DESIGN TEMPLATE. eBay listings can render with a seller-defined
    wrapper (headers/footers, "Check out our eBay reviews", etc.) around the
    description. This tool ONLY ever sets the Description field of the
    existing eBay template — Title, Attributes, Categories, Price and
    everything else on the template are read and carried through UNCHANGED
    (the ProcesseBayListings payload is the full existing EbayListing object
    with only Description replaced), matching the "nulls clear omitted
    fields" convention this codebase has hit on every other Linnworks update
    endpoint. Live evidence (this build, 25 Aug 2026): across ~1,600 real
    eBay templates read while building this tool, Description consistently
    held plain inner content, byte-identical to the item's own eBay-channel
    description row, with NO wrapper markers found in any of them —
    supporting that Description is inner-content-only and that this risk is
    structural (Linnworks never stores the wrapper), not something this tool
    could accidentally strip. This has NOT been checked against a listing
    known to use a visible design wrapper — verify on one before trusting a
    bulk run.

    Flow per SKU (read-before-write):
      1. Resolve SKU -> StockItemId (Inventory/GetInventoryItem).
      2. Read the channel-SKU link table (BatchGetInventoryItemChannelSKUs)
         and keep only rows whose Source is EBAY and whose SubSource matches
         `store` case-insensitively. No match -> not_listed.
      3. Group matched SKUs by ChannelReferenceId (the eBay item id) — this
         is the listing-id dedupe. One plan row per listing; covers_skus
         lists every SKU it serves.
      4. For each plan row, resolve the description to push: the item's own
         eBay-channel description row for `store` (Inventory/
         GetInventoryItemDescriptions, EBAY source), falling back to the
         item's default (blank Source/SubSource) description row via the
         same effective-value rule refresh_channel_listing's staleness check
         uses (_effective_channel_value), plus an eBay-specific prefix-match
         fallback for the "EBAY0" vs "EBAY0_UK" sub-source naming mismatch
         (live-confirmed same tenant, same SKU — see CLAUDE.md). No usable
         description on any covered SKU -> the row is `blocked` with a named
         reason, and NOTHING is pushed with empty/default content.
      5. For each UNBLOCKED plan row (dry run included — see the
         stale-snapshot note above), locate the eBay template serving that
         listing id by sweeping GeteBayConfigurators + GeteBayTemplates (no
         direct lookup exists — see the module note above
         _find_ebay_template_for_listing) and record its stored Title/Price
         (`staleness`) plus whether the title looks stale. The found template
         is cached for the rest of this call, so a live run right after does
         NOT re-sweep for it. This sweep is the tool's heaviest quota
         consumer; a RateLimitError here is NEVER folded into "not found" —
         the row is bucketed into `rate_limited` (flipping `complete` false)
         and blocked with reason "rate_limited_locating_template". A genuine
         "no template covers this listing" (no quota issue) ALSO marks the
         row `blocked` (reason "could not locate the eBay template serving
         this listing id"), so the dry-run pushable count never promises a
         push the live run would then refuse (QA round 3).
      6. (live run only) Blocked rows (incl. both flavours above) never
         reach Listings/ProcesseBayListings. Found -> push the full template
         object with only Description replaced via Listings/
         ProcesseBayListings, then re-check (config-scoped, cheap) whether
         the template's Description now matches what was sent
         (`post_push_description_matches`), and carry that same read-back's
         `Status`/`ErrorMessage` through verbatim (`post_push_status` /
         `post_push_error_message`) — eBay's own per-listing refusal (active
         bids, a recent sale, category restrictions) surfaces there, not as a
         call failure. A RateLimitError on THIS read-back is also bucketed
         into `rate_limited` (`post_push_read_back_rate_limited: True`)
         rather than silently reading as "read back found nothing".
      7. Every live-run row that reached a push is reported `unconfirmed`
         (never `success`) — a 204/accepted response is not evidence the
         listing actually changed. A row blocked purely by a locate-time
         quota failure is reported `rate_limited`, not `blocked`.

    Staging: threshold 10 (the destructive tier shared with
    unpublish_channel_listing / delist_all_channel_listings — this writes to
    a live, customer-facing, NOT-yet-proven channel). For batches over that
    this returns the plan + manifest and asks for confirmed_count=<N> before
    executing.

    Args:
        skus: Exact SKUs / ItemNumbers whose eBay listing description to
            revise.
        store: eBay account/store identifier, matched case-insensitively
            against the channel-SKU table's SubSource. Default "EBAY0" (this
            tenant's only eBay account, confirmed live via
            Listings/GetAllEbayConfigurators 25 Aug 2026 — see CLAUDE.md).
        confirmed_count: For batches over the threshold, pass len(skus) after
            reviewing the plan to confirm the write.
        dry_run: If True (default), returns the plan without pushing
            anything. A dry run makes ZERO calls to
            Listings/ProcesseBayListings (no write). It DOES sweep
            GeteBayConfigurators/GeteBayTemplates for each unblocked plan row
            to preview the stored Title/Price (the stale-snapshot check) —
            that's a read, not a write, and it's what lets the manifest show
            what would actually be pushed.

    Returns:
        A dict with:
          - dry_run, sku_count, store, revise_proven, push_observed_state,
            verification_note
          - plan: one row per eBay listing id — listing_id, covers_skus,
            description (the value that would be/was pushed),
            description_source (channel_override / channel_override_prefix /
            base), blocked (bool), blocked_reason (when blocked — includes
            "rate_limited_locating_template" when the template sweep hit the
            Linnworks quota, and "could not locate the eBay template serving
            this listing id" when it genuinely found nothing; both mark
            `blocked: True` so a dry run never counts an unpushable row as
            pushable), never_synced_skus (covered SKUs whose channel-SKU row
            has never synced — see the trap note above), never_synced_warning
            (when non-empty), template_found (bool|None — None when blocked
            before resolution was attempted, or when the sweep hit a quota
            failure), staleness (stored_title / stored_price / title_stale /
            warning — None if the template could not be located), revise_proven
            (from EBAY_CHANNELS — derived per row, not hard-coded, same as the
            top-level flag), push_observed_state (from EBAY_CHANNELS — the
            observation-state code, e.g. "accepted_but_not_processed";
            subordinate to revise_proven, which remains the single gate)
          - not_listed: SKUs with no matching eBay channel-SKU row for `store`
          - unresolved: SKUs that failed to resolve
          - rate_limited: SKUs/reads/listings that hit the Linnworks quota — a
            429 here is NEVER folded into not_listed, unresolved, or reported
            as "template not found"; covers SKU resolution, the channel-SKU
            read, the description read, the template-locate sweep, and the
            post-push read-back
          - complete: False whenever anything landed in rate_limited
          - results: per-row outcome (live run only) — each row's outcome is
            one of `unconfirmed`, `blocked`, `push_failed`, `rate_limited`
            (the last one reserved for a locate-time quota failure, so it is
            never confused with an ordinary block); never `success`; an
            `unconfirmed` row also carries `post_push_description_matches`
            (bool|None), `post_push_read_back_rate_limited` (bool — True when
            the post-push re-check itself hit the quota, so a None match
            isn't mistaken for "read back found nothing"), `post_push_status`
            and `post_push_error_message` (both from the read-back template
            verbatim, None if the read-back failed) — eBay's own per-listing
            refusal (active bids, a recent sale, category restrictions) shows
            up here, not as an exception, and this tool does not act on it
          - message
    """
    if not skus:
        raise ValueError("skus must contain at least one SKU.")
    _check_injection("store", store or "")

    resolved, titles, unresolved, rate_limited = _resolve_bulk_inputs(skus, None)

    not_listed: list[dict] = []
    planned_skus: list[dict] = []   # {sku, sid, listing_id, last_update}

    channel_rows_by_sid: dict[str, list] = {}
    if resolved:
        sid_list = [sid for _sku, sid in resolved]
        try:
            channel_rows_by_sid = _fetch_channel_skus_for_ids(sid_list)
        except RateLimitError as exc:
            for sku, _sid in resolved:
                rate_limited.append({"sku": sku, "error": str(exc)})
            resolved = []
        except RuntimeError as exc:
            for sku, _sid in resolved:
                unresolved.append({"sku": sku, "error": f"channel-SKU read failed: {exc}"})
            resolved = []

    for sku, sid in resolved:
        rows = channel_rows_by_sid.get(sid.lower(), [])
        ebay_rows = [
            r for r in rows
            if _norm_conf_name(r.get("Source")) == "ebay"
            and _norm_conf_name(r.get("SubSource")) == _norm_conf_name(store)
        ]
        if not ebay_rows:
            not_listed.append({
                "sku": sku, "stock_item_id": sid,
                "reason": f"no eBay channel-SKU row for store '{store}'",
            })
            continue
        listing_id = ebay_rows[0].get("ChannelReferenceId")
        if not listing_id:
            not_listed.append({
                "sku": sku, "stock_item_id": sid,
                "reason": "eBay channel-SKU row has no ChannelReferenceId (listing id)",
            })
            continue
        planned_skus.append({
            "sku": sku, "sid": sid, "listing_id": listing_id,
            "last_update": ebay_rows[0].get("LastUpdate"),
        })

    # ── Group by listing id (the dedupe) and resolve each row's description ──
    by_listing: dict[str, dict] = {}
    for row in planned_skus:
        entry = by_listing.setdefault(row["listing_id"], {
            "listing_id": row["listing_id"], "covers_skus": [], "sids": [],
            "never_synced_skus": [],
        })
        entry["covers_skus"].append(row["sku"])
        entry["sids"].append(row["sid"])
        if _is_never_synced_channel_sku(row["last_update"]):
            entry["never_synced_skus"].append(row["sku"])

    template_by_listing: dict[str, dict | None] = {}

    plan: list[dict] = []
    for listing_id, entry in by_listing.items():
        description = None
        description_source = None
        blocked = False
        blocked_reason = None
        mismatched: list[str] = []
        for sku, sid in zip(entry["covers_skus"], entry["sids"]):
            try:
                raw_desc_rows = call_linnworks_get(
                    "Inventory/GetInventoryItemDescriptions", {"inventoryItemId": sid})
            except RateLimitError as exc:
                rate_limited.append({"sku": sku, "error": str(exc)})
                blocked = True
                blocked_reason = "rate_limited_reading_description"
                continue
            except RuntimeError as exc:
                blocked = True
                blocked_reason = f"description read failed: {exc}"
                continue
            default_desc = next(
                (r.get("Description") for r in raw_desc_rows
                 if isinstance(raw_desc_rows, list) and not r.get("Source")
                 and not r.get("SubSource")),
                None,
            )
            sku_desc, sku_desc_source = _ebay_effective_description(
                raw_desc_rows, store, default_desc)
            if not sku_desc or not str(sku_desc).strip():
                blocked = True
                blocked_reason = blocked_reason or f"no usable description for SKU '{sku}'"
                continue
            if description is None:
                description, description_source = sku_desc, sku_desc_source
            elif sku_desc != description:
                mismatched.append(sku)

        if description is None and not blocked:
            blocked = True
            blocked_reason = "no usable description found for any covered SKU"

        row_out = {
            "listing_id": listing_id,
            "covers_skus": entry["covers_skus"],
            "description": description,
            "description_source": description_source,
            "blocked": blocked,
            "blocked_reason": blocked_reason,
            "never_synced_skus": entry["never_synced_skus"],
            "revise_proven": EBAY_CHANNELS["ebay"]["revise_proven"],
            "push_observed_state": EBAY_CHANNELS["ebay"]["push_observed_state"],
        }
        if entry["never_synced_skus"]:
            row_out["never_synced_warning"] = (
                f"{len(entry['never_synced_skus'])} covered SKU(s) "
                f"({', '.join(entry['never_synced_skus'])}) have an eBay channel-SKU "
                "LastUpdate of 0001-01-01 (or missing) — this mapping has never been "
                "confirmed by the channel, so the listing may not actually be live even "
                "though it resolved a listing id (see CLAUDE.md assumption #13)."
            )
        if mismatched:
            row_out["description_mismatch_skus"] = mismatched
            row_out["warning"] = (
                "Covered SKUs disagree on eBay description content — pushing the first "
                "resolved SKU's value; review before a live run."
            )

        # ── Stale-snapshot preview: resolve the template even on a dry run,
        # so whatever would actually be pushed (Title/Price on the stored
        # template) is visible in the manifest, not just discovered on a
        # live push. Cached here so a live run right after doesn't re-sweep.
        #
        # A RateLimitError here (QA round 3, issue #43) must NOT be folded
        # into "template not found" — that is the same #34/#37 quota mistake
        # already fixed twice elsewhere. This sweep is the tool's heaviest
        # quota consumer (no direct by-listing-id lookup exists — see the
        # module note above), and it now runs on every unblocked row of
        # every call, dry run included. A quota failure here is bucketed
        # into rate_limited (which flips `complete` false) and the row is
        # BLOCKED with a distinct reason — never reported as "not found".
        if not blocked:
            try:
                template = _find_ebay_template_for_listing(listing_id)
            except RateLimitError as exc:
                rate_limited.append({
                    "listing_id": listing_id, "skus": entry["covers_skus"],
                    "error": str(exc),
                })
                template = None
                template_by_listing[listing_id] = None
                blocked = True
                blocked_reason = "rate_limited_locating_template"
                row_out["blocked"] = True
                row_out["blocked_reason"] = blocked_reason
                row_out["template_found"] = None
                row_out["staleness"] = None
            else:
                template_by_listing[listing_id] = template
                row_out["template_found"] = template is not None
                if template is not None:
                    first_sid = entry["sids"][0]
                    current_title = titles.get(first_sid.lower())
                    row_out["staleness"] = _ebay_template_staleness(template, current_title)
                else:
                    row_out["staleness"] = None
                    row_out["stale_snapshot_warning"] = (
                        "Could not locate the eBay template for this listing — stored "
                        "Title/Price cannot be previewed."
                    )
                    # A row whose template can't be located can never actually
                    # be pushed (the live loop below refuses it) — reflect
                    # that in `blocked` here too, so the dry-run "N would be
                    # revised" count doesn't promise a push the live run will
                    # then refuse (QA round 3's related finding).
                    blocked = True
                    blocked_reason = "could not locate the eBay template serving this listing id"
                    row_out["blocked"] = True
                    row_out["blocked_reason"] = blocked_reason
        else:
            row_out["template_found"] = None
            row_out["staleness"] = None

        plan.append(row_out)

    verification_note = (
        "An 'unconfirmed' result is NOT evidence the listing changed — Linnworks' 204 "
        "response only means the push was ACCEPTED. Verify on the description-frame URL "
        f"({EBAY_DESCRIPTION_FRAME_URL_TEMPLATE.format(item_id='<item_id>')}), never the item "
        "page — the description sits in a cross-origin frame there and can report stale/old "
        f"text even when the new description is already live. Proven eBay accounts: "
        f"{_proven_ebay_revise_channels()}. {_ebay_push_observation_reason()} "
        f"{EBAY_UI_WORKAROUND_NOTE}"
    )

    base_out = {
        "sku_count": len(skus),
        "store": store,
        "revise_proven": EBAY_CHANNELS["ebay"]["revise_proven"],
        "push_observed_state": EBAY_CHANNELS["ebay"]["push_observed_state"],
        "verification_note": verification_note,
        "plan": plan,
        "not_listed": not_listed,
        "unresolved": unresolved,
        "rate_limited": rate_limited,
        "complete": not rate_limited,
    }

    guard = _write_guard("revise_ebay_listing_description", skus, confirmed_count, dry_run)
    if guard is not None:
        return {**guard, **base_out}

    pushable = [r for r in plan if not r["blocked"]]

    if dry_run:
        return {
            "dry_run": True, **base_out,
            "message": (
                f"{len(pushable)} listing(s) would be revised, {len(plan) - len(pushable)} "
                f"blocked, {len(not_listed)} not listed on '{store}', {len(unresolved)} "
                f"unresolved, {len(rate_limited)} rate-limited. No write calls were made. "
                + verification_note
            ),
        }

    # ── Live run ──────────────────────────────────────────────────────────
    results: list[dict] = []
    for row in plan:
        if row["blocked"]:
            # A quota failure while locating the template is a distinct
            # outcome from an ordinary block (QA round 3) — never "blocked",
            # which this criterion treats the same as a data-shaped failure.
            outcome = (
                "rate_limited" if row.get("blocked_reason") == "rate_limited_locating_template"
                else "blocked"
            )
            results.append({**row, "outcome": outcome})
            continue
        # Reuse the template resolved during planning above — it was already
        # swept for (dry run or not), so this never sweeps twice in one call.
        template = template_by_listing.get(row["listing_id"])
        if template is None:
            results.append({
                **row, "outcome": "blocked",
                "error": "could not locate the eBay template serving this listing id",
            })
            continue
        payload = dict(template)
        payload["Description"] = row["description"]
        try:
            call_linnworks_void("Listings/ProcesseBayListings", {
                "items": [payload], "force": True, "action": "Update",
            })
        except RateLimitError as exc:
            rate_limited.append({"listing_id": row["listing_id"], "error": str(exc)})
            results.append({**row, "outcome": "rate_limited", "error": str(exc)})
            continue
        except RuntimeError as exc:
            results.append({**row, "outcome": "push_failed", "error": str(exc)})
            continue
        # Read-back: re-fetch (cheap, config-scoped) the STORED template and
        # confirm it now shows the Description we sent. This confirms
        # Linnworks' own record, NOT that the live eBay listing changed (see
        # the verification-surface warning above) — the closest thing to a
        # read-back this API allows.
        #
        # A quota failure here (QA round 3) must not be silently
        # indistinguishable from a clean read-back that simply found nothing
        # — it is bucketed into rate_limited (flipping `complete` false) and
        # flagged on the row, rather than folded into the ordinary
        # post_push_description_matches: None case.
        try:
            fresh = _find_ebay_template_for_listing(
                row["listing_id"], config_id=template.get("ConfigId"))
            post_push_read_back_rate_limited = False
        except RateLimitError as exc:
            rate_limited.append({"listing_id": row["listing_id"], "error": str(exc)})
            fresh = None
            post_push_read_back_rate_limited = True
        post_push_match = (
            fresh.get("Description") == row["description"]
            if isinstance(fresh, dict) else None
        )
        # eBay can refuse or partially apply a revise (active bids, recent
        # sales, category restrictions) — a per-listing CONDITION, not a
        # transport error, so it never raises and a bodyless 204 can't carry
        # it either. The re-read template is the only place this signal is
        # visible to us; surface it rather than reading it and discarding it.
        post_push_status = fresh.get("Status") if isinstance(fresh, dict) else None
        post_push_error_message = (
            fresh.get("ErrorMessage") if isinstance(fresh, dict) else None
        )
        results.append({
            **row, "outcome": "unconfirmed",
            "post_push_description_matches": post_push_match,
            "post_push_status": post_push_status,
            "post_push_error_message": post_push_error_message,
            "post_push_read_back_rate_limited": post_push_read_back_rate_limited,
        })

    base_out["rate_limited"] = rate_limited
    base_out["complete"] = not rate_limited
    unconfirmed_count = sum(1 for r in results if r["outcome"] == "unconfirmed")
    return {
        "dry_run": False, **base_out, "results": results,
        "message": (
            f"{unconfirmed_count} push(es) accepted (unconfirmed — acceptance is not "
            "evidence of change). " + verification_note
        ),
    }


# ---------- Repair a channel listing's images (Shopify Admin API, write) ----------
#
# Issue #41. The one image job the GLT cannot do.
#
# When an item's images are corrected in Linnworks there is NO supported route
# to get the new picture onto an EXISTING Shopify listing:
#   - refresh_channel_listing pushes the template's STORED snapshot (#27/#40), so
#     it re-sends the old — sometimes deleted — URL and silently no-ops. Worse,
#     ProcessTemplates returns an empty 2xx either way, so the no-op reads as a fix.
#   - re-listing is not an option on a live product (loses reviews/ranking/handle).
# Every occurrence so far was fixed by hand through the Shopify Admin API.
#
# So this tool deliberately BYPASSES the GLT and talks to Shopify directly. That
# also means it stays useful whether or not #27 is ever unblocked.
#
# ── The matching key (live discovery, 19 Aug 2026) ────────────────────────────
# Shopify's CDN filename PRESERVES the Linnworks image GUID:
#   Linnworks image_id 88b7b1da-a06b-4439-b93e-9f4a7ec310f2
#   Shopify  image.url .../files/88b7b1da-a06b-4439-b93e-9f4a7ec310f2.jpg?v=...
# Confirmed across 3 products / 4 media on SWH Shopify. That gives a per-image
# identity, which is exactly what #40's staleness check could NOT have (the GLT
# template exposes only an image COUNT, so "1 == 1" hid a wrong picture).
#
# Media whose stem is NOT a GUID was not put there by Linnworks — a hand-uploaded
# photo, a lifestyle shot, a size chart. Those are reported as `unmanaged` and are
# NEVER auto-removed.
#
# ── Mutations (all validated against the live Admin schema, none deprecated) ──
#   add      productUpdate(product:{id}, media:[CreateMediaInput!])
#   featured productReorderMedia(id, moves:[MoveInput!])   (zero-based newPosition)
#   remove   fileUpdate(files:[{id, referencesToRemove:[productId]}])
# NB the manual procedure used productCreateMedia + productDeleteMedia; both are
# now DEPRECATED (-> productUpdate / fileUpdate). fileUpdate DETACHES the media
# from the product and leaves the file in Shopify Files, which is strictly safer
# and recoverable — productDeleteMedia destroyed it.
#
# Required Admin scopes: read_products, write_products, read_files, write_files.

SHOPIFY_API_VERSION = os.environ.get("SHOPIFY_API_VERSION", "2026-01")

# A Linnworks image id embedded in a Shopify CDN filename.
_GUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)

# ⚠️  Shopify UNIQUIFIES a filename when one of that name already exists in Files,
# appending "_<uuid>":  7385b433-….jpg  ->  7385b433-…_debe98f1-….jpg
# (live-proven 19 Aug 2026 on a throwaway product). A bare-GUID match therefore
# MISSES the image, which is worse than it sounds: the media reads as `unmanaged`
# (so it is never detached — the safe direction) but the Linnworks image reads as
# MISSING, so the tool re-attaches it, and does so again on every subsequent run.
# Unbounded duplicate growth.
#
# It is not a rare edge either — the collision happens whenever that Linnworks
# image already exists in the store's Files, and **a detached file stays in Files**,
# so this tool's own detach-then-reattach cycle is precisely what triggers it.
_GUID_PREFIX_RE = re.compile(
    r"^([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?:_|$)", re.I
)


def _linnworks_image_id(stem: str | None) -> str | None:
    """The Linnworks image id a Shopify filename stem carries, or None.

    Accepts both the plain form and Shopify's uniquified "<guid>_<uuid>" form.
    None means the file was not put there by Linnworks (a hand-uploaded photo,
    size chart or lifestyle shot) — those are reported `unmanaged` and never removed.
    """
    if not stem:
        return None
    m = _GUID_PREFIX_RE.match(stem.strip())
    return m.group(1).lower() if m else None


def _shopify_store_for(sub_source: str) -> dict | None:
    """Resolve a Linnworks SubSource ("SWH Shopify") -> Shopify Admin credentials.

    Two config shapes, because this tenant runs FIVE Shopify stores (SWH 18,
    Venom 21, Icarus 26, Lobster 29, TWG B2B 34) but most repairs only ever touch
    one of them:

      SHOPIFY_STORES  — JSON map, the multi-store form:
          {"SWH Shopify": {"shop_domain": "x.myshopify.com",
                           "access_token": "shpat_..."}}
      SHOPIFY_SHOP_DOMAIN + SHOPIFY_ADMIN_ACCESS_TOKEN — the single-store form,
          applied to SHOPIFY_DEFAULT_SUB_SOURCE (default "SWH Shopify").

    Returns None when nothing is configured for this store — the caller turns
    that into an actionable setup message rather than a stack trace.
    """
    want = (sub_source or "").strip().lower()

    raw = os.environ.get("SHOPIFY_STORES")
    if raw:
        try:
            stores = json.loads(raw)
        except ValueError as e:
            raise ValueError(f"SHOPIFY_STORES is not valid JSON: {e}") from e
        for name, cfg in (stores or {}).items():
            if (name or "").strip().lower() == want:
                dom, tok = cfg.get("shop_domain"), cfg.get("access_token")
                if dom and tok:
                    return {"sub_source": name, "shop_domain": dom, "access_token": tok}

    default_ss = os.environ.get("SHOPIFY_DEFAULT_SUB_SOURCE", "SWH Shopify")
    dom = os.environ.get("SHOPIFY_SHOP_DOMAIN")
    tok = os.environ.get("SHOPIFY_ADMIN_ACCESS_TOKEN")
    if dom and tok and (default_ss or "").strip().lower() == want:
        return {"sub_source": default_ss, "shop_domain": dom, "access_token": tok}

    return None


def _shopify_setup_error(sub_source: str) -> dict:
    """The one place that explains how to configure Shopify Admin access."""
    return {
        "error": (
            f"No Shopify Admin credentials configured for store '{sub_source}'. This tool "
            "writes to Shopify directly (the GLT cannot fix listing images — see #40), so it "
            "needs an Admin API access token that Linnworks does not provide."
        ),
        "how_to_fix": [
            "In Shopify admin: Settings > Apps and sales channels > Develop apps > Create an app.",
            "Grant Admin API scopes: read_products, write_products, read_files, write_files.",
            "Install the app and copy the Admin API access token (shpat_...).",
            "Add it to the linnworks MCP env — either single-store:",
            "    SHOPIFY_SHOP_DOMAIN=your-store.myshopify.com",
            "    SHOPIFY_ADMIN_ACCESS_TOKEN=shpat_...",
            f"    SHOPIFY_DEFAULT_SUB_SOURCE={sub_source}",
            "  or multi-store (JSON, one entry per Linnworks SubSource):",
            '    SHOPIFY_STORES={"SWH Shopify": {"shop_domain": "...", "access_token": "shpat_..."}}',
            "Restart Claude Desktop so the server picks the new env up.",
        ],
        "shopify_configured": False,
    }


def _shopify_graphql(store: dict, query: str, variables: dict) -> dict:
    """POST one GraphQL document to the Shopify Admin API and return `data`.

    Raises RuntimeError with the response body on transport or GraphQL errors —
    same house rule as call_linnworks: surface the real reason verbatim.

    Shopify throttles on a leaky-bucket query COST (not a request count), and
    signals it with HTTP 429 or a THROTTLED error extension. Both are retried on
    the same 5/10/20/30s ladder the Linnworks helpers use (#34/#37), so a quota
    pause is never mistaken for a data failure.
    """
    url = f"https://{store['shop_domain']}/admin/api/{SHOPIFY_API_VERSION}/graphql.json"
    headers = {
        "X-Shopify-Access-Token": store["access_token"],
        "Content-Type": "application/json",
    }

    for attempt, pause in enumerate([5, 10, 20, 30, None]):
        resp = requests.post(
            url, headers=headers, json={"query": query, "variables": variables}, timeout=60
        )

        throttled = resp.status_code == 429
        body: dict = {}
        if not throttled:
            if resp.status_code >= 400:
                raise RuntimeError(
                    f"Shopify Admin API HTTP {resp.status_code} on {store['shop_domain']}: "
                    f"{resp.text[:600]}"
                )
            try:
                body = resp.json()
            except ValueError as e:
                raise RuntimeError(f"Shopify returned non-JSON: {resp.text[:400]}") from e

            errors = body.get("errors") or []
            throttled = any(
                (e.get("extensions") or {}).get("code") == "THROTTLED" for e in errors
            )
            if errors and not throttled:
                raise RuntimeError(f"Shopify GraphQL errors: {json.dumps(errors)[:600]}")

        if not throttled:
            return body.get("data") or {}

        if pause is None:
            raise RuntimeError(
                f"Shopify API still throttled after backoff ({store['shop_domain']}). "
                "Retry shortly — this is a quota pause, not a data problem."
            )
        time.sleep(pause)

    return {}


def _shopify_product_gid(channel_reference_id: str | None) -> str | None:
    """Linnworks' Shopify ChannelReferenceId -> the product GID.

    The reference is a `product:variant:inventory` id triple — live-confirmed on
    this tenant, e.g. "9495050125558:51459308912886:52770497429750". The FIRST
    segment is the product, which is what carries the media. A variant-scoped id
    would be the wrong object entirely, so parse rather than assume.
    """
    ref = (channel_reference_id or "").strip()
    if not ref:
        return None
    head = ref.split(":")[0].strip()
    return f"gid://shopify/Product/{head}" if head.isdigit() else None


def _media_filename_stem(url: str | None) -> str | None:
    """Filename stem of a Shopify CDN image URL, minus query string and extension.

    ".../files/88b7b1da-a06b-4439-b93e-9f4a7ec310f2.jpg?v=1787068333"
        -> "88b7b1da-a06b-4439-b93e-9f4a7ec310f2"
    """
    if not url:
        return None
    name = url.split("?")[0].rstrip("/").rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[0] if "." in name else name


_SHOPIFY_READ_PRODUCT = """
query LwRepairReadProduct($id: ID!) {
  product(id: $id) {
    id
    title
    featuredMedia { id }
    media(first: 250) {
      nodes {
        id
        mediaContentType
        status
        mediaErrors { code details message }
        ... on MediaImage { image { url altText } }
      }
    }
  }
}
"""

_SHOPIFY_ADD_MEDIA = """
mutation LwRepairAddMedia($productId: ID!, $media: [CreateMediaInput!]!) {
  productUpdate(product: {id: $productId}, media: $media) {
    product { id }
    userErrors { field message }
  }
}
"""

_SHOPIFY_REORDER_MEDIA = """
mutation LwRepairSetFeatured($id: ID!, $moves: [MoveInput!]!) {
  productReorderMedia(id: $id, moves: $moves) {
    job { id done }
    mediaUserErrors { field code message }
  }
}
"""

_SHOPIFY_DETACH_MEDIA = """
mutation LwRepairDetachMedia($files: [FileUpdateInput!]!) {
  fileUpdate(files: $files) {
    files { id fileStatus }
    userErrors { field code message }
  }
}
"""


def _shopify_read_media(store: dict, product_gid: str) -> dict:
    """Read a Shopify product's media, normalised for diffing.

    `image` is null while a media is still PROCESSING, so `url`/`stem` may be
    None on a fresh upload — callers must not treat a missing stem as "not ours".
    """
    data = _shopify_graphql(store, _SHOPIFY_READ_PRODUCT, {"id": product_gid})
    product = data.get("product")
    if not product:
        return {"found": False, "product_gid": product_gid}

    nodes = ((product.get("media") or {}).get("nodes")) or []
    media = []
    for n in nodes:
        img = n.get("image") or {}
        url = img.get("url")
        stem = _media_filename_stem(url)
        media.append({
            "media_id":     n.get("id"),
            "status":       n.get("status"),
            "content_type": n.get("mediaContentType"),
            "url":          url,
            "stem":         stem,
            "lw_image_id":  _linnworks_image_id(stem),
            "alt":          img.get("altText"),
            "errors":       n.get("mediaErrors") or [],
        })

    featured = product.get("featuredMedia") or {}
    return {
        "found":             True,
        "product_gid":       product.get("id"),
        "product_title":     product.get("title"),
        "featured_media_id": featured.get("id"),
        "media":             media,
    }


def _shopify_poll_media(
    store: dict, product_gid: str, media_ids: list[str],
    timeout_s: int = 90, interval_s: int = 3,
) -> dict:
    """Poll newly-attached media until every id leaves PROCESSING.

    Shopify processes uploads asynchronously — the add mutation returns before
    the image exists, exactly like GLT creates (#38). A read-back taken straight
    after the write proves nothing, so this waits for READY/FAILED and reports
    which it got. Returns {media_id: {status, errors, url, stem}} plus `timed_out`.
    """
    wanted = {m for m in media_ids if m}
    deadline = time.time() + timeout_s
    seen: dict[str, dict] = {}

    while wanted and time.time() < deadline:
        time.sleep(interval_s)
        snapshot = _shopify_read_media(store, product_gid)
        for m in snapshot.get("media", []):
            mid = m["media_id"]
            if mid in wanted:
                seen[mid] = m
                if (m.get("status") or "").upper() != "PROCESSING":
                    wanted.discard(mid)

    return {"media": seen, "timed_out": sorted(wanted)}


def _owning_stock_item_ids(sku: str, stock_item_id: str) -> dict:
    """Every Linnworks item whose images legitimately live on this Shopify product.

    ⚠️  THE HAZARD THIS EXISTS FOR. On Shopify a variation group is ONE product
    shared by every variant (#26), but each variant is a SEPARATE Linnworks item
    with its OWN images. So a sibling's photo sits on the same product and — being
    a Linnworks GUID absent from THIS item's image list — would look exactly like
    a superseded image. Removing it would delete a live sibling's picture while
    the tool reported a clean repair.

    So the owning set is the whole group (parent + children), and a media file is
    only ever "superseded" when it belongs to NONE of them. Costs one variation
    lookup plus one batched image read.

    Returns {role, group_name, member_skus, stock_item_ids, shared_product}.
    A lookup failure degrades to "assume shared" (removal disabled), never to
    "assume standalone" — the safe direction.
    """
    try:
        rel = _resolve_variation(sku, stock_item_id)
    except RateLimitError:
        raise
    except Exception as e:
        return {
            "role": None, "group_name": None,
            "member_skus": [sku], "stock_item_ids": [stock_item_id],
            "shared_product": True,
            "lookup_error": f"Variation lookup failed ({e}) — treating the product as shared.",
        }

    role = rel.get("role")
    members = list(rel.get("children") or []) + list(rel.get("siblings") or [])
    ids = {(stock_item_id or "").lower()}
    skus = {sku}
    for m in members:
        if m.get("stock_item_id"):
            ids.add(m["stock_item_id"].lower())
        if m.get("sku"):
            skus.add(m["sku"])
    if rel.get("parent_stock_item_id"):
        ids.add(rel["parent_stock_item_id"].lower())
    if rel.get("parent_sku"):
        skus.add(rel["parent_sku"])

    return {
        "role":            role,
        "group_name":      rel.get("group_name"),
        "member_skus":     sorted(skus),
        "stock_item_ids":  sorted(ids),
        "shared_product":  role in ("parent", "child"),
    }


@mcp.tool()
def repair_channel_listing_images(
    skus: list[str],
    sub_source: str = "SWH Shopify",
    remove_superseded: bool = True,
    set_featured: bool = True,
    allow_net_media_loss: bool = False,
    confirmed_count: int | None = None,
    dry_run: bool = True,
) -> dict:
    """
    Push an item's CURRENT Linnworks images onto its EXISTING Shopify listing —
    the one image job the Generic Listing Tool cannot do.

    Use this when images were corrected in Linnworks but the live product page
    still shows the old picture, is missing photos, or has the wrong shot as its
    hero. `refresh_channel_listing` CANNOT fix that: it re-pushes the template's
    stored snapshot, so it re-sends the old (sometimes deleted) URL and returns a
    success that changed nothing (#40). This talks to the Shopify Admin API
    directly and verifies against the storefront's own data.

    What it does per SKU:
      1. reads the item's current Linnworks images (ids, main flag, sort order),
      2. finds the live Shopify product via the channel-SKU reference,
      3. diffs Linnworks images against the product's media,
      4. ATTACHES anything missing, waits for Shopify to finish processing it,
      5. makes the Linnworks main image the product's featured image,
      6. DETACHES media that came from Linnworks but is no longer on the item.

    Images are matched by GUID: Shopify's CDN filename preserves the Linnworks
    image id, so this compares actual pictures rather than counts — the blind
    spot that made #40 a silent no-op.

    SAFETY — what it will NOT touch:
      - Media whose filename is not a Linnworks GUID is reported as `unmanaged`
        and never removed. That is a hand-uploaded photo, size chart or lifestyle
        shot, and Linnworks has no opinion about it.
      - On a VARIATION group every variant shares ONE Shopify product but keeps
        its own Linnworks images, so a sibling's photo would otherwise look
        superseded. The whole group's images are read first, and removal is
        disabled for shared products unless every member was passed in `skus`.
      - Nothing is removed unless the replacement media reached READY. A failed
        upload leaves the old picture in place rather than emptying the listing.
      - Removal is blocked when it would take away MORE media than it adds. That
        is the signature of a storefront holding richer content than the catalogue
        — live-observed on a Luma product whose "superseded" media turned out to be
        lifestyle photography, against incoming studio packshots. Override with
        allow_net_media_loss=True once you have looked at the images.

    Requires Shopify Admin credentials (scopes: read_products, write_products,
    read_files, write_files) — Linnworks cannot supply these. If none are
    configured the tool returns setup instructions and writes nothing.

    Batches of more than 10 SKUs require confirmed_count=len(skus); these are
    live customer-facing product pages.

    Args:
        skus: Exact SKUs / ItemNumbers to repair.
        sub_source: The Shopify store, as named in Linnworks (default
            "SWH Shopify"). Must match a configured store.
        remove_superseded: Detach Linnworks-origin media that is no longer on the
            item (default True). Detach leaves the file in Shopify Files, so it
            is recoverable. Automatically disabled for shared variation products.
        set_featured: Make the Linnworks main image the product's featured image
            (default True). Linnworks' own is_main does not drive Shopify.
        allow_net_media_loss: Permit a repair that removes more media than it adds
            (default False). Off by default because "superseded" cannot tell a
            replaced image from one deleted out of Linnworks while still earning
            its place on the product page.
        confirmed_count: For batches > 10, pass len(skus) here.
        dry_run: If True (default), returns the manifest without writing.

    Returns:
        A dict with:
          - sub_source, shop_domain, dry_run, item_count
          - plan:       per-SKU diff — missing / superseded / unmanaged / matched
                        media, featured status, and the actions that would run
          - unresolved: SKUs not repairable here, each with a `blocked_reason`
          - results:    per-SKU outcome (live run) — added / detached / featured,
                        plus `in_sync` from a fresh read of the product afterwards
    """
    if not skus:
        raise ValueError("skus is empty — nothing to repair.")
    _check_injection("sub_source", sub_source)

    store = _shopify_store_for(sub_source)
    if store is None:
        return {**_shopify_setup_error(sub_source), "sub_source": sub_source, "skus": skus}

    lw_source = GLT_CHANNELS["shopify"]["source"]  # "SHOPIFY"
    want_ss = (sub_source or "").strip().lower()

    plan: list[dict] = []
    unresolved: list[dict] = []
    rate_limited: list[dict] = []
    id_cache: dict = {}

    for sku in skus:
        # ── Resolve the item ──────────────────────────────────────────────────
        try:
            sid = _resolve_sku_to_id(sku, id_cache)
        except RateLimitError as e:
            rate_limited.append({"sku": sku, "error": str(e)})
            continue
        except (ValueError, RuntimeError) as e:
            unresolved.append({"sku": sku, "blocked_reason": "not_found", "error": str(e)})
            continue

        # ── Is it listed on this store? ───────────────────────────────────────
        try:
            rows = _fetch_channel_skus_for_ids([sid]).get(sid.lower(), [])
        except RateLimitError as e:
            rate_limited.append({"sku": sku, "error": str(e)})
            continue

        row = next(
            (r for r in rows
             if (r.get("Source") or "").upper() == lw_source
             and (r.get("SubSource") or "").strip().lower() == want_ss),
            None,
        )
        if row is None:
            unresolved.append({
                "sku": sku, "blocked_reason": "not_listed",
                "error": f"'{sku}' has no {lw_source} listing on '{sub_source}' — nothing to repair.",
            })
            continue

        product_gid = _shopify_product_gid(row.get("ChannelReferenceId"))
        if not product_gid:
            unresolved.append({
                "sku": sku, "blocked_reason": "no_product_reference",
                "channel_reference_id": row.get("ChannelReferenceId"),
                "error": (
                    "The channel-SKU row carries no usable Shopify product id "
                    f"(ChannelReferenceId={row.get('ChannelReferenceId')!r}). Expected a "
                    "'product:variant:inventory' triple."
                ),
            })
            continue

        # ── Linnworks images for this item, and for its whole variation group ──
        try:
            own_raw = _fetch_raw_images(sid)
            owner = _owning_stock_item_ids(sku, sid)
            group_ids = owner["stock_item_ids"]
            group_imgs = _fetch_images_for_ids(group_ids) if len(group_ids) > 1 else {sid.lower(): own_raw}
        except RateLimitError as e:
            rate_limited.append({"sku": sku, "error": str(e)})
            continue

        own = [_format_image_row(r) for r in own_raw]
        own.sort(key=lambda im: (im["sort_order"] if im["sort_order"] is not None else 0))
        own_ids = {(im["image_id"] or "").lower() for im in own if im.get("image_id")}
        group_owned_ids = {
            (img.get("pkRowId") or "").lower()
            for rows_ in group_imgs.values() for img in rows_ if img.get("pkRowId")
        } or own_ids

        if not own:
            unresolved.append({
                "sku": sku, "blocked_reason": "no_linnworks_images",
                "product_gid": product_gid,
                "error": (
                    f"'{sku}' has no images in Linnworks — there is nothing to push. Add one "
                    "with add_inventory_item_images first."
                ),
            })
            continue

        # ── Read the live product ─────────────────────────────────────────────
        try:
            live = _shopify_read_media(store, product_gid)
        except RuntimeError as e:
            unresolved.append({
                "sku": sku, "blocked_reason": "shopify_read_failed",
                "product_gid": product_gid, "error": str(e),
            })
            continue

        if not live.get("found"):
            unresolved.append({
                "sku": sku, "blocked_reason": "shopify_product_missing",
                "product_gid": product_gid,
                "error": (
                    f"Shopify has no product {product_gid} — the Linnworks channel-SKU row is "
                    "stale. Nothing was changed."
                ),
            })
            continue

        # ── Diff ──────────────────────────────────────────────────────────────
        by_stem = {m["lw_image_id"]: m for m in live["media"] if m.get("lw_image_id")}
        matched, superseded, unmanaged = [], [], []
        for m in live["media"]:
            stem = m.get("lw_image_id")
            if not stem:
                # Filename is not a Linnworks GUID, so Linnworks never put it there —
                # a hand-uploaded photo, size chart or lifestyle shot. Not ours to remove.
                unmanaged.append(m)
                continue
            entry = {
                **m,
                "linnworks_image_id":  stem,
                "belongs_to_sibling":  stem not in own_ids and stem in group_owned_ids,
            }
            # "Superseded" = came from Linnworks, but NO item sharing this Shopify
            # product still carries it. A sibling variant's image is matched, not
            # superseded (own_ids is a subset of group_owned_ids).
            (matched if stem in group_owned_ids else superseded).append(entry)

        missing = [im for im in own if (im["image_id"] or "").lower() not in by_stem]

        main_img = next((im for im in own if im["is_main"]), own[0] if own else None)
        main_stem = (main_img["image_id"] or "").lower() if main_img else None
        main_media = by_stem.get(main_stem) if main_stem else None
        featured_ok = bool(main_media and main_media["media_id"] == live["featured_media_id"])

        # Removal is unsafe on a shared variation product unless the caller named
        # every member — a sibling's picture is not ours to take down.
        requested = {s.strip().lower() for s in skus}
        all_members_requested = all(
            (s or "").strip().lower() in requested for s in owner.get("member_skus", [sku])
        )
        # A failed variation lookup means we do not KNOW whether this product is
        # shared, so removal is disabled — "unknown" must never read as "standalone".
        lookup_failed = bool(owner.get("lookup_error"))
        removal_allowed = remove_superseded and not lookup_failed and (
            not owner.get("shared_product") or all_members_requested
        )
        removal_blocked_reason = None
        if remove_superseded and not removal_allowed:
            removal_blocked_reason = owner.get("lookup_error") or (
                f"'{sku}' is a variation {owner.get('role') or 'member'} of "
                f"'{owner.get('group_name')}', whose Shopify product is shared with "
                f"{len(owner.get('member_skus', [])) - 1} other variant(s). Superseded media is "
                "NOT removed unless every member is passed in `skus`, because a sibling's photo "
                "lives on the same product."
            )

        # ⚠️  NET-LOSS GUARD — the case that nearly shipped a content downgrade.
        # "Superseded" only means "Linnworks pushed this once and no longer has it".
        # It CANNOT distinguish "replaced by a better version" (detach is right) from
        # "removed from Linnworks but still valuable on the storefront" (detach is
        # wrong). Live example, luma-huxham-blue: 6 superseded media were genuine
        # LIFESTYLE shots (models wearing the product) while the 4 incoming Linnworks
        # images were studio packshots — a strict content downgrade, from a tool that
        # was behaving exactly to spec. Removing MORE media than you add is the tell
        # that Shopify holds richer content than Linnworks, so it blocks by default.
        net_loss = len(superseded) - len(missing)
        if removal_allowed and net_loss > 0 and not allow_net_media_loss:
            removal_allowed = False
            removal_blocked_reason = (
                f"Detaching would remove {len(superseded)} media while adding only "
                f"{len(missing)} — a net loss of {net_loss}. Linnworks is not always the "
                "richer image source (a storefront often carries lifestyle shots the "
                "catalogue never held), and a superseded image is indistinguishable from "
                "one that was simply removed from Linnworks. Review the images, then pass "
                "allow_net_media_loss=True if the removals really are stale versions."
            )

        to_detach = superseded if removal_allowed else []

        actions = []
        if missing:
            actions.append(f"attach {len(missing)} image(s)")
        if set_featured and main_media and not featured_ok:
            actions.append("set featured image")
        elif set_featured and not main_media and main_img:
            actions.append("set featured image (after attach)")
        if to_detach:
            actions.append(f"detach {len(to_detach)} superseded media")

        row_plan = {
            "sku":                  sku,
            "stock_item_id":        sid,
            "product_gid":          product_gid,
            "product_title":        live.get("product_title"),
            "channel_reference_id": row.get("ChannelReferenceId"),
            "linnworks_image_count": len(own),
            "shopify_media_count":  len(live["media"]),
            "missing":              missing,
            "matched":              matched,
            "superseded":           superseded,
            "unmanaged":            unmanaged,
            "to_detach":            to_detach,
            "main_image_id":        main_stem,
            "featured_media_id":    live["featured_media_id"],
            "featured_is_correct":  featured_ok,
            "variation":            {
                "role":           owner.get("role"),
                "group_name":     owner.get("group_name"),
                "shared_product": owner.get("shared_product"),
                "member_skus":    owner.get("member_skus"),
            },
            "removal_blocked_reason": removal_blocked_reason,
            "actions":            actions,
            "needs_repair":       bool(actions),
        }
        plan.append(row_plan)

    actionable = [p for p in plan if p["needs_repair"]]

    base = {
        "sub_source":          sub_source,
        "shop_domain":         store["shop_domain"],
        "shopify_configured":  True,
        "api_version":         SHOPIFY_API_VERSION,
        "item_count":          len(skus),
        "plan":                plan,
        "unresolved":          unresolved,
        "rate_limited":        rate_limited,
        "complete":            not rate_limited,
        "repairable_count":    len(actionable),
        "in_sync_count":       len(plan) - len(actionable),
        "blocked_count":       len(unresolved),
    }

    # ── Write guard ───────────────────────────────────────────────────────────
    guard = _write_guard("repair_channel_listing_images", actionable, confirmed_count, dry_run)
    if guard is not None:
        return {**guard, **base}

    if not actionable:
        return {
            **base, "dry_run": dry_run, "results": [],
            "message": (
                f"Nothing to repair — {len(plan)} listing(s) already match their Linnworks images"
                + (f"; {len(unresolved)} SKU(s) blocked, see `unresolved`." if unresolved else ".")
            ),
        }

    if dry_run:
        return {
            **base, "dry_run": True, "results": [],
            "message": (
                f"Dry run — nothing written. {len(actionable)} of {len(plan)} listing(s) would be "
                f"repaired on '{sub_source}'. Set dry_run=False to execute."
            ),
        }

    # ── Live execution ────────────────────────────────────────────────────────
    results = []
    for p in actionable:
        pgid = p["product_gid"]
        out = {
            "sku": p["sku"], "product_gid": pgid, "product_title": p["product_title"],
            "added": [], "add_failed": [], "detached": [], "detach_failed": [],
            "featured_set": None, "errors": [],
        }

        try:
            # 1. Attach missing images. Shopify fetches the public linnlive URL itself.
            new_ids: list[str] = []
            if p["missing"]:
                before = {m["media_id"] for m in _shopify_read_media(store, pgid)["media"]}
                media_input = [{
                    "originalSource":   im["full_url"],
                    "mediaContentType": "IMAGE",
                    "alt":              p["product_title"] or p["sku"],
                } for im in p["missing"] if im.get("full_url")]

                data = _shopify_graphql(
                    store, _SHOPIFY_ADD_MEDIA, {"productId": pgid, "media": media_input}
                )
                errs = ((data.get("productUpdate") or {}).get("userErrors")) or []
                if errs:
                    out["errors"].append({"stage": "add", "user_errors": errs})

                after = _shopify_read_media(store, pgid)["media"]
                new_ids = [m["media_id"] for m in after if m["media_id"] not in before]

                # 2. Wait for processing — an add mutation returns before the image
                #    exists, so a read-back taken now would prove nothing (cf. #38).
                polled = _shopify_poll_media(store, pgid, new_ids)
                for mid in new_ids:
                    m = polled["media"].get(mid, {})
                    status = (m.get("status") or "UNKNOWN").upper()
                    rec = {"media_id": mid, "status": status, "url": m.get("url"),
                           "linnworks_image_id": m.get("lw_image_id"),
                           "errors": m.get("errors") or []}
                    (out["added"] if status == "READY" else out["add_failed"]).append(rec)
                if polled["timed_out"]:
                    out["errors"].append({
                        "stage": "add",
                        "still_processing": polled["timed_out"],
                        "note": "Shopify was still processing these when the poll timed out.",
                    })

            # Re-read once: everything below keys off the product's real state.
            live = _shopify_read_media(store, pgid)
            by_stem = {m["lw_image_id"]: m for m in live["media"] if m.get("lw_image_id")}

            # 3. Featured image — Linnworks' is_main does not drive Shopify.
            if set_featured and p["main_image_id"]:
                main_media = by_stem.get(p["main_image_id"])
                if main_media and main_media["media_id"] != live["featured_media_id"]:
                    d = _shopify_graphql(store, _SHOPIFY_REORDER_MEDIA, {
                        "id": pgid,
                        "moves": [{"id": main_media["media_id"], "newPosition": "0"}],
                    })
                    merrs = ((d.get("productReorderMedia") or {}).get("mediaUserErrors")) or []
                    if merrs:
                        out["errors"].append({"stage": "featured", "user_errors": merrs})
                    else:
                        out["featured_set"] = main_media["media_id"]
                elif main_media:
                    out["featured_set"] = "already_correct"

            # 4. Detach superseded media — ONLY once its replacement is READY.
            #    Detaching first is how a product ends up with no image at all.
            if p["to_detach"]:
                if p["missing"] and not out["added"]:
                    out["errors"].append({
                        "stage": "detach",
                        "skipped": True,
                        "note": (
                            "No replacement image reached READY, so superseded media was left in "
                            "place rather than emptying the listing."
                        ),
                    })
                else:
                    files = [{"id": m["media_id"], "referencesToRemove": [pgid]}
                             for m in p["to_detach"]]
                    d = _shopify_graphql(store, _SHOPIFY_DETACH_MEDIA, {"files": files})
                    uerrs = ((d.get("fileUpdate") or {}).get("userErrors")) or []
                    if uerrs:
                        out["errors"].append({"stage": "detach", "user_errors": uerrs})

            # 5. Read back against the REAL surface — the storefront's own data,
            #    never this tool's assumption that the writes landed.
            final = _shopify_read_media(store, pgid)
            final_stems = {m["lw_image_id"] for m in final["media"] if m.get("lw_image_id")}
            lw_ids = {(im["image_id"] or "").lower()
                      for im in p["missing"]} | {
                      (m.get("linnworks_image_id") or "") for m in p["matched"]}
            lw_ids.discard("")

            detached_ids = {m["media_id"] for m in p["to_detach"]}
            remaining_ids = {m["media_id"] for m in final["media"]}
            out["detached"] = sorted(detached_ids - remaining_ids)
            out["detach_failed"] = sorted(detached_ids & remaining_ids)

            still_missing = sorted(i for i in lw_ids if i not in final_stems)
            # Recompute from the FINAL read — by_stem predates the detach step.
            final_by_stem = {m["lw_image_id"]: m
                             for m in final["media"] if m.get("lw_image_id")}
            main_media_final = final_by_stem.get(p["main_image_id"]) if p["main_image_id"] else None
            featured_now = final["featured_media_id"]
            featured_ok = bool(
                main_media_final and featured_now == main_media_final["media_id"]
            ) if (set_featured and p["main_image_id"]) else None

            out["still_missing"] = still_missing
            out["featured_media_id"] = featured_now
            out["featured_is_correct"] = featured_ok
            out["shopify_media_count"] = len(final["media"])
            out["in_sync"] = (
                not still_missing
                and not out["detach_failed"]
                and not out["add_failed"]
                and (featured_ok is not False)
            )
        except RuntimeError as e:
            out["errors"].append({"stage": "unhandled", "error": str(e)})
            out["in_sync"] = None

        results.append(out)

    repaired = sum(1 for r in results if r.get("in_sync") is True)
    failed = [r["sku"] for r in results if r.get("in_sync") is not True]

    return {
        **base, "dry_run": False, "results": results,
        "repaired_count": repaired,
        "failed_skus": failed,
        "message": (
            f"Repaired {repaired} of {len(results)} listing(s) on '{sub_source}'; verified by "
            "re-reading each product from Shopify."
            + (f" ⚠️ Not confirmed in sync: {failed} — read `results[].errors`." if failed else "")
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
