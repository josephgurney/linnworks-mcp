"""
Tests for create_or_update_inventory_item's category handling.

The defect (confirmed live 14 Sep 2026): the tool accepted `category_name` but
never set the category. Seven new SKUs created with category_name="Venom
Completes" all landed in "Default", and a follow-up upsert with category_name
(and a category_id key) still left them in Default.

Root cause: Linnworks keys an item's category on `CategoryId` (GUID), not the
name. The update path carried the EXISTING CategoryId through unchanged (so the
old Default GUID won), and the create path sent only a CategoryName with no
CategoryId (so Linnworks ignored it and assigned Default). Nothing read the
category back, so the no-op was silent.

The fix resolves category_name / category_id against Inventory/GetCategories
(case-insensitive), sends BOTH CategoryId and CategoryName on create and update,
and reads CategoryId back after every live write so a silent no-op cannot recur.
"""
import pytest
from unittest.mock import patch

import server

DEFAULT_ID = "00000000-0000-0000-0000-000000000000"
VENOM_ID   = "ac5f1f0c-cdab-4ac4-b35a-01e44de32903"
DECKS_ID   = "b1b1b1b1-0000-0000-0000-000000000002"

CATEGORIES = [
    {"CategoryId": DEFAULT_ID, "CategoryName": "Default",         "StructureCategoryId": 0, "ProductCategoryId": 0},
    {"CategoryId": VENOM_ID,   "CategoryName": "Venom Completes", "StructureCategoryId": 0, "ProductCategoryId": 0},
    {"CategoryId": DECKS_ID,   "CategoryName": "Skateboard Decks","StructureCategoryId": 0, "ProductCategoryId": 0},
]

EXISTING_SID = "11111111-0000-0000-0000-000000000001"


def _existing_item(sku="VEN-EXISTING"):
    """A realistic GetInventoryItem response for an item sitting in Default."""
    return {
        "StockItemId": EXISTING_SID, "ItemNumber": sku, "ItemTitle": "Existing Title",
        "BarcodeNumber": "5000000000001", "RetailPrice": 49.99, "PurchasePrice": 20.0,
        "TaxRate": 20.0, "CategoryId": DEFAULT_ID, "CategoryName": "Default",
        "Weight": 2.5, "Height": 10.0, "Width": 20.0, "Depth": 80.0,
        "MetaData": "keep me", "IsCompositeParent": False, "ShippedSeparately": False,
        "IsVariationParent": False, "isBatchedStockType": False,
        "PostalServiceId": "ps-1", "PostalServiceName": "DPD", "PackageGroupId": "pg-1",
        "PackageGroupName": "Skateboard", "InventoryTrackingType": 0,
        "BatchNumberScanRequired": False, "SerialNumberScanRequired": False,
    }


class FakeLinnworks:
    """
    Minimal in-memory Linnworks: GetInventoryItem / AddInventoryItem /
    UpdateInventoryItem / GetCategories.

    `honour_category=False` reproduces the live defect from the server side —
    every write lands in Default regardless of payload — so the read-back's
    mismatch detection is exercised against real tool behaviour.

    `null_category_name=True` reproduces the live GetInventoryItem quirk
    (verified 14 Sep 2026): the endpoint returns the stored CategoryId but a
    NULL CategoryName, on the probe and on the read-back alike, whatever was
    written.
    """

    def __init__(self, items=None, honour_category=True, readback_error=None,
                 null_category_name=False):
        self.items = {i["ItemNumber"]: dict(i) for i in (items or [])}
        self.honour_category = honour_category
        self.readback_error = readback_error
        self.null_category_name = null_category_name
        self.writes = []            # (path, payload)
        self.get_categories_calls = 0
        self.get_item_calls = []    # skus in order

    def call_linnworks(self, path, payload):
        if path == "Inventory/GetInventoryItem":
            sku = payload["sku"]
            self.get_item_calls.append(sku)
            # A read-back is any GetInventoryItem AFTER a write to that SKU.
            if self.readback_error and any(p["inventoryItem"]["ItemNumber"] == sku
                                           for _, p in self.writes):
                raise self.readback_error
            if sku in self.items:
                item = dict(self.items[sku])
                if self.null_category_name:
                    item["CategoryName"] = None
                return item
            raise RuntimeError("HTTP 400 — no such SKU")
        if path in ("Inventory/AddInventoryItem", "Inventory/UpdateInventoryItem"):
            self.writes.append((path, payload))
            item = dict(payload["inventoryItem"])
            if not self.honour_category or not item.get("CategoryId"):
                item["CategoryId"], item["CategoryName"] = DEFAULT_ID, "Default"
            self.items[item["ItemNumber"]] = item
            return {}
        raise AssertionError(f"Unexpected call_linnworks: {path}")

    def call_linnworks_get(self, path, params=None):
        if path == "Inventory/GetCategories":
            self.get_categories_calls += 1
            return [dict(c) for c in CATEGORIES]
        raise AssertionError(f"Unexpected call_linnworks_get: {path}")

    def written_item(self, path_suffix):
        for path, payload in self.writes:
            if path.endswith(path_suffix):
                return payload["inventoryItem"]
        raise AssertionError(f"No {path_suffix} write recorded; writes={self.writes}")


def _run(items, fake, **kwargs):
    with patch("server.call_linnworks", side_effect=fake.call_linnworks), \
         patch("server.call_linnworks_get", side_effect=fake.call_linnworks_get):
        return server.create_or_update_inventory_item(items, **kwargs)


# --- the two live-observed failure paths --------------------------------------

def test_create_sends_resolved_category_id_and_canonical_name():
    fake = FakeLinnworks()
    out = _run([{"sku": "VEN-NEW-1", "title": "Venom Complete 8.0",
                 "category_name": "venom completes"}], fake, dry_run=False)

    payload = fake.written_item("AddInventoryItem")
    assert payload["CategoryId"] == VENOM_ID
    assert payload["CategoryName"] == "Venom Completes"   # canonical casing, not the input
    assert out["created"] == 1 and out["errors"] == 0


def test_update_overrides_existing_default_category_id_not_just_the_name():
    """The live bug: an upsert on an existing item wrote the new CategoryName
    but carried the old (Default) CategoryId through — and the GUID wins."""
    fake = FakeLinnworks(items=[_existing_item()])
    out = _run([{"sku": "VEN-EXISTING", "category_name": "Venom Completes"}],
               fake, dry_run=False)

    payload = fake.written_item("UpdateInventoryItem")
    assert payload["CategoryId"] == VENOM_ID
    assert payload["CategoryName"] == "Venom Completes"
    assert out["updated"] == 1 and out["errors"] == 0


def test_update_still_carries_every_unsupplied_field():
    """UpdateInventoryItem clears omitted fields — the category fix must not
    disturb the existing merge."""
    fake = FakeLinnworks(items=[_existing_item()])
    _run([{"sku": "VEN-EXISTING", "category_name": "Venom Completes"}],
         fake, dry_run=False)

    payload = fake.written_item("UpdateInventoryItem")
    assert payload["StockItemId"] == EXISTING_SID
    assert payload["ItemTitle"] == "Existing Title"
    assert payload["BarcodeNumber"] == "5000000000001"
    assert payload["RetailPrice"] == 49.99
    assert payload["MetaData"] == "keep me"
    assert payload["PostalServiceName"] == "DPD"
    assert payload["PackageGroupName"] == "Skateboard"


# --- read-back: the silent no-op must be loud ---------------------------------

def test_live_results_report_category_read_back_from_linnworks():
    fake = FakeLinnworks(items=[_existing_item()])
    out = _run([{"sku": "VEN-EXISTING", "category_name": "Venom Completes"},
                {"sku": "VEN-NEW-1", "title": "New", "category_name": "Skateboard Decks"}],
               fake, dry_run=False)

    by_sku = {r["sku"]: r for r in out["results"]}
    assert by_sku["VEN-EXISTING"]["category_id"] == VENOM_ID
    assert by_sku["VEN-EXISTING"]["category_name"] == "Venom Completes"
    assert by_sku["VEN-EXISTING"]["category_verified"] is True
    assert by_sku["VEN-NEW-1"]["category_id"] == DECKS_ID
    assert by_sku["VEN-NEW-1"]["category_verified"] is True
    assert out["category_mismatches"] == 0
    # The read-back is a FRESH GetInventoryItem after the write, per SKU.
    assert fake.get_item_calls.count("VEN-EXISTING") == 2   # probe + read-back
    assert fake.get_item_calls.count("VEN-NEW-1") == 2


def test_read_back_flags_a_category_that_did_not_land():
    """Reproduce the live symptom from the server side: Linnworks accepts the
    write with a 2xx but leaves the item in Default. The tool must say so."""
    fake = FakeLinnworks(items=[_existing_item()], honour_category=False)
    out = _run([{"sku": "VEN-EXISTING", "category_name": "Venom Completes"}],
               fake, dry_run=False)

    row = out["results"][0]
    assert row["action"] == "updated"               # the write itself happened
    assert row["category_verified"] is False
    assert row["category_id"] == DEFAULT_ID          # what Linnworks actually holds
    assert "Venom Completes" in row["warning"] and "Default" in row["warning"]
    assert out["category_mismatches"] == 1
    assert "category" in out["warning"].lower()


def test_read_back_rate_limit_is_not_reported_as_a_mismatch():
    """A quota failure on the read-back must not be laundered into a claim
    that the category did or did not land (the standing #34/#37 rule)."""
    fake = FakeLinnworks(items=[_existing_item()],
                         readback_error=server.RateLimitError("HTTP 429"))
    out = _run([{"sku": "VEN-EXISTING", "category_name": "Venom Completes"}],
               fake, dry_run=False)

    row = out["results"][0]
    assert row["action"] == "updated"
    assert row["category_verified"] is None
    assert row["readback"] == "rate_limited"
    assert out["category_mismatches"] == 0


# --- read-back: GetInventoryItem returns a NULL CategoryName (live quirk) --------

def test_read_back_fills_null_category_name_from_the_batch_category_list():
    """Live (14 Sep 2026): GetInventoryItem returns CategoryId but a null
    CategoryName, so v1.52.1 reported `category_name: null` even when the
    category landed. The name is already known — GetCategories was fetched to
    resolve the request — so fill it from there. No extra API call."""
    fake = FakeLinnworks(items=[_existing_item()], null_category_name=True)
    out = _run([{"sku": "VEN-EXISTING", "category_name": "Venom Completes"},
                {"sku": "VEN-NEW-1", "title": "New", "category_name": "Skateboard Decks"}],
               fake, dry_run=False)

    by_sku = {r["sku"]: r for r in out["results"]}
    assert by_sku["VEN-EXISTING"]["category_id"] == VENOM_ID
    assert by_sku["VEN-EXISTING"]["category_name"] == "Venom Completes"
    assert by_sku["VEN-EXISTING"]["category_verified"] is True
    assert by_sku["VEN-NEW-1"]["category_id"] == DECKS_ID
    assert by_sku["VEN-NEW-1"]["category_name"] == "Skateboard Decks"
    assert by_sku["VEN-NEW-1"]["category_verified"] is True
    assert out["category_mismatches"] == 0
    assert fake.get_categories_calls == 1                # the resolve fetch, nothing more


def test_read_back_fills_the_name_for_an_item_that_named_no_category_when_the_batch_did():
    """The list was fetched for a sibling item; an item that asked for nothing
    still gets its stored category named from it — for free."""
    fake = FakeLinnworks(items=[_existing_item()], null_category_name=True)
    out = _run([{"sku": "VEN-NEW-1", "title": "New", "category_name": "Venom Completes"},
                {"sku": "VEN-EXISTING", "title": "Renamed"}],
               fake, dry_run=False)

    by_sku = {r["sku"]: r for r in out["results"]}
    assert by_sku["VEN-EXISTING"]["category_id"] == DEFAULT_ID
    assert by_sku["VEN-EXISTING"]["category_name"] == "Default"
    assert by_sku["VEN-EXISTING"]["category_verified"] is None   # nothing was requested
    assert fake.get_categories_calls == 1


def test_read_back_leaves_the_name_null_when_nothing_in_the_batch_named_a_category():
    """Deliberate: no category requested → GetCategories is never called (a
    tested invariant since v1.52.1), so there is nothing to fill from and the
    field stays null rather than spending a rate-limited call on a cosmetic."""
    fake = FakeLinnworks(items=[_existing_item()], null_category_name=True)
    out = _run([{"sku": "VEN-EXISTING", "title": "Renamed"}], fake, dry_run=False)

    row = out["results"][0]
    assert row["category_id"] == DEFAULT_ID
    assert row["category_name"] is None
    assert row["category_verified"] is None
    assert row["readback"] == "ok"
    assert fake.get_categories_calls == 0


def test_read_back_mismatch_warning_names_the_category_it_actually_landed_in():
    """With a null CategoryName the v1.52.1 warning read "read back 'None'".
    The GUID is known and the list is in hand — name it."""
    fake = FakeLinnworks(items=[_existing_item()], honour_category=False,
                         null_category_name=True)
    out = _run([{"sku": "VEN-EXISTING", "category_name": "Venom Completes"}],
               fake, dry_run=False)

    row = out["results"][0]
    assert row["category_verified"] is False
    assert row["category_id"] == DEFAULT_ID
    assert row["category_name"] == "Default"
    assert "Venom Completes" in row["warning"] and "'Default'" in row["warning"]
    assert "'None'" not in row["warning"]
    assert out["category_mismatches"] == 1


def test_read_back_keeps_a_name_linnworks_does_return():
    """If the endpoint ever starts returning CategoryName, that wins — the
    list is only a fallback for a null."""
    fake = FakeLinnworks(items=[_existing_item()])          # returns the written name
    out = _run([{"sku": "VEN-EXISTING", "category_name": "Venom Completes"}],
               fake, dry_run=False)

    assert out["results"][0]["category_name"] == "Venom Completes"
    assert fake.get_categories_calls == 1


def test_read_back_null_name_with_a_guid_not_in_the_list_stays_null_and_is_still_a_mismatch():
    """Verification is on the GUID, never the name: an unknown GUID with no
    name to fill is reported as-is and still counts as not landed."""
    class StrayCategory(FakeLinnworks):
        def call_linnworks(self, path, payload):
            out = super().call_linnworks(path, payload)
            if path == "Inventory/GetInventoryItem":
                out["CategoryId"] = "deadbeef-0000-0000-0000-000000000000"
            return out

    fake = StrayCategory(items=[_existing_item()], null_category_name=True)
    out = _run([{"sku": "VEN-EXISTING", "category_name": "Venom Completes"}],
               fake, dry_run=False)

    row = out["results"][0]
    assert row["category_id"] == "deadbeef-0000-0000-0000-000000000000"
    assert row["category_name"] is None
    assert row["category_verified"] is False
    assert out["category_mismatches"] == 1


# --- resolution rules -----------------------------------------------------------

def test_category_id_is_accepted_directly_and_matched_case_insensitively():
    fake = FakeLinnworks()
    out = _run([{"sku": "VEN-NEW-1", "title": "New", "category_id": VENOM_ID.upper()}],
               fake, dry_run=False)

    payload = fake.written_item("AddInventoryItem")
    assert payload["CategoryId"] == VENOM_ID
    assert payload["CategoryName"] == "Venom Completes"
    assert out["created"] == 1


def test_unknown_category_name_is_a_clear_per_item_error_and_writes_nothing():
    fake = FakeLinnworks(items=[_existing_item()])
    out = _run([{"sku": "VEN-EXISTING", "category_name": "Venom Complete"}],  # typo
               fake, dry_run=False)

    row = out["results"][0]
    assert row["action"] == "error"
    assert "Venom Complete" in row["error"]
    assert "get_categories" in row["error"]
    assert "Venom Completes" in row["error"]        # names the real options
    assert out["errors"] == 1 and out["updated"] == 0
    assert fake.writes == []


def test_unknown_category_id_is_a_clear_per_item_error():
    fake = FakeLinnworks()
    out = _run([{"sku": "VEN-NEW-1", "title": "New",
                 "category_id": "deadbeef-0000-0000-0000-000000000000"}],
               fake, dry_run=False)

    row = out["results"][0]
    assert row["action"] == "error"
    assert "deadbeef" in row["error"]
    assert fake.writes == []


def test_category_name_and_id_that_disagree_are_refused():
    fake = FakeLinnworks()
    out = _run([{"sku": "VEN-NEW-1", "title": "New",
                 "category_name": "Skateboard Decks", "category_id": VENOM_ID}],
               fake, dry_run=False)

    row = out["results"][0]
    assert row["action"] == "error"
    assert "Skateboard Decks" in row["error"] and "Venom Completes" in row["error"]
    assert fake.writes == []


def test_bad_category_on_one_item_does_not_block_the_others():
    fake = FakeLinnworks()
    out = _run([{"sku": "VEN-NEW-1", "title": "A", "category_name": "Nope"},
                {"sku": "VEN-NEW-2", "title": "B", "category_name": "Venom Completes"}],
               fake, dry_run=False)

    assert out["errors"] == 1 and out["created"] == 1
    assert [p["inventoryItem"]["ItemNumber"] for _, p in fake.writes] == ["VEN-NEW-2"]


def test_categories_are_fetched_once_per_batch():
    fake = FakeLinnworks()
    _run([{"sku": f"VEN-NEW-{i}", "title": "x", "category_name": "Venom Completes"}
          for i in range(3)], fake, dry_run=False)
    assert fake.get_categories_calls == 1


# --- no category supplied: behaviour unchanged ----------------------------------

def test_no_category_supplied_makes_no_categories_call_and_carries_existing():
    fake = FakeLinnworks(items=[_existing_item()])
    out = _run([{"sku": "VEN-EXISTING", "title": "Renamed"}], fake, dry_run=False)

    payload = fake.written_item("UpdateInventoryItem")
    assert payload["CategoryId"] == DEFAULT_ID
    assert payload["CategoryName"] == "Default"
    assert fake.get_categories_calls == 0
    row = out["results"][0]
    assert row["category_id"] == DEFAULT_ID          # still read back and reported
    assert row["category_verified"] is None          # nothing was requested to verify


def test_create_without_category_keeps_the_live_tested_payload_shape():
    """AddInventoryItem with no category was live-tested 15 Jun 2026 (lands in
    Default). Don't change what is sent when the caller asks for nothing."""
    fake = FakeLinnworks()
    _run([{"sku": "VEN-NEW-1", "title": "New"}], fake, dry_run=False)

    payload = fake.written_item("AddInventoryItem")
    assert "CategoryId" not in payload
    assert payload["CategoryName"] == ""
    assert fake.get_categories_calls == 0


# --- dry run ---------------------------------------------------------------------

def test_dry_run_manifest_shows_resolved_category_id_and_writes_nothing():
    fake = FakeLinnworks(items=[_existing_item()])
    out = _run([{"sku": "VEN-EXISTING", "category_name": "venom completes"}], fake)

    assert out["dry_run"] is True
    m = out["manifest"][0]
    assert m["category_name"] == "Venom Completes"
    assert m["category_id"] == VENOM_ID
    assert fake.writes == []


def test_dry_run_manifest_surfaces_an_unknown_category_before_any_write():
    fake = FakeLinnworks()
    out = _run([{"sku": "VEN-NEW-1", "title": "x", "category_name": "Nope"}], fake)

    m = out["manifest"][0]
    assert m["category_id"] is None
    assert "Nope" in m["category_error"]
    assert out["category_errors"] == 1
    assert fake.writes == []


# --- docs ---------------------------------------------------------------------------

def test_docstring_no_longer_claims_linnworks_auto_resolves_the_category():
    doc = server.create_or_update_inventory_item.__doc__
    assert "auto-resolves" not in doc
    assert "category_id" in doc
    assert "GetCategories" in doc
