"""
repair_channel_listing_images (issue #41) — the Shopify-side image repair.

This is the first tool in the server that writes to a system OTHER than
Linnworks, and it writes to live customer-facing product pages, so the tests
concentrate on the ways it could quietly do the wrong thing:

  - detaching a picture that Linnworks never put there (hand-uploaded),
  - detaching a SIBLING VARIANT's picture, because a Shopify variation product
    is shared by every variant while each keeps its own Linnworks images,
  - detaching the old image when the replacement never uploaded, which is how a
    product ends up with NO image at all (an observed failure mode in #41),
  - reporting a repair that the storefront never received.

All Shopify traffic is faked; nothing here touches a real store.
"""
import json

import pytest

import server


# --------------------------------------------------------------------------
# Fixtures / fakes
# --------------------------------------------------------------------------

PGID = "gid://shopify/Product/9495050125558"
REF = "9495050125558:51459308912886:52770497429750"

IMG_A = "88b7b1da-a06b-4439-b93e-9f4a7ec310f2"   # main
IMG_B = "be95aa80-bc69-4fac-8f24-43a2abb9197e"
IMG_SIB = "0f6ae7c6-7766-4a74-8cae-8d6d1e5bd645"  # a sibling variant's image
OLD = "11112222-3333-4444-5555-666677778888"      # deleted from Linnworks


def _lw_img(guid, is_main=False, sort=0):
    return {
        "pkRowId": guid,
        "IsMain": is_main,
        "SortOrder": sort,
        "FullSource": f"https://s3-eu-west-1.amazonaws.com/images.linnlive.com/x/{guid}.jpg",
        "Source": f"https://s3-eu-west-1.amazonaws.com/images.linnlive.com/x/tumbnail_{guid}.jpg",
    }


def _sh_media(mid, stem, status="READY", ext="jpg"):
    return {
        "id": f"gid://shopify/MediaImage/{mid}",
        "mediaContentType": "IMAGE",
        "status": status,
        "mediaErrors": [],
        "image": {"url": f"https://cdn.shopify.com/s/files/1/0/files/{stem}.{ext}?v=17",
                  "altText": "x"},
    }


class FakeShopify:
    """Minimal stateful stand-in for the Admin API's four documents."""

    def __init__(self, media, featured=None, add_status="READY", product=True):
        self.media = list(media)
        self.featured = featured or (media[0]["id"] if media else None)
        self.add_status = add_status
        self.product = product
        self.calls = []
        self._next = 900

    def __call__(self, store, query, variables):
        if "LwRepairReadProduct" in query:
            self.calls.append(("read", variables))
            if not self.product:
                return {"product": None}
            return {"product": {
                "id": PGID, "title": "Test Product",
                "featuredMedia": {"id": self.featured} if self.featured else None,
                "media": {"nodes": list(self.media)},
            }}

        if "LwRepairAddMedia" in query:
            self.calls.append(("add", variables))
            for m in variables["media"]:
                stem = m["originalSource"].rsplit("/", 1)[-1].rsplit(".", 1)[0]
                self._next += 1
                self.media.append(_sh_media(self._next, stem, status=self.add_status))
            return {"productUpdate": {"product": {"id": PGID}, "userErrors": []}}

        if "LwRepairSetFeatured" in query:
            self.calls.append(("featured", variables))
            self.featured = variables["moves"][0]["id"]
            return {"productReorderMedia": {"job": {"id": "j", "done": True},
                                            "mediaUserErrors": []}}

        if "LwRepairDetachMedia" in query:
            self.calls.append(("detach", variables))
            gone = {f["id"] for f in variables["files"]}
            self.media = [m for m in self.media if m["id"] not in gone]
            return {"fileUpdate": {"files": [{"id": i, "fileStatus": "READY"} for i in gone],
                                   "userErrors": []}}

        raise AssertionError(f"unexpected query: {query[:60]}")

    def stages(self):
        return [c[0] for c in self.calls]


@pytest.fixture
def shopify_env(monkeypatch):
    monkeypatch.setenv("SHOPIFY_SHOP_DOMAIN", "test.myshopify.com")
    monkeypatch.setenv("SHOPIFY_ADMIN_ACCESS_TOKEN", "shpat_test")
    monkeypatch.setenv("SHOPIFY_DEFAULT_SUB_SOURCE", "SWH Shopify")
    monkeypatch.delenv("SHOPIFY_STORES", raising=False)


@pytest.fixture
def lw(monkeypatch):
    """Linnworks side: one standalone SKU, listed on SWH Shopify."""
    state = {
        "images": {"sku-1": [_lw_img(IMG_A, is_main=True, sort=0)]},
        "variation": {"role": None, "children": [], "siblings": []},
        "channel_rows": [{"Source": "SHOPIFY", "SubSource": "SWH Shopify",
                          "ChannelReferenceId": REF}],
    }
    monkeypatch.setattr(server, "_resolve_sku_to_id", lambda s, c=None: f"sid-{s}")
    monkeypatch.setattr(server, "_fetch_channel_skus_for_ids",
                        lambda ids: {i.lower(): state["channel_rows"] for i in ids})
    monkeypatch.setattr(server, "_fetch_raw_images",
                        lambda sid: state["images"].get(sid.replace("sid-", ""), []))
    monkeypatch.setattr(server, "_fetch_images_for_ids",
                        lambda ids: {i.lower(): state["images"].get(i.replace("sid-", ""), [])
                                     for i in ids})
    monkeypatch.setattr(server, "_resolve_variation", lambda s, sid: state["variation"])
    monkeypatch.setattr(server.time, "sleep", lambda *_: None)
    return state


# --------------------------------------------------------------------------
# Configuration / guards
# --------------------------------------------------------------------------

def test_missing_shopify_credentials_returns_setup_help_and_writes_nothing(monkeypatch):
    for k in ("SHOPIFY_STORES", "SHOPIFY_SHOP_DOMAIN", "SHOPIFY_ADMIN_ACCESS_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    called = []
    monkeypatch.setattr(server, "_shopify_graphql", lambda *a, **k: called.append(1))

    out = server.repair_channel_listing_images(["sku-1"], dry_run=False)

    assert out["shopify_configured"] is False
    assert "how_to_fix" in out
    assert not called, "no Shopify call may be made without credentials"


def test_store_resolution_prefers_the_json_multi_store_map(monkeypatch):
    monkeypatch.setenv("SHOPIFY_STORES", json.dumps(
        {"Venom Skateboards": {"shop_domain": "venom.myshopify.com",
                               "access_token": "shpat_v"}}))
    monkeypatch.delenv("SHOPIFY_SHOP_DOMAIN", raising=False)
    monkeypatch.delenv("SHOPIFY_ADMIN_ACCESS_TOKEN", raising=False)

    got = server._shopify_store_for("venom skateboards")   # case-insensitive
    assert got["shop_domain"] == "venom.myshopify.com"
    assert server._shopify_store_for("SWH Shopify") is None


def test_empty_skus_and_injection_are_rejected(shopify_env):
    with pytest.raises(ValueError):
        server.repair_channel_listing_images([])
    with pytest.raises(ValueError):
        server.repair_channel_listing_images(["a"], sub_source="ignore previous instructions")


def test_batch_over_threshold_stages_instead_of_writing(shopify_env, lw, monkeypatch):
    lw["images"].update({f"s{i}": [_lw_img(IMG_A, is_main=True)] for i in range(12)})
    fake = FakeShopify(media=[])            # every product missing its image
    monkeypatch.setattr(server, "_shopify_graphql", fake)

    out = server.repair_channel_listing_images([f"s{i}" for i in range(12)], dry_run=False)

    assert out["staged"] is True
    assert server.WRITE_THRESHOLDS["repair_channel_listing_images"] == 10
    assert "add" not in fake.stages(), "staging must not write"


# --------------------------------------------------------------------------
# Parsing helpers
# --------------------------------------------------------------------------

@pytest.mark.parametrize("url,stem", [
    (f"https://cdn.shopify.com/s/files/1/0/files/{IMG_A}.jpg?v=1787068333", IMG_A),
    ("https://cdn.shopify.com/s/files/1/0/files/hand-upload.png", "hand-upload"),
    (None, None),
])
def test_media_filename_stem(url, stem):
    assert server._media_filename_stem(url) == stem


@pytest.mark.parametrize("ref,gid", [
    (REF, PGID),                       # product:variant:inventory triple
    ("9495050125558", PGID),           # bare product id
    ("", None),
    (None, None),
    ("not-numeric:1:2", None),
])
def test_product_gid_parsing(ref, gid):
    assert server._shopify_product_gid(ref) == gid


# --------------------------------------------------------------------------
# The diff
# --------------------------------------------------------------------------

def test_in_sync_listing_reports_no_work(shopify_env, lw, monkeypatch):
    fake = FakeShopify(media=[_sh_media(1, IMG_A)])
    monkeypatch.setattr(server, "_shopify_graphql", fake)

    out = server.repair_channel_listing_images(["sku-1"])

    assert out["repairable_count"] == 0
    assert out["in_sync_count"] == 1
    assert out["plan"][0]["needs_repair"] is False
    assert "add" not in fake.stages()


def test_missing_image_and_wrong_featured_are_both_detected(shopify_env, lw, monkeypatch):
    lw["images"]["sku-1"] = [_lw_img(IMG_A, is_main=True, sort=0),
                             _lw_img(IMG_B, sort=1)]
    # Shopify has only B, and B is featured — the Linnworks main is absent.
    fake = FakeShopify(media=[_sh_media(2, IMG_B)])
    monkeypatch.setattr(server, "_shopify_graphql", fake)

    p = server.repair_channel_listing_images(["sku-1"])["plan"][0]

    assert [m["image_id"] for m in p["missing"]] == [IMG_A]
    assert p["featured_is_correct"] is False
    assert p["main_image_id"] == IMG_A
    assert any("attach 1 image" in a for a in p["actions"])


def test_superseded_media_is_identified_by_guid_absent_from_linnworks(shopify_env, lw, monkeypatch):
    fake = FakeShopify(media=[_sh_media(1, IMG_A), _sh_media(9, OLD)])
    monkeypatch.setattr(server, "_shopify_graphql", fake)

    p = server.repair_channel_listing_images(["sku-1"])["plan"][0]

    assert [m["linnworks_image_id"] for m in p["superseded"]] == [OLD]
    assert [m["linnworks_image_id"] for m in p["matched"]] == [IMG_A]


def test_hand_uploaded_media_is_unmanaged_and_never_detached(shopify_env, lw, monkeypatch):
    fake = FakeShopify(media=[_sh_media(1, IMG_A), _sh_media(5, "lifestyle-shot", ext="png")])
    monkeypatch.setattr(server, "_shopify_graphql", fake)

    out = server.repair_channel_listing_images(["sku-1"], dry_run=False)
    p = out["plan"][0]

    assert [m["stem"] for m in p["unmanaged"]] == ["lifestyle-shot"]
    assert p["to_detach"] == []
    assert "detach" not in fake.stages()


def test_remove_superseded_false_leaves_old_media_alone(shopify_env, lw, monkeypatch):
    fake = FakeShopify(media=[_sh_media(1, IMG_A), _sh_media(9, OLD)])
    monkeypatch.setattr(server, "_shopify_graphql", fake)

    p = server.repair_channel_listing_images(
        ["sku-1"], remove_superseded=False, dry_run=False)["plan"][0]

    assert p["superseded"] and p["to_detach"] == []


# --------------------------------------------------------------------------
# The variation hazard — a shared Shopify product
# --------------------------------------------------------------------------

def _make_variation(lw):
    """sku-1 is a child of a group whose sibling owns IMG_SIB."""
    lw["variation"] = {
        "role": "child", "group_name": "G", "parent_sku": "parent",
        "parent_stock_item_id": "sid-parent",
        "children": [], "siblings": [{"sku": "sku-2", "stock_item_id": "sid-sku-2"}],
    }
    lw["images"]["sku-2"] = [_lw_img(IMG_SIB, is_main=True)]
    lw["images"]["parent"] = []


def test_sibling_variant_image_is_matched_not_superseded(shopify_env, lw, monkeypatch):
    _make_variation(lw)
    fake = FakeShopify(media=[_sh_media(1, IMG_A), _sh_media(3, IMG_SIB)])
    monkeypatch.setattr(server, "_shopify_graphql", fake)

    p = server.repair_channel_listing_images(["sku-1"])["plan"][0]

    sib = [m for m in p["matched"] if m["linnworks_image_id"] == IMG_SIB]
    assert sib and sib[0]["belongs_to_sibling"] is True
    assert p["superseded"] == [], "a sibling's live photo must never look superseded"


def test_shared_product_blocks_removal_unless_every_member_is_passed(shopify_env, lw, monkeypatch):
    _make_variation(lw)
    fake = FakeShopify(media=[_sh_media(1, IMG_A), _sh_media(9, OLD)])
    monkeypatch.setattr(server, "_shopify_graphql", fake)

    p = server.repair_channel_listing_images(["sku-1"], dry_run=False)["plan"][0]

    assert p["variation"]["shared_product"] is True
    assert p["superseded"], "the stale media is still reported"
    assert p["to_detach"] == [], "but it is not removed"
    assert "sku-2" in p["removal_blocked_reason"] or "variant" in p["removal_blocked_reason"]
    assert "detach" not in fake.stages()


def test_shared_product_allows_removal_when_all_members_requested(shopify_env, lw, monkeypatch):
    _make_variation(lw)
    fake = FakeShopify(media=[_sh_media(1, IMG_A), _sh_media(3, IMG_SIB), _sh_media(9, OLD)])
    monkeypatch.setattr(server, "_shopify_graphql", fake)

    # allow_net_media_loss isolates this to the VARIATION gate — the scenario
    # removes 1 and adds 0, which the separate net-loss guard would otherwise block.
    out = server.repair_channel_listing_images(
        ["sku-1", "sku-2", "parent"], allow_net_media_loss=True, dry_run=True)

    p = next(r for r in out["plan"] if r["sku"] == "sku-1")
    assert [m["linnworks_image_id"] for m in p["to_detach"]] == [OLD]
    assert p["removal_blocked_reason"] is None


def test_variation_lookup_failure_disables_removal_rather_than_assuming_standalone(
        shopify_env, lw, monkeypatch):
    def boom(sku, sid):
        raise RuntimeError("variation endpoint down")
    monkeypatch.setattr(server, "_resolve_variation", boom)
    fake = FakeShopify(media=[_sh_media(1, IMG_A), _sh_media(9, OLD)])
    monkeypatch.setattr(server, "_shopify_graphql", fake)

    p = server.repair_channel_listing_images(["sku-1"], dry_run=False)["plan"][0]

    assert p["to_detach"] == []
    assert "variation lookup failed" in p["removal_blocked_reason"].lower()


# --------------------------------------------------------------------------
# Blocked SKUs
# --------------------------------------------------------------------------

def test_sku_not_listed_on_the_store_is_blocked_with_a_reason(shopify_env, lw, monkeypatch):
    lw["channel_rows"] = [{"Source": "SHOPIFY", "SubSource": "Venom Skateboards",
                           "ChannelReferenceId": REF}]
    monkeypatch.setattr(server, "_shopify_graphql", FakeShopify(media=[]))

    out = server.repair_channel_listing_images(["sku-1"])

    assert out["unresolved"][0]["blocked_reason"] == "not_listed"
    assert out["plan"] == []


def test_item_without_linnworks_images_is_blocked(shopify_env, lw, monkeypatch):
    lw["images"]["sku-1"] = []
    monkeypatch.setattr(server, "_shopify_graphql", FakeShopify(media=[_sh_media(1, IMG_A)]))

    out = server.repair_channel_listing_images(["sku-1"])

    assert out["unresolved"][0]["blocked_reason"] == "no_linnworks_images"


def test_missing_shopify_product_is_blocked_not_recreated(shopify_env, lw, monkeypatch):
    fake = FakeShopify(media=[], product=False)
    monkeypatch.setattr(server, "_shopify_graphql", fake)

    out = server.repair_channel_listing_images(["sku-1"], dry_run=False)

    assert out["unresolved"][0]["blocked_reason"] == "shopify_product_missing"
    assert "add" not in fake.stages()


def test_rate_limit_is_its_own_bucket_and_marks_the_run_incomplete(shopify_env, lw, monkeypatch):
    def limited(sku, cache=None):
        raise server.RateLimitError("quota exceeded")
    monkeypatch.setattr(server, "_resolve_sku_to_id", limited)
    monkeypatch.setattr(server, "_shopify_graphql", FakeShopify(media=[]))

    out = server.repair_channel_listing_images(["sku-1"])

    assert out["rate_limited"] and out["complete"] is False
    assert out["unresolved"] == [], "a 429 is not 'SKU not found'"


# --------------------------------------------------------------------------
# dry_run / live execution
# --------------------------------------------------------------------------

def test_dry_run_is_the_default_and_writes_nothing(shopify_env, lw, monkeypatch):
    fake = FakeShopify(media=[_sh_media(9, OLD)])
    monkeypatch.setattr(server, "_shopify_graphql", fake)

    out = server.repair_channel_listing_images(["sku-1"])

    assert out["dry_run"] is True
    assert fake.stages().count("read") >= 1
    assert {"add", "detach", "featured"}.isdisjoint(fake.stages())


def test_live_run_attaches_sets_featured_detaches_and_reads_back(shopify_env, lw, monkeypatch):
    fake = FakeShopify(media=[_sh_media(9, OLD)], featured="gid://shopify/MediaImage/9")
    monkeypatch.setattr(server, "_shopify_graphql", fake)

    out = server.repair_channel_listing_images(["sku-1"], dry_run=False)
    r = out["results"][0]

    assert fake.stages().count("add") == 1
    assert "featured" in fake.stages() and "detach" in fake.stages()
    # ordering matters: nothing is detached before the replacement is attached
    assert fake.stages().index("add") < fake.stages().index("detach")
    assert [a["linnworks_image_id"] for a in r["added"]] == [IMG_A]
    assert r["detached"] == ["gid://shopify/MediaImage/9"]
    assert r["still_missing"] == []
    assert r["in_sync"] is True
    assert out["repaired_count"] == 1


def test_failed_upload_leaves_the_old_image_in_place(shopify_env, lw, monkeypatch):
    """The #41 failure mode: media lands FAILED and the page ends up with nothing."""
    fake = FakeShopify(media=[_sh_media(9, OLD)], add_status="FAILED")
    monkeypatch.setattr(server, "_shopify_graphql", fake)

    out = server.repair_channel_listing_images(["sku-1"], dry_run=False)
    r = out["results"][0]

    assert r["add_failed"] and not r["added"]
    assert "detach" not in fake.stages(), "must not empty the listing"
    assert any(e.get("stage") == "detach" and e.get("skipped") for e in r["errors"])
    assert r["in_sync"] is False
    assert out["failed_skus"] == ["sku-1"]


def test_readback_reports_not_in_sync_when_the_image_never_lands(shopify_env, lw, monkeypatch):
    class Silent(FakeShopify):
        def __call__(self, store, query, variables):
            if "LwRepairAddMedia" in query:      # accepted, but nothing appears
                self.calls.append(("add", variables))
                return {"productUpdate": {"product": {"id": PGID}, "userErrors": []}}
            return super().__call__(store, query, variables)

    fake = Silent(media=[])
    monkeypatch.setattr(server, "_shopify_graphql", fake)

    r = server.repair_channel_listing_images(["sku-1"], dry_run=False)["results"][0]

    assert r["still_missing"] == [IMG_A]
    assert r["in_sync"] is False, "a 2xx must never be reported as a repair"


def test_set_featured_false_skips_the_reorder(shopify_env, lw, monkeypatch):
    fake = FakeShopify(media=[_sh_media(9, OLD)], featured="gid://shopify/MediaImage/9")
    monkeypatch.setattr(server, "_shopify_graphql", fake)

    server.repair_channel_listing_images(["sku-1"], set_featured=False, dry_run=False)

    assert "featured" not in fake.stages()


# --------------------------------------------------------------------------
# Transport
# --------------------------------------------------------------------------

def test_graphql_retries_on_throttle_then_succeeds(monkeypatch):
    calls = {"n": 0}

    class R:
        def __init__(self, code, payload):
            self.status_code, self._p = code, payload
            self.text = json.dumps(payload)

        def json(self):
            return self._p

    def post(url, headers=None, json=None, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return R(200, {"errors": [{"message": "Throttled",
                                       "extensions": {"code": "THROTTLED"}}]})
        return R(200, {"data": {"ok": True}})

    monkeypatch.setattr(server.requests, "post", post)
    monkeypatch.setattr(server.time, "sleep", lambda *_: None)

    out = server._shopify_graphql({"shop_domain": "x", "access_token": "t"}, "q", {})
    assert out == {"ok": True} and calls["n"] == 2


def test_graphql_surfaces_errors_verbatim(monkeypatch):
    class R:
        status_code = 401
        text = '{"errors":"Invalid API key or access token"}'

    monkeypatch.setattr(server.requests, "post", lambda *a, **k: R())
    with pytest.raises(RuntimeError, match="Invalid API key"):
        server._shopify_graphql({"shop_domain": "x", "access_token": "t"}, "q", {})


# --------------------------------------------------------------------------
# Net-loss guard — the luma-huxham-blue case
# --------------------------------------------------------------------------
#
# Found on the first genuinely stale product in the catalogue. "Superseded" only
# means Linnworks pushed it once and no longer has it; it cannot tell a REPLACED
# image from one that was deleted out of Linnworks while still earning its place
# on the product page. On luma-huxham-blue the 6 superseded media were lifestyle
# shots (models wearing the product) and the 4 incoming Linnworks images were
# studio packshots — a content downgrade produced by a tool working exactly to
# spec. Removing more than you add is the tell.

OLD2 = "22223333-4444-5555-6666-777788889999"
OLD3 = "33334444-5555-6666-7777-888899990000"


def test_net_loss_blocks_removal_by_default(shopify_env, lw, monkeypatch):
    # Linnworks has 1 image; Shopify has 3 Linnworks-origin media, 2 of them stale.
    lw["images"]["sku-1"] = [_lw_img(IMG_A, is_main=True)]
    fake = FakeShopify(media=[_sh_media(1, IMG_A), _sh_media(8, OLD2), _sh_media(9, OLD3)])
    monkeypatch.setattr(server, "_shopify_graphql", fake)

    p = server.repair_channel_listing_images(["sku-1"], dry_run=False)["plan"][0]

    assert len(p["superseded"]) == 2, "both stale media are still reported"
    assert p["to_detach"] == [], "but nothing is removed"
    assert "net loss" in p["removal_blocked_reason"]
    assert "detach" not in fake.stages()


def test_net_loss_override_permits_removal(shopify_env, lw, monkeypatch):
    lw["images"]["sku-1"] = [_lw_img(IMG_A, is_main=True)]
    fake = FakeShopify(media=[_sh_media(1, IMG_A), _sh_media(8, OLD2), _sh_media(9, OLD3)])
    monkeypatch.setattr(server, "_shopify_graphql", fake)

    p = server.repair_channel_listing_images(
        ["sku-1"], allow_net_media_loss=True, dry_run=True)["plan"][0]

    assert sorted(m["linnworks_image_id"] for m in p["to_detach"]) == sorted([OLD2, OLD3])
    assert p["removal_blocked_reason"] is None


def test_even_swap_is_not_a_net_loss(shopify_env, lw, monkeypatch):
    """One image replaced by one image — the ordinary repair — stays allowed."""
    lw["images"]["sku-1"] = [_lw_img(IMG_A, is_main=True)]
    fake = FakeShopify(media=[_sh_media(9, OLD)])          # 1 stale, 1 incoming
    monkeypatch.setattr(server, "_shopify_graphql", fake)

    p = server.repair_channel_listing_images(["sku-1"], dry_run=True)["plan"][0]

    assert [m["linnworks_image_id"] for m in p["to_detach"]] == [OLD]
    assert p["removal_blocked_reason"] is None


def test_net_gain_still_allows_removal(shopify_env, lw, monkeypatch):
    """Adding 2 while removing 1 is a clear improvement — not blocked."""
    lw["images"]["sku-1"] = [_lw_img(IMG_A, is_main=True, sort=0), _lw_img(IMG_B, sort=1)]
    fake = FakeShopify(media=[_sh_media(9, OLD)])
    monkeypatch.setattr(server, "_shopify_graphql", fake)

    p = server.repair_channel_listing_images(["sku-1"], dry_run=True)["plan"][0]

    assert [m["linnworks_image_id"] for m in p["to_detach"]] == [OLD]
    assert len(p["missing"]) == 2
    assert p["removal_blocked_reason"] is None
