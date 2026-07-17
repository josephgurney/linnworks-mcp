"""
Tests for delete_inventory_item_images / set_inventory_item_image_order (issue #28).

Endpoint behaviour these tests encode (all live-confirmed 17 Jul 2026):
  - Inventory/DeleteInventoryItemImageBulk POSTs
    {"request":[{InventoryItemId, ItemNumber, ImageIds}]} and returns a
    BatchedAPIResponse (NOT 204). It keys off image IDs — unlike
    DeleteImagesFromInventoryItem, which keys off image URLs.
  - Inventory/UpdateImages POSTs {"images":[StockItemImageSimple,...]} → 204.
    It CLEARS any field omitted from a row, so the full row must be carried.
  - Inventory/SetInventoryItemImageAsMain POSTs {inventoryItemId, mainImageId}
    → 204. It sets the flag but does NOT reorder.
  - ⚠️ UpdateImages PINS the main image to SortOrder 0. The resulting order is
    always [main] + [requested order minus main]; a full main-first payload is
    honoured exactly, a non-main-first one is overridden. The tool must predict
    this rather than promise an order Linnworks won't deliver.
"""
import pytest
from unittest.mock import patch

SID = "22222222-0000-0000-0000-000000000001"
IMG1 = "aaaaaaaa-0000-0000-0000-000000000001"
IMG2 = "bbbbbbbb-0000-0000-0000-000000000002"
IMG3 = "cccccccc-0000-0000-0000-000000000003"
BOGUS = "00000000-0000-0000-0000-000000000999"


def _raw(img_id, sort, is_main=False):
    return {
        "pkRowId": img_id, "IsMain": is_main, "SortOrder": sort,
        "Source": f"https://img/{img_id}_t.jpg",
        "FullSource": f"https://img/{img_id}.jpg",
        "CheckSumValue": f"sum-{img_id[:4]}", "RawChecksum": f"raw-{img_id[:4]}",
        "StockItemId": SID, "StockItemIntId": 7,
    }


# Baseline: img1 is main at position 0.
BASE = [_raw(IMG1, 0, True), _raw(IMG2, 1), _raw(IMG3, 2)]


def _mocks(rows, delete_effect=None):
    """Patch the Linnworks layer. `rows` is mutated to simulate server state."""
    state = {"rows": [dict(r) for r in rows]}
    captured = {"void": [], "post": []}

    def call_linnworks(path, payload):
        if path.endswith("GetInventoryItem"):
            if payload.get("sku") == "TEST-SKU":
                return {"StockItemId": SID, "ItemTitle": "Test Item"}
            raise RuntimeError("HTTP 400 — no such SKU")
        if path.endswith("DeleteInventoryItemImageBulk"):
            captured["post"].append((path, payload))
            gone = set(payload["request"][0]["ImageIds"])
            if delete_effect != "noop":
                state["rows"] = [r for r in state["rows"] if r["pkRowId"] not in gone]
            return {"ResultStatus": "SUCCESSFUL", "TotalResults": len(gone)}
        raise AssertionError(f"Unexpected call_linnworks: {path}")

    def call_linnworks_get(path, params=None):
        if "GetInventoryItemImages" in path:
            return [dict(r) for r in state["rows"]]
        raise AssertionError(f"Unexpected call_linnworks_get: {path}")

    def call_linnworks_void(path, payload):
        captured["void"].append((path, payload))
        if path.endswith("SetInventoryItemImageAsMain"):
            for r in state["rows"]:
                r["IsMain"] = r["pkRowId"] == payload["mainImageId"]
        elif path.endswith("UpdateImages"):
            # Mirror the real server: main pinned to 0, rest in submitted order.
            submitted = payload["images"]
            main_id = next((s["pkRowId"] for s in submitted if s.get("IsMain")), None)
            ordered = ([s for s in submitted if s["pkRowId"] == main_id]
                       + [s for s in submitted if s["pkRowId"] != main_id])
            by_id = {r["pkRowId"]: r for r in state["rows"]}
            for pos, s in enumerate(ordered):
                by_id[s["pkRowId"]]["SortOrder"] = pos
                by_id[s["pkRowId"]]["IsMain"] = s["pkRowId"] == main_id

    return ([
        patch("server.call_linnworks", side_effect=call_linnworks),
        patch("server.call_linnworks_get", side_effect=call_linnworks_get),
        patch("server.call_linnworks_void", side_effect=call_linnworks_void),
    ], captured, state)


@pytest.fixture
def srv():
    import server
    return server


# ── delete_inventory_item_images ──────────────────────────────────────────────

def test_delete_dry_run_writes_nothing(srv):
    patches, captured, state = _mocks(BASE)
    for p in patches: p.start()
    try:
        r = srv.delete_inventory_item_images("TEST-SKU", [IMG2])
    finally:
        for p in patches: p.stop()
    assert r["dry_run"] is True
    assert captured["post"] == []
    assert len(state["rows"]) == 3
    assert r["manifest"][0]["image_id"] == IMG2


def test_delete_live_uses_image_ids_and_reads_back(srv):
    patches, captured, state = _mocks(BASE)
    for p in patches: p.start()
    try:
        r = srv.delete_inventory_item_images("TEST-SKU", [IMG2], dry_run=False)
    finally:
        for p in patches: p.stop()
    path, payload = captured["post"][0]
    assert path.endswith("DeleteInventoryItemImageBulk")
    assert payload == {"request": [{
        "InventoryItemId": SID, "ItemNumber": "TEST-SKU", "ImageIds": [IMG2],
    }]}
    assert r["deleted"] == [IMG2]
    assert r["still_present"] == []
    assert r["remaining_count"] == 2
    assert r["result_status"] == "SUCCESSFUL"


def test_delete_reports_still_present_when_server_keeps_image(srv):
    """A 2xx does not mean it's gone — the read-back is the source of truth."""
    patches, captured, state = _mocks(BASE, delete_effect="noop")
    for p in patches: p.start()
    try:
        r = srv.delete_inventory_item_images("TEST-SKU", [IMG2], dry_run=False)
    finally:
        for p in patches: p.stop()
    assert r["deleted"] == []
    assert r["still_present"] == [IMG2]


def test_delete_unknown_id_is_skipped_not_fatal(srv):
    patches, captured, state = _mocks(BASE)
    for p in patches: p.start()
    try:
        r = srv.delete_inventory_item_images("TEST-SKU", [IMG2, BOGUS], dry_run=False)
    finally:
        for p in patches: p.stop()
    assert r["deleted"] == [IMG2]
    assert len(r["unresolved"]) == 1
    assert r["unresolved"][0]["image_id"] == BOGUS
    # only the real id reached the API
    assert captured["post"][0][1]["request"][0]["ImageIds"] == [IMG2]


def test_delete_all_unknown_ids_writes_nothing(srv):
    patches, captured, state = _mocks(BASE)
    for p in patches: p.start()
    try:
        r = srv.delete_inventory_item_images("TEST-SKU", [BOGUS], dry_run=False)
    finally:
        for p in patches: p.stop()
    assert captured["post"] == []
    assert "nothing to delete" in r["message"]


def test_delete_empty_list_raises(srv):
    with pytest.raises(ValueError):
        srv.delete_inventory_item_images("TEST-SKU", [])


def test_delete_bad_sku_returns_error(srv):
    patches, _, _ = _mocks(BASE)
    for p in patches: p.start()
    try:
        r = srv.delete_inventory_item_images("NOPE", [IMG1], dry_run=False)
    finally:
        for p in patches: p.stop()
    assert "error" in r


def test_delete_staging_threshold_is_ten(srv):
    import server
    assert server.WRITE_THRESHOLDS["delete_inventory_item_images"] == 10


# ── set_inventory_item_image_order ────────────────────────────────────────────

def test_full_set_main_first_is_honoured_exactly(srv):
    """The one shape the server honours verbatim: full set, main at position 0."""
    patches, captured, state = _mocks(BASE)
    for p in patches: p.start()
    try:
        r = srv.set_inventory_item_image_order(
            "TEST-SKU", image_ids=[IMG1, IMG3, IMG2], dry_run=False)
    finally:
        for p in patches: p.stop()
    assert [i["image_id"] for i in r["images"]] == [IMG1, IMG3, IMG2]
    assert r["order_matches_plan"] is True
    assert r["main_forced_first"] is False
    assert r["warning"] is None


def test_partial_list_orders_behind_main(srv):
    """A partial list re-sorts the named images behind the untouched main."""
    patches, captured, state = _mocks(BASE)
    for p in patches: p.start()
    try:
        r = srv.set_inventory_item_image_order(
            "TEST-SKU", image_ids=[IMG3, IMG2], dry_run=False)
    finally:
        for p in patches: p.stop()
    assert [i["image_id"] for i in r["images"]] == [IMG1, IMG3, IMG2]
    assert r["order_matches_plan"] is True
    # IMG3 was listed first but the main image leads — the caller is told so
    assert r["main_forced_first"] is True


def test_main_is_pinned_first_and_flagged(srv):
    """Asking a non-main image to lead must predict the server's override."""
    patches, captured, state = _mocks(BASE)
    for p in patches: p.start()
    try:
        r = srv.set_inventory_item_image_order("TEST-SKU", image_ids=[IMG3], dry_run=False)
    finally:
        for p in patches: p.stop()
    assert r["main_forced_first"] is True
    assert r["warning"] and "pins the MAIN image" in r["warning"]
    # plan predicted main first, and reality agreed
    assert r["manifest"][0]["image_id"] == IMG1
    assert [i["image_id"] for i in r["images"]] == [IMG1, IMG3, IMG2]
    assert r["order_matches_plan"] is True


def test_set_main_promotes_to_front(srv):
    patches, captured, state = _mocks(BASE)
    for p in patches: p.start()
    try:
        r = srv.set_inventory_item_image_order("TEST-SKU", main_image_id=IMG3, dry_run=False)
    finally:
        for p in patches: p.stop()
    assert r["images"][0]["image_id"] == IMG3
    assert r["images"][0]["is_main"] is True
    assert sum(1 for i in r["images"] if i["is_main"]) == 1
    assert r["main_image_set"] == IMG3
    # main set BEFORE the sort write, so the two agree rather than fight
    paths = [p for p, _ in captured["void"]]
    assert paths[0].endswith("SetInventoryItemImageAsMain")


def test_update_images_carries_full_row(srv):
    """UpdateImages clears omitted fields — every field must be sent."""
    patches, captured, state = _mocks(BASE)
    for p in patches: p.start()
    try:
        srv.set_inventory_item_image_order("TEST-SKU", image_ids=[IMG3, IMG2], dry_run=False)
    finally:
        for p in patches: p.stop()
    payload = next(pl for pth, pl in captured["void"] if pth.endswith("UpdateImages"))
    row = payload["images"][0]
    for field in ("pkRowId", "IsMain", "SortOrder", "ChecksumValue",
                  "RawChecksum", "StockItemId", "StockItemIntId"):
        assert field in row, f"{field} missing — UpdateImages would clear it"
    assert row["ChecksumValue"] == f"sum-{IMG1[:4]}"
    # full set submitted (partial payloads get re-normalised by the server)
    assert len(payload["images"]) == 3


def test_reorder_dry_run_writes_nothing(srv):
    patches, captured, state = _mocks(BASE)
    for p in patches: p.start()
    try:
        r = srv.set_inventory_item_image_order("TEST-SKU", image_ids=[IMG3, IMG2])
    finally:
        for p in patches: p.stop()
    assert r["dry_run"] is True
    assert captured["void"] == []
    assert [m["image_id"] for m in r["manifest"]] == [IMG1, IMG3, IMG2]


def test_reorder_noop_when_already_in_order(srv):
    patches, captured, state = _mocks(BASE)
    for p in patches: p.start()
    try:
        r = srv.set_inventory_item_image_order(
            "TEST-SKU", image_ids=[IMG1, IMG2, IMG3], dry_run=False)
    finally:
        for p in patches: p.stop()
    assert captured["void"] == []
    assert "already in the requested order" in r["message"]


def test_reorder_unknown_main_refuses_to_write(srv):
    patches, captured, state = _mocks(BASE)
    for p in patches: p.start()
    try:
        r = srv.set_inventory_item_image_order("TEST-SKU", main_image_id=BOGUS, dry_run=False)
    finally:
        for p in patches: p.stop()
    assert "error" in r
    assert captured["void"] == []


def test_reorder_unknown_order_id_is_skipped(srv):
    patches, captured, state = _mocks(BASE)
    for p in patches: p.start()
    try:
        r = srv.set_inventory_item_image_order(
            "TEST-SKU", image_ids=[IMG3, BOGUS], dry_run=False)
    finally:
        for p in patches: p.stop()
    assert len(r["unresolved"]) == 1
    assert [i["image_id"] for i in r["images"]] == [IMG1, IMG3, IMG2]


def test_reorder_requires_an_argument(srv):
    with pytest.raises(ValueError):
        srv.set_inventory_item_image_order("TEST-SKU")


def test_reorder_item_with_no_images(srv):
    patches, captured, state = _mocks([])
    for p in patches: p.start()
    try:
        r = srv.set_inventory_item_image_order("TEST-SKU", image_ids=[IMG1], dry_run=False)
    finally:
        for p in patches: p.stop()
    assert r["images"] == []
    assert "no images" in r["message"]
    assert captured["void"] == []
