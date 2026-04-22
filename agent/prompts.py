import json

from agent.publisher import get_redis


async def build_system_prompt(device_id: str) -> str:
    redis = await get_redis()

    mood_raw = await redis.get("mood:current")
    mood = json.loads(mood_raw) if mood_raw else {}

    news_raw = await redis.get("news:context:latest")
    headlines = json.loads(news_raw) if news_raw else []

    mood_text = f"Current mood: {mood.get('label', 'neutral')}." if mood else ""
    news_text = (
        "Recent headlines: " + "; ".join(h.get("title", "") for h in headlines)
        if headlines
        else ""
    )

    return f"""You are Pigugu, a witty and opinionated AI companion who loves to debate.
{mood_text}
{news_text}
Engage the user in lively debate. Be provocative but fair."""
