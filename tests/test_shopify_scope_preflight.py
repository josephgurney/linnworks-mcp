"""#113 — an installed but under-scoped Shopify token must be caught at PLAN time.

The bug this guards: `_shopify_store_for` only knows whether a domain and token
are present, never which scopes they carry. A read-only token therefore passed
the only configuration guard, produced a healthy-looking dry-run manifest, and
failed at the first mutation on the LIVE run — after the operator had already
satisfied `_write_guard` with a confirmed_count.
"""
import pytest

import server


@pytest.fixture
def shopify_env(monkeypatch):
    monkeypatch.setenv("SHOPIFY_SHOP_DOMAIN", "test.myshopify.com")
    monkeypatch.setenv("SHOPIFY_ADMIN_ACCESS_TOKEN", "shpat_test")
    monkeypatch.setenv("SHOPIFY_DEFAULT_SUB_SOURCE", "SWH Shopify")
    monkeypatch.delenv("SHOPIFY_STORES", raising=False)


def _grant(monkeypatch, *scopes):
    monkeypatch.setattr(server, "_shopify_granted_scopes",
                        lambda store: frozenset(scopes))


class TestReadOnlyTokenIsRefusedAtPlanTime:

    def test_dry_run_refuses_rather_than_reporting_a_healthy_plan(self, shopify_env, monkeypatch):
        # The heart of #113: a dry run is exactly where this used to look fine.
        _grant(monkeypatch, "read_products")
        called = []
        monkeypatch.setattr(server, "_shopify_graphql", lambda *a, **k: called.append(1))

        out = server.repair_channel_listing_images(["sku-1"], dry_run=True)

        assert out["scopes_ok"] is False
        assert sorted(out["scopes_missing"]) == ["read_files", "write_files", "write_products"]
        assert not called, "no Shopify call may be made once scopes are known to be short"

    def test_live_run_refuses_before_any_mutation(self, shopify_env, monkeypatch):
        _grant(monkeypatch, "read_products")
        called = []
        monkeypatch.setattr(server, "_shopify_graphql", lambda *a, **k: called.append(1))

        out = server.repair_channel_listing_images(["sku-1"], dry_run=False, confirmed_count=1)

        assert out["scopes_ok"] is False
        assert not called

    def test_it_is_not_mistaken_for_missing_credentials(self, shopify_env, monkeypatch):
        # Different problem, different remedy. Sending someone to re-check a
        # token that is already correct is the failure mode being avoided.
        _grant(monkeypatch, "read_products")
        out = server.repair_channel_listing_images(["sku-1"], dry_run=True)

        assert out["shopify_configured"] is True
        assert "not granted" in out["error"]
        assert "write_products" in out["error"]
        joined = " ".join(out["how_to_fix"]).lower()
        assert "new access token" in joined or "new one when scopes change" in joined


@pytest.mark.real_scope_probe
class TestProbeFailureMustNotBlock:

    def test_unreadable_grant_does_not_refuse(self, shopify_env, monkeypatch):
        # A probe that fails is not evidence of a missing scope. Turning a
        # diagnostic into a hard failure would be worse than the original bug.
        monkeypatch.setattr(server, "_shopify_granted_scopes", lambda store: None)
        assert server._shopify_scope_block(
            {"shop_domain": "d", "access_token": "t"}, "SWH Shopify",
            server.SHOPIFY_SCOPES_IMAGE_REPAIR) is None

    def test_a_network_error_is_swallowed_into_none(self, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("DNS is down")
        monkeypatch.setattr(server.requests, "get", boom)
        assert server._shopify_granted_scopes(
            {"shop_domain": "x.myshopify.com", "access_token": "t"}) is None


class TestFullyScopedTokenIsUnaffected:

    def test_all_scopes_present_does_not_block(self, monkeypatch):
        _grant(monkeypatch, *server.SHOPIFY_SCOPES_IMAGE_REPAIR)
        assert server._shopify_scope_block(
            {"shop_domain": "d", "access_token": "t"}, "SWH Shopify",
            server.SHOPIFY_SCOPES_IMAGE_REPAIR) is None


@pytest.mark.real_scope_probe
class TestTheProbeIsCheap:

    def test_scopes_are_fetched_once_per_store_not_once_per_sku(self, monkeypatch):
        calls = []

        class Resp:
            status_code = 200
            @staticmethod
            def json():
                calls.append(1)
                return {"access_scopes": [{"handle": "read_products"}]}

        monkeypatch.setattr(server.requests, "get", lambda *a, **k: Resp())
        store = {"shop_domain": "one.myshopify.com", "access_token": "t"}
        for _ in range(25):
            server._shopify_granted_scopes(store)
        assert len(calls) == 1, "the grant cannot change mid-run; one probe is enough"

    def test_the_probe_sends_the_token_and_needs_no_scope_of_its_own(self, monkeypatch):
        seen = {}

        class Resp:
            status_code = 200
            @staticmethod
            def json():
                return {"access_scopes": [{"handle": "read_products"}]}

        def fake_get(url, headers=None, timeout=None):
            seen["url"] = url
            seen["headers"] = headers
            return Resp()

        monkeypatch.setattr(server.requests, "get", fake_get)
        got = server._shopify_granted_scopes(
            {"shop_domain": "one.myshopify.com", "access_token": "shpat_x"})

        assert got == frozenset({"read_products"})
        assert seen["url"].endswith("/admin/oauth/access_scopes.json")
        assert seen["headers"]["X-Shopify-Access-Token"] == "shpat_x"


class TestListingExistenceProbe:

    def test_missing_read_products_is_its_own_reason_not_not_configured(
            self, shopify_env, monkeypatch):
        _grant(monkeypatch, "write_products")     # deliberately not read_products
        called = []
        monkeypatch.setattr(server, "_shopify_graphql", lambda *a, **k: called.append(1))

        out = server._glt_listing_existence(
            [{"active_listing_id": "9277472506102"}], "SWH Shopify",
            server.GLT_SHOPIFY_CHANNEL_NAME)

        assert out["checked"] is False
        assert out["reason"].startswith("insufficient_scope")
        assert "read_products" in out["reason"]
        assert "not_configured" not in out["reason"]
        assert not called


@pytest.mark.real_scope_probe
class TestAnUnrecognisedBodyMustNotBlock:
    """QA round 1: a 2xx body we can't parse became frozenset(), which reads as
    'every scope missing' and blocked every Shopify tool -- breaking the rule
    the helper states for itself."""

    @pytest.mark.parametrize("body", [
        {"errors": "something"},           # 200 with an error payload
        {},                                # no access_scopes key at all
        {"access_scopes": []},             # present but empty
        {"access_scopes": "nonsense"},     # present but not a list
        {"access_scopes": [{"nope": 1}]},  # list of unusable entries
    ])
    def test_unparseable_grant_is_none_not_an_empty_set(self, monkeypatch, body):
        class Resp:
            status_code = 200
            @staticmethod
            def json():
                return body

        monkeypatch.setattr(server.requests, "get", lambda *a, **k: Resp())
        got = server._shopify_granted_scopes(
            {"shop_domain": "odd.myshopify.com", "access_token": "t"})
        assert got is None, "an unreadable grant must mean 'couldn't check', never 'nothing granted'"

    def test_and_therefore_does_not_block(self, monkeypatch):
        class Resp:
            status_code = 200
            @staticmethod
            def json():
                return {"errors": "something"}

        monkeypatch.setattr(server.requests, "get", lambda *a, **k: Resp())
        assert server._shopify_scope_block(
            {"shop_domain": "odd.myshopify.com", "access_token": "t"},
            "SWH Shopify", server.SHOPIFY_SCOPES_IMAGE_REPAIR) is None


class TestFullGrantReachesTheRealWork:
    """AC4 for real: a correctly-scoped token must get PAST the gate, not merely
    fail to be blocked by a helper called in isolation."""

    def test_repair_gets_past_the_scope_gate_to_planning(self, shopify_env, monkeypatch):
        # conftest grants the full scope set by default.
        monkeypatch.setattr(server, "_resolve_sku_to_id",
                            lambda s, c=None: (_ for _ in ()).throw(RuntimeError("reached planning")))
        out = server.repair_channel_listing_images(["sku-1"], dry_run=True)

        assert out.get("scopes_ok") is not False, "a full grant must not be refused"
        assert "scopes_missing" not in out
        # It got past the gate and into per-SKU resolution.
        assert out["shopify_configured"] is True

    def test_listing_existence_gets_past_the_scope_gate(self, shopify_env, monkeypatch):
        seen = []
        monkeypatch.setattr(server, "_shopify_listings_exist",
                            lambda store, gids: seen.append(gids) or {})
        out = server._glt_listing_existence(
            [{"active_listing_id": "9277472506102"}], "SWH Shopify",
            server.GLT_SHOPIFY_CHANNEL_NAME)

        assert "insufficient_scope" not in (out.get("reason") or "")
        assert seen, "a full grant must reach the actual existence read"
