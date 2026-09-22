"""
Tests for the four read-only Picking tools (issue #64):
  get_pick_waves, get_pick_wave_users, get_item_bins, check_orders_pickable

All endpoint shapes here (GET, query-param names, the wrapped
CheckAllocatableToPickwave body, the "'request' parameter is missing" 400 on
an empty query string, the "non WMS location" bin-tracking error, and the
observed live State values) match what was recorded live against the real
tenant during this build — see CLAUDE.md's confirmed-endpoints table.

All tests use unittest.mock — no live Linnworks API calls.
Run with: pytest tests/test_pick_waves.py -v
"""
import asyncio
import inspect
import sys
import os
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server


TOOL_NAMES = ("get_pick_waves", "get_pick_wave_users", "get_item_bins", "check_orders_pickable")


# ── AC1: tools registered and callable ──────────────────────────────────────

class TestRegistration:

    def test_all_four_tools_are_registered(self):
        registered = {t.name for t in asyncio.run(server.mcp.list_tools())}
        for name in TOOL_NAMES:
            assert name in registered

    def test_total_tool_count(self):
        registered = asyncio.run(server.mcp.list_tools())
        assert len(registered) == 98


# ── AC2: no write-tool machinery on any of the four ─────────────────────────

class TestNoWriteMachinery:

    def test_none_accept_dry_run(self):
        for name in TOOL_NAMES:
            sig = inspect.signature(getattr(server, name))
            assert "dry_run" not in sig.parameters, name

    def test_none_appear_in_write_thresholds(self):
        for name in TOOL_NAMES:
            assert name not in server.WRITE_THRESHOLDS, name

    def test_none_call_call_linnworks_void(self):
        for name in TOOL_NAMES:
            src = inspect.getsource(getattr(server, name))
            assert "call_linnworks_void" not in src, name


# ── AC7: module-level state map + formatters ────────────────────────────────

class TestModuleLevelSymbols:

    def test_state_label_mapping_is_module_level_and_non_empty(self):
        assert hasattr(server, "_PICK_WAVE_STATE_LABELS")
        assert isinstance(server._PICK_WAVE_STATE_LABELS, dict)
        assert len(server._PICK_WAVE_STATE_LABELS) > 0

    def test_state_label_mapping_only_contains_live_observed_states(self):
        # Only Abandoned and Shipped were actually seen on a real wave live
        # during this build (see CLAUDE.md) — the other six documented enum
        # values must NOT be pre-guessed into the map.
        assert set(server._PICK_WAVE_STATE_LABELS) == {"Abandoned", "Shipped"}

    def test_formatters_are_module_level_functions(self):
        assert inspect.isfunction(server._format_pick_wave)
        assert inspect.isfunction(server._format_bin_row)
        # Not nested inside a tool — module globals, not closures.
        assert server._format_pick_wave.__module__ == "server"
        assert "server" in server._format_bin_row.__qualname__ or "." not in server._format_bin_row.__qualname__


# ── AC6: raw state verbatim + confirmed-only label, never null/guessed ──────

class TestStateLabelling:

    def test_confirmed_state_gets_its_label(self):
        row = {"PickingWaveId": 5, "State": "Shipped"}
        out = server._format_pick_wave(row)
        assert out["state"] == "Shipped"
        assert out["state_label"] == "Shipped"
        assert out["state_confirmed"] is True

    def test_unconfirmed_but_documented_state_is_explicit_unknown_not_guessed(self):
        # "InProgress" is a real, spec-documented state, but was never
        # observed on a genuine wave live during this build.
        row = {"PickingWaveId": 42, "State": "InProgress"}
        out = server._format_pick_wave(row)
        assert out["state"] == "InProgress"
        assert out["state_confirmed"] is False
        assert out["state_label"] is not None
        assert "unknown" in out["state_label"].lower()
        assert "InProgress" in out["state_label"]

    def test_unmapped_state_label_is_never_null(self):
        row = {"PickingWaveId": 1, "State": "SomethingNeverSeenBefore"}
        out = server._format_pick_wave(row)
        assert out["state_label"] is not None
        assert "SomethingNeverSeenBefore" in out["state_label"]

    def test_raw_state_is_always_verbatim_even_when_unconfirmed(self):
        row = {"PickingWaveId": 1, "State": "Paused"}
        out = server._format_pick_wave(row)
        assert out["state"] == "Paused"  # never rewritten, never dropped


# ── AC12: no summed weight/volume figure ────────────────────────────────────

class TestNoWeightSummation:

    def test_wave_formatter_emits_no_weight_or_volume_key(self):
        row = {"PickingWaveId": 1, "State": "Shipped", "ItemCount": 4}
        out = server._format_pick_wave(row)
        for key in out:
            assert "weight" not in key.lower()
            assert "volume" not in key.lower()

    def test_bin_row_full_percentage_is_passed_through_not_summed(self):
        row = {"BinRack": "A1", "CurrentFullPercentage": 55.5, "Quantity": 3}
        out = server._format_bin_row(row)
        assert out["current_full_percentage"] == 55.5

    def test_get_item_bins_emits_no_total_across_bins(self):
        with patch.object(server, "_resolve_sku_to_id", return_value="sid-1"), \
             patch.object(server, "call_linnworks_get", return_value={
                 "PickableBins": [
                     {"BinRack": "A1", "Quantity": 3},
                     {"BinRack": "A2", "Quantity": 7},
                 ],
                 "NonPickableBins": [], "AlternateLocations": [],
             }):
            result = server.get_item_bins(["sku-1"])
        for key in result:
            assert "total" not in key.lower() or key == "count"


# ── AC13: snapshot + no-add-to-wave docstring claims ────────────────────────

class TestDocstringClaims:

    def test_get_pick_waves_states_point_in_time_snapshot(self):
        doc = server.get_pick_waves.__doc__
        assert "point-in-time snapshot" in doc

    def test_get_pick_wave_users_states_point_in_time_snapshot(self):
        doc = server.get_pick_wave_users.__doc__
        assert "point-in-time snapshot" in doc or "same snapshot" in doc

    def test_get_pick_waves_states_no_add_order_to_existing_wave(self):
        doc = server.get_pick_waves.__doc__
        assert "no endpoint for adding an order to an existing pickwave" in doc.lower() \
            or "exposes no endpoint for adding an order to an existing pickwave" in doc.lower()

    def test_check_orders_pickable_states_side_effect_free_proof(self):
        doc = server.check_orders_pickable.__doc__
        assert "side-effect free" in doc.lower() or "side effect" in doc.lower()


# ── get_pick_waves ───────────────────────────────────────────────────────────

class TestGetPickWaves:

    def test_state_filter_actually_filters(self):
        """AC4: two live calls with different state values return different
        wave sets. Simulated here with a mock that returns different bodies
        per state, mirroring what was recorded live (Abandoned=238-shape vs
        Shipped=3281-shape, both != 0)."""
        def fake_get(path, params=None):
            assert path == "Picking/GetAllPickingWaveHeaders"
            state = (params or {}).get("state")
            if state == "Abandoned":
                return {"PickwaveHeaders": [
                    {"PickingWaveId": 8, "State": "Abandoned", "OrderCount": 1},
                ]}
            if state == "Shipped":
                return {"PickwaveHeaders": [
                    {"PickingWaveId": 5, "State": "Shipped", "OrderCount": 2},
                    {"PickingWaveId": 9, "State": "Shipped", "OrderCount": 2},
                ]}
            return {"PickwaveHeaders": []}

        with patch.object(server, "call_linnworks_get", side_effect=fake_get):
            abandoned = server.get_pick_waves(state="Abandoned")
            shipped = server.get_pick_waves(state="Shipped")

        assert abandoned["count"] == 1
        assert shipped["count"] == 2
        assert abandoned["count"] != shipped["count"]
        ids_abandoned = {w["picking_wave_id"] for w in abandoned["waves"]}
        ids_shipped = {w["picking_wave_id"] for w in shipped["waves"]}
        assert ids_abandoned != ids_shipped

    def test_detail_level_does_not_change_wave_count(self):
        """AC5: detailLevel is a detail-loading option, not a result-set
        filter — live-confirmed identical counts at All vs OnlyPickWave."""
        row = {"PickingWaveId": 5, "State": "Shipped", "OrderCount": 2}

        def fake_get(path, params=None):
            return {"PickwaveHeaders": [row]}

        with patch.object(server, "call_linnworks_get", side_effect=fake_get):
            all_level = server.get_pick_waves(state="Shipped", detail_level="All")
            only_wave = server.get_pick_waves(state="Shipped", detail_level="OnlyPickWave")

        assert all_level["count"] == only_wave["count"] == 1

    def test_no_params_at_all_defaults_location_to_avoid_400(self):
        """Both Picking GET endpoints 400 with 'request parameter missing'
        on a genuinely empty query string — the tool must always send at
        least one param."""
        captured = {}

        def fake_get(path, params=None):
            captured["params"] = params
            return {"PickwaveHeaders": []}

        with patch.object(server, "call_linnworks_get", side_effect=fake_get):
            server.get_pick_waves()

        assert captured["params"]  # never empty
        assert captured["params"].get("locationId") == server.DEFAULT_LOCATION_ID

    def test_state_supplied_alone_does_not_force_a_location(self):
        captured = {}

        def fake_get(path, params=None):
            captured["params"] = params
            return {"PickwaveHeaders": []}

        with patch.object(server, "call_linnworks_get", side_effect=fake_get):
            server.get_pick_waves(state="Shipped")

        assert captured["params"] == {"state": "Shipped"}

    def test_state_filter_note_always_present(self):
        with patch.object(server, "call_linnworks_get", return_value={"PickwaveHeaders": []}):
            result = server.get_pick_waves(state="Shipped")
        assert "does not" in result["state_filter_note"].lower() or "not mean" in result["state_filter_note"].lower()

    def test_rate_limited_reports_bucket_not_empty_waves(self):
        """AC10: rate_limited is distinct from an ordinary '0 waves' result,
        and complete is set False."""
        with patch.object(server, "call_linnworks_get",
                           side_effect=server.RateLimitError("quota exceeded")):
            result = server.get_pick_waves(state="Shipped")

        assert result["rate_limited"] is True
        assert result["complete"] is False
        assert "error" in result
        assert result["waves"] == []
        assert result["count"] == 0


# ── get_pick_wave_users ──────────────────────────────────────────────────────

class TestGetPickWaveUsers:

    def test_reuses_the_pick_wave_formatter(self):
        row = {"PickingWaveId": 0, "State": "Unallocated", "UserId": 68,
               "EmailAddress": "warehouse+01@thewarehousegroup.co.uk"}
        with patch.object(server, "call_linnworks_get",
                           return_value={"PickingWaves": [row]}):
            result = server.get_pick_wave_users(state="Unallocated")

        assert result["count"] == 1
        assert result["users"][0]["email_address"] == "warehouse+01@thewarehousegroup.co.uk"
        assert result["users"][0]["state"] == "Unallocated"

    def test_non_terminal_state_warning_always_present(self):
        with patch.object(server, "call_linnworks_get", return_value={"PickingWaves": []}):
            result = server.get_pick_wave_users(state="InProgress")
        assert "non_terminal_state_warning" in result
        assert result["non_terminal_state_warning"]

    def test_no_params_defaults_location(self):
        captured = {}

        def fake_get(path, params=None):
            captured["params"] = params
            return {"PickingWaves": []}

        with patch.object(server, "call_linnworks_get", side_effect=fake_get):
            server.get_pick_wave_users()

        assert captured["params"].get("locationId") == server.DEFAULT_LOCATION_ID

    def test_rate_limited_reports_bucket_not_empty_users(self):
        with patch.object(server, "call_linnworks_get",
                           side_effect=server.RateLimitError("quota exceeded")):
            result = server.get_pick_wave_users(state="Shipped")

        assert result["rate_limited"] is True
        assert result["complete"] is False
        assert result["users"] == []


# ── get_item_bins (AC11) ─────────────────────────────────────────────────────

class TestGetItemBins:

    def test_bins_found_when_real_bin_data_returned(self):
        with patch.object(server, "_resolve_sku_to_id", return_value="sid-1"), \
             patch.object(server, "call_linnworks_get", return_value={
                 "PickableBins": [{"BinRack": "A1", "Quantity": 3}],
                 "NonPickableBins": [], "AlternateLocations": [],
             }):
            result = server.get_item_bins(["sku-1"])

        row = result["results"][0]
        assert row["outcome"] == "bins_found"
        assert len(row["bins"]) == 1
        assert row["bins"][0]["bin_rack"] == "A1"
        assert row["bins"][0]["bin_category"] == "pickable"

    def test_no_bins_configured_is_a_clean_empty_response(self):
        """A genuine 200 with nothing in any of the three arrays."""
        with patch.object(server, "_resolve_sku_to_id", return_value="sid-1"), \
             patch.object(server, "call_linnworks_get", return_value={
                 "PickableBins": [], "NonPickableBins": [], "AlternateLocations": [],
             }):
            result = server.get_item_bins(["sku-1"])

        assert result["results"][0]["outcome"] == "no_bins_configured"

    def test_bin_tracking_unavailable_is_distinct_from_no_bins_configured(self):
        """AC11: this must NOT read the same as 'no bins configured' or
        'lookup failed' — live-confirmed error text on a non-WMS location."""
        err = RuntimeError(
            "Linnworks Picking/GetItemBinracks failed: HTTP 400 — "
            '{"Code":"-","Message":"Alternate locations aren\'t available for '
            'non batched items or items in a non WMS location"}'
        )
        with patch.object(server, "_resolve_sku_to_id", return_value="sid-1"), \
             patch.object(server, "call_linnworks_get", side_effect=err):
            result = server.get_item_bins(["sku-1"])

        row = result["results"][0]
        assert row["outcome"] == "bin_tracking_unavailable"
        assert row["outcome"] != "no_bins_configured"
        assert row["outcome"] != "lookup_failed"
        assert "bins" not in row or row["bins"] == []

    def test_no_bins_configured_and_bin_tracking_unavailable_are_distinguishable(self):
        """The two outcomes must never render identically."""
        with patch.object(server, "_resolve_sku_to_id", return_value="sid-1"), \
             patch.object(server, "call_linnworks_get", return_value={
                 "PickableBins": [], "NonPickableBins": [], "AlternateLocations": [],
             }):
            empty_result = server.get_item_bins(["sku-1"])["results"][0]

        err = RuntimeError("HTTP 400 — non WMS location")
        with patch.object(server, "_resolve_sku_to_id", return_value="sid-1"), \
             patch.object(server, "call_linnworks_get", side_effect=err):
            unavailable_result = server.get_item_bins(["sku-1"])["results"][0]

        assert empty_result["outcome"] != unavailable_result["outcome"]

    def test_other_runtime_error_is_lookup_failed_not_no_bins(self):
        err = RuntimeError("Linnworks Picking/GetItemBinracks failed: HTTP 500 — server error")
        with patch.object(server, "_resolve_sku_to_id", return_value="sid-1"), \
             patch.object(server, "call_linnworks_get", side_effect=err):
            result = server.get_item_bins(["sku-1"])

        row = result["results"][0]
        assert row["outcome"] == "lookup_failed"
        assert row["outcome"] != "no_bins_configured"

    def test_unresolvable_sku_does_not_sink_the_batch(self):
        def fake_resolve(sku, cache=None):
            if sku == "BAD-SKU":
                raise ValueError("SKU 'BAD-SKU' not found in Linnworks")
            return "sid-1"

        with patch.object(server, "_resolve_sku_to_id", side_effect=fake_resolve), \
             patch.object(server, "call_linnworks_get", return_value={
                 "PickableBins": [{"BinRack": "A1"}], "NonPickableBins": [], "AlternateLocations": [],
             }):
            result = server.get_item_bins(["BAD-SKU", "GOOD-SKU"])

        assert result["count"] == 2
        bad, good = result["results"]
        assert bad["outcome"] == "sku_not_found"
        assert good["outcome"] == "bins_found"

    def test_rate_limited_on_resolve_is_its_own_outcome(self):
        with patch.object(server, "_resolve_sku_to_id",
                           side_effect=server.RateLimitError("quota exceeded")):
            result = server.get_item_bins(["sku-1"])

        row = result["results"][0]
        assert row["outcome"] == "rate_limited"
        assert row["outcome"] != "no_bins_configured"
        assert result["complete"] is False

    def test_rate_limited_on_lookup_is_its_own_outcome(self):
        with patch.object(server, "_resolve_sku_to_id", return_value="sid-1"), \
             patch.object(server, "call_linnworks_get",
                           side_effect=server.RateLimitError("quota exceeded")):
            result = server.get_item_bins(["sku-1"])

        row = result["results"][0]
        assert row["outcome"] == "rate_limited"
        assert result["complete"] is False


# ── check_orders_pickable (AC8, AC9) ─────────────────────────────────────────

class TestCheckOrdersPickable:

    def test_mixed_batch_valid_and_bogus_id(self):
        """AC9: one resolvable id + one unresolvable id — the bogus one is
        reported in resolve_errors, and the valid one is still checked."""
        def fake_resolve_order_guid(order_id):
            if order_id == "999999999":
                raise RuntimeError("No order found for numeric ID '999999999'.")
            return "guid-611360", {"NumOrderId": 611360, "OrderId": "guid-611360"}

        def fake_call_linnworks(path, payload):
            assert path == "Picking/CheckAllocatableToPickwave"
            assert payload == {"request": {"OrderIds": [611360]}}
            return {"Results": [{
                "OrderId": 611360, "OrderId_Guid": "guid-611360",
                "Errors": [], "HasErrors": False,
                "OrderDetails": [{"SKU": "TEST-SKU", "Quantity": 1}],
            }]}

        with patch.object(server, "_resolve_order_guid", side_effect=fake_resolve_order_guid), \
             patch.object(server, "call_linnworks", side_effect=fake_call_linnworks):
            result = server.check_orders_pickable(["611360", "999999999"])

        assert result["count"] == 2
        assert len(result["results"]) == 1
        assert result["results"][0]["order_id"] == "611360"
        assert result["results"][0]["pickable"] is True

        assert len(result["resolve_errors"]) == 1
        assert result["resolve_errors"][0]["order_id"] == "999999999"
        assert result["complete"] is False

    def test_wraps_the_request_body(self):
        """CheckAllocatableToPickwave's live-confirmed shape is
        {"request": {"OrderIds": [...]}} — an unwrapped body 400s."""
        captured = {}

        def fake_resolve_order_guid(order_id):
            return f"guid-{order_id}", {"NumOrderId": int(order_id), "OrderId": f"guid-{order_id}"}

        def fake_call_linnworks(path, payload):
            captured["payload"] = payload
            return {"Results": []}

        with patch.object(server, "_resolve_order_guid", side_effect=fake_resolve_order_guid), \
             patch.object(server, "call_linnworks", side_effect=fake_call_linnworks):
            server.check_orders_pickable(["611360"])

        assert captured["payload"] == {"request": {"OrderIds": [611360]}}

    def test_linnworks_own_per_order_error_is_surfaced(self):
        """Linnworks can return HasErrors:true / 'Order doesn't exist' inside
        a successful batch response for a numeric id that resolved fine at
        the pre-flight stage but Linnworks itself rejects."""
        def fake_resolve_order_guid(order_id):
            return "guid-1", {"NumOrderId": 1, "OrderId": "guid-1"}

        def fake_call_linnworks(path, payload):
            return {"Results": [{
                "OrderId": 1, "OrderId_Guid": "00000000-0000-0000-0000-000000000000",
                "Errors": [{"Error": "Order doesn't exist", "ErrorType": "OrderDoesntExist"}],
                "HasErrors": True, "OrderDetails": [],
            }]}

        with patch.object(server, "_resolve_order_guid", side_effect=fake_resolve_order_guid), \
             patch.object(server, "call_linnworks", side_effect=fake_call_linnworks):
            result = server.check_orders_pickable(["1"])

        row = result["results"][0]
        assert row["has_errors"] is True
        assert row["pickable"] is False
        assert row["errors"][0]["ErrorType"] == "OrderDoesntExist"

    def test_rate_limited_on_resolve_is_bucketed_separately(self):
        with patch.object(server, "_resolve_order_guid",
                           side_effect=server.RateLimitError("quota exceeded")):
            result = server.check_orders_pickable(["611360"])

        assert result["rate_limited"] == [{"order_id": "611360", "reason": "quota exceeded"}]
        assert result["resolve_errors"] == []
        assert result["complete"] is False

    def test_rate_limited_on_check_call_is_bucketed_not_dropped(self):
        def fake_resolve_order_guid(order_id):
            return "guid-611360", {"NumOrderId": 611360, "OrderId": "guid-611360"}

        with patch.object(server, "_resolve_order_guid", side_effect=fake_resolve_order_guid), \
             patch.object(server, "call_linnworks",
                           side_effect=server.RateLimitError("quota exceeded")):
            result = server.check_orders_pickable(["611360"])

        assert len(result["rate_limited"]) == 1
        assert result["rate_limited"][0]["order_id"] == "611360"
        assert result["results"] == []
        assert result["complete"] is False

    def test_order_missing_from_response_is_not_silently_dropped(self):
        """Defensive: if Linnworks ever omits a result row for a requested,
        non-rate-limited order, it must not vanish from every bucket."""
        def fake_resolve_order_guid(order_id):
            return f"guid-{order_id}", {"NumOrderId": int(order_id), "OrderId": f"guid-{order_id}"}

        def fake_call_linnworks(path, payload):
            # Two ids requested, only one comes back.
            return {"Results": [{
                "OrderId": 611360, "OrderId_Guid": "guid-611360",
                "Errors": [], "HasErrors": False, "OrderDetails": [],
            }]}

        with patch.object(server, "_resolve_order_guid", side_effect=fake_resolve_order_guid), \
             patch.object(server, "call_linnworks", side_effect=fake_call_linnworks):
            result = server.check_orders_pickable(["611360", "611361"])

        assert len(result["results"]) == 1
        assert result["results"][0]["num_order_id"] == 611360
        missing = [e for e in result["resolve_errors"] if e["order_id"] == "611361"]
        assert len(missing) == 1
        assert result["complete"] is False

    def test_accepts_guid_input(self):
        """AC9: both GUID and numeric order ids are accepted."""
        def fake_resolve_order_guid(order_id):
            assert order_id == "1605322d-69ba-4511-a76a-141247629524"
            return order_id, {"NumOrderId": 611360, "OrderId": order_id}

        def fake_call_linnworks(path, payload):
            return {"Results": [{
                "OrderId": 611360, "OrderId_Guid": "1605322d-69ba-4511-a76a-141247629524",
                "Errors": [], "HasErrors": False, "OrderDetails": [],
            }]}

        with patch.object(server, "_resolve_order_guid", side_effect=fake_resolve_order_guid), \
             patch.object(server, "call_linnworks", side_effect=fake_call_linnworks):
            result = server.check_orders_pickable(["1605322d-69ba-4511-a76a-141247629524"])

        assert result["results"][0]["order_guid"] == "1605322d-69ba-4511-a76a-141247629524"
        assert result["results"][0]["num_order_id"] == 611360
