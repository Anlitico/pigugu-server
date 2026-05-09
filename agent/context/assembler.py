# agent/context/assembler.py
"""
ContextAssembler — dynamically builds the system prompt for every LLM turn.

System prompt structure per tech docs:
  [persona preamble]
  [persona personality prompt]
  [current mood state]
  [news context]
  [game mode instructions]
  [memory summary]
  [ending state / review tone]
  [persona suffix]
"""

from typing import Optional, TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from personas.base import Persona
    from roasts import GameMode
    from models import ConversationState, MoodState, NewsContext
    from memory.store import MemoryStore


class ContextAssembler:
    """Builds the full system prompt before each LLM call.

    Uses a layered approach: each layer appends its section to the prompt.
    Sections are ordered by priority (persona first, turn-specific last).

    Usage:
        assembler = ContextAssembler()
        prompt = await assembler.assemble(
            persona=persona,
            game_mode=game_mode,
            state=conv_state,
            memory=memory_store,
            provider="qwen",
        )
        # Inject prompt into ChatContext as system message
    """

    async def assemble(
        self,
        persona: "Persona",
        game_mode: "GameMode",
        state: "ConversationState",
        memory: "MemoryStore",
        provider: str = "",
    ) -> str:
        """Build the complete system prompt for the upcoming LLM call."""

        parts: list[str] = []

        # Layer 1: Provider-specific preamble (e.g., Grok voice rules)
        preamble = persona.get_preamble()
        if preamble:
            parts.append(preamble)

        # Layer 2: Core personality
        parts.append(persona.personality_prompt)

        # Layer 3: Current mood
        if state.mood:
            parts.append(state.mood.render())
        else:
            parts.append("Mood: Default (dry sarcasm)")

        # Layer 4: News context
        if state.news and state.news.title:
            parts.append(state.news.render())
            parts.append(f"Game Mode: {game_mode.display_name} ({game_mode.mode_id})")

        # Layer 5: Game mode instructions
        mode_prompt = game_mode.system_prompt_extension
        if mode_prompt:
            parts.append(mode_prompt)

        # Layer 6: Memory summary
        user_id = state.user_id or "default"
        memory_summary = memory.get_summary(user_id)
        if memory_summary:
            parts.append(f"## MEMORY\n{memory_summary}")

        # Layer 7: Turn context (how many turns in, what's happening)
        parts.append(
            f"## CURRENT TURN\n"
            f"This is turn {state.turn_count + 1} of the conversation. "
            f"Max turns before ending: {game_mode.get_max_turns()}."
        )

        # Layer 8: Ending state / review tone
        if state.ending.triggered:
            parts.append(state.ending.render())

        # Layer 9: Provider-specific suffix
        suffix = persona.get_suffix()
        if suffix and provider.lower() in {"grok", "xai"}:
            parts.append(suffix)

        full_prompt = "\n\n".join(filter(None, parts))

        logger.debug(
            f"📝 [CONTEXT] Assembled prompt: {len(full_prompt)} chars, "
            f"{len(parts)} layers, turn={state.turn_count}"
        )

        return full_prompt

    def inject_into_chat_ctx(self, prompt: str, chat_ctx) -> None:
        """Replace or add the system message in a LiveKit ChatContext.

        If a system message already exists, replaces its content.
        Otherwise, adds a new system message at the front.
        """
        system_items = [
            i for i in chat_ctx.items
            if getattr(i, "role", None) == "system"
        ]

        if system_items:
            system_items[0].content = [{"type": "text", "text": prompt}]
            for extra in system_items[1:]:
                chat_ctx.items.remove(extra)
        else:
            chat_ctx.add_message(role="system", content=prompt)
