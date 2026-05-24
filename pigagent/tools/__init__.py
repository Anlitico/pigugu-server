"""tools — pigugu agent tool definitions.

The Tool and ToolRegistry classes live in core.agent (framework layer).
This module only contains concrete tool instances — each tool is a file
that defines a Tool(...) object.

Registration into a ToolRegistry happens in the bootstrap layer, not here.
"""

from core.agent import Tool, ToolRegistry

from tools.web_search import create_web_search_tool
from tools.volume import volume_tool

__all__ = ["Tool", "ToolRegistry", "create_web_search_tool", "volume_tool"]
