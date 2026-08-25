# 独立代码审计报告 (第二遍 — 我自己)

> 副标题:第一次 subagent (codex exec) **失败**,只跑了 3 个 exec 没真审计。下面这份是我自己按 INDEPENDENT_AUDIT_PROMPT.md 要求从零读的独立审计结果。

---

## Summary

| 维度 | 计数 |
|---|---|
| 原 13 个 findings | **10 完全修复**,1 部分修复 (H2 race),**1 修复 50% 但仍有自相矛盾 (H3)**,1 已修复但相关代码保留隐患 (C1) |
| 新发现 issues | **1 Critical**(H3 修复不一致触发 sum != E2E + 数字偏小),1 High (H2 race),3 Medium,3 Low,5 Info |
| 建议提交/推送? | **No — 必须先修 H3(1 行代码)**;修完 H3 后可 Yes。 |

---

## 0. Subagent 失败原因 (process note)

跑了 `codex exec -s read-only` 的独立 subagent (session 01a036ec...),但:
- 只跑了 3 个 exec: 提取原 audit 的 python 脚本 + 2 次 `git status`
- **完全没跑 `git diff`、没读任何源码**
- 把原 audit 文本当成了自己的"输出"
- stderr 末尾 2 行 `exec /bin/zsh -lc 'cd ... && git status'` 后就停了

可能原因:模型推理 effort=high 时"想"得太多没产出实际 exec,或者 codex exec 的某个内部错误截断。我自己重新从零做了一遍审计(下述)。

---

## 1. 原 13 个 issues verification

### C1 — `contextvars` 迁移后 Deepgram 线程回调看不到 turn 指标 — **✅ 修复**
- 位置: [pigagent/voice/connection.py:494-519](pigugu-server/pigagent/voice/connection.py:494), [pigagent/metrics/turn.py:114-138](pigugu-server/pigagent/metrics/turn.py:114)
- 改动:`_capture_turn_context` (dispatch 端) + `_restore_turn_context` (entry 端),在 `_on_stt_final` / `_on_stt_result` 入口恢复。
- 评估:**修复正确**。`start_turn` 后立刻 `_capture_turn_context()` 把 dict ref 存到 `self._active_turn`,跨线程协程入口 `_restore_turn_context()` 把 ctx 重新 bind 到这个 dict ref。`_active_turn` 在 `__init__` 中已初始化为 None (line 111)。
- Edge case:`_on_tts_played` 入口**没调** `_restore_turn_context()` — 见新 issue N3。

### H1 — 无固件 VAD 时 wake-word turn 把 detect 当 vad_end — **✅ 修复**
- 位置: [pigagent/voice/connection.py:603-621](pigugu-server/pigagent/voice/connection.py:603)
- 改动:fallback 顺序改为 `self._vad_end_mark` (固件 vad_silence 重建) → `stt_commit` (服务端 Silero) → `detect` (wake-word, meta 标 `vad_end_fallback=detect`) → `none`。
- 评估:逻辑正确,fallback meta 显式标注便于下游识别。

### H2 — 固件只在 Listening arm VAD stop,Connecting 已起音频处理 — **⚠️ 部分修复 (race 风险)**
- 位置: [main/application.cc:1313-1321](pigugu-firmware/main/application.cc:1313) (HandleStateChangedEvent listening 分支)
- 改动:进入 listening 时若 `IsVoiceDetected()` 主动设 `vad_voice_seen_in_listening_=true`。
- 评估:修复方向对,但 `IsVoiceDetected()` 是瞬时值。若用户语音在 listening 边界前 100ms 触发 VAD 上升,AFE 状态在 listening entry 时已 hysteresis reset 为 false,**仍漏 arm**。缓解:VAD `vad_min_noise_ms=100` 让 hysteresis 撑住窗口。实际影响:首轮 fallback 到 H1 的 detect 路径,meta 标 fallback,数据不丢。
- 建议:可考虑监听一个 `voice_was_detected_since_connecting_` 类 sticky 标志(在 AFE VAD callback 里 set 一次,只 set 不清)。**置信度:中**

### H3 — 用 age_ms 在 server perf_counter 上重建 vad_end 少算上行网络时延 — **❌ 修复 50% / 自相矛盾 (Critical)**
- 位置: [pigagent/metrics/turn.py:267](pigugu-server/pigagent/metrics/turn.py:267) (e2e 计算), [pigagent/metrics/turn.py:72](pigugu-server/pigagent/metrics/turn.py:72) (SEGMENTS stt 段起点)
- 改动:
  - ✅ SEGMENTS stt 段起点改为 `server_received_vad_at` (line 72)
  - ✅ 新增 `vad_to_recv` 诊断段 (line 84)
  - ❌ **`_log` 仍用 `vad_end` 算 e2e** (line 267): `e2e = _diff(m, "vad_end", "agent_spk")`
  - 矛盾点: `_check_main_chain` 注释 (line 355-356) 说 "E2E = agent_spk - server_received_vad_at, 主链路第一段也是 stt_final - server_received_vad_at, 所以 sum==E2E 仍然成立"。但实际 e2e 是基于 vad_end,sum 永远 != e2e,触发永远 WARN。
- 后果:
  1. E2E 数字**系统性偏小**(少算一个上行 RTT,典型 10-50ms)
  2. `analyze_latency.py:262` 已经按 `server_received_vad_at` 算 E2E — 服务端 log 与分析脚本**口径不一致**
  3. `_check_main_chain` 永远 WARN,WARN log 噪音污染日志
- 修复:第 267 行改为 `e2e = _diff(m, "server_received_vad_at", "agent_spk")`。如果该 mark 不存在,fallback 到 `vad_end`(向后兼容老数据)。

### H4 — voice_detected_ 普通 bool 跨 task 数据竞争 — **✅ 修复**
- 位置: [main/audio/audio_service.h:196-199](pigugu-firmware/main/audio/audio_service.h:196), [main/audio/audio_service.h:119](pigugu-firmware/main/audio/audio_service.h:119)
- 改动:`std::atomic<bool> voice_detected_{false}`,store 用 release,load 用 acquire。
- 评估:正确,内存序配对,无遗漏。

### H5 — 父/子 task 的 ContextVar 是副本,finish_turn 清不掉父 task 旧 turn — **✅ 修复**
- 位置: [pigagent/metrics/turn.py:119-122](pigugu-server/pigagent/metrics/turn.py:119) (_finished 标志), [pigagent/metrics/turn.py:209-225](pigugu-server/pigagent/metrics/turn.py:209) (finish_turn 只 set _finished), [pigagent/metrics/turn.py:225-234](pigugu-server/pigagent/metrics/turn.py:225) (_flush_turn 真正 log), [pigagent/voice/connection.py:646-661](pigugu-server/pigagent/voice/connection.py:646) (父 task await 后 _flush_turn)
- 改动:turn dict 加 `_finished` 标志;`finish_turn` 只 set `_finished=True`;新增 `_flush_turn` classmethod 只由父 task 调,负责 `_log` + 清 ctx。
- 评估:正确,语义清晰。冗余分支 `if/elif` (line 153-156) 两支都调 `_flush_turn`,纯代码味道,无功能影响。

### M1 — tts_played 挂错 turn / sentence_id 关联 — **✅ 修复**
- 位置:
  - 服务: [pigagent/voice/connection.py:493-525](pigugu-server/pigagent/voice/connection.py:493) (_on_tts_played 校验 sentence_id)
  - 服务: [pigagent/voice/connection.py:815-833](pigugu-server/pigagent/voice/connection.py:815) (tts/start 带 sentence_id)
  - 服务: [pigagent/voice/connection.py:636](pigugu-server/pigagent/voice/connection.py:636) (`_current_tts_sentence_id` 跟踪)
  - 固件: [main/application.cc:826-832](pigugu-firmware/main/application.cc:826), [main/protocols/protocol.cc:73-82](pigugu-firmware/main/protocols/protocol.cc:73)
- 评估:协议、服务端、固件三端都改。mismatch 路径丢弃,数据不丢到错误 turn 但会**丢失**(见新 N3)。

### M2 — 诊断段和主链路混在 segments JSON — **✅ 修复**
- 位置: [pigagent/metrics/turn.py:368-385](pigugu-server/pigagent/metrics/turn.py:368) (_pg_write 写 role), [scripts/migrate_metrics_format.py:70-74](pigugu-server/scripts/migrate_metrics_format.py:70) (历史数据补 role)
- 评估:role 字段已加,迁移脚本会回填。`MAIN_SEGMENT_LABELS` 在 turn.py 和 migrate 脚本双份维护,见新 N5。

### M3 — _diff 去掉非负截断 + 缺失 telescope 校验 — **✅ 修复**
- 位置: [pigagent/metrics/turn.py:336-364](pigugu-server/pigagent/metrics/turn.py:336) (_check_main_chain)
- 改动:主链路非负校验 + sum ≈ E2E 校验,失败 WARN。
- 评估:校验逻辑正确,但**因 H3 修复不一致**(N1),现在会**永远 WARN**。修 H3 后此校验才真正生效。

### M4 — std::atomic<int64_t> 在 32 位 ESP32 上可能非 lock-free — **✅ 修复**
- 位置: [main/audio/audio_service.h:199-214](pigugu-firmware/main/audio/audio_service.h:199)
- 改动:退回到普通 `int64_t first_packet_received_ms_/first_output_played_ms_` + `playback_timing_mutex_`。
- 评估:lock order 正确 (PushPacketToDecodeQueue 内 `audio_queue_mutex_` → `playback_timing_mutex_` 嵌套,其他持锁点不反向)。

### M5 — SendWakeWordDetected 仍发送旧 user_stop_ms — **✅ 修复**
- 位置: [main/protocols/protocol.cc:51-58](pigugu-firmware/main/protocols/protocol.cc:51), [main/application.cc:1244](pigugu-firmware/main/application.cc:1244)
- 改动:`SendWakeWordDetected` 不再带 user_stop_ms,只发 wake_word。
- 评估:正确,协议层清理彻底。

### L1 — llm_first_token 在首个非文本 yield 上打点 — **✅ 修复**
- 位置: [pigagent/agent.py:214-250](pigugu-server/pigagent/agent.py:214)
- 改动:重命名为 `first_string_yield`,只在 `isinstance(text, str)` 分支内 mark。
- 评估:正确,FlushSentinel / 工具 filler 不再污染首 token 语义。

### L2 — first_packet_received_ms_ 被本地音效污染 — **✅ 修复**
- 位置: [main/audio/audio_service.cc:594-600](pigugu-firmware/main/audio/audio_service.cc:594) (is_tts_playback_active_ guard), [main/audio/audio_service.cc:615-628](pigugu-firmware/main/audio/audio_service.cc:615) (Reset/EndTtsPlaybackTiming)
- 评估:`is_tts_playback_active_` 仅在 tts/start 之后为 true,本地音效不会触发 timing 更新。`tts/stop` 和 `tts/abort` 调 `EndTtsPlaybackTiming()` 关闭。

---

## 2. 新发现的 issues

### [Critical] N1 — H3 修复 50% 实施,E2E 数字偏小且 _check_main_chain 永远 WARN
- 位置: [pigagent/metrics/turn.py:267](pigugu-server/pigagent/metrics/turn.py:267)
- 现状:`_log` 用 `e2e = _diff(m, "vad_end", "agent_spk")`;但 SEGMENTS stt 段起点是 `server_received_vad_at`。
- 后果:
  1. E2E 系统性少算上行网络时延(典型 10-50ms),与 `analyze_latency.py:262` (用 `server_received_vad_at`) 口径不一致
  2. `_check_main_chain` 永远 WARN,日志噪音
- 修复(1 行):
  ```python
  e2e = _diff(m, "server_received_vad_at", "agent_spk")
  ```
  fallback 到 `vad_end` 以兼容 mark 缺失场景。
- 置信度:**高**(代码字面对比即得)

### [High] N2 — H2 修复有 race:listening 入口读 IsVoiceDetected() 是瞬时值
- 位置: [main/application.cc:1317](pigugu-firmware/main/application.cc:1317)
- 现状:进入 listening 时一次性读 `IsVoiceDetected()`,若 false 则不 arm `vad_voice_seen_in_listening_`。
- 后果:用户在 listening 边界前 100ms 触发 VAD 上升,hysteresis 状态在 listening entry 时已 reset 为 false → 漏 arm → 本 utterance 不发 vad_silence → 首轮 fallback 到 H1 detect 路径。
- 缓解:AFE `vad_min_noise_ms=100` 让窗口缩小,但 race 仍可能。
- 修复建议:在 `audio_processor_->OnVadStateChange` 回调里 set 一个 sticky 标志 `voice_seen_since_connecting_`,listening 入口读这个 sticky 标志。
- 置信度:中

### [Medium] N3 — tts_played 晚到 → 丢失 device_playback_ms 数据
- 位置: [pigagent/voice/connection.py:493-525](pigugu-server/pigagent/voice/connection.py:493)
- 现状:当 tts_played 在父 task `_flush_turn()` 之后到达,`cur_sid=None` → mismatch 路径 → return(丢弃 device_playback_ms)。
- 后果:罕见但设备调度极慢时,设备播放延迟数据丢失,下游看不到这个 device 的 playback delay。
- 修复建议:用 `_active_turn` ref(若还未被清掉)写入 meta;或维护一个 `_late_tts_played_buffer` 等下一个 turn 写到 meta[device_playback_ms_late]=。
- 置信度:中

### [Medium] N4 — start_turn 冗余 if/elif 分支
- 位置: [pigagent/metrics/turn.py:148-159](pigugu-server/pigagent/metrics/turn.py:148)
- 现状:两个分支都调 `_flush_turn`。
- 修复:合并成 `if current is not None: cls._flush_turn()`。
- 置信度:高(纯代码味道,无功能影响)

### [Medium] N5 — MAIN_SEGMENT_LABELS 双份维护,无运行时校验
- 位置: [pigagent/metrics/turn.py:241-244](pigugu-server/pigagent/metrics/turn.py:241), [scripts/migrate_metrics_format.py:70-74](pigugu-server/scripts/migrate_metrics_format.py:70)
- 现状:注释提醒 "Keep in sync with..." 但无 test / runtime check。
- 后果:未来改 turn.py 忘改 migrate 脚本(或反之),历史数据迁移结果与新写入结果不一致。
- 修复建议:把 MAIN_SEGMENT_LABELS 提到一个共享 module(`pigagent/metrics/segments.py`),migrate 脚本 import。
- 置信度:低(目前两份完全一致)

### [Low] N6 — agent_init 段语义与命名
- 位置: [pigagent/voice/connection.py:638](pigugu-server/pigagent/voice/connection.py:638), [pigagent/metrics/turn.py:73](pigugu-server/pigagent/metrics/turn.py:73)
- 现状:`agent_init` mark 打在 `await create_pig_agent` 完成**之后**,所以段 = "stt_final → PigAgent 创建完成",实际包含 init + ctx init。命名 OK 但段名可考虑 `agent_init_total` 更准确。
- 置信度:低(命名风格,无功能影响)

### [Low] N7 — `on_first_audio_played` 回调内 lock release/reacquire
- 位置: [main/audio/audio_service.cc:335-345](pigugu-firmware/main/audio/audio_service.cc:335)
- 现状:line 335 持 `playback_timing_mutex_` 设 `first_output_played_ms_`,line 340 释放后调 `GetFirstAudioPlayedMs()` 重新获取。
- 后果:无死锁(都是 std::mutex 不可重入,但 unlock 之后重 lock 安全),但性能路径上多一次 lock/unlock cycle。
- 修复建议:line 335 持锁期间直接计算 `first_output_played_ms_ - first_packet_received_ms_`,把 ms 算好后传出去,避免重 lock。
- 置信度:低

### [Info] I1 — 主链路 telescope 数学正确
- `turn.py:72-79` 主链路 8 段串行,首轮有 `agent_init`、后续轮 orchestrator 改用 stt_final 起点,数学上 sum ≈ E2E(= agent_spk - server_received_vad_at)。**前提是 N1 修复后**。

### [Info] I2 — 固件协议路由匹配
- `SendVadSilence` 的 `user_stop_age_ms` 与服务端 `_on_vad_silence` 匹配(line 287)。
- `SendTtsPlayed` 的 `device_playback_ms` + `sentence_id` 与服务端 `_on_tts_played` 匹配。

### [Info] I3 — 设备播放时延内部时间基准一致
- `first_packet_received_ms_` / `first_output_played_ms_` 都用 `esp_timer_get_time()`,没有和 `std::chrono::steady_clock` 混算。

### [Info] I4 — AFE VAD/AEC 互斥初始化
- [main/audio/processors/afe_audio_processor.cc:104](pigugu-firmware/main/audio/processors/afe_audio_processor.cc:104) `vad_init = !aec_init` 与 `EnableDeviceAec` 互斥逻辑一致。

### [Info] I5 — 旧字段残留扫描
- 服务端无 `user_stop_ms` / `first_audio_played_ms` / `e2e_true_s` / `llm_internal` 残留。
- 固件仅 `SendWakeWordDetected` 残留(M5 已修)。

---

## 3. Action list (sorted by priority)

1. **[must-fix]** N1 — `_log` 第 267 行改 `e2e = _diff(m, "server_received_vad_at", "agent_spk")` (1 行)
2. **[should-fix]** N2 — H2 race 修复:在 AFE callback 里 set sticky `voice_seen_since_connecting_`
3. **[should-fix]** N3 — `_on_tts_played` 晚到时 buffer 一拍,避免丢 device_playback_ms
4. **[nice-to-have]** N4 — start_turn 合并 if/elif
5. **[nice-to-have]** N5 — MAIN_SEGMENT_LABELS 提到共享 module
6. **[nice-to-have]** N6 / N7 — 命名 / lock 优化

---

## 4. 其他观察 (非 issue)

- `migrate_metrics_format.py` 与 `analyze_latency.py` 的 SQL 处理逻辑清晰,旧格式识别条件用 `jsonb_typeof` 严格区分数字 vs 对象,正确。
- `analyze_latency.py:percentile()` 用了标准线性插值,等价 numpy 默认。
- `Dockerfile.tools` 镜像分层合理(只装 asyncpg,不复制 pigagent/),不触发 agent 重建。
- `deploy.yml` 加了 tools image build step,放在 api/agent 之后,符合依赖顺序。
- `ops/metrics.md` 文档结构清晰,`kubectl run` 语法 OK。

