"""
Tests for get_shipping_quote (ShippingService/GetIntegrations + GetShippingQuote).

All mocked — no live Linnworks calls. The request shape is the one proven live
on 29 Sep 2026: POST, wrapped, with a required Accounts list (the documented
GET 400s on this tenant).
"""

import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402

GUID = "d19715b6-725f-4049-93a2-1126418ecfee"

INTEGRATIONS = [
    {"pkShippingAPIConfigId": -1, "Vendor": "Generic Shipping", "AccountId": None,
     "VendorFriendlyName": None, "QuoteEnabled": None},
    {"pkShippingAPIConfigId": 9, "Vendor": "AMAZONSHIPPING_V2", "AccountId": "The Warehouse Group",
     "VendorFriendlyName": "Amazon Buy Shipping", "QuoteEnabled": True},
    {"pkShippingAPIConfigId": 6, "Vendor": "DPD", "AccountId": "DPD",
     "VendorFriendlyName": "DPD UK", "QuoteEnabled": False},
    {"pkShippingAPIConfigId": 13, "Vendor": "InPost Lockers", "AccountId": "in-post",
     "VendorFriendlyName": "InPost Lockers", "QuoteEnabled": True},
]


def _order(source="AMAZON", tags=("FIFO_READY",), processed=False, country="United Kingdom"):
    return {
        "OrderId": GUID,
        "NumOrderId": 612129,
        "Processed": processed,
        "GeneralInfo": {
            "Source": source,
            "SubSource": "The Warehouse Group",
            "ReferenceNum": "205-5660270-8145112",
            "Identifiers": [{"Tag": t} for t in tags],
        },
        "ShippingInfo": {"PostalServiceName": "EVRi Standard 24Hr"},
        "CustomerInfo": {"Address": {"Country": country}},
    }


def _quote(name, total):
    return {
        "Vendor": "AMAZONSHIPPING_V2", "FriendlyName": "Amazon Buy Shipping",
        "AccountId": "The Warehouse Group", "ServiceName": name, "ServiceCode": name.upper(),
        "ServiceId": "x", "Cost": total, "Tax": 0.0, "TotalCost": total, "Currency": "GBP",
        "CollectionDate": "2026-10-01T00:00:00Z", "EstimatedDeliveryDate": "2026-10-01T00:00:00Z",
        "PropertyItem": [{"Title": "Benefit", "Value": "CLAIMS_PROTECTED"}, {"Title": "", "Value": ""}],
        "Options": [],
    }


QUOTE_RESPONSE = {
    "pkOrderId": GUID,
    "Quotes": [_quote("Evri Next Day", 3.73), _quote("Evri Standard", 2.67)],
    "Errors": [{"Vendor": "InPost Lockers", "AccountId": "in-post",
                "ErrorMessage": "(404) Not Found."}],
}


def _run(order=None, quote_resp=QUOTE_RESPONSE, **kwargs):
    order = order or _order()
    with patch.object(server, "_resolve_order_guid", return_value=(GUID, order)), \
         patch.object(server, "call_linnworks_get", return_value=INTEGRATIONS) as get, \
         patch.object(server, "call_linnworks", return_value=quote_resp) as post:
        result = server.get_shipping_quote("612129", **kwargs)
    return result, get, post


def test_default_quotes_every_quote_enabled_account_with_wrapped_post():
    result, get, post = _run()
    get.assert_called_once_with("ShippingService/GetIntegrations")
    path, body = post.call_args.args
    assert path == "ShippingService/GetShippingQuote"
    assert body["request"]["pkOrderId"] == GUID
    assert [a["Vendor"] for a in body["request"]["Accounts"]] == ["AMAZONSHIPPING_V2", "InPost Lockers"]
    assert set(body["request"]["Accounts"][0]) == {"Vendor", "AccountId", "VendorFriendlyName"}
    assert result["accounts_quoted"] == ["Amazon Buy Shipping", "InPost Lockers"]


def test_quotes_sorted_cheapest_first_and_blank_properties_dropped():
    result, _, _ = _run()
    assert [q["total_cost"] for q in result["quotes"]] == [2.67, 3.73]
    assert result["cheapest"]["service_name"] == "Evri Standard"
    assert result["cheapest"]["properties"] == [{"title": "Benefit", "value": "CLAIMS_PROTECTED"}]
    assert result["quote_count"] == 2


def test_per_account_errors_reported_with_friendly_name_backfilled():
    result, _, _ = _run()
    assert result["errors"] == [{"vendor": "InPost Lockers", "friendly_name": "InPost Lockers",
                                 "account_id": "in-post", "message": "(404) Not Found."}]


def test_generic_shipping_pseudo_integration_never_sent():
    _, _, post = _run(accounts=["Generic Shipping"])
    assert post.call_count == 0


def test_accounts_filter_matches_name_vendor_account_or_config_id():
    for want in ("amazon buy shipping", "AMAZONSHIPPING_V2", "the warehouse group", "9"):
        _, _, post = _run(accounts=[want])
        accts = post.call_args.args[1]["request"]["Accounts"]
        assert [a["Vendor"] for a in accts] == ["AMAZONSHIPPING_V2"], want


def test_unknown_account_refused_before_quote_call():
    result, _, post = _run(accounts=["Royal Mail"])
    assert "Unknown shipping account" in result["error"]
    assert post.call_count == 0
    assert {a["config_id"] for a in result["available_accounts"]} == {9, 6, 13}


def test_quote_disabled_account_can_be_requested_but_warns():
    result, _, post = _run(accounts=["DPD"])
    assert post.call_args.args[1]["request"]["Accounts"][0]["Vendor"] == "DPD"
    assert any("Quoting is disabled on 'DPD UK'" in w for w in result["warnings"])


def test_amazon_account_on_non_amazon_order_warns():
    result, _, _ = _run(order=_order(source="SHOPIFY"))
    assert any("only quotes Amazon orders" in w for w in result["warnings"])


def test_amazon_order_does_not_get_source_warning():
    result, _, _ = _run()
    assert not any("only quotes Amazon orders" in w for w in result["warnings"])


def test_prime_tag_detected_and_flagged():
    result, _, _ = _run(order=_order(tags=("amazon_prime",)))
    assert result["is_prime"] is True
    assert any("Prime-compliant services" in w for w in result["warnings"])


def test_non_prime_order():
    result, _, _ = _run()
    assert result["is_prime"] is False


def test_missing_delivery_country_warns():
    for country in ("UNKNOWN", ""):
        result, _, _ = _run(order=_order(country=country))
        assert any("no delivery country" in w for w in result["warnings"]), country


def test_silent_empty_response_is_called_out():
    result, _, _ = _run(order=_order(processed=True), quote_resp={"Quotes": [], "Errors": []})
    assert result["quote_count"] == 0 and result["cheapest"] is None
    assert any("already processed" in w for w in result["warnings"])
    assert any("No quotes and no errors" in w for w in result["warnings"])


def test_unresolvable_order_returns_error_without_calls():
    with patch.object(server, "_resolve_order_guid", side_effect=RuntimeError("No order found")), \
         patch.object(server, "call_linnworks") as post, \
         patch.object(server, "call_linnworks_get") as get:
        result = server.get_shipping_quote("999")
    assert "No order found" in result["error"]
    assert post.call_count == 0 and get.call_count == 0


def test_no_quote_enabled_integrations():
    with patch.object(server, "_resolve_order_guid", return_value=(GUID, _order())), \
         patch.object(server, "call_linnworks_get",
                      return_value=[{**i, "QuoteEnabled": False} for i in INTEGRATIONS]), \
         patch.object(server, "call_linnworks") as post:
        result = server.get_shipping_quote("612129")
    assert "quoting enabled" in result["error"]
    assert post.call_count == 0


def test_rate_limit_is_not_swallowed():
    import pytest
    with patch.object(server, "_resolve_order_guid", return_value=(GUID, _order())), \
         patch.object(server, "call_linnworks_get", return_value=INTEGRATIONS), \
         patch.object(server, "call_linnworks", side_effect=server.RateLimitError("429")):
        with pytest.raises(server.RateLimitError):
            server.get_shipping_quote("612129")
