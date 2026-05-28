"""Tool dataclass  -  the core data structure for defining agent tools.

A Tool combines spec (name, description, parameters) with handler (execute)
in one explicit, immutable object. Inspired by Vercel AI SDK's tool() pattern.

No docstring parsing. No decorator magic. No inheritance required.
Every field is hand-written by the tool author for precise control.
"""

from __future__ import annotations

from dataclasses import dataclass

from core.llm.types import ToolSpec
from .executor import ToolHandler


@dataclass(frozen=True)
class Tool:
    """A complete tool definition  -  spec + handler in one explicit object.

    Usage:
        async def search_handler(args: dict) -> dict:
            result = await perplexity_web_search(query=args["query"])
            return {"content": result["content"], "citations": result["citations"]}

        web_search = Tool(
            name="web_search",
            description="Search the web for current information using Perplexity AI.",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The search query"}
                },
                "required": ["query"]
            },
            execute=search_handler,
        )
    """

    name: str
    description: str
    parameters: dict          # JSON Schema  -  hand-written for precise control
    execute: ToolHandler      # Callable[[dict], Any]  -  receives parsed args dict, returns any JSON-serializable value

    @property
    def spec(self) -> ToolSpec:
        """Derive ToolSpec for LLM API calls."""
        return ToolSpec(
            name=self.name,
            description=self.description,
            parameters=self.parameters,
        )
