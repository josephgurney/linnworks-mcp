"""
Tests for 429 rate-limit handling (issue #34).

The defect this pins down: Linnworks returns HTTP 429 with
`{"Message":"API calls quota exceeded! Maximum admitted 150 per Minute."}` when a
per-minute quota is spent. Nothing retried it, and `_resolve_sku_to_id` folded ANY
RuntimeError into `ValueError("SKU 'X' not found in Linnworks: ...")`. Callers that
bucket per-item failures then reported a transient quota failure as a factual claim
about the catalogue.

Measured impact before the fix (6 Aug 2026, live):
  - get_channel_listings_bulk on 5,391 SKUs: 310 "listed" / 4,804 "unresolved",
    every one of those a 429. True answer via ids: 2,628 listed, 27 calls, 15.6s.
    An 88% under-report.
  - delist_all_channel_listings silently skipped 92 of 154 SKUs while reporting
    success.

Because "unresolved" reads as "this SKU doesn't exist", the failure pointed toward
the destructive answer — which is why these tests assert the *classification*, not
just that a retry happens.
"""
import pytest
from unittest.mock import patch, MagicMock

import server


QUOTA_BODY = '{"Message":"API calls quota exceeded! Maximum admitted 150 per Minute."}'


def _resp(status=200, text='{"ok":true}', headers=None):
    r = MagicMock()
    r.status_code = status
    r.text = text
    r.ok = 200 <= status < 300
    r.headers = headers or {}
    r.json.return_value = {"ok": True} if text == '{"ok":true}' else {}
    return r


@pytest.fixture(autouse=True)
def _no_sleep():
    """Keep the backoff ladder instant in tests."""
    with patch.object(server.time, "sleep"):
        yield


@pytest.fixture(autouse=True)
def _auth():
    with patch.object(server, "ensure_auth", return_value=("tok", "https://eu.linnworks.net")):
        yield


# --- detection ---------------------------------------------------------------

def test_detects_429_status():
    assert server._is_rate_limited(_resp(429, QUOTA_BODY)) is True


def test_detects_quota_message_on_other_status():
    """The quota message has been seen on non-429 statuses — belt and braces."""
    assert server._is_rate_limited(_resp(400, QUOTA_BODY)) is True


def test_does_not_flag_ordinary_errors():
    assert server._is_rate_limited(_resp(400, '{"Message":"The request is invalid."}')) is False
    assert server._is_rate_limited(_resp(200)) is False


def test_rate_limit_error_is_a_runtime_error():
    """Existing `except RuntimeError` handlers must keep working."""
    assert issubclass(server.RateLimitError, RuntimeError)


# --- retry / backoff ---------------------------------------------------------

def test_call_linnworks_retries_then_succeeds():
    responses = [_resp(429, QUOTA_BODY), _resp(429, QUOTA_BODY), _resp(200)]
    with patch.object(server._session, "post", side_effect=responses) as post:
        out = server.call_linnworks("Inventory/GetInventoryItem", {"sku": "X"})
    assert out == {"ok": True}
    assert post.call_count == 3


def test_call_linnworks_raises_rate_limit_error_when_retries_exhausted():
    n = len(server._RATE_LIMIT_BACKOFF) + 1
    with patch.object(server._session, "post", side_effect=[_resp(429, QUOTA_BODY)] * n):
        with pytest.raises(server.RateLimitError) as e:
            server.call_linnworks("Inventory/GetInventoryItem", {"sku": "X"})
    # The message must not read like a missing record.
    assert "rate-limited" in str(e.value)
    assert "NOT a missing record" in str(e.value)


def test_call_linnworks_get_retries():
    responses = [_resp(429, QUOTA_BODY), _resp(200)]
    with patch.object(server._session, "get", side_effect=responses) as get:
        server.call_linnworks_get("Stock/GetItemChangesHistory", {"stockItemId": "x"})
    assert get.call_count == 2


def test_call_linnworks_void_retries():
    responses = [_resp(429, QUOTA_BODY), _resp(204, "")]
    with patch.object(server._session, "post", side_effect=responses) as post:
        server.call_linnworks_void("ImportExport/RunNowImport", {"importId": 1})
    assert post.call_count == 2


def test_retry_after_header_is_honoured():
    responses = [_resp(429, QUOTA_BODY, {"Retry-After": "99"}), _resp(200)]
    with patch.object(server.time, "sleep") as slept:
        with patch.object(server._session, "post", side_effect=responses):
            server.call_linnworks("X/Y", {})
    assert slept.call_args[0][0] >= 99


def test_non_rate_limit_errors_are_not_retried():
    """A 400 is a real error — retrying it just wastes a minute."""
    with patch.object(server._session, "post",
                      side_effect=[_resp(400, '{"Message":"The request is invalid."}')]) as post:
        with pytest.raises(RuntimeError) as e:
            server.call_linnworks("X/Y", {})
    assert post.call_count == 1
    assert not isinstance(e.value, server.RateLimitError)


# --- classification: the actual bug ------------------------------------------

def test_resolve_sku_reraises_rate_limit_not_not_found():
    """THE regression. A 429 must never be reported as a missing SKU."""
    with patch.object(server, "call_linnworks",
                      side_effect=server.RateLimitError("quota exceeded")):
        with pytest.raises(server.RateLimitError):
            server._resolve_sku_to_id("REAL-SKU")


def test_resolve_sku_still_raises_value_error_for_genuine_miss():
    with patch.object(server, "call_linnworks",
                      side_effect=RuntimeError("HTTP 400 — Could not determine inventory item id")):
        with pytest.raises(ValueError, match="not found in Linnworks"):
            server._resolve_sku_to_id("NOPE")


# --- bulk tools --------------------------------------------------------------

def _fake_item(sid, title="T"):
    return {"StockItemId": sid, "ItemTitle": title}


def test_bulk_channel_listings_separates_rate_limited_from_unresolved():
    def resolve(path, payload):
        sku = payload["sku"]
        if sku == "LIMITED":
            raise server.RateLimitError("quota exceeded")
        if sku == "GONE":
            raise RuntimeError("HTTP 400 — no such item")
        return _fake_item("aaaa-1111")

    with patch.object(server, "call_linnworks", side_effect=resolve), \
         patch.object(server, "_fetch_channel_skus_for_ids", return_value={}):
        out = server.get_channel_listings_bulk(skus=["OK", "LIMITED", "GONE"])

    assert [r["sku"] for r in out["unresolved"]] == ["GONE"]
    assert [r["sku"] for r in out["rate_limited"]] == ["LIMITED"]
    assert out["complete"] is False
    assert out["resolved_count"] == 1


def test_bulk_channel_listings_complete_true_when_no_throttling():
    with patch.object(server, "call_linnworks", return_value=_fake_item("aaaa-1111")), \
         patch.object(server, "_fetch_channel_skus_for_ids", return_value={}):
        out = server.get_channel_listings_bulk(skus=["A"])
    assert out["complete"] is True
    assert out["rate_limited"] == []


def test_stock_item_ids_skip_resolution_entirely():
    """The whole point of the id path: zero GetInventoryItem calls."""
    with patch.object(server, "call_linnworks") as cl, \
         patch.object(server, "_fetch_channel_skus_for_ids", return_value={}) as fetch:
        out = server.get_channel_listings_bulk(stock_item_ids=["aaaa-1111", "bbbb-2222"])
    cl.assert_not_called()
    assert out["resolved_count"] == 2
    assert sorted(fetch.call_args[0][0]) == ["aaaa-1111", "bbbb-2222"]


def test_skus_and_ids_can_be_combined():
    with patch.object(server, "call_linnworks", return_value=_fake_item("cccc-3333")), \
         patch.object(server, "_fetch_channel_skus_for_ids", return_value={}):
        out = server.get_channel_listings_bulk(skus=["A"], stock_item_ids=["aaaa-1111"])
    assert out["resolved_count"] == 2
    assert out["item_count"] == 2


def test_bulk_channel_listings_requires_some_input():
    with pytest.raises(ValueError, match="at least one of skus or stock_item_ids"):
        server.get_channel_listings_bulk()


def test_images_bulk_separates_rate_limited_and_accepts_ids():
    def resolve(path, payload):
        if payload["sku"] == "LIMITED":
            raise server.RateLimitError("quota exceeded")
        return _fake_item("aaaa-1111")

    with patch.object(server, "call_linnworks", side_effect=resolve), \
         patch.object(server, "_fetch_images_for_ids", return_value={}):
        out = server.get_inventory_item_images_bulk(skus=["OK", "LIMITED"])
    assert [r["sku"] for r in out["rate_limited"]] == ["LIMITED"]
    assert out["complete"] is False

    with patch.object(server, "call_linnworks") as cl, \
         patch.object(server, "_fetch_images_for_ids", return_value={}):
        out2 = server.get_inventory_item_images_bulk(stock_item_ids=["aaaa-1111"])
    cl.assert_not_called()
    assert out2["resolved_count"] == 1


def test_images_bulk_requires_some_input():
    with pytest.raises(ValueError, match="at least one of skus or stock_item_ids"):
        server.get_inventory_item_images_bulk()


def test_empty_inputs_are_still_reported_as_unresolved_not_rate_limited():
    with patch.object(server, "call_linnworks", return_value=_fake_item("aaaa-1111")), \
         patch.object(server, "_fetch_channel_skus_for_ids", return_value={}):
        out = server.get_channel_listings_bulk(skus=["", "OK"])
    assert len(out["unresolved"]) == 1
    assert out["rate_limited"] == []
