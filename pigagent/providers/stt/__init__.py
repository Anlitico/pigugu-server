"""STT providers — plugin selection keyed on the STT_PROVIDER config.

Providers stay pluggable: Deepgram and AssemblyAI coexist, and the active one
is chosen at runtime by ``create_stt_provider()`` (env ``STT_PROVIDER`` =
"deepgram" | "assemblyai"). Both implement the same ``STTProvider`` contract
(``providers/base.py``) and are consumed by the same ``PiguguSttBridge``.
"""

from providers.stt.deepgram import DeepgramSTT
from vocabulary import stt_keyterms

__all__ = ["DeepgramSTT", "AssemblyAISttProvider", "create_stt_provider"]


def create_stt_provider():
    """Instantiate the configured STT provider (deepgram | assemblyai)."""
    from agent_config import get_config

    provider = get_config().STT_PROVIDER
    keyterms = stt_keyterms()
    if provider == "assemblyai":
        from providers.stt.assemblyai import AssemblyAISttProvider

        cfg = get_config()
        return AssemblyAISttProvider(
            model=cfg.ASSEMBLYAI_STT_MODEL,
            sample_rate=cfg.ASSEMBLYAI_STT_SAMPLE_RATE,
            min_turn_silence=cfg.ASSEMBLYAI_MIN_TURN_SILENCE,
            max_turn_silence=cfg.ASSEMBLYAI_MAX_TURN_SILENCE,
            keyterms=keyterms,
        )
    if provider == "deepgram":
        return DeepgramSTT(keyterms=keyterms)
    raise ValueError(f"unknown STT_PROVIDER: {provider!r}")
