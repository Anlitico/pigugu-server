# 语音延迟测量整体方案(设备 + 服务器)

> 基于 main 分支代码 | 2026-09-03 | 状态:设计定稿,待实施

## 1. 背景与问题

**目标口径**:用户真正体感的端到端延迟 = **用户说完 → 用户听到第一个回复声音**。

当前所有指标都在服务器钟上、且从 `stt_final` 起跑:

```
用户说完 ──(Deepgram 端点静默确认)──> stt_final ──(服务器管道)──> agent_spk ──(下行/解码)──> 听到
                                        └──────── 现在只量这一段 ────────┘
```

三个缺口:

1. **STT 端点尾巴(说完 → stt_final)不可见**:`stt_ms` 恒 0(xiaozhi 固件不发 `vad_silence`,`server_received_vad_at` 从不置位),这段其实是用户体感等待的一部分。
2. **wake 轮被系统性低估**:wake 轮是每会话第一轮(设备空闲就断 WS、唤醒时重连),连接 + 上行 + 音频被缓存补发都会让"服务器听到音频的时间"偏晚 → 以服务器收音频为锚会把 E2E 算小。实测 CH 里 wake 完整回复 e2e 4.1s(follow_up 1.44s),实际只会更大,方向就是低估。
3. **连接到底慢不慢无数据**:纯靠感觉。

业界结论(Pipecat `UserBotLatencyObserver`、LiveKit `EOUMetrics`、各实测基准):**"用户说完"由 VAD(最好是设备 AFE)锚定,STT 端点尾巴单独量;服务器管道指标与设备感知指标分开测再融合**。设备时间上行本已是 AEC 的标准先例(xiaozhi 协议 v2 帧 timestamp、设备上报播放游标跑云端 AEC3)。

## 2. 设计方案一句话

> 设备用单调钟给"说完 / 开始播"盖 age 上报;服务器把 age 重建到自己的钟上成为权威起点/终点;服务器内部照旧打管道 mark。于是**全程只有一块钟**,完整感知延迟可拆成三段,每段可单独诊断。

## 3. 时钟策略(age/delta,单钟)

- 设备不维护 unix 墙钟。**wake 轮发生在联网/NTP 对时之前或边缘**,unix 时间在那一下最不可靠,恰好污染最要准的一轮;ESP32 单调钟漂移也免于考虑。
- 设备用 `esp_timer`(上电即单调)算 age,随消息上报:"某事件发生在我(设备钟)的 N 毫秒前"。
- 服务器收到消息时记 `received_at = perf_counter()`,`事件在服务器钟上 = received_at − N/1000`。误差 = 消息单向传输时间(几十 ms),固定可接受。
- 服务器内部 mark 继续 **perf_counter + unix ms 双记**(现有 `event_unix_ms`),保证人眼可读 + 跨系统对账。

```
设备单调钟:   ...──── event ──────────── now(发消息)
                └── age = now − event ──┘
服务器时钟:   received_at − age  ← 事件落在服务器钟上
```

## 4. 设备(firmware)要打/上报的点

| 事件 | 何时发生 | 上报载体 | 上报字段 |
|---|---|---|---|
| 用户说完 `voice_end` | AFE VAD `speech → silence` 翻转瞬间(仅倾听态、且本 turn 见过人声,防播放回声误报) | `listen/vad_silence`(现有协议消息) | `user_stop_age_ms`(老字段,已删除待恢复) |
| 首个回复音频开播 `play_start` | 第一帧 tts 音频交给 DAC 的瞬间 | `tts_played` 扩展(或新增 ack) | `first_play_age_ms` |
| (可选)唤醒 `wake` | 唤醒词识别瞬间 | `listen/detect` 扩展 | `wake_age_ms` —— 用于算唤醒→连上→首帧 |

参考实现:**`pigugu-firmware` commit `af3c9c8`**(后被 `438f03a` 对齐 xiaozhi 砍掉):
- `main/application.cc:408-435` VAD 翻转打点(`vad_voice_seen_in_listening_` / `user_stop_speaking_ms_`)
- `main/application.cc:450-475` CLOCK_TICK 触发发送
- `main/protocols/protocol.cc:59-67` `SendVadSilence(user_stop_age_ms)`

平移目标:`pigugu-firmware-xiaozhi`(VAD 事件链已存在,现只驱动 LED):
- `main/audio/processors/afe_audio_processor.cc:156-162`(VAD_SPEECH/SILENCE 回调)
- `main/application.cc:93`(`on_vad_change` → `MAIN_EVENT_VAD_CHANGE`)

## 5. 服务器(voice/pipecat)要打/记的点

| 点 | 说明 | 现状 |
|---|---|---|
| `accept` / `hello` / `first_audio` | 连接预卷(连接慢不慢) | ❌ 新增(会话级) |
| `user_stop` | `vad_bridge._on_vad_silence` 里 `received − user_stop_age_ms` 重建 | ⚠️ 代码在(`vad_bridge.py:108-128`),当前只记录不启用 → 提升为权威 |
| `stt_final` | turn 合并出最终文字 | ✅ |
| `llm_req` / `llm_first_token` / `tts_first_ready` / `agent_spk` | 管道 mark | ✅ |
| `play_start` | 设备"开始播"ack 重建 | ❌ 新增 |

## 6. 派生指标与诊断用途

核心等式(把感知延迟拆成三段,谁也不丢):

```
e2e_perceived = stt_tail + server_pipeline + device_render
(说完→听到) = (说完→出文字) + (出文字→发音频) + (发音频→喇叭响)
```

| 指标 | 计算 | 诊断 |
|---|---|---|
| `e2e_perceived` | `play_start − user_stop` | 最终 UX 数字;wake vs follow_up 对比不再被低估 |
| `stt_tail` | `stt_final − user_stop` | Deepgram 端点确认耗时;大 → 换端点 / 尾字抢先 |
| `server_pipeline` | `agent_spk − stt_final`(= 现 e2e,保留) | 服务器管道;内含 `llm_ttft`、`llm_to_tts`、`tts_ttfb` |
| `llm_ttft` | `llm_req → llm_first_token` | 模型/冷启动/上下文(wake 首轮实测 1.7~1.9s vs 稳态 ~1.0s) |
| `device_render` | `play_start − agent_spk` | 下行 / 设备解码 / 抖动缓冲;大 → 设备侧或服务器 pacing |
| `connect_pre_roll` | `accept→hello→first_audio` | 唤醒后服务器接上话前;回答"连接慢不慢" |

## 7. 落地阶段

- **Phase 0 — 服务器提升 + 连接计时(不动固件)**:
  - `vad_bridge._on_vad_silence` 从诊断提升为权威起点;会话级记 `accept→hello→first_audio`。
  - 无设备信号时 `user_stop` 退化为现 `stt_final` 锚,旧数据可比性不破。
  - 落表:`stt_tail` / `e2e(perceived)` / `connect_ms`(缺设备点则留空)。
- **Phase 1 — 固件移植**(`pigugu-firmware-xiaozhi`):恢复 `vad_silence{user_stop_age_ms}`(port `af3c9c8`)+ `tts_played` 带首帧 age;烧录真机验证。
- **Phase 2 — 指标融合与看数**:pgsql `metrics` / CH 落新字段;wake vs follow_up 完整分解(p50/p90)。

## 8. Metrics 落库架构:pg metrics → ClickHouse

**决策(2026-09-03):所有 metrics 体系从 pgsql 迁到 ClickHouse,统一收口到 CH。** pgsql 保留为应用 OLTP(context/prompts/用户);只迁 metrics 类表。coldstart 为死代码(无调用者),连同其 alembic 表一起删。

长期结构:**CH `metrics` 库,共享信封 + 每域类型化事实表**(不是一张通用 JSON 表,也不塞进 `voice.turns`——热指标需类型化列做 p50/p90,JSON blob 现拆又慢又易错;`voice.turns` 保持"音频证据"角色)。

```
metrics(CH 24.3)
├─ turn_latency   每 turn 一行,1:1 关联 voice.turns(turn_id)
│   信封: date / ts / user_id / device_id / session_id / turn_id / persona_id
│   类型化热指标: turn_type LowCardinality + agent_init_ms … tts_ttfb_ms(stt_tail/e2e/llm_ttft 等,Nullable Float64)
│   + marks_json / meta_json String(取证/重算)
├─ session        每连接一行: connect_pre_roll(accept→hello→首帧)、协议、device
└─ compression    每压缩事件一行(port compression_metrics,类型化 token 数 + json 尾)
```

- json 一律 **String + `JSONExtract*`**(CH 24.3 JSON 类型 experimental,不开)。
- 写入:单一异步 fire-and-forget 通道;每轮 `metrics.turn_latency` insert 并入 `TurnStorage.commit` 同一 job;各表写失败各自 log + swallow,互不阻塞。
- 分析主源从 `voice.turns` 快照列迁到 `metrics.turn_latency`;`voice.turns` 快照列留给 admin/turn.json。
- 写端去掉 `_pg_write`/asyncpg/`DATABASE_URL`(metrics 模块);读端 `scripts/analyze_latency.py` + `.claude/skills/ops/metrics.md` 改为读 CH。

## 9. 非目标 / 边界

- 不做 NTP/双钟同步。
- 不改 turn 决策(仍 Deepgram utterance-end);本方案只管测量。
- 每帧设备时间戳(proto v2,给服务器 AEC/连续对齐)暂不上,留作后手。
- 不把连接延迟并进 `e2e_perceived`(用户说话时连接已重叠);连接由 `connect_pre_roll` 单独量化。

## 10. 观测子系统架构(Instrument → Scope → Export)

**决策(2026-09-03,分支 refactor/telemetry-observability):** metrics 采集/上报从生产链路解耦,按最清晰三层重构,统一收口到 CH。

```
生产链路(voice/pipecat · agent)          观测侧(独立)
只调薄 emit API(Telemetry.mark/set_meta) ──关闭turn──▶ Scope 快照 → 有界队列 → Exporter(loop 单任务)
同步·纯内存·O(1)·不抛异常                            批量 INSERT → CH(单连接 asynch)
不 import asynch/asyncpg/DSN/表名                       满→丢最老+计数;失败→log+计数,绝不 raise
```

- `metrics/scope.py`  Scope —— 纯记录:marks({key:perf})+event_unix_ms+meta+turn_id/user/persona;mark/set_attr/finish/snapshot。无 sink、无 asyncio。
- `metrics/registry.py` current-scope contextvar + open/bind/finish/flush;open() 自动 close 上一未关。
- `metrics/bus.py` 有界线程安全队列(drop-oldest)。
- `metrics/exporter.py` 唯一 sink:loop 上单后台任务消费 → 批量 INSERT CH;超时/吞错/失败计数;lazy start;无 CH 配置 → no-op。
- `metrics/turn.py` 变薄门面(`TelemetryCollector`,方法名不变,agent/context/llm 调用点不改);删除 `_pg_write`/asyncpg/`_PG_DSN`。
- `metrics/render.py` 分段/e2e/日志行/CH row 构建(原 `_log` 逻辑迁此)。
- pipecat 跨 task:`state.turn_scope`(Scope);tts_bridge 进 `_run_tts` bind 一次,agent/context/llm mark 自动命中;删除散点 `ensure_turn_context`;`telemetry_snapshot` 读 Scope。
- compression 复用 exporter(本地 Scope → 入队),删 `_flush_pg`;coldstart(metrics/session.py)死代码删除。

保证:emit 路径纯内存;落库仅 exporter 一个任务;队列有界 + drop → 背压不外泄;失败只 log。loguru 人类日志与 voice/storage.py(TurnStorage,音频证据域)不纳入本次。
