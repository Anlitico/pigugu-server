async def score_conversation(transcript: str, device_id: str) -> tuple[str, int]:
    """Analyse transcript, return (outcome, score_delta).

    outcome: 'win' | 'lose' | 'draw'
    score_delta: points to add/subtract from credibility score
    """
    ...


async def persist_score(conversation_id: str, outcome: str, score_delta: int) -> None:
    ...
