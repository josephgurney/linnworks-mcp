"""
Tests for the product-level duplicate guard in list_to_shopify (issue #38).

The incident: on 6 Aug 2026 a bulk run over the Surfboard Fins category created
**177 duplicate product pairs** on SWH Shopify. The item-level dedupe worked
perfectly — it correctly excluded all 459 already-listed items — and the outcome
was still wrong, because the category carried the same physical fins under two
SKU schemes:

    21SP101411-black  (old scheme, already listed, 0 stock)
    21SP101411-B      (new scheme, in stock, listed by this run)

Both are separate Linnworks items with separate StockItemIds, so neither knows
about the other. 184 of the 265 new listings had a BYTE-IDENTICAL ItemTitle to a
listing already live on the same store.

The guard therefore matches on normalised ItemTitle, and excludes by default.
"""
import pytest
from unittest.mock import patch

import server

STORE = "SWH Shopify"
CID = 18
CONF = "Fins Configurator"

# The real pair from the incident.
OLD_SKU, NEW_SKU = "21SP101411-black", "21SP101411-B"
TITLE = "Captain Fin Co - CF Twin + Trailer - Medium - Black (Futures)"

SID_OLD = "old00000-0000-0000-0000-000000000001"
SID_NEW = "new00000-0000-0000-0000-000000000002"
SID_UNIQ = "uni00000-0000-0000-0000-000000000003"


def _catalogue():
    return [{"id": 7, "name": CONF, "channel_id": CID, "sub_source": STORE,
             "show_in_inventory": True}]


ITEMS = {
    OLD_SKU:  {"StockItemId": SID_OLD,  "ItemTitle": TITLE},
    NEW_SKU:  {"StockItemId": SID_NEW,  "ItemTitle": TITLE},
    "UNIQUE": {"StockItemId": SID_UNIQ, "ItemTitle": "Something Else Entirely"},
}


def _channel_row(sku):
    return {"Source": "SHOPIFY", "SubSource": STORE, "SKU": sku,
            "ChannelReferenceId": "p:v:i", "ListedQuantity": 0}


def _run(skus, listed_sids=(SID_OLD,), **kw):
    """Run list_to_shopify as a dry run with a fixed catalogue and listing state."""
    def fake_post(path, payload):
        if path == "Inventory/GetInventoryItem":
            return ITEMS[payload["sku"]]
        raise AssertionError(f"unexpected POST {path}")

    def fake_props(sku, *a, **k):
        return {}

    channel_map = {}
    for sid in listed_sids:
        sku = next(k for k, v in ITEMS.items() if v["StockItemId"] == sid)
        channel_map[sid.lower()] = [_channel_row(sku)]

    with patch.object(server, "_fetch_shopify_configurators", side_effect=_catalogue), \
         patch.object(server, "call_linnworks", side_effect=fake_post), \
         patch.object(server, "_fetch_channel_skus_for_ids", return_value=channel_map), \
         patch.object(server, "_extended_properties_map", side_effect=fake_props, create=True):
        return server.list_to_shopify(
            skus=list(skus), configurator=CONF, sub_source=STORE, dry_run=True, **kw
        )


# --- normalisation -----------------------------------------------------------

@pytest.mark.parametrize("a,b", [
    ("Captain Fin Co - Twin", "captain fin co - twin"),
    ("Captain  Fin   Co", "Captain Fin Co"),
    ("  Trailing Space  ", "Trailing Space"),
])
def test_titles_normalise_equal(a, b):
    assert server._norm_title(a) == server._norm_title(b)


def test_distinct_titles_do_not_collide():
    assert server._norm_title("Fin - Medium") != server._norm_title("Fin - Large")


def test_empty_title_is_empty_and_never_matches():
    assert server._norm_title(None) == ""
    assert server._norm_title("") == ""


# --- the incident ------------------------------------------------------------

def test_new_scheme_sku_is_blocked_when_old_scheme_is_live():
    """THE regression: different SKU, same product, already live → excluded."""
    out = _run([OLD_SKU, NEW_SKU])

    planned = [r["sku"] for r in out["plan"]]
    assert NEW_SKU not in planned, "the duplicate must not reach CreateTemplates"

    assert [d["sku"] for d in out["possible_duplicates"]] == [NEW_SKU]
    dup = out["possible_duplicates"][0]
    assert dup["duplicate_of_skus"] == [OLD_SKU]
    assert dup["listed_anyway"] is False
    assert dup["matched_on"] == "normalised ItemTitle"
    assert "duplicate_warning" in out


def test_unique_titles_still_list_normally():
    out = _run([OLD_SKU, "UNIQUE"])
    assert [r["sku"] for r in out["plan"]] == ["UNIQUE"]
    assert out["possible_duplicates"] == []
    assert "duplicate_warning" not in out


def test_item_level_dedupe_still_applies():
    """The already-listed SKU itself is excluded by layer 1, as before."""
    out = _run([OLD_SKU, "UNIQUE"])
    assert [r["sku"] for r in out["already_listed"]] == [OLD_SKU]


def test_override_lists_the_duplicate_but_still_reports_it():
    out = _run([OLD_SKU, NEW_SKU], allow_duplicate_titles=True)
    assert NEW_SKU in [r["sku"] for r in out["plan"]]
    assert [d["sku"] for d in out["possible_duplicates"]] == [NEW_SKU]
    assert out["possible_duplicates"][0]["listed_anyway"] is True
    # An override is not a licence to go quiet, but it isn't a warning either.
    assert "duplicate_warning" not in out


def test_known_listed_titles_catches_it_when_the_old_sku_is_absent():
    """
    If the batch contains ONLY the new SKUs there is nothing in-batch to compare
    against — the caller must supply the live titles.
    """
    without = _run([NEW_SKU], listed_sids=())
    assert [r["sku"] for r in without["plan"]] == [NEW_SKU]
    assert without["possible_duplicates"] == []

    with_hint = _run([NEW_SKU], listed_sids=(), known_listed_titles=[TITLE])
    assert with_hint["plan"] == []
    assert [d["sku"] for d in with_hint["possible_duplicates"]] == [NEW_SKU]


def test_known_listed_titles_is_case_and_whitespace_insensitive():
    out = _run([NEW_SKU], listed_sids=(),
               known_listed_titles=["  captain FIN co  -  CF Twin + Trailer - Medium - Black (Futures) "])
    assert [d["sku"] for d in out["possible_duplicates"]] == [NEW_SKU]


def test_duplicate_is_excluded_from_the_configurator_groups():
    """A blocked duplicate must not sneak through via the grouping step."""
    out = _run([OLD_SKU, NEW_SKU])
    all_grouped = [s for g in out["groups"] for s in g["skus"]]
    assert NEW_SKU not in all_grouped


def test_dry_run_message_reports_the_exclusion():
    out = _run([OLD_SKU, NEW_SKU])
    assert "EXCLUDED" in out["message"]
    assert "possible_duplicates" in out["message"]


def test_override_message_says_listed_anyway():
    out = _run([OLD_SKU, NEW_SKU], allow_duplicate_titles=True)
    assert "allow_duplicate_titles=True" in out["message"]
