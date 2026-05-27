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
