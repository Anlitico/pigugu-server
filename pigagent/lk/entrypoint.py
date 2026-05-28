# pigagent/lk/entrypoint.py
"""CLI entry point for the Pigugu voice agent."""

import os

from loguru import logger

from livekit.agents import WorkerOptions, cli
from config import get_config
from bootstrap.factory import validate_configuration
from lk.session import run as session_run

config = get_config()


def main() -> None:
    """Validate config and start LiveKit workers."""
    logger.info("=" * 70)
    logger.info("Pigugu Voice Agent")
    logger.info("=" * 70)
    logger.info(f"LiveKit URL: {config.LIVEKIT_URL}")
    logger.info(f"LLM: {config.QWEN_MODEL}")
    logger.info(f"TTS: Cartesia {config.CARTESIA_TTS_MODEL}")
    logger.info(f"Workers: {config.AGENT_WORKERS}")
    logger.info("=" * 70)

    if not validate_configuration():
        logger.error("Configuration errors  -  please fix and try again.")
        exit(1)

    # Pre-load VAD on main thread before worker threads start
    from bootstrap.factory import get_vad
    get_vad()

    logger.info("Configuration validated, starting workers...")

    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=session_run,
            agent_name="pigugu-agent",
            api_key=os.getenv("LIVEKIT_API_KEY"),
            api_secret=os.getenv("LIVEKIT_API_SECRET"),
            ws_url=config.LIVEKIT_URL,
        )
    )


if __name__ == "__main__":
    main()
