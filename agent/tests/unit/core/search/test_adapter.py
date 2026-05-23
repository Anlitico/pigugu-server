# tests/unit/core/search/test_adapter.py
"""Tests for core.search.adapter — build_search_messages, _normalize_role, SearchAdapter."""


class MockChatItem:
    """Minimal mock for LiveKit ChatContext items."""

    def __init__(self, role, text_content):
        self.role = role
        self.text_content = text_content


class TestNormalizeRole:
    def test_system(self):
        from core.search.adapter import _normalize_role
        assert _normalize_role("system") == "system"

    def test_user(self):
        from core.search.adapter import _normalize_role
        assert _normalize_role("user") == "user"

    def test_assistant(self):
        from core.search.adapter import _normalize_role
        assert _normalize_role("assistant") == "assistant"

    def test_tool(self):
        from core.search.adapter import _normalize_role
        assert _normalize_role("tool") == "tool"

    def test_developer_maps_to_system(self):
        from core.search.adapter import _normalize_role
        assert _normalize_role("developer") == "system"

    def test_unknown_falls_to_user(self):
        from core.search.adapter import _normalize_role
        assert _normalize_role("moderator") == "user"

    def test_empty_string(self):
        from core.search.adapter import _normalize_role
        assert _normalize_role("") == "user"

    def test_none(self):
        from core.search.adapter import _normalize_role
        assert _normalize_role(None) == "user"

    def test_case_insensitive(self):
        from core.search.adapter import _normalize_role
        assert _normalize_role("SYSTEM") == "system"
        assert _normalize_role("User") == "user"


class TestBuildSearchMessages:
    def test_basic_conversion(self):
        from core.search.adapter import build_search_messages
        items = [
            MockChatItem("system", "You are helpful."),
            MockChatItem("user", "hello"),
        ]
        result = build_search_messages(items)
        assert len(result) == 2
        assert result[0] == {"role": "system", "content": "You are helpful."}
        assert result[1] == {"role": "user", "content": "hello"}

    def test_skips_empty_content(self):
        from core.search.adapter import build_search_messages
        items = [
            MockChatItem("user", ""),
            MockChatItem("user", "hi"),
        ]
        result = build_search_messages(items)
        assert len(result) == 1
        assert result[0]["content"] == "hi"

    def test_dedup_system_messages(self):
        from core.search.adapter import build_search_messages
        items = [
            MockChatItem("system", "You are helpful."),
            MockChatItem("system", "You are helpful."),
            MockChatItem("user", "hi"),
        ]
        result = build_search_messages(items)
        assert len(result) == 2

    def test_system_first(self):
        from core.search.adapter import build_search_messages
        items = [
            MockChatItem("user", "hi"),
            MockChatItem("system", "Rules"),
            MockChatItem("user", "bye"),
        ]
        result = build_search_messages(items)
        assert result[0]["role"] == "system"

    def test_empty_list(self):
        from core.search.adapter import build_search_messages
        assert build_search_messages([]) == []


class TestCreateSearchAdapter:
    def test_default_qwen(self):
        from core.search.adapter import create_search_adapter, QwenSearchAdapter
        a = create_search_adapter("qwen-us")
        assert isinstance(a, QwenSearchAdapter)

    def test_grok(self):
        from core.search.adapter import create_search_adapter, GrokSearchAdapter
        a = create_search_adapter("grok")
        assert isinstance(a, GrokSearchAdapter)

    def test_xai_aliases_to_grok(self):
        from core.search.adapter import create_search_adapter, GrokSearchAdapter
        a = create_search_adapter("xai")
        assert isinstance(a, GrokSearchAdapter)

    def test_unknown_falls_back_to_qwen(self):
        from core.search.adapter import create_search_adapter, QwenSearchAdapter
        a = create_search_adapter("unknown-provider")
        assert isinstance(a, QwenSearchAdapter)
