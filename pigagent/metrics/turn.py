# pigagent/metrics/turn.py
"""Per-turn latency collector — async-safe singleton for conversation turns.

Usage (any module, zero setup):

    from metrics.turn import TurnMetrics

    TurnMetrics.start_turn(user_id="web-xxx", persona_id=1)
    TurnMetrics.mark("vad_start")
    TurnMetrics.set_meta("llm_model", "qwen-plus")
    TurnMetrics.finish_turn()
"""

from __future__ import annotations

import asyncio
import contextvars
import os
import time
from typing import Any

from loguru import logger

_PG_DSN: str = os.getenv("DATABASE_URL", "").replace("+asyncpg", "")

# ── Segment breakdown ──────────────────────────────────────────────
#
# E2E (server-perspective) = server_received_vad_at → agent_spk
#
# 起点是服务器实际收到固件 vad_silence 消息的时刻(time.perf_counter),
# 不再用 user_stop_age_ms 反推 —— 那样会少算设备到服务器的上行网络时延。
# 旧反推出来的 vad_end 仍保留为 mark,作为"用户停嘴"的诊断参考。
#
# ``agent_spk`` 是服务器向设备发出第一个 TTS 音频帧的时刻。设备从收到
# 第一个包到真正播出来还有一段播放时延,单独记在 device_playback_ms。
#
# 时间基准(双轨制):
#   - perf_counter 浮点:用于算 segment/E2E 间隔,单调,纳秒级,不跳
#   - event_unix_ms 整型:每个 mark 同步记一个 UTC 毫秒时间戳,
#     用于跨系统比对、人眼可读、跟日志对账。time.time() 本就 UTC,
#     不存在时区问题。
#
#   ── E2E 加和链（严格串行,sum == E2E,role="main"）────────
#   stt          | stt_final - server_received_vad_at | 收到停嘴消息 → STT 出最终结果
#   agent_init   | agent_init - stt_final    | Lazy PigAgent/context 创建(仅首轮)
#   orchestrator | agent_req - (agent_init|stt_final) | Bridge setup → generate_reply
#   context      | ctx_done - agent_req      | Context load + system prompt + roast
#   llm_prep     | llm_req - ctx_done        | Pre-LLM 装配
#   llm_ttft     | llm_first_token - llm_req | LLM time to first token
#   llm_to_tts   | tts_first_ready - llm_first_token | First token → first TTS frame ready
#   tts_ttfb     | agent_spk - tts_first_ready       | First TTS frame ready → first audio frame sent
#
# ── 诊断段（不参与 E2E 加和,role="diagnostic",可能与其它段重叠/为负）─
#   vad          | vad_end - vad_start       | 用户语音时长(含 VAD 静音)
#   server_vad   | stt_commit - vad_end      | 服务端 Silero VAD 确认停嘴 → STT commit
#   llm_rest     | llm_end - llm_first_token | LLM 剩余输出(并行 TTS)
#   tts          | tts_end - tts_start       | TTS 合成时长
#   vad_to_recv  | server_received_vad_at - vad_end | 设备停嘴 → 服务器收到(上行网络时延)
#
# 主链路和诊断段在写入 DB 时分别带 role 字段,下游不能 sum(segments) 直接
# 当作 E2E 拆解;只能 sum(role=="main")。非主链路段可能与主链路重叠或
# 为负,这是预期行为。
#
# All segment/E2E values are stored and logged in milliseconds (industry
# standard for voice-agent latency). Raw marks remain perf_counter seconds.
#
# Metadata: stt_model, llm_model, tts_model, prompt_tokens,
#           completion_tokens, cached_tokens, turn_phase, device_playback_ms

SEGMENTS: list[tuple[str, str, str]] = [
    # ── 主链路（E2E = server_received_vad_at → agent_spk）──
    ("stt",         "server_received_vad_at", "stt_final"),
    ("agent_init",  "stt_final",   "agent_init"),
    ("orchestrator","agent_init",  "agent_req"),
    ("context",     "agent_req",   "ctx_done"),
    ("llm_prep",    "ctx_done",    "llm_req"),
    ("llm_ttft",    "llm_req",     "llm_first_token"),
    ("llm_to_tts",  "llm_first_token", "tts_first_ready"),
    ("tts_ttfb",    "tts_first_ready", "agent_spk"),
    # ── 诊断段 ──
    ("vad",         "vad_start",   "vad_end"),
    ("server_vad",  "vad_end",     "stt_commit"),
    # 新增:上行网络时延(设备停嘴 → 服务器收到停嘴消息),用于评估网络质量
    ("vad_to_recv", "vad_end",     "server_received_vad_at"),
    ("llm_rest",    "llm_first_token", "llm_end"),
    ("tts",         "tts_start",   "tts_end"),
    ("ctx_l1",      "agent_req",   "ctx_l1_done"),
    ("ctx_l2",      "ctx_l1_done", "ctx_l2_done"),
    ("ctx_roast",   "ctx_l2_done", "ctx_roast_done"),
]

META_KEYS = [
    "stt_model", "llm_model", "tts_model",
    "prompt_tokens", "completion_tokens", "cached_tokens",
    "turn_phase", "device_playback_ms",
]

# ── Per-turn storage ─────────────────────────────────────────────────

_current_var: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "pigagent_turn_metrics", default=None
)
_turn_counter: int = 0


def _make_turn(user_id: str, persona_id: int) -> dict[str, Any]:
    global _turn_counter
    _turn_counter += 1
    return {
        "turn_id": _turn_counter,
        "user_id": user_id,
        "persona_id": persona_id,
        "marks": {},         # perf_counter 浮点(秒,单调),用于算间隔
        "event_unix_ms": {}, # 同步记的 UTC 毫秒时间戳,用于跨系统比对/日志
        "meta": {},
        # H5: turn dict 加 finished 标记。子 task 只能置 True,真正 flush
        # (_log + 清 ctx) 由父 task (WebSocket handler) 负责。
        "_finished": False,
    }


def _now_unix_ms() -> int:
    """返回当前 UTC 毫秒时间戳(int)。time.time() 本就 UTC,无时区问题。"""
    return int(time.time() * 1000)


def _resolve_active_turn() -> dict[str, Any] | None:
    """Return the current active turn dict, or None if no turn is active
    (or the current turn has been marked _finished).

    H5: a turn that has been marked _finished is no longer active — it has
    already been (or is about to be) flushed by the owning task. Callers
    that need to write marks/meta should treat finished turns as gone and
    avoid double-logging.
    """
    current = _current_var.get()
    if current is None or current.get("_finished"):
        return None
    return current


# ── Public API ───────────────────────────────────────────────────────

class TurnMetrics:
    """Async-safe singleton. Call class methods from anywhere — each
    asyncio task gets its own turn dict via contextvars."""

    @classmethod
    def start_turn(cls, *, user_id: str, persona_id: int) -> None:
        # H5: 旧 turn 必须真的 flush 完才能开新 turn。这里 ctx 可能已被子
        # task 标记 _finished 但未 flush(父 task 还没等到 await tts_task),
        # 所以无论 _finished 与否,只要有未清 ctx 就主动 flush 一次。
        current = _current_var.get()
        if current is not None:
            cls._flush_turn()
        _current_var.set(_make_turn(user_id, persona_id))

    @classmethod
    def has_mark(cls, key: str) -> bool:
        current = _resolve_active_turn()
        if current is None:
            return False
        return key in current["marks"]

    @classmethod
    def mark(cls, key: str) -> None:
        # C1: 必须用 _resolve_active_turn 过滤掉 _finished 的 turn,避免
        # 在子 task 里把 mark 写到已被父 task flush 过的 turn dict。
        current = _resolve_active_turn()
        if current is not None:
            now = time.perf_counter()
            current["marks"][key] = now
            # 同步记 UTC 毫秒时间戳,用于跨系统比对/日志对账
            current["event_unix_ms"][key] = _now_unix_ms()

    @classmethod
    def mark_time(cls, key: str) -> float | None:
        """Return the stored perf_counter value for a mark, if any."""
        current = _resolve_active_turn()
        if current is not None:
            return current["marks"].get(key)
        return None

    @classmethod
    def set_mark(cls, key: str, value: float) -> None:
        """Set a mark to an arbitrary perf_counter value (e.g. a timestamp
        reconstructed from the firmware's user_stop_age_ms duration)."""
        current = _resolve_active_turn()
        if current is not None:
            current["marks"][key] = value
            # 重建的 mark(如 vad_end from age_ms)没有真实的"事件时刻"概念,
            # 但为了下游对齐,仍记一个与 perf_counter 同时刻的 unix ms 近似。
            # 注意:这个 unix ms 是"perf_counter 当时"的本地时间,不一定是
            # 真实事件发生时间(固件侧的真实时刻无法从这里反推)。
            current["event_unix_ms"][key] = _now_unix_ms()

    @classmethod
    def set_meta(cls, key: str, value: object) -> None:
        current = _resolve_active_turn()
        if current is not None:
            current["meta"][key] = value
            if key == "turn_number" and isinstance(value, int):
                current["turn_id"] = value

    @classmethod
    def finish_turn(cls) -> None:
        """Mark the current turn as finished. Does NOT log or clear ctx.

        H5: 子 task (TTS) 调本方法只把 _finished 置 True,真正 flush
        (_log + 清 ctx) 由父 task 调 _flush_turn 负责。这样父 task
        (WebSocket handler) 才能在 await tts_task 之后真正清理,
        避免 ctx 副本导致的双 log 和 stale turn 残留。
        """
        current = _current_var.get()
        if current is None:
            return
        if current.get("_finished"):
            return  # 已经标记过,幂等
        current["_finished"] = True

    @classmethod
    def _flush_turn(cls) -> None:
        """真正 flush turn: _log + 清 ctx。只能由父 task 调。

        入口处 start_turn 也会调一次,补 flush 子 task 来不及 flush 的 turn。
        """
        current = _current_var.get()
        if current is None:
            return
        _current_var.set(None)
        _log(current)


# ── Internal ─────────────────────────────────────────────────────────

# M2: 主链路 8 段和诊断段分开存储,避免下游 sum(segments) 把诊断段加进去
# 导致 sum != E2E。每段是 "main" 严格串行,"diagnostic" 可能重叠/为负/为 None。
MAIN_SEGMENT_LABELS: set[str] = {
    "stt", "agent_init", "orchestrator", "context",
    "llm_prep", "llm_ttft", "llm_to_tts", "tts_ttfb",
}


def _segment_role(label: str) -> str:
    return "main" if label in MAIN_SEGMENT_LABELS else "diagnostic"


def _build_segments(m: dict[str, float]) -> dict[str, float | None]:
    """Return a flat {label: ms} dict for this turn (raw, may include None)."""
    out: dict[str, float | None] = {}
    for label, a, b in SEGMENTS:
        if label == "orchestrator":
            start = "agent_init" if m.get("agent_init") is not None else "stt_final"
            out[label] = _diff(m, start, "agent_req")
        else:
            out[label] = _diff(m, a, b)
    return out


def _log(turn: dict[str, Any]) -> None:
    m = turn["marks"]
    # E2E 起点 = server_received_vad_at(服务器实际收到停嘴消息的时刻),
    # 与 SEGMENTS 主链路第一段(stt 起点)对齐,这样 sum==E2E 真的成立
    # (N1 修复;之前用 vad_end 起点会少算一个上行 RTT 且触发永久 WARN)。
    # Fallback 到 vad_end 兼容:旧数据没有 server_received_vad_at 仍能算 E2E。
    e2e = _diff(m, "server_received_vad_at", "agent_spk")
    if e2e is None:
        e2e = _diff(m, "vad_end", "agent_spk")
    if e2e is None:
        return

    all_segments = _build_segments(m)
    main_segments: dict[str, float] = {
        k: v for k, v in all_segments.items() if v is not None and _segment_role(k) == "main"
    }
    diag_segments: dict[str, float] = {
        k: v for k, v in all_segments.items() if v is not None and _segment_role(k) == "diagnostic"
    }

    # M3: 主链路相邻段非负校验 + telescope sum ≈ E2E 校验,失败打 WARN
    _check_main_chain(m, main_segments, e2e)

    seg_parts: list[str] = []
    for label in (
        "stt", "agent_init", "orchestrator", "context",
        "llm_prep", "llm_ttft", "llm_to_tts", "tts_ttfb",
    ):
        d = main_segments.get(label)
        seg_parts.append(f"{label}={_fmt(d)}")
    for label in ("vad", "server_vad", "llm_rest", "tts", "ctx_l1", "ctx_l2", "ctx_roast"):
        d = diag_segments.get(label)
        if d is not None:
            seg_parts.append(f"{label}={_fmt(d)}")

    meta_parts: list[str] = []
    meta = turn["meta"]
    for k in META_KEYS:
        v = meta.get(k)
        if v is not None and v != "":
            meta_parts.append(f"{k}={v}")

    real_turn = meta.get("turn_number", "")
    tid = f"n={turn['turn_id']}"
    if real_turn:
        tid += f"(#={real_turn})"

    # ISO UTC 时间戳:取首末事件时间,人眼可读,跟日志对账用
    e = turn.get("event_unix_ms", {})
    started_iso = ""
    ended_iso = ""
    if e:
        # 优先用 server_received_vad_at 作为本回合时间锚,没有就退到 vad_end
        anchor_ms = e.get("server_received_vad_at") or e.get("vad_end")
        if anchor_ms is not None:
            started_iso = _iso_utc(anchor_ms)
        if e.get("agent_spk") is not None:
            ended_iso = _iso_utc(e["agent_spk"])
    time_str = ""
    if started_iso and ended_iso:
        time_str = f"  [{started_iso} → {ended_iso}]"
    elif started_iso:
        time_str = f"  [{started_iso}]"

    logger.bind(user=turn["user_id"], turn=turn["turn_id"]).info(
        f"[METRIC u={turn['user_id']} {tid}{time_str}] E2E={_fmt(e2e)}  "
        f"{'  '.join(seg_parts)}"
        + (f"  [{', '.join(meta_parts)}]" if meta_parts else "")
    )

    if _PG_DSN:
        try:
            asyncio.ensure_future(
                _pg_write(turn, m, e2e, main_segments, diag_segments)
            )
        except RuntimeError:
            pass


def _check_main_chain(
    m: dict[str, float],
    main_segments: dict[str, float],
    e2e: float | None,
) -> None:
    """M3: 校验主链路非负 + sum ≈ E2E,失败打 WARN 但不阻断 log。

    链路定义(MAIN_CHAIN 顺序):stt → agent_init → orchestrator → context
    → llm_prep → llm_ttft → llm_to_tts → tts_ttfb
    """
    # 1) 任何一段为负 -> WARN
    for label, v in main_segments.items():
        if v < 0:
            logger.warning(
                f"[METRIC] main segment '{label}' is negative: {v:.1f}ms "
                f"(marks={ {k: round(v, 4) for k, v in m.items()} })"
            )

    # 2) telescope sum 应该 ≈ E2E(允许 round 累计 0.5ms 误差)
    # E2E = agent_spk - server_received_vad_at,主链路第一段也是
    # stt_final - server_received_vad_at,所以 sum==E2E 仍然成立。
    if e2e is not None and main_segments:
        total = sum(main_segments.values())
        if abs(total - e2e) > 1.0:
            logger.warning(
                f"[METRIC] main chain sum != E2E: sum={total:.1f}ms "
                f"e2e={e2e:.1f}ms diff={(total - e2e):.1f}ms "
                f"segments={main_segments}"
            )


async def _pg_write(
    turn: dict[str, Any],
    m: dict[str, float],
    e2e: float | None,
    main_segments: dict[str, float],
    diag_segments: dict[str, float],
) -> None:
    import json as _json
    import asyncpg  # type: ignore[import-untyped]

    # M2: 主链路和诊断段分开存,带 role 字段,让下游能区分
    payload_segments: dict[str, dict[str, Any]] = {}
    for label, v in main_segments.items():
        payload_segments[label] = {"role": "main", "ms": v}
    for label, v in diag_segments.items():
        payload_segments[label] = {"role": "diagnostic", "ms": v}
    if e2e is not None:
        payload_segments["e2e"] = {"role": "main", "ms": e2e}

    # marks_with_ts: 每个 perf_counter 标记都附带 UTC ms,方便下游做
    # 跨系统比对/可视化。结构: {key: {perf_counter: <float>, unix_ms: <int>}}
    event_unix_ms = turn.get("event_unix_ms", {})
    marks_with_ts: dict[str, dict[str, Any]] = {}
    for k, perf in m.items():
        entry: dict[str, Any] = {"perf_counter": perf}
        if k in event_unix_ms:
            entry["unix_ms"] = event_unix_ms[k]
        marks_with_ts[k] = entry

    try:
        conn = await asyncpg.connect(_PG_DSN)
        try:
            await conn.execute(
                """INSERT INTO metrics
                   (user_id, turn_id, persona_id, marks, segments, meta)
                   VALUES ($1,$2,$3, $4::jsonb, $5::jsonb, $6::jsonb)
                   ON CONFLICT (user_id, turn_id) DO NOTHING""",
                turn["user_id"],
                turn["turn_id"],
                turn.get("persona_id", 0),
                _json.dumps(marks_with_ts),
                _json.dumps(payload_segments),
                _json.dumps({k: v for k, v in turn["meta"].items()
                            if v is not None and v != "" and v != 0}),
            )
        finally:
            await conn.close()
    except Exception:
        pass


def _diff(m: dict[str, float], a: str, b: str) -> float | None:
    va, vb = m.get(a), m.get(b)
    if va is not None and vb is not None:
        return round((vb - va) * 1000.0, 1)
    return None


def _iso_utc(ms: int) -> str:
    """Format UTC 毫秒为 ISO 8601 字符串(带 Z 后缀)。时区统一 UTC。"""
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


def _fmt(v: float | None) -> str:
    return f"{v:.1f}ms" if v is not None else "—"


# Backward compat alias
TelemetryCollector = TurnMetrics
