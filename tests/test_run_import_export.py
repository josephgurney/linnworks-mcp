"""
QA tests for run_import and run_export.

All tests use unittest.mock — no live Linnworks API calls.
Run with: pytest tests/test_run_import_export.py -v
"""

import sys
import os
import pytest
from unittest.mock import patch, call as mock_call

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ── Fixtures ──────────────────────────────────────────────────────────────────

IMPORT_ID = 298
EXPORT_ID = 42


def _make_import_register(
    friendly_name="Shiner - OD - Skateboard Goods",
    import_type="Inventory",
    enabled=True,
    executing=False,
    is_queued=False,
    feed_url="https://docs.google.com/spreadsheets/d/test/export?format=csv",
):
    return {
        "Register": {
            "Id": IMPORT_ID,
            "FriendlyName": friendly_name,
            "Type": import_type,
            "Enabled": enabled,
            "Executing": executing,
            "IsQueued": is_queued,
            "ImportStatus": None,  # unreliable on detail endpoint
            "ImportSkipped": False,
            "NextSchedule": "2026-06-05T06:00:00",
            "AllSchedulesDisabled": False,
        },
        "Schedules": [],
        "Specification": {
            "Feed": {
                "Url": feed_url,
                "FeedType": "CSV",
            }
        },
    }


def _make_export_register(
    friendly_name="Stock Export",
    export_type="StockLevel",
    enabled=True,
    executing=False,
    is_queued=False,
):
    return {
        "Register": {
            "Id": EXPORT_ID,
            "FriendlyName": friendly_name,
            "Type": export_type,
            "Enabled": enabled,
            "Executing": executing,
            "IsQueued": is_queued,
            "LastExportStatus": True,
            "NextSchedule": "2026-06-05T06:00:00",
            "AllSchedulesDisabled": False,
        },
        "Schedules": [],
        "Specification": {},
    }


# ── run_import — dry run ───────────────────────────────────────────────────────

class TestRunImportDryRun:

    def test_dry_run_does_not_call_runnow(self):
        """dry_run=True must never call RunNowImport."""
        import server

        void_calls = []

        def void_side(path, payload, **kwargs):
            void_calls.append(path)

        with patch("server.call_linnworks_get",
                   return_value=_make_import_register()):
            with patch("server.call_linnworks_void", side_effect=void_side):
                server.run_import(IMPORT_ID, dry_run=True)

        assert not any("RunNow" in p for p in void_calls)

    def test_dry_run_returns_config_fields(self):
        """Dry run must surface friendly_name, type, feed_url, enabled."""
        import server

        with patch("server.call_linnworks_get",
                   return_value=_make_import_register()):
            result = server.run_import(IMPORT_ID, dry_run=True)

        assert result["dry_run"] is True
        assert result["import_id"] == IMPORT_ID
        assert result["friendly_name"] == "Shiner - OD - Skateboard Goods"
        assert result["type"] == "Inventory"
        assert result["feed_url"] == "https://docs.google.com/spreadsheets/d/test/export?format=csv"
        assert result["enabled"] is True
        assert result["queued"] is False

    def test_dry_run_message_contains_runnow_hint(self):
        """Dry run message should mention dry_run=False."""
        import server

        with patch("server.call_linnworks_get",
                   return_value=_make_import_register()):
            result = server.run_import(IMPORT_ID, dry_run=True)

        assert "dry_run=False" in result["message"]


# ── run_import — guard: already executing / queued ────────────────────────────

class TestRunImportGuards:

    def test_refuses_if_already_executing(self):
        """Must not queue if import is already executing."""
        import server

        with patch("server.call_linnworks_get",
                   return_value=_make_import_register(executing=True)):
            with patch("server.call_linnworks_void") as mock_void:
                result = server.run_import(IMPORT_ID, dry_run=False)

        mock_void.assert_not_called()
        assert result["queued"] is False
        assert "already executing" in result["message"]

    def test_refuses_if_already_queued(self):
        """Must not queue if import is already queued."""
        import server

        with patch("server.call_linnworks_get",
                   return_value=_make_import_register(is_queued=True)):
            with patch("server.call_linnworks_void") as mock_void:
                result = server.run_import(IMPORT_ID, dry_run=False)

        mock_void.assert_not_called()
        assert result["queued"] is False
        assert "already queued" in result["message"]

    def test_executing_guard_fires_in_dry_run_too(self):
        """Even in dry_run mode, guard should fire for already-executing imports."""
        import server

        with patch("server.call_linnworks_get",
                   return_value=_make_import_register(executing=True)):
            result = server.run_import(IMPORT_ID, dry_run=True)

        assert result["queued"] is False
        assert "already executing" in result["message"]


# ── run_import — live run ─────────────────────────────────────────────────────

class TestRunImportLive:

    def test_live_run_calls_runnow_import(self):
        """Live run must call RunNowImport with the correct payload."""
        import server

        void_calls = []

        def void_side(path, payload, **kwargs):
            void_calls.append((path, payload))

        # First call: pre-run read. Second: post-trigger read-back (queued=True).
        readback = _make_import_register(is_queued=True)
        call_count = {"n": 0}

        def get_side(path, params=None, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return _make_import_register()
            return readback

        with patch("server.call_linnworks_get", side_effect=get_side):
            with patch("server.call_linnworks_void", side_effect=void_side):
                result = server.run_import(IMPORT_ID, dry_run=False)

        assert len(void_calls) == 1
        path, payload = void_calls[0]
        assert "RunNowImport" in path
        assert payload == {"importId": IMPORT_ID}

    def test_live_run_returns_queued_true_when_readback_shows_queued(self):
        """queued=True when post-trigger read-back shows IsQueued."""
        import server

        readback = _make_import_register(is_queued=True)
        call_count = {"n": 0}

        def get_side(path, params=None, **kwargs):
            call_count["n"] += 1
            return _make_import_register() if call_count["n"] == 1 else readback

        with patch("server.call_linnworks_get", side_effect=get_side):
            with patch("server.call_linnworks_void"):
                result = server.run_import(IMPORT_ID, dry_run=False)

        assert result["dry_run"] is False
        assert result["queued"] is True
        assert result["now_queued"] is True

    def test_live_run_queued_true_when_readback_shows_executing(self):
        """queued=True also when read-back shows Executing (picked up instantly)."""
        import server

        readback = _make_import_register(executing=True)
        call_count = {"n": 0}

        def get_side(path, params=None, **kwargs):
            call_count["n"] += 1
            return _make_import_register() if call_count["n"] == 1 else readback

        with patch("server.call_linnworks_get", side_effect=get_side):
            with patch("server.call_linnworks_void"):
                result = server.run_import(IMPORT_ID, dry_run=False)

        assert result["queued"] is True
        assert result["now_executing"] is True

    def test_live_run_reads_config_before_triggering(self):
        """GetImport must be called before RunNowImport."""
        import server

        call_order = []

        def get_side(path, params=None, **kwargs):
            call_order.append(f"GET:{path}")
            return _make_import_register()

        def void_side(path, payload, **kwargs):
            call_order.append(f"POST:{path}")

        with patch("server.call_linnworks_get", side_effect=get_side):
            with patch("server.call_linnworks_void", side_effect=void_side):
                server.run_import(IMPORT_ID, dry_run=False)

        get_idx  = next(i for i, c in enumerate(call_order) if "GetImport" in c)
        post_idx = next(i for i, c in enumerate(call_order) if "RunNow" in c)
        assert get_idx < post_idx, "GetImport must be called before RunNowImport"


# ── run_export — dry run ───────────────────────────────────────────────────────

class TestRunExportDryRun:

    def test_dry_run_does_not_call_runnow(self):
        import server

        with patch("server.call_linnworks_get",
                   return_value=_make_export_register()):
            with patch("server.call_linnworks_void") as mock_void:
                server.run_export(EXPORT_ID, dry_run=True)

        mock_void.assert_not_called()

    def test_dry_run_returns_config_fields(self):
        import server

        with patch("server.call_linnworks_get",
                   return_value=_make_export_register()):
            result = server.run_export(EXPORT_ID, dry_run=True)

        assert result["dry_run"] is True
        assert result["export_id"] == EXPORT_ID
        assert result["friendly_name"] == "Stock Export"
        assert result["type"] == "StockLevel"
        assert result["queued"] is False


# ── run_export — guards ────────────────────────────────────────────────────────

class TestRunExportGuards:

    def test_refuses_if_already_executing(self):
        import server

        with patch("server.call_linnworks_get",
                   return_value=_make_export_register(executing=True)):
            with patch("server.call_linnworks_void") as mock_void:
                result = server.run_export(EXPORT_ID, dry_run=False)

        mock_void.assert_not_called()
        assert result["queued"] is False
        assert "already executing" in result["message"]

    def test_refuses_if_already_queued(self):
        import server

        with patch("server.call_linnworks_get",
                   return_value=_make_export_register(is_queued=True)):
            with patch("server.call_linnworks_void") as mock_void:
                result = server.run_export(EXPORT_ID, dry_run=False)

        mock_void.assert_not_called()
        assert result["queued"] is False
        assert "already queued" in result["message"]


# ── run_export — live run ─────────────────────────────────────────────────────

class TestRunExportLive:

    def test_live_run_calls_runnow_export(self):
        import server

        void_calls = []

        def void_side(path, payload, **kwargs):
            void_calls.append((path, payload))

        readback = _make_export_register(is_queued=True)
        call_count = {"n": 0}

        def get_side(path, params=None, **kwargs):
            call_count["n"] += 1
            return _make_export_register() if call_count["n"] == 1 else readback

        with patch("server.call_linnworks_get", side_effect=get_side):
            with patch("server.call_linnworks_void", side_effect=void_side):
                result = server.run_export(EXPORT_ID, dry_run=False)

        assert len(void_calls) == 1
        path, payload = void_calls[0]
        assert "RunNowExport" in path
        assert payload == {"exportId": EXPORT_ID}
        assert result["queued"] is True


# ── version ───────────────────────────────────────────────────────────────────

def test_version_is_at_least_1_10_0():
    import server
    major, minor, patch = (int(x) for x in server.__version__.split("."))
    assert (major, minor, patch) >= (1, 10, 0), (
        f"Expected >= 1.10.0, got {server.__version__}"
    )
"""
QA tests for run_import / run_export (issue: proving the last two spec-based tools).

All tests use unittest.mock — no live Linnworks API calls.

run_export is LIVE-PROVEN (18 Sep 2026, export 36 "supplier test": Started
jumped from 2018-05-15 to 2026-09-18 within 15s of firing).

run_import is NOT proven and was deliberately not fired: every one of the 126
configured imports on this tenant writes to real catalogue data, and 9 are
outright deletion types (DeleteImages, DeletePrimaryImages,
DeleteSuppliersFromItems, RenameSKU, DeleteComposition). See CLAUDE.md.
"""
import sys
import os
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server


def _register(started="2018-05-15T11:09:08Z", queued=False, executing=False):
    return {
        "Register": {
            "FriendlyName": "supplier test",
            "Type": "InventorySupplier",
            "Enabled": True,
            "Executing": executing,
            "IsQueued": queued,
            "Started": started,
        }
    }


class TestRunExportVerification:
    """The immediate read-back after an async trigger proves nothing — live-proven
    on export 36, which reported now_queued=False and then ran 15s later."""

    def test_started_before_is_captured_from_the_pre_fire_read(self):
        with patch.object(server, "call_linnworks_get", return_value=_register()), \
             patch.object(server, "call_linnworks_void") as void:
            r = server.run_export(36, dry_run=False)
        assert r["started_before"] == "2018-05-15T11:09:08Z"
        void.assert_called_once()

    def test_not_queued_readback_is_not_reported_as_failure(self):
        """False here means 'not picked up in the last few milliseconds', not
        'it failed' — the distinction that matters on an async trigger."""
        with patch.object(server, "call_linnworks_get", return_value=_register(queued=False)), \
             patch.object(server, "call_linnworks_void"):
            r = server.run_export(36, dry_run=False)
        msg = r["message"]
        assert "EXPECTED" in msg and "not a failure" in msg
        assert "Acceptance is not execution" in msg

    def test_how_to_verify_names_the_started_timestamp_not_the_status_flag(self):
        with patch.object(server, "call_linnworks_get", return_value=_register()), \
             patch.object(server, "call_linnworks_void"):
            r = server.run_export(36, dry_run=False)
        assert "started" in r["how_to_verify"]
        assert "2018-05-15T11:09:08Z" in r["how_to_verify"]

    def test_how_to_verify_warns_last_export_status_is_a_constant(self):
        """last_export_status is False on ALL 70 exports on this tenant,
        including production ones that ran correctly today. Reading it as
        success/failure is the NextSuggestedAction trap again (v1.43.0)."""
        with patch.object(server, "call_linnworks_get", return_value=_register()), \
             patch.object(server, "call_linnworks_void"):
            r = server.run_export(36, dry_run=False)
        assert "last_export_status" in r["how_to_verify"]
        assert "constant" in r["how_to_verify"]

    def test_dry_run_fires_nothing(self):
        with patch.object(server, "call_linnworks_get", return_value=_register()), \
             patch.object(server, "call_linnworks_void") as void:
            r = server.run_export(36, dry_run=True)
        assert r["dry_run"] is True
        void.assert_not_called()

    def test_already_executing_export_is_not_double_queued(self):
        with patch.object(server, "call_linnworks_get", return_value=_register(executing=True)), \
             patch.object(server, "call_linnworks_void") as void:
            r = server.run_export(36, dry_run=False)
        assert r["queued"] is False
        void.assert_not_called()


class TestRunImportVerification:
    """Same endpoint family, same async shape — plus an explicit warning that an
    import WRITES to the catalogue. run_import itself is NOT live-proven."""

    def test_started_before_is_captured(self):
        with patch.object(server, "call_linnworks_get", return_value=_register()), \
             patch.object(server, "call_linnworks_void"):
            r = server.run_import(353, dry_run=False)
        assert r["started_before"] == "2018-05-15T11:09:08Z"

    def test_message_warns_that_an_import_writes_to_the_catalogue(self):
        with patch.object(server, "call_linnworks_get", return_value=_register()), \
             patch.object(server, "call_linnworks_void"):
            r = server.run_import(353, dry_run=False)
        assert "WRITES to your catalogue" in r["message"]

    def test_how_to_verify_warns_import_status_is_unreliable(self):
        """import_status returns null even for erroring imports — a
        long-standing tenant quirk already documented in CLAUDE.md."""
        with patch.object(server, "call_linnworks_get", return_value=_register()), \
             patch.object(server, "call_linnworks_void"):
            r = server.run_import(353, dry_run=False)
        assert "import_status" in r["how_to_verify"]

    def test_dry_run_fires_nothing(self):
        with patch.object(server, "call_linnworks_get", return_value=_register()), \
             patch.object(server, "call_linnworks_void") as void:
            server.run_import(353, dry_run=True)
        void.assert_not_called()
