"""Unit tests for the observability layer (scope / registry / exporter).

Covers the non-blocking contract:
- emit is pure in-memory (Scope),
- CH row shape matches the 0004 DDL,
- the exporter batches rows and drops-oldest when its queue overflows,
- the registry flushes the previous turn scope when a new one opens.
No real ClickHouse is touched — sinks are stubbed or disabled.
"""

import asyncio

import pytest

from metrics import exporter as exporter_mod
from metrics import registry
from metrics import render
from metrics.exporter import MetricsExporter
from metrics.scope import Scope, TurnScope


# ── Scope: pure record, finished scopes stop writing ──────────────────


def test_scope_mark_finish_ignores_writes():
    s = Scope(user_id="u1", persona_id=1)
    s.mark("a")
    s.set_meta("llm_model", "qwen")
    s.finish()
    s.mark("b")  # after finish — must be ignored
    assert "a" in s.marks and "b" not in s.marks
    assert s.meta["llm_model"] == "qwen"
    assert s.finished


# ── Render: CH rows match the 0004 DDL columns ───────────────────────


def test_render_turn_row_shape():
    s = TurnScope(user_id="u1", persona_id=1, turn_id=7)
    # Controlled perf_counter times so stt_tail / e2e_perceived are exact.
    s.set_mark("vad_end", 1.0)      # device-reported user stop
    s.set_mark("stt_final", 1.4)    # server has the sentence
    s.set_mark("agent_spk", 1.9)    # first audio sent
    s.set_meta("turn_phase", "follow_up")
    s.set_meta("llm_model", "qwen")
    table, columns, values = s.ch_row()
    assert table == "metrics.turn_latency"
    assert columns == render.TURN_COLUMNS
    # ts_ms anchor = agent_spk unix stamp
    assert values[0] == s.event_unix_ms["agent_spk"]
    assert values[1] == "u1"
    assert values[2] == 7
    assert values[3] == 1
    assert values[4] == "follow_up"
    import json
    segs = json.loads(values[6])
    assert segs["e2e"]["role"] == "main"
    meta = json.loads(values[7])
    assert meta["llm_model"] == "qwen"
    # stt_tail = user_stop->stt_final = 400ms; e2e_perceived = user_stop->spk = 900ms
    assert values[8] == 400
    assert values[9] == 900


def test_render_e2e_perceived_falls_back_without_device():
    s = TurnScope(user_id="u1", persona_id=1)
    s.set_mark("stt_final", 1.0)   # no vad_end — legacy/old-firmware path
    s.set_mark("agent_spk", 1.3)
    table, columns, values = s.ch_row()
    assert values[8] == 0                       # no user_stop anchor
    assert values[9] == 300                     # falls back to server E2E


def test_render_session_row_shape():
    from metrics.scope import SessionScope
    s = SessionScope(user_id="u1", device_id="d1", session_id="s1")
    # fake perf anchors: accept at t0, hello +0.1s, first audio +0.25s
    s.set_meta("accept_pc", 100.0)
    s.set_meta("hello_pc", 100.1)
    s.set_meta("first_audio_pc", 100.25)
    table, columns, values = s.ch_row()
    assert table == "metrics.session"
    assert columns == render.SESSION_COLUMNS
    assert values[2] == "d1" and values[3] == "s1"
    assert values[4] == 100   # connect_hello_ms
    assert values[5] == 250   # connect_first_audio_ms


def test_render_compression_row_shape():
    from metrics.scope import CompressionScope
    s = CompressionScope(user_id="u1", scenario="free_chat")
    s.mark("start")
    s.mark("end")
    table, columns, values = s.ch_row()
    assert table == "metrics.compression"
    assert columns == render.COMPRESSION_COLUMNS
    assert values[2] == "free_chat"
    assert "total" in values[3]  # segments json


# ── Exporter: bounded queue, drop-oldest, single task, no-op when off ──


@pytest.mark.asyncio
async def test_exporter_batch_insert(monkeypatch):
    ex = MetricsExporter(enabled=True, dsn="clickhouse://u:p@h:9000/db")
    inserted = []

    async def fake_insert(table, columns, rows):
        inserted.append((table, len(rows)))

    monkeypatch.setattr(ex, "_insert", fake_insert)
    row = ("metrics.turn_latency", render.TURN_COLUMNS, (0, "u", 1, 1, "", "{}", "{}", "{}"))
    for _ in range(3):
        ex.submit(row)
    assert ex.enqueued == 3
    # Consumer task runs on the loop; give it a tick to drain.
    await asyncio.sleep(0.3)
    assert inserted, "exporter task should have drained the queue"
    assert sum(n for _, n in inserted) == 3
    ex.stop()


@pytest.mark.asyncio
async def test_exporter_drop_oldest_when_full():
    ex = MetricsExporter(enabled=True, dsn="clickhouse://u:p@h:9000/db", queue_max=2)
    row = ("metrics.turn_latency", render.TURN_COLUMNS, (0, "u", 1, 1, "", "{}", "{}", "{}"))
    ex.submit(row)
    ex.submit(row)
    ex.submit(row)  # 3rd pushes oldest out
    assert ex.dropped == 1
    assert len(ex._pending) == 2
    ex.stop()


def test_exporter_disabled_is_noop():
    ex = MetricsExporter(enabled=False, dsn="")
    assert ex.submit(("t", ("a",), (1,))) is False
    assert ex.enqueued == 0


@pytest.mark.asyncio
async def test_exporter_insert_connect_timeout_is_bounded(monkeypatch):
    """A connect that never establishes must not stall the consumer task:
    the whole connect+cursor sits inside wait_for, so the insert is aborted
    (counted as failed, nothing written) instead of hanging forever."""
    ex = MetricsExporter(enabled=True, dsn="clickhouse://u:p@h:9000/db")

    class HangingConn:
        async def __aenter__(self):
            await asyncio.sleep(3600)  # TCP connect that never completes

        async def __aexit__(self, *exc):
            return None

    monkeypatch.setattr(exporter_mod, "_INSERT_TIMEOUT_S", 0.05)
    monkeypatch.setattr("asynch.connect", lambda *a, **k: HangingConn())
    await asyncio.wait_for(
        ex._insert("metrics.turn_latency", ("a",), [(1,)]),
        timeout=2.0,
    )
    assert ex.failed == 1 and ex.written == 0


# ── Registry: opening a turn flushes the previous one ─────────────────


def test_registry_open_flushes_prior(monkeypatch):
    flushed: list = []
    monkeypatch.setattr(registry, "enqueue", lambda scope: flushed.append(scope))
    s1 = registry.open(user_id="u1", persona_id=1)
    s1.mark("vad_start")
    s1.mark("stt_final")  # a real sentence — meaningful enough to export
    s2 = registry.open(user_id="u1", persona_id=1)
    s2.mark("stt_final")  # so the final flush exports it too
    assert len(flushed) == 1 and flushed[0] is s1
    assert s1.finished
    assert registry.current() is s2
    registry.flush_current()
    assert len(flushed) == 2 and flushed[1] is s2
    assert registry.current() is None


def test_registry_finish_current_does_not_flush(monkeypatch):
    flushed: list = []
    monkeypatch.setattr(registry, "enqueue", lambda scope: flushed.append(scope))
    s = registry.open(user_id="u1", persona_id=1)
    s.mark("stt_final")
    registry.finish_current()  # child-task freeze only
    assert flushed == []
    assert s.finished
    registry.flush_current()
    assert flushed == [s]


def test_registry_drops_phantom_turn(monkeypatch):
    """A VAD window with no sentence and no reply is not exported (no empty
    CH row): it only ever got vad_start/vad_end before being closed."""
    flushed: list = []
    monkeypatch.setattr(registry, "enqueue", lambda scope: flushed.append(scope))
    s = registry.open(user_id="u1", persona_id=1)
    s.mark("vad_start")  # wake burst / noise blip — never reaches stt_final
    s.mark("vad_end")
    assert not s.meaningful
    registry.flush_current()
    assert flushed == []  # dropped, not enqueued
    assert s.finished


def test_enqueue_never_double_submits(monkeypatch):
    """A scope already handed to the exporter is ignored on a second flush
    (racing owner tasks must not duplicate the CH row)."""
    class FakeScope(TurnScope):
        def ch_row(self):
            return ("t", ("a",), (1,))

    s = FakeScope(user_id="u1", persona_id=1)
    calls = []
    monkeypatch.setattr(exporter_mod.exporter, "_enabled", True)
    monkeypatch.setattr(exporter_mod.exporter, "submit", lambda row: calls.append(row) or True)
    assert exporter_mod.enqueue(s)
    assert exporter_mod.enqueue(s) is True  # second call — no-op
    assert len(calls) == 1
