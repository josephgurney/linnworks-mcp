"""
QA tests for delete_order_notes_by_text.

All tests use unittest.mock — no live Linnworks API calls.
Run with: pytest tests/test_delete_notes_by_text.py -v
"""

import sys
import os
import pytest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ── Fixtures ─────────────────────────────────────────────────────────────────

ORDER_GUID = "aaaaaaaa-1234-5678-0000-000000000001"

NOTE_ID_1 = "note-guid-0001-0001-0001-000000000001"
NOTE_ID_2 = "note-guid-0002-0002-0002-000000000002"
NOTE_ID_3 = "note-guid-0003-0003-0003-000000000003"


def _make_raw_note(note_id, text, internal=True):
    return {
        "OrderNoteId": note_id,
        "Note": text,
        "Internal": internal,
        "NoteDate": "2026-05-01T10:00:00",
        "CreatedBy": "test@example.com",
        "NoteTypeId": 0,
    }


NOTES_THREE = [
    _make_raw_note(NOTE_ID_1, "This is a claude test"),
    _make_raw_note(NOTE_ID_2, "Gift wrap"),
    _make_raw_note(NOTE_ID_3, "THIS IS A CLAUDE TEST"),  # uppercase duplicate
]

NOTES_ONE = [
    _make_raw_note(NOTE_ID_1, "This is a claude test"),
]

NOTES_EMPTY: list = []


def _get_side_effect(notes_before, notes_after=None):
    """Returns a side_effect for call_linnworks_get that returns notes."""
    calls = {"n": 0}
    if notes_after is None:
        notes_after = []

    def side_effect(path, params=None, **kwargs):
        if "GetOrderNotes" in path:
            calls["n"] += 1
            return notes_before if calls["n"] == 1 else notes_after
        raise AssertionError(f"Unexpected GET path: {path}")

    return side_effect


def _delete_side_effect(path, payload, **kwargs):
    if "DeleteOrderNote" in path:
        return {"success": True}
    raise AssertionError(f"Unexpected POST path: {path}")


# ── dry_run behaviour ─────────────────────────────────────────────────────────

class TestDryRun:

    def test_dry_run_does_not_call_delete(self):
        """dry_run=True must never call ProcessedOrders/DeleteOrderNote."""
        import server

        posted = []

        def post_side(path, payload, **kwargs):
            posted.append(path)
            return {}

        with patch("server.call_linnworks_get",
                   side_effect=_get_side_effect(NOTES_ONE)):
            with patch("server.call_linnworks", side_effect=post_side):
                server.delete_order_notes_by_text(
                    ORDER_GUID, "This is a claude test", dry_run=True
                )

        assert not any("DeleteOrderNote" in p for p in posted)

    def test_dry_run_lists_matched_notes(self):
        """Dry-run output must include the matched notes."""
        import server

        with patch("server.call_linnworks_get",
                   side_effect=_get_side_effect(NOTES_ONE)):
            result = server.delete_order_notes_by_text(
                ORDER_GUID, "This is a claude test", dry_run=True
            )

        assert result["dry_run"] is True
        assert result["matched_count"] == 1
        assert result["deleted"] == 0
        assert result["matched"][0]["note"] == "This is a claude test"
        assert result["matched"][0]["note_id"] == NOTE_ID_1

    def test_dry_run_zero_matches_is_success(self):
        """Zero matches must return success=True, not an error."""
        import server

        with patch("server.call_linnworks_get",
                   side_effect=_get_side_effect(NOTES_ONE)):
            result = server.delete_order_notes_by_text(
                ORDER_GUID, "nonexistent text", dry_run=True
            )

        assert result["success"] is True
        assert result["matched_count"] == 0
        assert result["deleted"] == 0
        assert result["matched"] == []


# ── match modes ───────────────────────────────────────────────────────────────

class TestMatchModes:

    def test_exact_match_default(self):
        """Default match='exact' only matches the full note text."""
        import server

        notes = [
            _make_raw_note(NOTE_ID_1, "This is a claude test"),
            _make_raw_note(NOTE_ID_2, "This is a claude test extended"),
        ]

        with patch("server.call_linnworks_get", side_effect=_get_side_effect(notes)):
            result = server.delete_order_notes_by_text(
                ORDER_GUID, "This is a claude test", dry_run=True
            )

        assert result["matched_count"] == 1
        assert result["matched"][0]["note_id"] == NOTE_ID_1

    def test_contains_match(self):
        """match='contains' matches notes that contain the search text."""
        import server

        notes = [
            _make_raw_note(NOTE_ID_1, "Please gift wrap this order"),
            _make_raw_note(NOTE_ID_2, "No special instructions"),
        ]

        with patch("server.call_linnworks_get", side_effect=_get_side_effect(notes)):
            result = server.delete_order_notes_by_text(
                ORDER_GUID, "gift wrap", match="contains", dry_run=True
            )

        assert result["matched_count"] == 1
        assert result["matched"][0]["note_id"] == NOTE_ID_1

    def test_starts_with_match(self):
        """match='starts_with' matches notes that begin with the search text."""
        import server

        notes = [
            _make_raw_note(NOTE_ID_1, "TEST: this is a test note"),
            _make_raw_note(NOTE_ID_2, "Not a test note"),
        ]

        with patch("server.call_linnworks_get", side_effect=_get_side_effect(notes)):
            result = server.delete_order_notes_by_text(
                ORDER_GUID, "test:", match="starts_with", dry_run=True
            )

        assert result["matched_count"] == 1
        assert result["matched"][0]["note_id"] == NOTE_ID_1

    def test_case_insensitive_by_default(self):
        """case_sensitive=False (default) must match regardless of case."""
        import server

        with patch("server.call_linnworks_get",
                   side_effect=_get_side_effect(NOTES_THREE)):
            result = server.delete_order_notes_by_text(
                ORDER_GUID, "this is a claude test", dry_run=True
            )

        # Both "This is a claude test" and "THIS IS A CLAUDE TEST" should match
        assert result["matched_count"] == 2

    def test_case_sensitive_flag(self):
        """case_sensitive=True must only match exact case."""
        import server

        with patch("server.call_linnworks_get",
                   side_effect=_get_side_effect(NOTES_THREE)):
            result = server.delete_order_notes_by_text(
                ORDER_GUID,
                "This is a claude test",
                case_sensitive=True,
                dry_run=True,
            )

        # Only the mixed-case version should match
        assert result["matched_count"] == 1
        assert result["matched"][0]["note_id"] == NOTE_ID_1

    def test_contains_case_insensitive(self):
        """contains + case_insensitive should find substrings regardless of case."""
        import server

        notes = [
            _make_raw_note(NOTE_ID_1, "GIFT WRAP PLEASE"),
            _make_raw_note(NOTE_ID_2, "No special requirements"),
        ]

        with patch("server.call_linnworks_get", side_effect=_get_side_effect(notes)):
            result = server.delete_order_notes_by_text(
                ORDER_GUID, "gift wrap", match="contains", dry_run=True
            )

        assert result["matched_count"] == 1


# ── max_to_delete guard ───────────────────────────────────────────────────────

class TestMaxToDelete:

    def test_max_to_delete_refuses_when_exceeded(self):
        """If matches > max_to_delete, refuse and return success=False."""
        import server

        with patch("server.call_linnworks_get",
                   side_effect=_get_side_effect(NOTES_THREE)):
            result = server.delete_order_notes_by_text(
                ORDER_GUID,
                "this is a claude test",
                max_to_delete=1,
                dry_run=True,
            )

        assert result["success"] is False
        assert "error" in result
        assert result["matched_count"] == 2

    def test_max_to_delete_refused_result_includes_matches(self):
        """The refusal response must include the matched notes for review."""
        import server

        with patch("server.call_linnworks_get",
                   side_effect=_get_side_effect(NOTES_THREE)):
            result = server.delete_order_notes_by_text(
                ORDER_GUID,
                "this is a claude test",
                max_to_delete=1,
                dry_run=False,  # even with dry_run=False, guard fires first
            )

        assert result["success"] is False
        assert len(result["matched"]) == 2
        # No delete calls should have been made
        # (the guard happens before any write)

    def test_max_to_delete_allows_when_not_exceeded(self):
        """max_to_delete should not block when match count <= limit."""
        import server

        with patch("server.call_linnworks_get",
                   side_effect=_get_side_effect(NOTES_THREE)):
            result = server.delete_order_notes_by_text(
                ORDER_GUID,
                "this is a claude test",
                max_to_delete=5,
                dry_run=True,
            )

        assert result["success"] is True
        assert result["matched_count"] == 2

    def test_max_to_delete_none_has_no_limit(self):
        """max_to_delete=None (default) places no cap on deletions."""
        import server

        with patch("server.call_linnworks_get",
                   side_effect=_get_side_effect(NOTES_THREE)):
            result = server.delete_order_notes_by_text(
                ORDER_GUID,
                "this is a claude test",
                max_to_delete=None,
                dry_run=True,
            )

        assert result["success"] is True
        assert result["matched_count"] == 2


# ── live deletion ─────────────────────────────────────────────────────────────

class TestLiveDeletion:

    def test_live_run_calls_delete_for_each_match(self):
        """Live run must call DeleteOrderNote once per matched note."""
        import server

        deleted_ids = []

        def post_side(path, payload, **kwargs):
            if "DeleteOrderNote" in path:
                deleted_ids.append(payload.get("pkOrderNoteId"))
                return {}
            raise AssertionError(f"Unexpected POST: {path}")

        # First GET returns notes; second GET (read-back) returns empty
        with patch("server.call_linnworks_get",
                   side_effect=_get_side_effect(NOTES_THREE, notes_after=[])):
            with patch("server.call_linnworks", side_effect=post_side):
                result = server.delete_order_notes_by_text(
                    ORDER_GUID,
                    "this is a claude test",
                    dry_run=False,
                )

        assert result["deleted"] == 2
        assert NOTE_ID_1 in deleted_ids
        assert NOTE_ID_3 in deleted_ids

    def test_live_run_returns_note_count_after(self):
        """Live run must include note_count_after from the read-back."""
        import server

        def post_side(path, payload, **kwargs):
            if "DeleteOrderNote" in path:
                return {}
            raise AssertionError(f"Unexpected POST: {path}")

        remaining = [_make_raw_note(NOTE_ID_2, "Gift wrap")]

        with patch("server.call_linnworks_get",
                   side_effect=_get_side_effect(NOTES_THREE, notes_after=remaining)):
            with patch("server.call_linnworks", side_effect=post_side):
                result = server.delete_order_notes_by_text(
                    ORDER_GUID,
                    "this is a claude test",
                    dry_run=False,
                )

        assert result["note_count_after"] == 1

    def test_live_run_success_true_when_all_deleted(self):
        """success must be True when all matched notes were deleted."""
        import server

        def post_side(path, payload, **kwargs):
            if "DeleteOrderNote" in path:
                return {}
            raise AssertionError()

        with patch("server.call_linnworks_get",
                   side_effect=_get_side_effect(NOTES_ONE, notes_after=[])):
            with patch("server.call_linnworks", side_effect=post_side):
                result = server.delete_order_notes_by_text(
                    ORDER_GUID, "This is a claude test", dry_run=False
                )

        assert result["success"] is True
        assert result["deleted"] == 1

    def test_live_zero_matches_returns_success(self):
        """Zero matches on a live run must still return success=True."""
        import server

        with patch("server.call_linnworks_get",
                   side_effect=_get_side_effect(NOTES_ONE, notes_after=NOTES_ONE)):
            with patch("server.call_linnworks") as mock_post:
                result = server.delete_order_notes_by_text(
                    ORDER_GUID, "no such note", dry_run=False
                )

        assert result["success"] is True
        assert result["deleted"] == 0
        mock_post.assert_not_called()


# ── numeric order ID resolution ───────────────────────────────────────────────

class TestOrderIdResolution:

    def test_numeric_order_id_resolves_to_guid(self):
        """A numeric order ID must be resolved to a GUID before fetching notes."""
        import server

        raw_order = {
            "OrderId": ORDER_GUID,
            "NumOrderId": 123456,
            "GeneralInfo": {},
            "ShippingInfo": {},
            "CustomerInfo": {"Address": {}, "BillingAddress": {}},
            "Items": [],
            "Notes": [],
        }

        def post_side(path, payload, **kwargs):
            if "GetOrdersById" in path:
                return [raw_order]
            raise AssertionError(f"Unexpected POST: {path}")

        def get_side(path, params=None, **kwargs):
            if "GetOrderDetailsByNumOrderId" in path:
                return raw_order
            if "GetOrderNotes" in path:
                return NOTES_ONE
            raise AssertionError(f"Unexpected GET: {path}")

        with patch("server.call_linnworks", side_effect=post_side):
            with patch("server.call_linnworks_get", side_effect=get_side):
                result = server.delete_order_notes_by_text(
                    "123456", "This is a claude test", dry_run=True
                )

        assert result["matched_count"] == 1


# ── version ───────────────────────────────────────────────────────────────────

def test_version_is_at_least_1_9_1():
    import server
    major, minor, patch = (int(x) for x in server.__version__.split("."))
    assert (major, minor, patch) >= (1, 9, 1), f"Expected >= 1.9.1, got {server.__version__}"
