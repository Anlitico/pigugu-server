"""Tests for tools.search.utils — build_search_messages, _normalize_role."""


class MockChatItem:
    def __init__(self, role, text_content):
        self.role = role
        self.text_content = text_content


class TestNormalizeRole:
    def test_system(self):
        from tools.search.utils import _normalize_role
        assert _normalize_role("system") == "system"

    def test_user(self):
        from tools.search.utils import _normalize_role
        assert _normalize_role("user") == "user"

    def test_assistant(self):
        from tools.search.utils import _normalize_role
        assert _normalize_role("assistant") == "assistant"

    def test_tool(self):
        from tools.search.utils import _normalize_role
        assert _normalize_role("tool") == "tool"

    def test_developer_maps_to_system(self):
        from tools.search.utils import _normalize_role
        assert _normalize_role("developer") == "system"

    def test_unknown_falls_to_user(self):
        from tools.search.utils import _normalize_role
        assert _normalize_role("moderator") == "user"

    def test_empty_and_none(self):
        from tools.search.utils import _normalize_role
        assert _normalize_role("") == "user"
        assert _normalize_role(None) == "user"


class TestBuildSearchMessages:
    def test_basic_conversion(self):
        from tools.search.utils import build_search_messages
        items = [
            MockChatItem("system", "You are helpful."),
            MockChatItem("user", "hello"),
        ]
        result = build_search_messages(items)
        assert len(result) == 2
        assert result[0] == {"role": "system", "content": "You are helpful."}
        assert result[1] == {"role": "user", "content": "hello"}

    def test_skips_empty_content(self):
        from tools.search.utils import build_search_messages
        items = [MockChatItem("user", ""), MockChatItem("user", "hi")]
        result = build_search_messages(items)
        assert len(result) == 1

    def test_dedup_system_messages(self):
        from tools.search.utils import build_search_messages
        items = [
            MockChatItem("system", "You are helpful."),
            MockChatItem("system", "You are helpful."),
            MockChatItem("user", "hi"),
        ]
        result = build_search_messages(items)
        assert len(result) == 2

    def test_system_first(self):
        from tools.search.utils import build_search_messages
        items = [
            MockChatItem("user", "hi"),
            MockChatItem("system", "Rules"),
            MockChatItem("user", "bye"),
        ]
        result = build_search_messages(items)
        assert result[0]["role"] == "system"

    def test_empty_list(self):
        from tools.search.utils import build_search_messages
        assert build_search_messages([]) == []
