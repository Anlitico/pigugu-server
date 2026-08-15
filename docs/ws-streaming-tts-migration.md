# Cartesia WS Streaming TTS 迁移清单

## 目标

将 TTS 从"逐段 REST 合成"迁移到"持续流式合成"（Cartesia WebSocket streaming，`wss://api.cartesia.ai/tts/websocket`）：

```
现在：LLM → 攒字切段（10/40字符·逗号·80字符）→ 逐段 REST 合成（1.3~8.6s 波动）→ 流水线 → 节流发送
目标：LLM → 直通 Cartesia WS（无切段）→ 音频 chunk 持续流出 → 60ms 重帧 → 节流发送
```

收益：首字节音频 40-90ms（对比现在逐段 REST 1.3-8.6s）；段边界/卡顿/韵律问题彻底消失；架构更简单（删除切段与流水线预取）。

## 实施清单

### 1. Provider 层（cartesia.py）
- [ ] 安装官方 SDK `cartesia`（3.0.1+，生产版流式内部：每 context 队列、接收超时、`cancel()`、重连与健康检查）——**不要手写 aiohttp WS**
- [ ] 新增流式方法：`AsyncCartesia` → `client.tts.websocket()` → `ws.context()`
- [ ] 双任务结构：喂文本任务（`ctx.send(model_id, voice_id, continue_=True)` + `flush()` + 结束时 `no_more_inputs()`）/ 收音频任务（`recv_bytes()`）
- [ ] 保留现有 REST `synthesize()` 作为降级路径

### 2. Connection 层（connection.py）
- [ ] sender 简化为纯节流器：消费流式音频 → 按 60ms/960 样本重帧 → opuslib 编码 → 走现有 `_send_tts_frames` 虚拟时钟
- [ ] producer 直通：LLM chunk → Cartesia WS（文本不再进 accumulate）
- [ ] 删除 `_accumulate` 切段逻辑、流水线预取、逐段 synth 日志（或改为流式健康日志）
- [ ] abort 处理：关闭/`cancel()` Cartesia context（替代现在的任务 cancel）
- [ ] 重连策略（借鉴 Pipecat 官方修复模式）：
  - 指数退避 + 最大重试次数（如 3 次）
  - 检查连接真实 open 状态（不只看对象存在）
  - finally 彻底清理（socket/context 置 None）
  - session-closed 标志防孤儿重连
- [ ] 429 并发上限优雅处理（退避/降级 REST，不热重试）

### 3. 稳定性（来自调研的已知坑）
- [ ] Cartesia 服务端 5 分钟空闲自动断开——每回合用完即关，不留长连接；或心跳保活
- [ ] 预暖 WS 连接（会话建立时建连，首轮免握手 100-400ms）
- [ ] 会话结束正确 drain（`no_more_inputs()` + 收完音频再关，避免缓冲丢失）
- [ ] 服务商侧事故降级（status 页曾有 41 分钟故障——REST fallback 必须可用）

### 4. 测试
- [ ] 首字延迟实测（预期 40-90ms + 网络）
- [ ] 长回答（>1 分钟）稳定性压测
- [ ] 打断：abort 后 context 关闭是否干净、下一轮是否正常
- [ ] 韵律对比：流式 vs 现方案 B 听感
- [ ] 降级演练：杀 WS → REST fallback 生效

## 已知事实（调研记录）

- [Pipecat #871](https://github.com/pipecat-ai/pipecat/issues/871)：无限重连无退避的坑
- [cartesia-python 3.0.1](https://github.com/cartesia-ai/cartesia-python/pull/71)：官方 SDK 生产版流式重写
- [Pipecat Cartesia 空闲超时分析](https://blog.gitcode.com/9de92bacc73961617d035685329b5b15.html)：连接状态机 + 清理模式
- [LiveKit #2010](https://github.com/livekit/agents/issues/2010)：WebSocket 并发上限 429
- [Cartesia status](https://status.cartesia.ai/incidents/rrmjrzhf1kl0)：服务商侧事故记录
- 输出格式建议 raw PCM 16kHz（低延迟），重帧由服务端做

## 依赖的前置决策

- STT 侧（Deepgram）可能更换提供商（见项目记忆 deepgram-finalization-deferred）——若换 Flux 可一并对齐架构
