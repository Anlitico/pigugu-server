# pigagent/context/__init__.py
"""Context pipeline — 4-layer agent context with compression and extraction.

ContextManager is the main entry point:
    ctx = ContextManager(redis_client=redis, pg_pool=pg)
    messages = await ctx.load(user_id="u1")
    await ctx.add_turn(user_id="u1", role="assistant", content="...")
"""

from .schema import WorkingContext, UserMemory, TokenBudget, RoastContext
from .storage.redis import RedisKeys
from .snapshot import ContextSnapshot
from .manager import ContextManager
from .roast import RoastState

__all__ = [
    "WorkingContext", "UserMemory", "RedisKeys",
    "ContextSnapshot", "ContextManager",
    "RoastState",
]
