# 设备静默 idle — 技术设计(TECH.md)

> 代码基线:server `333e2a7`(feat/device-idle)·firmware `fdd4274`(feat/device-idle-trigger)
> 产品规格:[docs/product-design/voice-idle-prd.md](../product-design/voice-idle-prd.md)
> 状态:实现中(固件落地,服务端接线中) | 2026-09-06

## 1. Context(现状与问题)

**做的东西**:让对话"告一段落后设备自动回待命"。产品上 idle = 断开 WS(PRD §3/§4.5)。两类触发 + 兜底:
- idle#1 静默超时:固件 VAD 30s 无有效人声,**或**服务端 30s 无用户语音,任一先到 → 断 WS;计时自 TTS 播完起算、播放期暂停、窗口内开口续轮;
- idle#2 关电源/失联:设备异常断电、WS 没关但音频停传,服务端无音频帧超时(VOICE_LOST_TIMEOUT_SECS=30)收口;
- 兜底:复位/重启重连新 session。

**当前系统为什么做不到**(行为细节与验收见 PRD,不在此复述):

- 固件(main/application.cc):状态机 Idle/Listening/Speaking(device_state_machine.cc)。主循环 1s CLOCK_TICK 里的"30s 静默回 idle"被 `listening_mode_ == kListeningModeAutoStop` 闸死([application.cc:481-510 @ fdd4274](https://github.com/Anlitico/pigugu-firmware/blob/fdd4274/main/application.cc#L481-L510));而本产品 `CONFIG_USE_DEVICE_AEC=y`(sdkconfig:751)→ 默认模式 Realtime(`GetDefaultListeningMode`,[application.cc:1386 @ fdd4274](https://github.com/Anlitico/pigugu-firmware/blob/fdd4274/main/application.cc#L1386))→ 永不进那条分支。回复播完 `tts/stop` 走非 ManualStop 分支回 Listening([application.cc:860-878 @ fdd4274](https://github.com/Anlitico/pigugu-firmware/blob/fdd4274/main/application.cc#L860-L878)),mic 持续上传 → 服务端收不到"无帧",120s 兜底也不触发。
- 服务端(pigagent/voice/pipecat):PipelineWorker 只有 120s 无 InputAudioRawFrame 的 idle([session.py:254-267 @ 333e2a7](https://github.com/Anlitico/pigugu-server/blob/333e2a7/pigagent/voice/pipecat/session.py#L254-L267));turn 判定、回复结束信号(`state.client_is_speaking`)与 commit-on-finalize 均已就绪(tts_bridge),可作 idle 判定的现成信号。
- 关键差异(gate 口径,业界对照 LiveKit `user_away_timeout`):计时**只在 bot 说完、且无回合在飞时**累计,不能在 agent 生成期误关。

## 2. Proposed changes

### 2.1 固件:idle 决策抽纯单元 + 去模式 + idle 断 WS

（本仓库已按如下落地,分支 `feat/device-idle-trigger`）

**① 模式收敛**:产品只走 Realtime(`CONFIG_USE_DEVICE_AEC=y`),顺手**删掉 AutoStop/ManualStop** 两模式及其分支(`ListeningMode` 枚举、`listening_mode_`、`SetListeningMode`/`GetDefaultListeningMode`、`SendStartListening(mode)`→无参固定 realtime)。tts stop/abort 不再分 ManualStop→Idle,统一回 Listening(追问窗口由 idle#1 管)。保留 `ToggleChatState/StartListening/StopListening` 公开方法(fork 其它板编译依赖)。

**② idle 判定 = 纯决策单元**(为 host 单测抽的 `main/idle_policy.h/.cc`):
- `IdleTimer::on_second({listening, has_audio_to_play, voice, agent_busy})`,gate = `Listening && !has_audio_to_play && !agent_busy`(不再要求 AutoStop 模式 → Realtime 也计);
- **agent_busy = 服务端回合生成在飞(审计 #1 修正)**:固件无法知道"用户停嘴后服务端是否正在生成",由服务端在回合 dispatch 时推 `{"type":"turn","state":"start"}`(见 2.3);收到即置 `turn_in_flight_` 并清零计时 → **LLM/tool 长生成(>30s 无首帧)绝不误断**,违反 §1 line 18 的"不能在 agent 生成期误关"被堵死;该回合 `tts/start·stop·abort` 清标志 → 回复播完回 Listening 后窗口从 0 重新累计(全 30s 追问窗)。
- 迟滞/重置(`voice_weight_` 0..3、`silence_ticks_`)保持原样:持续 voice(weight≥2)清零窗口,单次噪声尖峰不重置;Speaking/播放中走清零 → 天然满足"自播完起算";
- 阈值 `CONFIG_DEVICE_IDLE_SILENCE_SECS`(默认 30),`CONFIG_DEVICE_IDLE_ENABLE` 总开关(默认 y)。
- 服务端无终态兜底:每个真实用户回合都经 gateway dispatch(唤醒/追问同咽喉)发 `turn/start`;服务端保证每回合以设备可见消息收尾——正常 tts/stop、打断 tts/abort、空回复走 finally tts/stop,**无音频早退(no_tts / agent 起不来)补发 tts/abort**(设备在 Listening,收 abort 仅清标志,幂等无副作用),避免设备永久 disarm 卡 Listening。

**③ idle 动作**:主循环 1s CLOCK_TICK 喂 `IdleTimer`,触发时
```
SendStopListening()                       // 避免服务端把主动断当异常
GetAudioProtocol()->CloseAudioChannel()   // = websocket_.reset():彻底断 WS → OnAudioChannelClosed → LOW_POWER + Idle + 唤醒词 re-arm
```
固件 Kconfig:`CONFIG_DEVICE_IDLE_ENABLE` / `_IDLE_SILENCE_SECS`(on / 30)。

> 曾计划固件心跳 `{"type":"ping"}`,**已废弃**:服务端 idle#2 复用 PipelineWorker 无音频帧超时(见 2.2②),无需心跳。设备正常 idle 由本动作主动断 WS 感知,异常失联由服务端超时兜底。

### 2.2 服务端:idle#1 复用 `UserIdleController`;idle#2 复用 Worker idle

**① idle#1(静默)= 复用 Pipecat `UserIdleController`**(pipecat-ai 1.8.1,`turns/user_idle_controller.py`):
- 调研结论:本管线已用 `UserTurnProcessor`,其**内部就内建了 `UserIdleController`**(user_turn_processor.py 构造 `user_idle_timeout` 参数,`process_frame` 把每帧喂给内部 controller,回合起止由它自己合成 UserStarted/Stopped 喂入)。**无需自建/自接线 controller**——只要:
  1. 给 `UserTurnProcessor` 传 `user_idle_timeout=VOICE_IDLE_SILENCE_SECS(默认30)`;
  2. 在它上面挂 `event_handler("on_user_turn_idle")` → 服务端**主动关 WS**(reason=`idle_no_speech`)。
- 语义(BotStopped 武装/BotStarted·UserStarted 取消/回合与 function-call 中不武装)即我们要的 gate;tts_bridge 已 `UPSTREAM` push `BotStarted/BotStoppedSpeakingFrame`(MinWords 同源在用),帧能到达 UserTurnProcessor。
- 已知缝:bot 从未说话前不武装——"唤醒后首轮无回复就静默"由固件端 30s VAD(2.1)兜住(OR 语义),不改服务端。

**② idle#2(失联)= 复用 `PipelineWorker` 无音频帧 idle(关键简化)**:
- 判据:`idle_timeout_frames=(InputAudioRawFrame,)` 已存在,`IdleFrameObserver` 每收一帧音频重置;持续无音频帧超时触发。**设备在 Realtime 下只要连着就持续传音频帧**;设备正常 idle 会主动 `CloseAudioChannel`(= 断 WS)走 disconnect 路径收 session;**只有异常失联(断电/断网/半开)才"WS 没关但音频停"**,正是本 idle 兜底的对象 → 语义精确。
- 改动:session 的 `PipelineWorker` 构造 `idle_timeout_secs=VOICE_LOST_TIMEOUT_SECS(默认30,原 120s)`;subscribe `on_idle_timeout` → 关 WS(reason=`lost`)。
- **自研 LivenessWatchdog、固件心跳 ping、服务端 ping 识别全部废弃**——判据是"无音频帧",ping 不参与,收了也不重置 worker idle。`voice/pipecat/liveness.py` + `test_liveness.py` 删除。

### 2.3 协议与配置

| 端 | 配置键 | 默认 | 用途 |
|---|---|---|---|
| 固件 Kconfig | `CONFIG_DEVICE_IDLE_ENABLE` / `_IDLE_SILENCE_SECS` | on / 30 | idle#1 静默断 WS |
| 服务端 env | `VOICE_IDLE_SILENCE_SECS` / `VOICE_LOST_TIMEOUT_SECS` | 30 / 30 | idle#1 窗口 / idle#2 worker idle 超时 |

协议消息(心跳 ping 已废弃;新增**一条**下行生成期标记):

| 消息 | 方向 | 时机 | 设备动作 |
|---|---|---|---|
| `{"type":"turn","state":"start"}` | server→device | 用户回合合并后、dispatch 给 LLM 前(gateway 咽喉点) | 置 `turn_in_flight_`,暂停 idle#1 计时并清零;回复的 `tts/start·stop·abort` 清标志恢复 |

服务端 env 取值在代码里做健壮解析(`session.py _env_float`):占位符/非数字值(绕过 deploy 的裸 `kubectl apply`、Actions var 误配)→ warn + 回退 30,不 crash-loop(审计 #3)。

**Tradeoffs / 关键决策**
- idle#1 用 `UserIdleController`(内建于 UserTurnProcessor,含 function-call/回合 guard;业界同款语义),不自研。代价:接线依赖 1.8.1 内部结构,若 API 有出入以本机源码为准(首步最小接线验证)。
- idle#2 复用 worker idle vs 自研 watchdog(收任何包):选**前者**——本产品音频帧即 liveness(Realtime 持续传),自研 watchdog 需引心跳、多一套状态,收益为零。代价:静默≠无帧,故 idle#1 与 idle#2 判据不同,必须分开(idle#1 数"无用户语音 turn",idle#2 数"无音频帧")。
- idle 动作=断 WS(而非仅 `listen/stop`):符合 PRD idle 定义,结束占用;代价:追问窗口之外需唤醒词(产品已接受)。
- 双端 30s 独立计时(OR):冗余兜 VAD/STT 任一失灵,代价轻微(各自偏置)。

## 3. Testing and validation

映射 PRD §8 验收场景(产品行为不重复):

| PRD 验收 | 单测(先于实现) | 真机验证 |
|---|---|---|
| 问答后 35s 静默→断 WS 回待命 | 固件 idle_policy host:Realtime 下 30 ticks 触发/29 不触发;服务端 UserTurnProcessor 配 user_idle_timeout 后 on_user_turn_idle→close | checklist#1 |
| 回复后 20s 追问→无缝续聊 | 服务端:UserStarted 取消计时/新 BotStopped 重新武装(内置 controller);固件:voice 重置 ticks | #2 |
| 长回复>30s 不误断 | 固件:has_audio_to_play=true 不计;服务端:BotStarted/BotStopped 间不计 | #3 |
| 生成期>30s 无首帧不误断(审计#1) | 固件 host:agent_busy 100 ticks 不触发、busy 清后窗口归零重计;服务端:turn/start 早于 tts/start 的顺序断言 | checklist#3 变体 |
| 播放中打断不误 idle | 服务端:回合进行中不武装(内置 controller);固件:打断路径不进 Listening 计时 | #4 |
| 对话中断电→服务端 ~30s 收口 | 服务端:worker 无 InputAudioRawFrame 超 idle_timeout_secs→on_idle_timeout→close(reason=lost) | #5 |
| 复位重启→新 session、不丢行 | 服务端:session 结束 controller.stop()(UserTurnProcessor cleanup 自带);固件:重启重连路径回归 | #6 |

- 固件单测:最小 host target(不烧录),编 `idle_policy` 用例,断言 gate/阈值/重置/动作决策(tests/idle_policy_test.cc)。
- 服务端单测(pytest):接线验证 = 全链路 harness(test_pigugu_server)驱动:①回复后无后续音频→session 主动 close(idle_no_speech);②连接后无任何音频帧→worker idle 超时 close(lost);③env 默认/覆盖。
- 回归:服务端 `tests/unit/voice` + `tests/unit/metrics` 全量;固件 host test + 现有行为。

## 4. Parallelization

本 feature 固件与服务端**紧耦合、需顺序验证**,不使用并行子代理:两端共享同一套 idle 语义与验收,并行易出现口径漂移;且发布顺序要求先服务端后固件(PRD §6)。(心跳方案废弃后无独立并行线。)

## 5. Risks and mitigations

| 风险 | 缓解 |
|---|---|
| Pipecat `UserTurnProcessor` 1.8.1 内部 UserIdleController 接线/事件名不符 | 实现首步先写接线最小验证(读本机源码 + 全链路 harness 跑一次 on_user_turn_idle);不符则改在 session 层自建轻量 turn 计时 |
| worker idle 误杀"WS 开着但短暂无音频"会话 | 判据验证:Realtime 下设备连着就持续传帧,无帧=真失联;全链路 harness 覆盖"连接无音频→lost"与"活跃音频不误杀" |
| 固件 host 测试基建缺失 | 抽纯 `idle_policy` 最小化耦合;宿主 g++ 小 target;设备行为仍以 checklist 兜 |
| 双端计时偏置/误判 | OR 冗余;参数 env/sdkconfig 可调;加 idle 原因日志(固件 VAD/服务端 idle/lost)核对 |
| 发布中旧固件+新服务端 / 反向 | PRD §6 兼容矩阵;先服务端后固件 |
| idle 主动断被当异常 | 固件先 `SendStopListening()` 礼貌通知再 Close;close code 语义确认 |

## 6. Follow-ups

- commit 本设计 + PRD + 两个 skill(prd-generator/write-tech-spec)到 feat/device-idle,文档留痕。
- 实现期若模块边界/时序变化,同 PR 更新本 TECH.md(keep spec current)。
