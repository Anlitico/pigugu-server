# agent/context/compression.py
"""ContextCompressor — uses LLM to asynchronously summarize conversation turns.

Three-tier compression:
  Tier 0: All turns raw (no compression)
  Tier 1: Turns [5, 20) compressed into a "recent summary"
  Tier 2: All turns before current window merged into a "global summary"

Each tier frees context window space so the model sees:
  [system] [global_summary?] [recent_summary?] [raw_turn_1..5]
"""

from __future__ import annotations

from core.llm.types import Message


SUMMARIZE_TIER_1 = """\
Summarize the following conversation segment concisely. Focus on:
- Key topics discussed
- User's stated preferences, opinions, or facts about themselves
- The assistant's responses and positions
- Any decisions or conclusions reached

Keep the summary under 200 words. Write in third person, past tense.

CONVERSATION:
{turns_text}

SUMMARY:"""


SUMMARIZE_TIER_2 = """\
Below is an existing summary of a longer conversation, followed by a new segment.
Merge them into a single updated summary of no more than 300 words.

EXISTING SUMMARY:
{existing_summary}

NEW SEGMENT:
{new_segment}

UPDATED SUMMARY:"""


SUMMARIZE_ROAST = """\
Summarize the gameplay segment below. Focus on preserving:
- Character state and actions taken
- Plot progression and key events
- Game state changes (scores, progress, decisions)
- User's in-game choices and reactions

Keep the summary under 250 words. Write in present tense from the player's perspective.

GAMEPLAY:
{turns_text}

SUMMARY:"""


EXTRACT_FACTS = """\
Extract discrete, durable facts about the user from this conversation.
Each fact = one sentence. Assign a category. Only extract what would be useful
for personalizing future interactions.

Categories: personal | preference | health | work | interest | other

GOOD (durable):
  {"fact": "Name is John", "category": "personal"}
  {"fact": "Allergic to peanuts", "category": "health"}
  {"fact": "Prefers dark humor", "category": "preference"}
  {"fact": "Works as a software engineer", "category": "work"}
  {"fact": "Lives in Shanghai", "category": "personal"}

BAD (transient — do NOT extract):
  "Asked about the weather today"
  "Said hello"
  Common knowledge like "The sky is blue"

Return JSON: {{"facts": [{{"fact": "...", "category": "..."}}]}}
If nothing durable: {{"facts": []}}

CONVERSATION:
{turns_text}

FACTS:"""


SUMMARIZE_PROFILE_INITIAL = """\
Generate a concise user profile from the following facts about a person.
Keep it under 150 words. Use third person. This will be injected into an
AI agent's context so it can personalize its responses.

FACTS:
{facts_text}

PROFILE:"""

SUMMARIZE_PROFILE_MERGE = """\
Update the existing user profile below with new facts about the person.
Merge naturally — preserve important details (name, birthday, health info,
preferences). Resolve contradictions by favoring newer information.

EXISTING PROFILE:
{existing_profile}

NEW FACTS:
{new_facts}

UPDATED PROFILE (under 150 words, third person):"""


class ContextCompressor:
    """Async conversation summarizer using the LLM provider pool."""

    def __init__(self, model: str = "qwen-plus"):
        self._model = model

    async def compress_tier_1(self, turns: list[Message]) -> str:
        """Compress a middle segment into a short summary.

        Args:
            turns: List of Message turns to summarize.

        Returns:
            Summary text, or empty string if no turns provided.
        """
        if not turns:
            return ""

        turns_text = "\n".join(
            f"[{t.role}]: {t.content[:500]}"
            for t in turns
        )
        prompt = SUMMARIZE_TIER_1.format(turns_text=turns_text)

        from core.llm import get_llm, Message
        try:
            llm = get_llm(self._model)
            resp = await llm.chat(
                messages=[Message.user(prompt)],
                model=self._model,
                max_tokens=500,
            )
            return resp.content.strip()
        except Exception:
            return ""

    async def compress_tier_2(
        self, existing_summary: str, new_turns: list[Message]
    ) -> str:
        """Merge existing global summary with new turns.

        Args:
            existing_summary: Previous global summary (may be empty).
            new_turns: New Message turns to merge in.

        Returns:
            Updated summary text.
        """
        if not existing_summary and not new_turns:
            return ""
        if not existing_summary:
            return await self.compress_tier_1(new_turns)

        new_segment = "\n".join(
            f"[{t.role}]: {t.content[:500]}"
            for t in new_turns
        )
        prompt = SUMMARIZE_TIER_2.format(
            existing_summary=existing_summary,
            new_segment=new_segment,
        )

        from core.llm import get_llm, Message
        try:
            llm = get_llm(self._model)
            resp = await llm.chat(
                messages=[Message.user(prompt)],
                model=self._model,
                max_tokens=800,
            )
            return resp.content.strip()
        except Exception:
            return existing_summary

    async def compress_roast(
        self, turns: list[Message], *, existing_summary: str = "", roast_prompt: str = ""
    ) -> str:
        """Compress older roast turns. Output includes the roast prompt verbatim.

        Returns: roast_prompt + "\n\n---\n\n" + gameplay_summary.
        This way the LLM context always has the game rules, even after compression.
        """
        result_parts = [roast_prompt] if roast_prompt else []

        if not turns:
            if existing_summary:
                result_parts.append(existing_summary)
            return "\n\n---\n\n".join(p for p in result_parts if p)

        turns_text = "\n".join(
            f"[{t.role}]: {t.content[:500]}"
            for t in turns
        )

        if existing_summary:
            # Strip previous roast_prompt prefix from existing_summary to avoid duplication
            existing_body = existing_summary.split("\n---\n", 1)[-1] if "\n---\n" in existing_summary else existing_summary
            merge_prompt = (
                f"Existing game summary:\n{existing_body}\n\n"
                f"New gameplay:\n{turns_text}\n\n"
                f"Merge into a single summary under 250 words. "
                f"Preserve character state, plot points, and game decisions."
            )
        else:
            merge_prompt = SUMMARIZE_ROAST.format(turns_text=turns_text)

        from core.llm import get_llm, Message
        try:
            llm = get_llm(self._model)
            resp = await llm.chat(
                messages=[Message.user(merge_prompt)],
                model=self._model,
                max_tokens=600,
            )
            body = resp.content.strip()
        except Exception:
            body = existing_summary.split("\n---\n", 1)[-1] if existing_summary and "\n---\n" in existing_summary else existing_summary

        if body:
            result_parts.append(body)
        return "\n\n---\n\n".join(p for p in result_parts if p)

    async def extract_facts(self, turns: list[Message]) -> list[dict]:
        """Layer 1: Extract categorized facts from conversation turns.

        Returns list of {"fact": "...", "category": "..."} dicts.
        Deduplication is handled at the PG layer (UNIQUE constraint).
        """
        if not turns:
            return []

        turns_text = "\n".join(
            f"[{t.role}]: {t.content[:500]}"
            for t in turns
        )
        prompt = EXTRACT_FACTS.format(turns_text=turns_text)

        from core.llm import get_llm, Message
        import json
        try:
            llm = get_llm(self._model)
            resp = await llm.chat(
                messages=[Message.user(prompt)],
                model=self._model,
                max_tokens=500,
            )
            content = resp.content.strip()
            if content.startswith("```"):
                content = content.split("\n", 1)[-1].rsplit("\n```", 1)[0]
            data = json.loads(content)
            return data.get("facts", [])
        except Exception:
            return []

    async def summarize_profile(
        self, facts: list[str], *, existing: str = ""
    ) -> str:
        """Layer 2: Generate/update a plain-text user profile from facts.

        If existing profile is provided, merges incrementally (existing + new facts).
        Otherwise generates from scratch from the full facts list.

        Args:
            facts: New fact strings (e.g. "Name: John (personal)").
            existing: Existing profile_summary for incremental merge.

        Returns:
            A concise narrative profile (under 150 words).
        """
        if not facts and not existing:
            return ""

        from core.llm import get_llm, Message

        if existing:
            new_facts_text = "\n".join(f"- {f}" for f in facts)
            prompt = SUMMARIZE_PROFILE_MERGE.format(
                existing_profile=existing,
                new_facts=new_facts_text,
            )
        else:
            facts_text = "\n".join(f"- {f}" for f in facts)
            prompt = SUMMARIZE_PROFILE_INITIAL.format(facts_text=facts_text)

        try:
            llm = get_llm(self._model)
            resp = await llm.chat(
                messages=[Message.user(prompt)],
                model=self._model,
                max_tokens=400,
            )
            return resp.content.strip()
        except Exception:
            return existing  # on failure, return existing unchanged
