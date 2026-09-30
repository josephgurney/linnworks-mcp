"""
Tests for accept_shipping_quote — mocked, no live Linnworks calls.

Mechanism proven live 30 Sep 2026: a quote's ServiceId equals a postal
service's IntegratedServiceID, and Orders/ChangeShippingMethod takes the
postal service NAME.
"""

import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402

GUID = "11111111-1111-1111-1111-111111111111"
PS_CHEAP = "aaaaaaaa-0000-0000-0000-000000000001"
PS_NEXTDAY = "aaaaaaaa-0000-0000-0000-000000000002"
Q_CHEAP = "bbbbbbbb-0000-0000-0000-000000000001"
Q_NEXTDAY = "bbbbbbbb-0000-0000-0000-000000000002"
Q_UNMAPPED = "bbbbbbbb-0000-0000-0000-000000000009"

SHIPPING_METHODS = [
    {"Vendor": "(New) Amazon Shipping", "PostalServices": [
        {"pkPostalServiceId": PS_CHEAP, "PostalServiceName": "Evri Standard AMZ",
         "IntegratedServiceID": Q_CHEAP},
        {"pkPostalServiceId": PS_NEXTDAY, "PostalServiceName": "Evri Next Day AMZ",
         "IntegratedServiceID": Q_NEXTDAY},
    ]},
    {"Vendor": "EVRi", "PostalServices": [
        {"pkPostalServiceId": "cccccccc-0000-0000-0000-000000000001",
         "PostalServiceName": "EVRi Standard 24Hr"},
    ]},
]


def _order(ps_id="cccccccc-0000-0000-0000-000000000001", ps_name="EVRi Standard 24Hr",
           processed=False, label=False, tracking=""):
    return {
        "OrderId": GUID, "NumOrderId": 612190, "Processed": processed,
        "GeneralInfo": {"ReferenceNum": "202-1", "Source": "AMAZON", "LabelPrinted": label},
        "ShippingInfo": {"PostalServiceId": ps_id, "PostalServiceName": ps_name,
                         "TrackingNumber": tracking},
    }


def _q(name, sid, cost):
    return {"service_name": name, "service_code": name.upper(), "service_id": sid,
            "total_cost": cost, "currency": "GBP", "estimated_delivery_date": "2026-10-02"}


QUOTE = {"order_id": GUID, "is_prime": False, "errors": [], "warnings": [],
         "quotes": [_q("Evri Standard", Q_CHEAP, 2.67), _q("Evri Next Day", Q_NEXTDAY, 3.73)]}


class Fake:
    def __init__(self, order=None, quote=QUOTE, after_ps=PS_CHEAP, methods=SHIPPING_METHODS):
        self.order, self.quote, self.after_ps, self.methods = order or _order(), quote, after_ps, methods
        self.writes = []
        self.written = False

    def get(self, path, params=None):
        assert path == "Orders/GetShippingMethods"
        return self.methods

    def post(self, path, payload):
        assert path == "Orders/ChangeShippingMethod"
        self.writes.append(payload)
        self.written = True
        return {}

    def resolve(self, oid):
        if self.written:
            o = _order(ps_id=self.after_ps)
            return GUID, o
        return GUID, self.order

    def run(self, **kw):
        with patch.object(server, "call_linnworks_get", side_effect=self.get), \
             patch.object(server, "call_linnworks", side_effect=self.post), \
             patch.object(server, "_resolve_order_guid", side_effect=self.resolve), \
             patch.object(server, "get_shipping_quote", return_value=self.quote):
            return server.accept_shipping_quote(["612190"], **kw)


def test_dry_run_default_picks_cheapest_and_writes_nothing():
    f = Fake()
    r = f.run()
    assert r["dry_run"] is True and f.writes == []
    row = r["plan"][0]
    assert row["action"] == "change"
    assert row["chosen_quote"]["service_name"] == "Evri Standard"
    assert row["target_postal_service"] == "Evri Standard AMZ"


def test_live_run_sends_name_unwrapped_and_reads_back():
    f = Fake()
    r = f.run(dry_run=False)
    assert f.writes == [{"orderIds": [GUID], "shippingMethod": "Evri Standard AMZ"}]
    assert r["results"][0]["outcome"] == "changed" and r["changed"] == 1


def test_read_back_mismatch_is_not_changed():
    f = Fake(after_ps="cccccccc-0000-0000-0000-000000000001")
    r = f.run(dry_run=False)
    assert r["results"][0]["outcome"] == "not_changed"


def test_explicit_service_overrides_cheapest():
    f = Fake()
    r = f.run(service="Evri Next Day")
    assert r["plan"][0]["target_postal_service"] == "Evri Next Day AMZ"


def test_unknown_service_is_blocked_with_available_list():
    r = Fake().run(service="Royal Mail Tracked 24")
    row = r["plan"][0]
    assert row["blocked"] and row["reason"] == "service_not_quoted"
    assert row["available"] == ["Evri Standard", "Evri Next Day"]


def test_cheaper_unmappable_quote_is_skipped_not_guessed():
    quote = {**QUOTE, "quotes": [_q("RM 2nd", Q_UNMAPPED, 1.55)] + QUOTE["quotes"]}
    r = Fake(quote=quote).run()
    row = r["plan"][0]
    assert row["target_postal_service"] == "Evri Standard AMZ"
    assert row["cheaper_unmappable"][0]["service_name"] == "RM 2nd"


def test_no_mappable_quote_is_blocked():
    quote = {**QUOTE, "quotes": [_q("RM 2nd", Q_UNMAPPED, 1.55)]}
    row = Fake(quote=quote).run()["plan"][0]
    assert row["blocked"] and row["reason"] == "no_mappable_quote"


def test_already_on_chosen_service_is_no_op_and_not_written():
    f = Fake(order=_order(ps_id=PS_CHEAP, ps_name="Evri Standard AMZ"))
    r = f.run(dry_run=False)
    assert r["plan"][0]["action"] == "no_op" and f.writes == [] and r["results"] == []


@pytest.mark.parametrize("kw,reason", [
    ({"processed": True}, "processed"),
    ({"label": True}, "label_already_printed"),
    ({"tracking": "TRK123"}, "label_already_printed"),
])
def test_refusals_before_any_write(kw, reason):
    f = Fake(order=_order(**kw))
    r = f.run(dry_run=False)
    assert r["plan"][0]["reason"] == reason and f.writes == []


def test_ambiguous_postal_service_name_is_blocked():
    methods = SHIPPING_METHODS + [{"Vendor": "Other", "PostalServices": [
        {"pkPostalServiceId": "dddd", "PostalServiceName": "Evri Standard AMZ"}]}]
    row = Fake(methods=methods).run()["plan"][0]
    assert row["reason"] == "ambiguous_postal_service_name"


def test_no_quotes_is_blocked():
    row = Fake(quote={**QUOTE, "quotes": [], "warnings": ["processed"]}).run()["plan"][0]
    assert row["reason"] == "no_quotes"


def test_unresolvable_order_is_blocked_not_fatal():
    with patch.object(server, "call_linnworks_get", return_value=SHIPPING_METHODS), \
         patch.object(server, "_resolve_order_guid", side_effect=RuntimeError("No order")):
        r = server.accept_shipping_quote(["999"])
    assert r["plan"][0]["reason"] == "order_not_found"


def test_rate_limit_on_quote_is_reported_as_rate_limited():
    f = Fake()
    with patch.object(server, "call_linnworks_get", side_effect=f.get), \
         patch.object(server, "_resolve_order_guid", side_effect=f.resolve), \
         patch.object(server, "get_shipping_quote", side_effect=server.RateLimitError("429")):
        r = server.accept_shipping_quote(["612190"])
    assert r["plan"][0]["reason"] == "rate_limited"


def test_rate_limit_on_read_back_says_change_may_have_landed():
    f = Fake()
    calls = {"n": 0}

    def resolve(oid):
        calls["n"] += 1
        if f.written:
            raise server.RateLimitError("429")
        return GUID, f.order

    with patch.object(server, "call_linnworks_get", side_effect=f.get), \
         patch.object(server, "call_linnworks", side_effect=f.post), \
         patch.object(server, "_resolve_order_guid", side_effect=resolve), \
         patch.object(server, "get_shipping_quote", return_value=QUOTE):
        r = server.accept_shipping_quote(["612190"], dry_run=False)
    res = r["results"][0]
    assert res["outcome"] == "rate_limited" and "may have landed" in res["detail"]


def test_batch_above_threshold_is_staged():
    f = Fake()
    with patch.dict(server.WRITE_THRESHOLDS, {"accept_shipping_quote": 0}):
        r = f.run(dry_run=False)
    assert r.get("staged") is True and f.writes == []
