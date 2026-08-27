# Pigugu Voice — Per-Turn ClickHouse + S3 Audio Storage

## 背景

`pigagent/voice/connection.py` 之前把 WAV 写到 `/tmp/pigugu_*.wav`：
- TTS 文件 `/tmp/pigugu_out_{session_id}.wav` 是**每 session 覆写**（每个新 turn 覆盖上一个 turn 的音频）
- 输入文件按 sid 分散在 `/tmp` 里
- 全部使用 pod 本地临时存储，pod 重启即丢失
- 无 per-turn 分组、无元数据索引、无法按 `device_id` / `user_id` / `turn_id` 检索
- `voice_segments`（"用户说了什么话"）的标记缺失，无法选择性回放只听用户说的话

这套新架构把所有 turn 的原始 PCM + 完整元数据落库，**按 `device_id` / `session_id` / `turn_id` 可查可定位**，并能选择性只回放用户说话段。

## 设计要点

### 一次 turn 落 5 个 S3 文件 + 1 行 ClickHouse

每次 turn 完成（STT final + TTS 完成/中断/空），产生 5 个 S3 文件 + 1 行 CH 索引：

```
s3://pigugu-clickhouse-audio/
  {utc_date}/{session_id}/{turn_id}/
    input.wav         # 原始 user PCM（16k mono int16，未做增益）
    input.json        # voice_segments[]、stt_interims[]、abandoned_stts[]、stt_status
    tts.wav           # TTS PCM，可能 0 字节（LLM 空回 / TTS 未起音）
    tts.json          # tts_status、tts_truncated_reason
    turn.json         # turn_id / user_id / device_id / session_id / turn_idx / models / telemetry
```

`turn_id` 格式：`{utc_start_ms}_{session_id}_{turn_idx:04d}`
- 例：`1787734749647_fef37f1f_0001`
- 单调递增、可排序

### Commit 顺序：S3 先、CH 后（best-effort）

1. 计算 `voice_segments`（从本 turn 的 Silero chunk flags 切片）
2. 构造 5 个文件 payload
3. **S3 上传**（5 个 PUT，顺序执行）
   - 任何 PUT 失败 → 记 ERROR `error_phase="s3"`，**中止**（不写 CH）
4. **ClickHouse INSERT**（`asynch` 异步驱动）
   - INSERT 失败 → 记 ERROR `error_phase="clickhouse"`，**S3 文件保留为孤儿**（等 v2 janitor CronJob 对账）

这个顺序保证不会写出"指向不存在音频"的 CH 行；孤儿 S3 文件（无 CH 行）是有意接受的失败模式。

### S3 桶

- 桶名：`pigugu-clickhouse-audio`
- region：`us-west-1`
- 生命周期：400 天（CH TTL 365 天 + 余量）

### ClickHouse 表

`voice.turns`（MergeTree，参见 `k8s/clickhouse-migration-job.yaml` 引用的 `migrations/0001_voice_turns.sql`）：

```sql
ENGINE = MergeTree
PARTITION BY toYYYYMM(fromUnixTimestamp64Milli(utc_start_ms))
ORDER BY (device_id, fromUnixTimestamp64Milli(utc_start_ms), turn_id)
TTL fromUnixTimestamp64Milli(utc_start_ms) + INTERVAL 365 DAY
```

- `PARTITION BY month`：便于按月冷热分层（后续可加 S3 冷存）
- `ORDER BY (device_id, ts, turn_id)`：主查询模式是"某 device 最近 N 条 turn"
- `TTL 365d`：CH 端 1 年后自动 DROP，S3 端 lifecycle 400d 兜底

### voice_segments 来源：复用 Silero chunk flags

**不引入新的 RMS 静音检测器**——Silero VAD 每个 32ms 块已经输出 `is_speech` 布尔。piggyback：

1. `silero.py` 每次产出 chunk 后，append `is_voice` 到 `conn._voice_chunk_flags: list[bool]`
2. 列表上限 ~18750 项（≈10 分钟），amortized 10% trim，防止长 session 无界增长
3. `TurnStorage` 在每个 turn 开始时记录 `len(_voice_chunk_flags)` 作为本 turn 的起点
4. `commit()` 时取切片 `flags[start_idx:]`，调 `voice.segments.compute_voice_segments()` 算出 `{start_ms, end_ms, duration_ms}[]`
5. 切到 `input.json` 的 `voice_segments[]` 和 CH 行的 `voice_segments` 列（`Array(Tuple(Int32, Int32, Int32))`）

算法（`voice/segments.py`）：开 segment 遇到第一个 `True`；遇到连续 10 个（≈320ms）`False` 后关 segment。短于 320ms 的 gap 自动合并。容错：`voice_chunk_flags_slice()` 抛异常时回退到 `[]`（不阻塞 commit）。

### STT interims 收集

Deepgram 每次发 `is_final=false` 的 interim 消息时，agent 把它记到 `InterimBuffer`（`threading.Lock` + `deque(maxlen=1024)`）：

- **跨线程安全**：Deepgram 的 `on_message` 在后台线程跑；用 `deque + Lock` 而不是 `asyncio.Queue`，免去 `loop.call_soon_threadsafe` 跳板
- **STT final 时排空**：interim 序列进入 `stt_interims[]`（LLM 实际看到的是这一个 final，但 LLM 是被一系列越来越接近 final 的 interim "驱动"的，把它们存起来对调试 STT 质量很有价值）
- **barge-in 时**排到 `abandoned_stts[]`

### TTS 没起音 / 被中断

- TTS 未起音（LLM 空回）：写 `tts.wav`（0 字节）+ `tts.json`，`tts_status="empty"`，`tts_truncated_reason="no_tts_started"` 或 `"barge_in"`
- TTS 完成：`tts_status="complete"`，`tts_truncated_reason=""`
- TTS 被中断（用户打断）：TTS 已经合成但还没播完的部分保留在 `tts.wav`，`tts_status="interrupted"`，`tts_truncated_reason="barge_in"`（或其他中止原因）
- TTS 被 `CancelledError` 取消（barge-in / abort / inject-replace）：`_tts_producer_consumer` 的 `mark_tts_complete` 调用包在 `try/except CancelledError` 里，确保 cancel 时也写上 `tts_status="interrupted"`，`tts_truncated_reason="cancelled"`（如果之前没标过）

### Inject (roast 推送) 不污染主 turn 的 TTS 缓冲

`_inject_tts` 在 `_turn_storage` 处于活动状态时，会把 inject 自己的 TTS PCM 写到**独立的** `/tmp/pigugu_inject_out_{session}_{sentence_id}_{ts}.wav`，**不会** append 到 `self._turn_storage.tts_pcm_buf`——否则 inject 的声音会混进下一个真实 turn 的 `tts.wav`，S3 上的音频就跟 `tts_text` 对不上。

### TurnStorage 的位置与生命周期

- `TurnStorage` 实例挂在 `ConnectionHandler` 上（**不是** PigAgent）
  - 一个连接 = 一个会话；`TurnStorage` 也是 per-turn
  - 同一连接上 turn 结束/中断时新 turn 复用 `ConnectionHandler`、开新 `TurnStorage`
- 使用 `__slots__` 减少内存（每字段单独 slot）
- `commit()` 是 **fire-and-forget**（`asyncio.create_task(storage.commit())`），失败只记日志，从不冒泡到 WS loop——与既有的 `_save_input_wav` / `_save_tts_wav` "吞掉异常 + 记日志"行为一致
- 幂等：第二次调 `commit()` 直接 short-circuit（`_committed` + `commit_started` 标志位）
- **重要：`ConnectionHandler._turn_storage_committing_turn_idx`** 跟踪当前哪个 turn_idx 的 commit 在飞。`_save_input_wav` 清理分支在构造 phantom TurnStorage 之前会查这个标志位——如果主 task 已经 fire-and-forget 了 commit，但 `asr_audio` 还没清，清理分支就不会用同一份 PCM 再造一个 TurnStorage（否则同一个 turn 会有两个 S3 目录 + 两行 CH）

### voice_chunk_flags 切片捕获

`TurnStorage` 构造时把 `_voice_chunk_start` 捕获到 lambda 闭包里，**不**在 `commit()` 调时读 `self._voice_chunk_start`——因为 S3 + CH I/O 要几秒，而下一个 turn 在这期间会覆写 `_voice_chunk_start` 指向更靠后的索引，导致本 turn 的 `voice_segments` 计算错位（拿不到本 turn 的 voice 段）。

## EKS / 资源

### 新 Node Group: `ng-clickhouse`

- 实例：`c7g.large` spot（2 vCPU / 4 GiB，arm64，**Graviton3**）
- 污点：`workload=clickhouse:NoSchedule`
- 标签：`workload=clickhouse`、`kubernetes.io/arch=arm64`
- 容量：1 节点起步（v1 HA 单副本接受 SPOF；v2 扩 2 副本 + clickhouse-keeper）

### 资源

| 资源 | 类型 | 说明 |
|---|---|---|
| `k8s/clickhouse.yaml` | StatefulSet (1) + Service (ClusterIP) | CH server, ports 8123/9000 |
| `k8s/clickhouse-pvc.yaml` | PVC 50GB | 绑 `pigugu-gp3` |
| `k8s/clickhouse-storageclass.yaml` | StorageClass | `gp3`, iops=3000, throughput=125, WaitForFirstConsumer |
| `k8s/clickhouse-configmap.yaml` | ConfigMap | `config.xml`（listen 0.0.0.0, timezone UTC, max_server_memory_usage 3G）+ `users.xml`（password sha256 hex placeholder，由 initContainer 替换）|
| `k8s/clickhouse-secret.yaml` | Secret | `password` + `password-sha256-hex` |
| `k8s/clickhouse-migration-job.yaml` | Job (one-shot) | 跑 `migrations/0001_voice_turns.sql` |
| `k8s/sa-pigugu-s3.yaml` | ServiceAccount | IRSA 注解指向 `pigugu-clickhouse-audio-writer` IAM role |

### Agent 侧

- 镜像 `pigagent` 启动时读 `CLICKHOUSE_URL` / `CLICKHOUSE_DATABASE` / `CLICKHOUSE_PASSWORD` / `AUDIO_S3_BUCKET` / `AUDIO_S3_PREFIX` / `ENABLE_TURN_STORAGE` 等环境变量
- 挂 `pigugu-s3-sa` ServiceAccount（IRSA）以获取 S3 写权限
- Pod 重启时 TurnStorage 丢失，**未 commit 的 turn 数据全部丢失**（已 commit 的在 S3 + CH 里安全）

## 失败语义

| 失败点 | 行为 | 数据状态 |
|---|---|---|
| S3 不可达 | commit() 记 ERROR，**无 CH 行**；S3 上什么都没写 | turn 音频丢失（可接受：录音时 S3 故障基本等于 pod 不健康） |
| CH INSERT 失败 | commit() 记 ERROR，**S3 文件保留** | 孤儿 S3 文件（v2 janitor 对账） |
| Voice segments 计算抛异常 | 走 `try/except`，`voice_segments=[]`，commit 照常继续 | 音频完整，仅少一段元数据 |
| Payload build 抛异常 | commit() 记 ERROR `error_phase="build"`，早返回 | 音频丢失（同 S3 失败） |
| 重复 commit() | `_committed` short-circuit | 无副作用 |

## 验证

### 单元测试

```bash
.venv/bin/pytest tests/unit/voice/ -v
```

覆盖：
- `test_segments.py` — `compute_voice_segments` 边界（空、全静音、全语音、短 gap 合并、长 gap 分段、阈值边界）
- `test_interims.py` — `InterimBuffer` 跨线程并发写无丢失
- `test_storage.py` — `TurnStorage` 完整生命周期：5 文件 payload、commit 顺序（S3 先 / CH 后）、S3 失败时 CH 跳过、CH 失败时已 committed 不重试、幂等

### 上线 smoke

1. 部署 `ENABLE_TURN_STORAGE=true`
2. WS 端录音 ~30s，收到 STT final + TTS reply
3. `clickhouse-client --query "SELECT turn_id, stt_text, tts_status FROM voice.turns ORDER BY utc_start_ms DESC LIMIT 3"`
4. `aws s3 ls s3://pigugu-clickhouse-audio/$(date -u +%Y-%m-%d)/{session_id}/{turn_id}/` 应当列出 5 个文件
5. `aws s3 cp s3://.../input.wav /tmp/play.wav`，Audacity 打开；`input.json` 的 `voice_segments[]` 应与听得见的说话段吻合

### 故障演练

- 杀 CH pod：agent 记 WARNING，turn 继续工作（CH INSERT 重试到 backoff 上限后放弃），S3 文件仍写入
- 改 S3 桶名（不可达）：agent 记 ERROR per turn，CH INSERT 跳过

## 后续 / v2 待办

- **Janitor CronJob**：周期性扫 S3 prefix `voice-turns/` 里存在但 CH `voice.turns` 里没有对应 `s3_input_wav` 的孤儿文件 → INSERT 一行孤儿记录（或直接 DELETE）
- **S3 → CH 冷存**：`storage.xml` 加 S3 disk 配置，把超 30 天的 partition 推到 S3 标准层；超 180 天推到 Glacier
- **ClickHouse 副本**：2 副本 + clickhouse-keeper（替换单点 StatefulSet）
- **CH 查询物化视图**：按 `(device_id, date)` 预聚合 turn 计数 / 平均 e2e / 静音率，给运营 dashboard 用
- **CH 跨 region 复制**：us-west-1 ↔ us-east-1 异地备份（依赖 AWS IoT mTLS 现状）

## 依赖

`pyproject.toml` 新增：

```toml
"aioboto3>=13.0",  # async S3 client for TurnStorage uploads
"asynch>=0.3",     # async ClickHouse driver for voice.turns inserts
```

## 相关文档

- [CLAUDE.md](../CLAUDE.md) — Pigugu server 项目约定（文件管理、临时文件、技术文档）
- `k8s/clickhouse.yaml` — StatefulSet 完整定义
- `migrations/0001_voice_turns.sql` — `voice.turns` DDL
- `voice/storage.py` — `TurnStorage` 实现
- `voice/segments.py` — `compute_voice_segments` 算法
- `voice/interims.py` — 跨线程 `InterimBuffer`
