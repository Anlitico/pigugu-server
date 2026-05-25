# tests/unit/context/test_snapshot.py
"""Unit tests for ContextSnapshot  -  segment splitting, scenario detection, conversion."""


class TestContextSnapshot:
    """ContextSnapshot  -  segment splitting, scenario detection, conversion."""

    @staticmethod
    def _rec(turn_number, role="user", content="", roast_instance_id=None):
        from context.schema import ConversationRecord
        return ConversationRecord(
            turn_number=turn_number, role=role, content=content,
            roast_instance_id=roast_instance_id, created_at=float(turn_number),
        )

    def test_init_empty(self):
        from context.snapshot import ContextSnapshot
        snap = ContextSnapshot([])
        assert snap.records == []

    def test_roast_start_idx_none_when_no_roast(self):
        from context.snapshot import ContextSnapshot
        records = [self._rec(1), self._rec(2)]
        snap = ContextSnapshot(records)
        assert snap.roast_start_idx is None

    def test_roast_start_idx_returns_first_roast_index(self):
        from context.snapshot import ContextSnapshot
        records = [
            self._rec(1, roast_instance_id=None),
            self._rec(2, roast_instance_id=None),
            self._rec(3, roast_instance_id="rx"),
            self._rec(4, roast_instance_id="rx"),
        ]
        snap = ContextSnapshot(records)
        assert snap.roast_start_idx == 2

    def test_scenario_free_chat(self):
        from context.snapshot import ContextSnapshot
        records = [self._rec(1), self._rec(2)]
        snap = ContextSnapshot(records)
        assert snap.scenario == "free_chat"

    def test_scenario_roast(self):
        from context.snapshot import ContextSnapshot
        records = [self._rec(1, roast_instance_id="rx")]
        snap = ContextSnapshot(records)
        assert snap.scenario == "roast"

    def test_roast_instance_id_empty(self):
        from context.snapshot import ContextSnapshot
        snap = ContextSnapshot([self._rec(1)])
        assert snap.roast_instance_id == ""

    def test_roast_instance_id_value(self):
        from context.snapshot import ContextSnapshot
        snap = ContextSnapshot([self._rec(1, roast_instance_id="active_roast")])
        assert snap.roast_instance_id == "active_roast"

    def test_pre_roast_all_when_no_roast(self):
        from context.snapshot import ContextSnapshot
        records = [self._rec(1), self._rec(2)]
        snap = ContextSnapshot(records)
        assert len(snap.pre_roast) == 2

    def test_pre_roast_before_boundary(self):
        from context.snapshot import ContextSnapshot
        records = [
            self._rec(1, roast_instance_id=None),
            self._rec(2, roast_instance_id=None),
            self._rec(3, roast_instance_id="rx"),
        ]
        snap = ContextSnapshot(records)
        pre = snap.pre_roast
        assert len(pre) == 2
        assert pre[0].turn_number == 1
        assert pre[1].turn_number == 2

    def test_roast_property_empty_when_no_roast(self):
        from context.snapshot import ContextSnapshot
        snap = ContextSnapshot([self._rec(1)])
        assert snap.roast == []

    def test_roast_property_from_boundary(self):
        from context.snapshot import ContextSnapshot
        records = [
            self._rec(1, roast_instance_id=None),
            self._rec(2, roast_instance_id="rx"),
            self._rec(3, roast_instance_id="rx"),
        ]
        snap = ContextSnapshot(records)
        roast = snap.roast
        assert len(roast) == 2
        assert roast[0].turn_number == 2
        assert roast[1].turn_number == 3

    def test_split(self):
        from context.snapshot import ContextSnapshot
        records = [
            self._rec(1, roast_instance_id=None),
            self._rec(2, roast_instance_id="rx"),
        ]
        snap = ContextSnapshot(records)
        pre, roast = snap.split()
        assert len(pre) == 1
        assert len(roast) == 1

    def test_to_messages(self):
        from context.snapshot import ContextSnapshot
        records = [self._rec(1, role="user", content="hi")]
        snap = ContextSnapshot(records)
        msgs = snap.to_messages(records)
        assert len(msgs) == 1
        assert msgs[0].role == "user"
        assert msgs[0].content == "hi"

    def test_scenario_roast_when_last_has_roast_instance_id(self):
        from context.snapshot import ContextSnapshot
        records = [
            self._rec(1, roast_instance_id=None),
            self._rec(2, roast_instance_id="rx"),
        ]
        snap = ContextSnapshot(records)
        assert snap.scenario == "roast"
