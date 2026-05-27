"""
QA tests for order-notes tools:
  get_order_notes, add_order_note, update_order_note, delete_order_note.

All tests use unittest.mock — no live Linnworks API calls.
Run with: pytest tests/test_order_notes.py -v
"""

import sys
import os
import pytest
from unittest.mock import patch, call as mock_call_obj

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ── Fixtures ─────────────────────────────────────────────────────────────────

VALID_GUID    = "a1b2c3d4-1234-5678-abcd-ef0123456789"
NOTE_ID_1     = "nnnnnnnn-0001-0001-0001-000000000001"
NOTE_ID_2     = "nnnnnnnn-0002-0002-0002-000000000002"
NUMERIC_ID    = "596475"

# Raw response shape returned by Orders/GetOrderNotes
NOTES_RESPONSE = [
    {
        "pkOrderNoteId": NOTE_ID_1,
        "Note": "First note",
        "IsInternal": True,
        "NoteCreatedOn": "2026-05-01T10:00:00",
        "NoteCreatedBy": "admin",
    },
    {
        "pkOrderNoteId": NOTE_ID_2,
        "Note": "Second note",
        "IsInternal": False,
        "NoteCreatedOn": "2026-05-02T11:00:00",
        "NoteCreatedBy": "admin",
    },
]

EMPTY_NOTES_RESPONSE = []

# Minimal order response used by _resolve_order_guid (for numeric routing tests)
OPEN_ORDER_RESPONSE = {
    "OrderId": VALID_GUID,
    "NumOrderId": 596475,
    "GeneralInfo": {"Status": 1},
    "CustomerInfo": {"Address": {}, "BillingAddress": {}},
    "Processed": False,
}


# ── get_order_notes ───────────────────────────────────────────────────────────

class TestGetOrderNotes:
    def test_returns_notes_for_guid(self):
        import server
        with patch("server.call_linnworks_get", return_value=NOTES_RESPONSE):
            result = server.get_order_notes(order_id=VALID_GUID)

        assert result["order_id"] == VALID_GUID
        assert result["note_count"] == 2
        assert result["notes"][0]["note"] == "First note"
        assert result["notes"][0]["internal"] is True
        assert result["notes"][1]["note"] == "Second note"

    def test_returns_empty_list_when_no_notes(self):
        import server
        with patch("server.call_linnworks_get", return_value=EMPTY_NOTES_RESPONSE):
            result = server.get_order_notes(order_id=VALID_GUID)

        assert result["note_count"] == 0
        assert result["notes"] == []

    def test_numeric_id_resolves_to_guid(self):
        """Numeric order IDs must be resolved to a GUID before calling GetOrderNotes."""
        import server

        def fake_get(path, params=None):
            if "GetOrderDetailsByNumOrderId" in path:
                return OPEN_ORDER_RESPONSE
            # GetOrderNotes call
            return NOTES_RESPONSE

        with patch("server.call_linnworks_get", side_effect=fake_get):
            result = server.get_order_notes(order_id=NUMERIC_ID)

        assert result["order_id"] == VALID_GUID
        assert result["note_count"] == 2

    def test_dict_response_is_unwrapped(self):
        """GetOrderNotes may return {"Notes": [...]} — should still work."""
        import server
        with patch(
            "server.call_linnworks_get",
            return_value={"Notes": NOTES_RESPONSE},
        ):
            result = server.get_order_notes(order_id=VALID_GUID)

        assert result["note_count"] == 2


# ── add_order_note ────────────────────────────────────────────────────────────

class TestAddOrderNote:
    def test_dry_run_does_not_call_api(self):
        import server
        with patch("server.call_linnworks") as mock_post, \
             patch("server.call_linnworks_get") as mock_get:
            result = server.add_order_note(
                order_id=VALID_GUID,
                note="Test note",
                # dry_run not passed — must default to True
            )

        assert result["dry_run"] is True
        assert result["success"] is True
        assert result["note"] == "Test note"
        mock_post.assert_not_called()
        mock_get.assert_not_called()

    def test_live_run_calls_add_orders_note(self):
        import server
        with patch("server.call_linnworks") as mock_post, \
             patch("server.call_linnworks_get", return_value=NOTES_RESPONSE):
            result = server.add_order_note(
                order_id=VALID_GUID,
                note="New note",
                internal=True,
                dry_run=False,
            )

        assert result["dry_run"] is False
        assert result["success"] is True

        called_paths = [c.args[0] for c in mock_post.call_args_list]
        assert any("AddOrdersNote" in p for p in called_paths), (
            "AddOrdersNote was never called on a live run"
        )

    def test_live_run_payload_shape(self):
        """The POST payload must contain OrderIds (list), NoteText, IsInternal."""
        import server
        with patch("server.call_linnworks") as mock_post, \
             patch("server.call_linnworks_get", return_value=NOTES_RESPONSE):
            server.add_order_note(
                order_id=VALID_GUID,
                note="Payload check",
                internal=False,
                dry_run=False,
            )

        # Find the AddOrdersNote call
        add_call = next(
            c for c in mock_post.call_args_list if "AddOrdersNote" in c.args[0]
        )
        payload = add_call.args[1]
        assert payload["OrderIds"] == [VALID_GUID]
        assert payload["NoteText"] == "Payload check"
        assert payload["IsInternal"] is False

    def test_default_internal_flag_is_true(self):
        """internal should default to True (internal/staff-only note)."""
        import server
        with patch("server.call_linnworks") as mock_post, \
             patch("server.call_linnworks_get", return_value=NOTES_RESPONSE):
            server.add_order_note(order_id=VALID_GUID, note="Staff note", dry_run=False)

        add_call = next(
            c for c in mock_post.call_args_list if "AddOrdersNote" in c.args[0]
        )
        assert add_call.args[1]["IsInternal"] is True

    def test_returns_notes_after_add(self):
        """Live run result must include notes list (read-back confirmation)."""
        import server
        with patch("server.call_linnworks"), \
             patch("server.call_linnworks_get", return_value=NOTES_RESPONSE):
            result = server.add_order_note(
                order_id=VALID_GUID, note="X", dry_run=False
            )

        assert "notes" in result
        assert result["note_count_after"] == 2


# ── update_order_note ─────────────────────────────────────────────────────────

class TestUpdateOrderNote:
    def test_dry_run_shows_before_after(self):
        import server
        with patch("server.call_linnworks_get", return_value=NOTES_RESPONSE), \
             patch("server.call_linnworks") as mock_post:
            result = server.update_order_note(
                order_id=VALID_GUID,
                note_id=NOTE_ID_1,
                note="Updated text",
                # dry_run not passed — must default to True
            )

        assert result["dry_run"] is True
        assert result["success"] is True
        assert result["old_note"] == "First note"
        assert result["new_note"] == "Updated text"
        mock_post.assert_not_called()

    def test_live_run_deletes_then_adds(self):
        import server
        with patch("server.call_linnworks_get", return_value=NOTES_RESPONSE), \
             patch("server.call_linnworks") as mock_post:
            # Second call to call_linnworks_get (read-back) can return anything
            result = server.update_order_note(
                order_id=VALID_GUID,
                note_id=NOTE_ID_1,
                note="Replacement",
                dry_run=False,
            )

        called_paths = [c.args[0] for c in mock_post.call_args_list]
        assert any("DeleteOrderNote" in p for p in called_paths), "DeleteOrderNote was not called"
        assert any("AddOrdersNote" in p for p in called_paths), "AddOrdersNote was not called"

    def test_delete_is_called_before_add(self):
        """Delete must precede add to avoid duplicate notes if add fails."""
        import server

        call_order = []

        def record_call(path, *args, **kwargs):
            call_order.append(path)

        with patch("server.call_linnworks_get", return_value=NOTES_RESPONSE), \
             patch("server.call_linnworks", side_effect=record_call):
            server.update_order_note(
                order_id=VALID_GUID,
                note_id=NOTE_ID_1,
                note="Ordered call test",
                dry_run=False,
            )

        delete_idx = next(i for i, p in enumerate(call_order) if "DeleteOrderNote" in p)
        add_idx    = next(i for i, p in enumerate(call_order) if "AddOrdersNote" in p)
        assert delete_idx < add_idx

    def test_preserves_internal_flag_when_not_given(self):
        """If internal is not supplied, the existing note's flag must be preserved."""
        import server
        with patch("server.call_linnworks_get", return_value=NOTES_RESPONSE), \
             patch("server.call_linnworks") as mock_post:
            result = server.update_order_note(
                order_id=VALID_GUID,
                note_id=NOTE_ID_1,  # IsInternal=True on this note
                note="Same flag",
                dry_run=False,
            )

        add_call = next(
            c for c in mock_post.call_args_list if "AddOrdersNote" in c.args[0]
        )
        assert add_call.args[1]["IsInternal"] is True

    def test_error_when_note_id_not_found(self):
        import server
        with patch("server.call_linnworks_get", return_value=NOTES_RESPONSE):
            result = server.update_order_note(
                order_id=VALID_GUID,
                note_id="00000000-dead-dead-dead-000000000000",
                note="Ghost update",
                dry_run=False,
            )

        assert result["success"] is False
        assert "error" in result


# ── delete_order_note ─────────────────────────────────────────────────────────

class TestDeleteOrderNote:
    def test_dry_run_does_not_delete(self):
        import server
        with patch("server.call_linnworks_get", return_value=NOTES_RESPONSE), \
             patch("server.call_linnworks") as mock_post:
            result = server.delete_order_note(
                order_id=VALID_GUID,
                note_id=NOTE_ID_1,
                # dry_run not passed — must default to True
            )

        assert result["dry_run"] is True
        assert result["success"] is True
        assert result["deleted_note"] == "First note"
        mock_post.assert_not_called()

    def test_live_run_calls_delete_endpoint(self):
        import server
        with patch("server.call_linnworks_get", return_value=NOTES_RESPONSE), \
             patch("server.call_linnworks") as mock_post:
            result = server.delete_order_note(
                order_id=VALID_GUID,
                note_id=NOTE_ID_1,
                dry_run=False,
            )

        assert result["dry_run"] is False
        assert result["success"] is True

        called_paths = [c.args[0] for c in mock_post.call_args_list]
        assert any("DeleteOrderNote" in p for p in called_paths)

    def test_delete_payload_uses_note_id(self):
        import server
        with patch("server.call_linnworks_get", return_value=NOTES_RESPONSE), \
             patch("server.call_linnworks") as mock_post:
            server.delete_order_note(
                order_id=VALID_GUID,
                note_id=NOTE_ID_2,
                dry_run=False,
            )

        del_call = next(
            c for c in mock_post.call_args_list if "DeleteOrderNote" in c.args[0]
        )
        assert del_call.args[1]["pkOrderNoteId"] == NOTE_ID_2

    def test_error_when_note_id_not_found(self):
        import server
        with patch("server.call_linnworks_get", return_value=NOTES_RESPONSE):
            result = server.delete_order_note(
                order_id=VALID_GUID,
                note_id="00000000-dead-dead-dead-000000000000",
                dry_run=False,
            )

        assert result["success"] is False
        assert "error" in result

    def test_returns_note_count_after_delete(self):
        import server

        def fake_get(path, params=None):
            # First call = read existing notes; second = read-back
            if not hasattr(fake_get, "_calls"):
                fake_get._calls = 0
            fake_get._calls += 1
            if fake_get._calls == 1:
                return NOTES_RESPONSE
            # After delete, one note remains
            return [NOTES_RESPONSE[1]]

        with patch("server.call_linnworks_get", side_effect=fake_get), \
             patch("server.call_linnworks"):
            result = server.delete_order_note(
                order_id=VALID_GUID,
                note_id=NOTE_ID_1,
                dry_run=False,
            )

        assert result["note_count_after"] == 1


# ── _format_order_detail notes field ─────────────────────────────────────────

class TestFormatOrderDetailNotes:
    def test_notes_extracted_when_present(self):
        import server
        raw = {
            "OrderId": VALID_GUID,
            "NumOrderId": 1,
            "Processed": False,
            "GeneralInfo": {},
            "ShippingInfo": {},
            "CustomerInfo": {"Address": {}, "BillingAddress": {}},
            "Items": [],
            "Notes": [
                {
                    "pkOrderNoteId": NOTE_ID_1,
                    "Note": "hello",
                    "IsInternal": True,
                    "NoteCreatedOn": "2026-01-01T00:00:00",
                    "NoteCreatedBy": "user",
                }
            ],
        }
        result = server._format_order_detail(raw)
        assert len(result["notes"]) == 1
        assert result["notes"][0]["note"] == "hello"
        assert result["notes"][0]["internal"] is True

    def test_notes_empty_list_when_absent(self):
        import server
        raw = {
            "OrderId": VALID_GUID,
            "NumOrderId": 1,
            "Processed": False,
            "GeneralInfo": {},
            "ShippingInfo": {},
            "CustomerInfo": {"Address": {}, "BillingAddress": {}},
            "Items": [],
            # No "Notes" key
        }
        result = server._format_order_detail(raw)
        assert result["notes"] == []


# ── version check ─────────────────────────────────────────────────────────────

def test_version_is_1_7_0():
    import server
    assert server.__version__ == "1.7.0"
