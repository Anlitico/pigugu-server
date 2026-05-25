"""ToolRegistry — collects Tool instances and provides tools/tool_handlers lists."""

from __future__ import annotations

from .executor import ToolHandler
from .tool import Tool
from core.llm.types import ToolSpec


class ToolRegistry:
    """Collects Tool instances for PigAgent's RunnerConfig."""

    def __init__(self) -> None:
        self._items: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """Register a Tool instance. Overwrites if name already exists."""
        self._items[tool.name] = tool

    def register_many(self, tools: list[Tool]) -> None:
        """Register multiple Tool instances at once."""
        for tool in tools:
            self._items[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        """Look up a registered tool by name."""
        return self._items.get(name)

    @property
    def tools(self) -> list[ToolSpec]:
        """All registered tools as ToolSpec list (for LLM API calls)."""
        return [tool.spec for tool in self._items.values()]

    @property
    def tool_handlers(self) -> dict[str, ToolHandler]:
        """Name → handler mapping (for ToolExecutor)."""
        return {tool.name: tool.execute for tool in self._items.values()}

    def __len__(self) -> int:
        return len(self._items)

    def __contains__(self, name: str) -> bool:
        return name in self._items
