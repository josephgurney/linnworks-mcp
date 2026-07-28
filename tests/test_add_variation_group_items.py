"""
Tests for add_variation_group_items (issue #29) and the corrected
create_variation_group.

Endpoint behaviour these tests encode (all live-confirmed 28 Jul 2026):
  - Stock/AddVariationItems POST {"pkVariationItemId":guid,"pkStockItemIds":[...]}
    adds children to an existing group (empty 2xx).
  - Stock/GetVariationGroupByName is UNRELIABLE (returns null even for groups
    that exist) — group-name lookup goes via Stock/SearchVariationGroups
    (searchType=VariationName, substring) with exact-match confirmation.
  - Stock/CreateVariationGroup requires a NEW parent SKU (not an existing item);
    ParentStockItemId is the zero-GUID (server mints the real id, returned as
    pkVariationItemId). CheckVariationParentSKUExists → Exists / AlreadyVariation
    / NotExists.
"""
import pytest
from unittest.mock import patch

GID = "aaaaaaaa-0000-0000-0000-000000000001"   # group id (== parent StockItemId)
VSKU = "VG-PARENT-NEW"                          # virtual parent SKU
C1_ID = "11111111-0000-0000-0000-000000000001"
C2_ID = "22222222-0000-0000-0000-000000000002"
C3_ID = "33333333-0000-0000-0000-000000000003"
GROUP = "My Test Group"

SKU_IDS = {"C1": C1_ID, "C2": C2_ID, "C3": C3_ID}


def _mock(members, existing_parent=True):
    """
    members: list of child SKU strings currently in the group.
    existing_parent: whether GetVariationGroupByParentId(GID) resolves.
    Returns (patches, captured).
    """
    captured = {"add": [], "create": []}
    state = {"members": list(members)}

    def call_linnworks(path, payload):
        if path.endswith("GetInventoryItem"):
            sku = payload.get("sku")
            if sku in SKU_IDS:
                return {"StockItemId": SKU_IDS[sku], "ItemTitle": sku}
            if sku == VSKU:
                return {"StockItemId": GID, "ItemTitle": VSKU}
            raise RuntimeError(
                f"HTTP 400 — Could not determine inventory item id from SKU {sku}"
            )
        if path.endswith("AddVariationItems"):
            captured["add"].append(payload)
            # reflect the add into state (id -> sku)
            id_to_sku = {v: k for k, v in SKU_IDS.items()}
            for iid in payload.get("pkStockItemIds", []):
                state["members"].append(id_to_sku.get(iid, iid))
            return {}
        if path.endswith("CreateVariationGroup"):
            captured["create"].append(payload)
            return {"VariationSKU": VSKU, "pkVariationItemId": GID,
                    "VariationGroupName": GROUP}
        raise AssertionError(f"Unexpected call_linnworks: {path}")

    def call_linnworks_get(path, params=None):
        params = params or {}
        if "GetVariationGroupByParentId" in path:
            if existing_parent and params.get("pkStockItemId") == GID:
                return {"VariationSKU": VSKU, "pkVariationItemId": GID,
                        "VariationGroupName": GROUP}
            return None
        if "GetVariationItems" in path:
            return [
                {"ItemNumber": s, "pkStockItemId": SKU_IDS.get(s, s),
                 "ItemTitle": s} for s in state["members"]
            ]
        if "SearchVariationGroups" in path:
            if params.get("searchType") == "VariationName":
                if (params.get("searchText") or "").strip().lower() in GROUP.lower():
                    return {"Data": [{"VariationSKU": VSKU,
                                      "pkVariationItemId": GID,
                                      "VariationGroupName": GROUP}],
                            "TotalPages": 1}
            return {"Data": [], "TotalPages": 1}
        if "CheckVariationParentSKUExists" in path:
            sku = params.get("parentSKU")
            if sku == VSKU:
                return "NotExists"
            if sku in SKU_IDS:
                return "Exists"
            return "NotExists"
        raise AssertionError(f"Unexpected call_linnworks_get: {path}")

    patches = [
        patch("server.call_linnworks", side_effect=call_linnworks),
        patch("server.call_linnworks_get", side_effect=call_linnworks_get),
    ]
    return patches, captured


def _run(fn, *args, members=None, existing_parent=True, **kwargs):
    import server
    patches, captured = _mock(members or [], existing_parent)
    for p in patches:
        p.start()
    try:
        out = getattr(server, fn)(*args, **kwargs)
    finally:
        for p in patches:
            p.stop()
    return out, captured


# ─────────────────────────── add_variation_group_items ──────────────────────

def test_add_by_parent_dry_run_shows_diff():
    out, cap = _run("add_variation_group_items", ["C3"], parent_sku=VSKU,
                    members=["C1", "C2"], dry_run=True)
    assert out["status"] == "dry_run"
    assert out["to_add"] == ["C3"]
    assert out["already_present"] == []
    assert cap["add"] == []  # nothing written on dry run


def test_add_by_parent_live_writes_and_reads_back():
    out, cap = _run("add_variation_group_items", ["C3"], parent_sku=VSKU,
                    members=["C1", "C2"], dry_run=False)
    assert out["status"] == "added"
    assert len(cap["add"]) == 1
    assert cap["add"][0]["pkVariationItemId"] == GID
    assert cap["add"][0]["pkStockItemIds"] == [C3_ID]
    assert out["added"] == [{"sku": "C3", "added": True}]
    assert out["member_count_after"] == 3


def test_add_idempotent_no_op_when_all_present():
    out, cap = _run("add_variation_group_items", ["C1", "C2"], parent_sku=VSKU,
                    members=["C1", "C2"], dry_run=False)
    assert out["status"] == "no_op"
    assert out["to_add"] == []
    assert sorted(out["already_present"]) == ["C1", "C2"]
    assert cap["add"] == []


def test_add_by_group_name_path():
    out, cap = _run("add_variation_group_items", ["C3"], group_name=GROUP,
                    members=["C1", "C2"], dry_run=True)
    assert out["status"] == "dry_run"
    assert out["parent_sku"] == VSKU
    assert out["pk_variation_item_id"] == GID
    assert out["to_add"] == ["C3"]


def test_add_bogus_child_becomes_error_row():
    out, cap = _run("add_variation_group_items", ["NOPE"], parent_sku=VSKU,
                    members=["C1"], dry_run=True)
    assert out["status"] == "error"
    assert out["child_errors"][0]["sku"] == "NOPE"
    assert cap["add"] == []


def test_add_partial_bogus_still_adds_valid():
    out, cap = _run("add_variation_group_items", ["C3", "NOPE"], parent_sku=VSKU,
                    members=["C1"], dry_run=False)
    assert out["status"] == "added"
    assert out["to_add"] == ["C3"]
    assert len(out["child_errors"]) == 1
    assert cap["add"][0]["pkStockItemIds"] == [C3_ID]


def test_add_non_parent_sku_errors():
    out, cap = _run("add_variation_group_items", ["C2"], parent_sku="C1",
                    members=["C1", "C2"], existing_parent=False, dry_run=True)
    assert out["status"] == "error"
    assert "not a variation parent" in out["message"]


def test_add_requires_an_identifier():
    out, cap = _run("add_variation_group_items", ["C1"], dry_run=True)
    assert out["status"] == "error"
    assert "parent_sku or group_name" in out["message"]


def test_add_group_name_mismatch_errors():
    out, cap = _run("add_variation_group_items", ["C3"], parent_sku=VSKU,
                    group_name="Different Name", members=["C1"], dry_run=True)
    assert out["status"] == "error"
    assert "does not match" in out["message"]


def test_add_dedupes_within_request():
    out, cap = _run("add_variation_group_items", ["C3", "C3"], parent_sku=VSKU,
                    members=["C1"], dry_run=True)
    assert out["to_add"] == ["C3"]


# ─────────────────────────── create_variation_group (fixed) ─────────────────

def test_create_rejects_existing_sku_as_parent():
    out, cap = _run("create_variation_group", GROUP, "C1", ["C2"],
                    members=[], dry_run=True)
    assert out["status"] == "error"
    assert out["parent_sku_state"] == "Exists"
    assert cap["create"] == []


def test_create_dry_run_new_parent():
    # No group of that name yet: force SearchVariationGroups to miss.
    import server
    patches, cap = _mock([], existing_parent=False)
    # override name search to return empty for a fresh group
    for p in patches:
        p.start()
    try:
        with patch("server._find_variation_group_by_name", return_value=None):
            out = server.create_variation_group("Fresh Group", VSKU, ["C1", "C2"],
                                                dry_run=True)
    finally:
        for p in patches:
            p.stop()
    assert out["status"] == "dry_run"
    assert out["child_ids"] == [C1_ID, C2_ID]
    assert cap["create"] == []


def test_create_live_uses_zero_guid_parent_id():
    import server
    patches, cap = _mock([], existing_parent=True)
    for p in patches:
        p.start()
    try:
        with patch("server._find_variation_group_by_name", return_value=None):
            out = server.create_variation_group("Fresh Group", VSKU, ["C1"],
                                                dry_run=False)
    finally:
        for p in patches:
            p.stop()
    assert out["status"] == "created"
    tmpl = cap["create"][0]["template"]
    assert tmpl["ParentStockItemId"] == "00000000-0000-0000-0000-000000000000"
    assert tmpl["ParentSKU"] == VSKU
    assert tmpl["VariationItemIds"] == [C1_ID]
    assert out["pk_variation_item_id"] == GID


def test_create_already_exists_by_name():
    out, cap = _run("create_variation_group", GROUP, VSKU, ["C1"],
                    members=["C1"], dry_run=False)
    assert out["status"] == "already_exists"
    assert cap["create"] == []
