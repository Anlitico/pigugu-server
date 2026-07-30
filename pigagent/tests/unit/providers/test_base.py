"""Tests for provider abstract base classes."""

import pytest

from providers.base import VADProvider, STTProvider, TTSProvider, LLMProvider


class TestVADProvider:
    def test_is_abstract(self):
        with pytest.raises(TypeError):
            VADProvider()  # type: ignore[abstract]

    def test_concrete_subclass(self):
        class MyVAD(VADProvider):
            def is_vad(self, conn, pcm_frame):
                return len(pcm_frame) > 0

        vad = MyVAD()
        assert vad.is_vad(None, b"\x00\x00") is True
        assert vad.is_vad(None, b"") is False


class TestSTTProvider:
    def test_is_abstract(self):
        with pytest.raises(TypeError):
            STTProvider()  # type: ignore[abstract]

    def test_concrete_subclass(self):
        class MySTT(STTProvider):
            async def transcribe(self, pcm: bytes) -> str:
                return "hello"

        import asyncio

        stt = MySTT()
        result = asyncio.run(stt.transcribe(b"fake"))
        assert result == "hello"


class TestTTSProvider:
    def test_is_abstract(self):
        with pytest.raises(TypeError):
            TTSProvider()  # type: ignore[abstract]

    def test_concrete_subclass(self):
        class MyTTS(TTSProvider):
            async def synthesize(self, text: str, raw_pcm: bool = False) -> list[bytes]:
                return [b"opus_frame"]

        import asyncio

        tts = MyTTS()
        result = asyncio.run(tts.synthesize("hi"))
        assert result == [b"opus_frame"]


class TestLLMProvider:
    def test_is_abstract(self):
        with pytest.raises(TypeError):
            LLMProvider()  # type: ignore[abstract]

    def test_concrete_subclass(self):
        class MyLLM(LLMProvider):
            def response(self, session_id, dialogue, **kwargs):
                for word in ["hello", "world"]:
                    yield word

        llm = MyLLM()
        tokens = list(llm.response("s1", [{"role": "user", "content": "hi"}]))
        assert tokens == ["hello", "world"]
