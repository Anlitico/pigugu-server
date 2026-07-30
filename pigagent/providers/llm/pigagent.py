"""PigAgent LLM provider — wraps PigAgent.generate_reply() as LLMProvider."""

from __future__ import annotations

import asyncio
import threading
from typing import Any, Generator

from loguru import logger

from providers.base import LLMProvider


class PigAgentLLM(LLMProvider):
    """Adapts PigAgent's async streaming to the synchronous LLMProvider interface.

    Uses a background thread + asyncio.run_coroutine_threadsafe to bridge
    PigAgent's ``async generate_reply()`` into a synchronous generator.

    Parameters
    ----------
    factory : callable
        ``create_pig_agent(user_id, hw_id) -> PigAgent`` — factory function.
    """

    def __init__(self, factory: Any = None):
        self._factory = factory  # bootstrap.factory.create_pig_agent

    def response(
        self,
        session_id: str,
        dialogue: list[dict],
        **kwargs: Any,
    ) -> Generator[str, None, None]:
        """Synchronous generator yielding text tokens.

        Extracts user text from the last message in ``dialogue``, calls
        PigAgent.generate_reply(), and yields text chunks.

        ``kwargs`` may include:
        - ``persona_id`` (int, default 1)
        - ``interrupt_event`` (asyncio.Event, optional)
        - ``user_id`` (str)
        - ``hw_id`` (str)
        """
        raise NotImplementedError(
            "PigAgentLLM requires async execution. Use response_async() instead."
        )

    async def response_async(
        self,
        user_text: str,
        *,
        user_id: str = "",
        hw_id: str = "",
        persona_id: int = 1,
        session_id: str = "",
        interrupt_event: asyncio.Event | None = None,
    ):
        """Return an async generator yielding text chunks from PigAgent.

        This is the primary API — the synchronous ``response()`` is not used.
        """
        if not user_text.strip():
            return

        if self._factory is None:
            from bootstrap.factory import create_pig_agent

            factory_fn = create_pig_agent
        else:
            factory_fn = self._factory

        pig = await factory_fn(user_id, hw_id=hw_id)
        logger.info(
            f"[PigAgent] generate_reply user={user_id} persona={persona_id}"
        )

        async for chunk in pig.generate_reply(
            user_text.strip(),
            persona_id=persona_id,
            interrupt_event=interrupt_event,
            session_id=session_id,
        ):
            if isinstance(chunk, str):
                yield chunk
