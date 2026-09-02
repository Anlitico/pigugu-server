"""STT provider plugin selection + AssemblyAI provider unit tests."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from providers.stt import create_stt_provider
from providers.stt.assemblyai import _build_url, AssemblyAISttProvider
from providers.stt.deepgram import DeepgramSTT


def _fake_config(provider: str):
    return SimpleNamespace(
        STT_PROVIDER=provider,
        ASSEMBLYAI_STT_MODEL="u3-rt-pro",
        ASSEMBLYAI_STT_SAMPLE_RATE=16000,
        ASSEMBLYAI_MIN_TURN_SILENCE=300,
        ASSEMBLYAI_MAX_TURN_SILENCE=1500,
    )


def test_factory_defaults_to_deepgram(monkeypatch):
    monkeypatch.setattr("agent_config.get_config", lambda: _fake_config("deepgram"))
    assert isinstance(create_stt_provider(), DeepgramSTT)


def test_factory_selects_assemblyai(monkeypatch):
    monkeypatch.setattr("agent_config.get_config", lambda: _fake_config("assemblyai"))
    provider = create_stt_provider()
    assert isinstance(provider, AssemblyAISttProvider)
    assert provider._model == "u3-rt-pro"
    assert provider._min_turn_silence == 300
    assert provider._max_turn_silence == 1500


def test_factory_forwards_vocabulary_keyterms_to_deepgram(monkeypatch):
    monkeypatch.setattr("agent_config.get_config", lambda: _fake_config("deepgram"))
    monkeypatch.setattr("providers.stt.stt_keyterms", lambda: ["Pigugu", "Trump"])
    provider = create_stt_provider()
    assert provider._keyterms == ["Pigugu", "Trump"]


def test_factory_forwards_vocabulary_keyterms_to_assemblyai(monkeypatch):
    monkeypatch.setattr("agent_config.get_config", lambda: _fake_config("assemblyai"))
    monkeypatch.setattr("providers.stt.stt_keyterms", lambda: ["Pigugu", "Trump"])
    provider = create_stt_provider()
    assert provider._keyterms == ["Pigugu", "Trump"]


def test_factory_rejects_unknown_provider(monkeypatch):
    monkeypatch.setattr("agent_config.get_config", lambda: _fake_config("nope"))
    with pytest.raises(ValueError):
        create_stt_provider()


def test_deepgram_turn_end_signal_is_vad():
    assert DeepgramSTT.turn_end_signal == "vad"


def test_assemblyai_turn_end_signal_is_external():
    assert AssemblyAISttProvider.turn_end_signal == "external"


def test_assemblyai_build_url_params():
    url = _build_url(
        "wss://streaming.assemblyai.com/v3/ws",
        {
            "sample_rate": 16000,
            "encoding": "pcm_s16le",
            "speech_model": "u3-rt-pro",
            "min_turn_silence": 300,
            "max_turn_silence": 1500,
            "agent_context": "some previous context",
        },
    )
    assert url.startswith("wss://streaming.assemblyai.com/v3/ws?")
    assert "speech_model=u3-rt-pro" in url
    assert "sample_rate=16000" in url
    assert "min_turn_silence=300" in url
    assert "max_turn_silence=1500" in url
    assert "agent_context=some+previous+context" in url


def test_assemblyai_build_url_serializes_keyterms_prompt_as_json_array():
    from urllib.parse import parse_qs

    # keyterms_prompt is ONE JSON-array string — repeated keys are rejected by
    # AssemblyAI (server error 3006 "Invalid JSON array").
    url = _build_url(
        "wss://streaming.assemblyai.com/v3/ws",
        {"speech_model": "u3-rt-pro", "keyterms_prompt": '["Pigugu", "Trump"]'},
    )
    assert url.count("keyterms_prompt=") == 1
    qs = parse_qs(url.split("?", 1)[1])
    assert qs["keyterms_prompt"] == ['["Pigugu", "Trump"]']


def test_assemblyai_open_injects_keyterms_prompt(monkeypatch):
    captured = {}

    class _FakeSession:
        def __init__(self, *a, **k):
            pass

        async def ws_connect(self, *a, **k):
            return SimpleNamespace()

        async def close(self):
            pass

    async def _fake_receive_loop(conn, ws, session):
        pass

    monkeypatch.setattr("aiohttp.ClientSession", _FakeSession)
    monkeypatch.setattr(AssemblyAISttProvider, "_receive_loop", staticmethod(_fake_receive_loop))
    monkeypatch.setattr(
        "providers.stt.assemblyai._build_url",
        lambda base, params: captured.update(params=params) or "ws://fake",
    )
    monkeypatch.setenv("ASSEMBLYAI_API_KEY", "x")
    provider = AssemblyAISttProvider(keyterms=["Pigugu", "Trump"])
    conn = SimpleNamespace(_stt_open=False)
    asyncio.run(provider.open_audio_channels(conn))
    assert conn._stt_open is True
    assert captured["params"]["keyterms_prompt"] == json.dumps(["Pigugu", "Trump"])


def test_assemblyai_open_omits_keyterms_when_empty(monkeypatch):
    captured = {}

    class _FakeSession:
        def __init__(self, *a, **k):
            pass

        async def ws_connect(self, *a, **k):
            return SimpleNamespace()

        async def close(self):
            pass

    async def _fake_receive_loop(conn, ws, session):
        pass

    monkeypatch.setattr("aiohttp.ClientSession", _FakeSession)
    monkeypatch.setattr(AssemblyAISttProvider, "_receive_loop", staticmethod(_fake_receive_loop))
    monkeypatch.setattr(
        "providers.stt.assemblyai._build_url",
        lambda base, params: captured.update(params=params) or "ws://fake",
    )
    monkeypatch.setenv("ASSEMBLYAI_API_KEY", "x")
    provider = AssemblyAISttProvider()
    conn = SimpleNamespace(_stt_open=False)
    asyncio.run(provider.open_audio_channels(conn))
    assert "keyterms_prompt" not in captured["params"]


def test_assemblyai_provider_no_api_key_logs_and_stays_closed(monkeypatch):
    monkeypatch.setenv("ASSEMBLYAI_API_KEY", "")
    provider = AssemblyAISttProvider()
    conn = SimpleNamespace()
    assert provider.is_open(conn) is False


def test_assemblyai_connect_failure_marks_attempted_and_clears_socket(monkeypatch):
    async def fake_connect(*a, **k):
        raise ConnectionError("boom")

    monkeypatch.setattr("aiohttp.ClientSession.ws_connect", fake_connect)
    monkeypatch.setenv("ASSEMBLYAI_API_KEY", "x")
    provider = AssemblyAISttProvider()
    conn = SimpleNamespace(_stt_open=False, _aai_ws="old-ws", _aai_http="old-http")
    asyncio.run(provider.open_audio_channels(conn))
    assert conn._stt_open is True  # marked attempted — no per-frame retry
    assert conn._aai_ws is None  # stale socket cleared — no send-to-dead-ws
    assert conn._aai_http is None


def test_assemblyai_reconnect_backoff_suppresses_storm(monkeypatch):
    async def fake_connect(*a, **k):
        raise ConnectionError("boom")

    monkeypatch.setattr("aiohttp.ClientSession.ws_connect", fake_connect)
    monkeypatch.setenv("ASSEMBLYAI_API_KEY", "x")
    provider = AssemblyAISttProvider()
    conn = SimpleNamespace(_stt_open=False)
    asyncio.run(provider.open_audio_channels(conn))
    assert conn._stt_open is True
    first_attempt = conn._aai_open_attempt_at
    # Simulate the receive-loop finally clearing the flag, then an immediate
    # per-frame retry — the backoff must suppress it (no new attempt).
    conn._stt_open = False
    asyncio.run(provider.open_audio_channels(conn))
    assert conn._stt_open is False
    assert conn._aai_open_attempt_at == first_attempt


def test_assemblyai_dispatch_end_of_turn_schedules_final_and_stop(monkeypatch):
    import asyncio

    class FakeConn:
        async def _on_stt_final(self, text):
            pass

        async def _on_stt_interim(self, text):
            pass

        async def _on_utterance_end(self):
            pass

    scheduled = []
    monkeypatch.setattr(
        AssemblyAISttProvider, "_schedule", staticmethod(lambda conn, coro: scheduled.append(coro))
    )
    provider = AssemblyAISttProvider()
    conn = FakeConn()

    provider._dispatch(conn, {"type": "Turn", "transcript": "Hello, are you there?", "end_of_turn": True})
    assert len(scheduled) == 2
    assert all(asyncio.iscoroutine(c) for c in scheduled)  # final + utterance_end

    scheduled.clear()
    provider._dispatch(conn, {"type": "Turn", "transcript": "Hello", "end_of_turn": False})
    assert len(scheduled) == 1  # interim only
    scheduled[0].close()

    scheduled.clear()
    provider._dispatch(conn, {"type": "SpeechStarted"})
    assert scheduled == []


def test_assemblyai_update_context_caches_and_sends_when_ws_open():
    sent = []

    class FakeWs:
        async def send_str(self, s):
            sent.append(s)

    provider = AssemblyAISttProvider()
    conn = SimpleNamespace(_aai_ws=FakeWs())
    asyncio.run(provider.update_context(conn, "What's your email address?"))
    assert conn._aai_agent_context == "What's your email address?"
    assert len(sent) == 1
    import json as _json

    payload = _json.loads(sent[0])
    assert payload == {"type": "UpdateConfiguration", "agent_context": "What's your email address?"}


def test_assemblyai_update_context_without_ws_just_caches():
    provider = AssemblyAISttProvider()
    conn = SimpleNamespace(_aai_ws=None)
    asyncio.run(provider.update_context(conn, "hello"))
    assert conn._aai_agent_context == "hello"


def test_assemblyai_update_context_uses_conn_not_shared_instance():
    # The provider is a shared singleton — context must be per-connection.
    provider = AssemblyAISttProvider()
    conn_a = SimpleNamespace(_aai_ws=None)
    conn_b = SimpleNamespace(_aai_ws=None)
    asyncio.run(provider.update_context(conn_a, "context for A"))
    assert conn_a._aai_agent_context == "context for A"
    assert not hasattr(conn_b, "_aai_agent_context")


def test_bridge_push_context_inert_for_non_context_provider():
    from voice.pipecat.stt_bridge import PiguguSttBridge

    bridge = PiguguSttBridge(DeepgramSTT())
    asyncio.run(bridge.push_context("the agent just said something"))
    # Deepgram does not support context — nothing should be stored/forwarded.
    assert bridge._last_context == ""


def test_bridge_push_context_trims_to_trailing_for_assemblyai():
    from voice.pipecat.stt_bridge import PiguguSttBridge

    provider = AssemblyAISttProvider()
    bridge = PiguguSttBridge(provider)
    long = "A" * 5000
    asyncio.run(bridge.push_context(long))
    # 1750-char cap, trailing kept.
    assert len(bridge._last_context) == provider.max_context_chars
    assert bridge._last_context == "A" * 1750
    assert bridge._aai_agent_context == "A" * 1750


def test_bridge_context_loader_seeds_on_stream_open(monkeypatch):
    from voice.pipecat.stt_bridge import PiguguSttBridge

    monkeypatch.setenv("ASSEMBLYAI_API_KEY", "")  # no WS connect attempted

    async def loader():
        return "What's your email address?"

    provider = AssemblyAISttProvider()
    bridge = PiguguSttBridge(provider, context_loader=loader)
    asyncio.run(bridge._feed(b"\x00" * 320))  # first frame → open + seed
    assert bridge._last_context == "What's your email address?"
    # The provider cached it on this connection (the bridge IS the conn).
    assert bridge._aai_agent_context == "What's your email address?"


def test_bridge_in_session_context_beats_loader(monkeypatch):
    from voice.pipecat.stt_bridge import PiguguSttBridge

    monkeypatch.setenv("ASSEMBLYAI_API_KEY", "")

    async def loader():
        return "STALE history reply"

    provider = AssemblyAISttProvider()
    bridge = PiguguSttBridge(provider, context_loader=loader)
    asyncio.run(bridge.push_context("fresh in-session reply"))
    asyncio.run(bridge._feed(b"\x00" * 320))
    # In-session context wins; the loader is not consulted.
    assert bridge._last_context == "fresh in-session reply"
    assert bridge._aai_agent_context == "fresh in-session reply"


# ── keyterms (proper-noun prompting) ─────────────────────────────────


def test_deepgram_connect_passes_keyterm_list(monkeypatch):
    captured = {}

    class _FakeCtx:
        def __enter__(self):
            return SimpleNamespace(on=lambda *a, **k: None, start_listening=lambda: None)

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        class listen:
            class v1:
                @staticmethod
                def connect(**kwargs):
                    captured.update(kwargs)
                    return _FakeCtx()

    monkeypatch.setattr("providers.stt.deepgram.DeepgramClient", _FakeClient)
    monkeypatch.setenv("DEEPGRAM_API_KEY", "x")
    provider = DeepgramSTT(keyterms=["Pigugu", "Trump"])
    conn = SimpleNamespace()
    asyncio.run(provider.open_audio_channels(conn))
    assert conn._stt_open is True
    assert captured["keyterm"] == ["Pigugu", "Trump"]


def test_deepgram_connect_omits_keyterm_when_empty(monkeypatch):
    captured = {}

    class _FakeCtx:
        def __enter__(self):
            return SimpleNamespace(on=lambda *a, **k: None, start_listening=lambda: None)

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        class listen:
            class v1:
                @staticmethod
                def connect(**kwargs):
                    captured.update(kwargs)
                    return _FakeCtx()

    monkeypatch.setattr("providers.stt.deepgram.DeepgramClient", _FakeClient)
    monkeypatch.setenv("DEEPGRAM_API_KEY", "x")
    provider = DeepgramSTT()
    conn = SimpleNamespace()
    asyncio.run(provider.open_audio_channels(conn))
    assert captured["keyterm"] is None


def test_deepgram_rest_transcribe_url_includes_keyterms(monkeypatch):
    class _FakeResp:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def json(self):
            return {"results": {"channels": [{"alternatives": [{"transcript": "hi"}]}]}}

    class _FakeSession:
        def __init__(self):
            self.posted_urls = []

        def post(self, url, **kwargs):
            self.posted_urls.append(url)
            return _FakeResp()

    session = _FakeSession()

    async def _fake_ensure_http(self):
        return session

    monkeypatch.setattr(DeepgramSTT, "_ensure_http", _fake_ensure_http)
    monkeypatch.setenv("DEEPGRAM_API_KEY", "x")
    provider = DeepgramSTT(keyterms=["Pigugu", "Trump"])
    asyncio.run(provider.transcribe(bytes(1600)))
    url = session.posted_urls[0]
    assert "keyterm=Pigugu" in url
    assert "keyterm=Trump" in url
