# tests/unit/core/agent/test_state.py
"""Tests for core.agent.state  -  AgentState, StateStatus."""


class TestAgentState:
    def test_defaults(self):
        from core.agent.state import AgentState, StateStatus
        s = AgentState()
        assert s.status == StateStatus.RUNNING.value
        assert s.is_running
        assert not s.is_terminal

    def test_terminal_success(self):
        from core.agent.state import AgentState
        s = AgentState(status="success")
        assert s.is_terminal

    def test_terminal_fail(self):
        from core.agent.state import AgentState
        assert AgentState(status="fail").is_terminal

    def test_terminal_error(self):
        from core.agent.state import AgentState
        assert AgentState(status="error").is_terminal

    def test_terminal_interrupted(self):
        from core.agent.state import AgentState
        assert AgentState(status="interrupted").is_terminal

    def test_running_not_terminal(self):
        from core.agent.state import AgentState
        assert not AgentState(status="running").is_terminal
