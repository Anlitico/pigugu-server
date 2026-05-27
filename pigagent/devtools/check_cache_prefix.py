"""Verify KV cache prefix stability — simulate real agent conversation."""
import asyncio, os, sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from dotenv import load_dotenv, find_dotenv

load_dotenv(find_dotenv())

from context.manager import ContextManager
from core.llm.types import Message


def _prefix_key(msgs: list[Message]) -> list[str]:
    """Build stable prefix key: role:first_30_chars for each message."""
    return [f"{m.role}:{(m.content or '')[:40].replace(chr(10), ' ')}" for m in msgs]


async def main(user_id: str, rounds: int = 10):
    import redis.asyncio as redis_client

    r = redis_client.from_url("redis://localhost:6379/0")
    pg_pool = "postgresql://pigugu:pigugu@localhost:5432/pigugu"
    ctx = ContextManager(redis_client=r, pg_pool=pg_pool)

    prev_key = None
    prev_n = None

    for i in range(rounds):
        # Simulate user + assistant turn (as agent does in _persist_turns)
        await ctx.add_turn(user_id=user_id, role="user", content=f"Test question number {i}")
        await ctx.add_turn(user_id=user_id, role="assistant", content=f"Test answer number {i}")

        # Now load context — same as what agent sends to LLM next turn
        wc = await ctx.assemble(user_id)
        msgs = wc.to_messages()
        key = _prefix_key(msgs)
        n = len(msgs)

        if prev_key is None:
            prev_key = key
            prev_n = n
            continue

        matches = 0
        for j in range(min(len(prev_key), len(key))):
            if prev_key[j] == key[j]:
                matches += 1
            else:
                break

        prefix_ok = matches == len(prev_key)
        status = "STABLE OK" if prefix_ok else f"BROKEN at [{matches}]"
        print(f"Turn {i:2d}: {prev_n}->{n} msgs  prefix={matches}/{len(prev_key)}  {status}")
        if not prefix_ok:
            print(f"  OLD[{matches}]: {prev_key[matches]}")
            print(f"  NEW[{matches}]: {key[matches]}")
            # Show 3 more context lines
            for k in range(matches + 1, min(matches + 4, max(len(prev_key), len(key)))):
                if k < len(prev_key):
                    print(f"  OLD[{k}]: {prev_key[k]}")
                if k < len(key):
                    print(f"  NEW[{k}]: {key[k]}")

        prev_key = key
        prev_n = n

    await r.aclose()


if __name__ == "__main__":
    uid = sys.argv[1] if len(sys.argv) > 1 else "cache-test-user"
    asyncio.run(main(uid))
