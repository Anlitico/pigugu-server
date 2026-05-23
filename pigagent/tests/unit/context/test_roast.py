# tests/unit/context/test_roast.py
"""Unit tests for RoastState — roast_id assignment and state queries."""


class TestRoastState:
    """RoastState — pure functions for roast_id assignment and queries."""

    @staticmethod
    def _rec(turn_number, role="user", content="", roast_id=None, created_at=0.0):
        from context.schema import ConversationRecord
        return ConversationRecord(
            turn_number=turn_number, role=role, content=content,
            roast_id=roast_id, created_at=created_at,
        )

    def test_current_roast_id_empty(self):
        from context.roast import RoastState
        assert RoastState.current_roast_id([]) is None

    def test_current_roast_id_returns_last(self):
        from context.roast import RoastState
        records = [
            self._rec(1, roast_id="ra"),
            self._rec(2, roast_id="rb"),
        ]
        assert RoastState.current_roast_id(records) == "rb"

    def test_is_active_true(self):
        from context.roast import RoastState
        records = [self._rec(1, roast_id="rx")]
        assert RoastState.is_active(records) is True

    def test_is_active_false_empty(self):
        from context.roast import RoastState
        assert RoastState.is_active([]) is False

    def test_is_active_false_no_roast_id(self):
        from context.roast import RoastState
        records = [self._rec(1, roast_id=None)]
        assert RoastState.is_active(records) is False

    def test_assign_roast_id_already_has_one(self):
        from context.roast import RoastState
        current = self._rec(5, roast_id="existing")
        result = RoastState.assign_roast_id([], current)
        assert result == "existing"
        assert current.roast_id == "existing"

    def test_assign_roast_id_inherits_from_active_previous(self):
        from context.roast import RoastState
        history = [self._rec(4, roast_id="rx", created_at=100.0)]
        current = self._rec(5, created_at=101.0)
        result = RoastState.assign_roast_id(history, current)
        assert result == "rx"
        assert current.roast_id == "rx"

    def test_assign_roast_id_stale_no_inherit(self):
        from context.roast import RoastState
        history = [self._rec(4, roast_id="rx", created_at=0.0)]
        current = self._rec(5, created_at=999999.0)
        result = RoastState.assign_roast_id(history, current)
        assert result is None
        assert current.roast_id is None

    def test_assign_roast_id_no_previous_roast(self):
        from context.roast import RoastState
        history = [self._rec(4, roast_id=None)]
        current = self._rec(5, created_at=100.0)
        result = RoastState.assign_roast_id(history, current)
        assert result is None
        assert current.roast_id is None
