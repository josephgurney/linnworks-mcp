"""
pytest configuration for the linnworks-mcp test suite.

server.py calls sys.exit(1) at module level when Linnworks credentials are
missing. This conftest sets dummy env vars before any test module imports
server, so the credential guard passes and the module loads cleanly.
Real API calls are always mocked in tests — these dummy values are never sent
to Linnworks.
"""
import os

os.environ.setdefault("LINNWORKS_APPLICATION_ID", "test-app-id")
os.environ.setdefault("LINNWORKS_APPLICATION_SECRET", "test-app-secret")
os.environ.setdefault("LINNWORKS_INSTALLATION_TOKEN", "test-install-token")


import pytest


@pytest.fixture(autouse=True)
def _shopify_scopes_fully_granted(request, monkeypatch):
    """Default every test to a FULLY-SCOPED Shopify token (#113).

    Two things this fixes at once.

    1. Before it, no existing Shopify test stubbed the scope probe, so the
       suite made ~31 real outbound requests to */admin/oauth/access_scopes.json
       on domains like swh.myshopify.com, carrying fake tokens. Those tests
       passed only because the probe FAILED and returned None ("couldn't
       check", which does not block) -- so the green suite was proving the
       probe-failed path, not the fully-scoped one it appeared to prove.
    2. It makes the full grant the default, so every pre-existing Shopify test
       now genuinely exercises the path a correctly-configured token takes.

    Tests that are ABOUT the probe opt out with @pytest.mark.real_scope_probe.

    `server` is imported HERE, not at module level: conftest.py loads before
    pytest puts the repo root on sys.path, so a top-level `import server`
    survives `python -m pytest` (which adds the cwd) and dies under a bare
    `pytest`, which is what CI runs.
    """
    import server as _server

    _server._SHOPIFY_SCOPE_CACHE.clear()
    if "real_scope_probe" not in request.keywords:
        monkeypatch.setattr(
            _server, "_shopify_granted_scopes",
            lambda store: frozenset(_server.SHOPIFY_SCOPES_IMAGE_REPAIR),
        )
    yield
    _server._SHOPIFY_SCOPE_CACHE.clear()
