# 爬虫 → 玩法规则生成管线

> 目标：每轮爬虫入库后，自动分析特朗普新帖，提取与四种玩法相关的关键信息，生成游戏场景文本（prompt），写入 `roast_scenarios` 共用表，供 ContextLoader 在用户进入玩法时注入 Agent 上下文。
>
> **核心原则：管线只做分类+提取，不做对话生成。** 主 Agent 已经完整掌握 Pigugu 人设和每种玩法的对话脚本——管线只需要告诉它"这条帖适合哪个玩法、游戏场景是什么"。

---

## 1. 整体架构

```
[K8s CronJob 每日 10:00 UTC]
            │
            ▼
[爬虫写入 trump_social_posts] ─── 返回本次新插入的 post 列表
            │
            ▼
[玩法分类器] ─── LLM 分析每条新帖：适合哪些玩法？生成该玩法的游戏场景 prompt
            │
            ▼
[写入 roast_scenarios 表] ─── 一条 post 可对应多条 scenario（每条一个 game_mode）
            │
            ▼
[ContextLoader 查询] ─── 用户进入玩法时按 roast_id 读取 prompt，注入 L4 上下文
```

**核心原则**：爬虫只管数据采集，玩法分类是独立后处理。分类失败不影响爬虫写入。分类与爬虫在同一 CronJob 执行（upsert 后同步调用 classifier），分类失败有模板兜底，不会丢失数据。

---

## 2. 共用数据表 `roast_scenarios`

> 此表由爬虫管线写入，ContextLoader 读取。双方接口约定详见 [roast-scenarios-shared-table.md](./roast-scenarios-shared-table.md)。

```sql
CREATE TABLE IF NOT EXISTS roast_scenarios (
    roast_id        TEXT PRIMARY KEY,                  -- "poison_2026-05-17_001"
    game_mode       TEXT NOT NULL,                     -- poison_opinion | debate | prediction | breaking_bomb
    prompt          TEXT NOT NULL,                     -- L4 context 注入文本（建议 ≤500 tokens）
    headline        TEXT NOT NULL DEFAULT '',          -- 卡片标题（≤120 chars）
    source          TEXT NOT NULL DEFAULT '',          -- 来源平台：truthsocial / x
    source_url      TEXT NOT NULL DEFAULT '',          -- 原始帖子链接
    teaser          TEXT NOT NULL DEFAULT '',          -- Pigugu teaser（≤150 chars）
    is_urgent       BOOLEAN NOT NULL DEFAULT FALSE,    -- 紧急标记
    news_id         TEXT DEFAULT '',                   -- 来源 trump_social_posts.id（UUID）
    tags            JSONB DEFAULT '[]',                -- 分类标签，可扩展
    status          TEXT NOT NULL DEFAULT 'active',    -- active | expired
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    expires_at      TIMESTAMPTZ DEFAULT NULL
);

CREATE INDEX IF NOT EXISTS idx_roast_scenarios_mode
    ON roast_scenarios(game_mode, status);
```

### 2.1 `roast_id` 命名规则

格式：`{mode_abbrev}_{date}_{seq}`

| mode_abbrev | 对应 game_mode |
|---|---|
| `poison` | `poison_opinion` |
| `debate` | `debate` |
| `predict` | `prediction` |
| `bomb` | `breaking_bomb` |

示例：`poison_2026-05-17_001`、`debate_2026-05-17_002`

### 2.2 `prompt` 字段内容（按模式）

**管线生成的是英文自然语言游戏场景描述，不是结构化 JSON。ContextLoader 直接将此文本注入 Agent 的 L4 上下文。**

**毒观点 (`poison_opinion`)** — 场景文本包含：帖文内容 + 争议角度标签 + 矛盾钩子：

```
[POISON SCENARIO]
Trump just posted on Truth Social: "Excellent Poll Numbers. Thank you!"
Controversy angle: TRUMP_POLL_BRAG
Hook: He cites no specific poll — the claim is unverifiable.
```

**来辩 (`debate`)** — 场景文本包含：帖文内容 + 核心主张 + Pigugu 挑衅立场 + 论据强处 + 论据弱处：

```
[DEBATE SCENARIO]
Trump posted: "China has been hit so hard by our Tariffs..."
Core claim: Tariffs have severely damaged China while boosting the US.
Pigugu's provocative stance: Tariffs actually hurt US consumers — China is still growing.
Argument strength: Strong absolute language, appeals to nationalist sentiment.
Argument weakness: No data cited, ignores tariff costs on the US side.
```

**预测混乱 (`prediction`)** — 场景文本包含：帖文内容 + 预测目标 + 截止时间 + 揭晓标准：

```
[PREDICTION SCENARIO]
Trump posted: "We will have a deal with Iran by Friday..."
Prediction target: A deal with Iran by Friday.
Deadline: 2026-05-15T23:59:59Z
Resolution criteria: Formal agreement or public announcement by the deadline.
```

**突发炸弹 (`breaking_bomb`)** — 场景文本包含：帖文内容 + 紧急原因：

```
[BREAKING SCENARIO]
Trump just posted authorizing precision strikes in Syria.
Urgency reason: Major military action — ongoing strikes with immediate geopolitical implications.
```

---

## 3. 分类器流程

### 3.1 触发时机

爬虫 `upsert_posts()` 之后，拿到本次**新插入**的 post 列表（`updated` 的跳过，不重复分类）。对每条新帖同步调用 LLM 进行分类。分类与爬虫在同一 CronJob 进程内执行，无额外调度。

### 3.2 LLM 调用

分类器使用项目已有的 `core.llm` provider 池，通过 DeepSeek API（OpenAI 兼容）：

```python
import json
from core.llm import get_llm, Message

async def classify_and_store(post: dict) -> None:
    llm = get_llm("deepseek-chat")
    resp = await llm.chat(
        messages=[Message.user(build_classifier_prompt(post))],
        model="deepseek-chat",
        response_format={"type": "json_object"},
        temperature=0.1,
    )
    data = json.loads(resp.content)

    for mode_result in data["modes"]:
        await db.execute(
            "INSERT INTO roast_scenarios (roast_id, game_mode, prompt, news_id, expires_at) "
            "VALUES ($1, $2, $3, $4, $5)",
            mode_result["roast_id"], mode_result["game_mode"],
            mode_result["prompt"], post["id"], mode_result.get("expires_at"),
        )
```

- 模型 `deepseek-chat`（DeepSeek V4 fast）
- 同步 OpenAI client + `asyncio.to_thread`（跨平台可靠）
- 超时 60s，重试 2 次
- API key 从环境变量 `DEEPSEEK_API_KEY` 读取

### 3.3 分类 Prompt

```
You are a content classifier. Below is a Trump social media post on {platform}.

Post content: {content}
Posted at: {created_at}
Tags: {tags}

Determine which Pigugu game modes this post fits, and for each match
generate a game scenario prompt in English.

Four modes:
- poison_opinion: The post has controversy or a hot-take angle → poison scenario
  MUST include: post content + controversy angle tag + hook (weakest point)

- debate: The post makes a clear claim/argument → debate scenario
  MUST include: post content + core claim + Pigugu's provocative stance
  (pick the angle the user is MOST likely to disagree with) + argument strength + weakness

- prediction: The post contains a verifiable prediction/deadline → prediction scenario
  MUST include: post content + prediction target + deadline + resolution criteria

- breaking_bomb: The post is a major breaking event → breaking scenario
  MUST include: post content + urgency reason. is_urgent true ONLY for war/military/major disaster.

Return JSON. Only return modes that actually fit — skip unfit modes.
```

### 3.4 过滤与入库

| 模式 | 入库条件 | 过期时间 |
|---|---|---|
| 毒观点 | 总是入库 | 帖子发布后 48h |
| 来辩 | prompt 包含有效主张 | 帖子发布后 48h |
| 预测混乱 | prompt 包含预测目标且截止时间在未来 | 截止时间 |
| 突发炸弹 | prompt 中标记为紧急 | 帖子发布后 2h |

### 3.5 调度策略

- **同步执行**：爬虫进程内串行调用 LLM 分类，每个 post 一次 API 调用。
- **成本控制**：使用 `deepseek-chat`，纯分类任务，token 极少。
- **去重**：`roast_id` 为主键，同一条 post 同一种 mode 不会重复生成。入库前检查 roast_id 是否存在，冲突时追加后缀。
- **兜底**：LLM 调用失败 → 至少生成一条 `poison_opinion` scenario（模板生成，不需要 LLM）。

---

## 4. ContextLoader 消费方式

> 此部分由 ContextLoader 侧实现，此处仅说明接口约定。详见 [roast-scenarios-shared-table.md](./roast-scenarios-shared-table.md)。

### 4.1 加载

ContextLoader 在用户进入玩法时按 `roast_id` 读取 prompt：

```python
async def load_roast_prompt(roast_id: str) -> str:
    row = await db.fetchrow(
        "SELECT prompt FROM roast_scenarios WHERE roast_id=$1 AND status='active'",
        roast_id,
    )
    return row["prompt"] if row else ""
```

### 4.2 注入位置

prompt 作为 user 角色消息注入到 Agent 的 L4 上下文层：

```
[system] persona + global rules
[system] user profile
[system] conversation summaries (compressed)
───
[user]   recent chat turns
[user]   roast_scenarios.prompt  ← 游戏场景文本，作为 user 角色注入
[user]   in-game chat turns
```

### 4.3 管线不管的事

以下由 Agent 自己决定，管线不生成：
- 用什么语气开场
- 选哪条素材先抛出
- 怎么回应用户的立场
- 什么时候认输
- 预测揭晓时怎么嘲讽

---

## 5. 实现优先级

| 阶段 | 内容 | 状态 |
|---|---|---|
| **Phase 1** | `roast_scenarios` 表 + 迁移 | done |
| **Phase 2** | 分类器（LLM prompt 模板 + 解析 + 入库） | done |
| **Phase 3** | 爬虫管线集成（upsert 后自动触发分类） | done |
| **Phase 4** | 联调 ContextLoader（验证 prompt 读取 + L4 注入） | pending |

---

## 6. 示例：一条真实帖子 → 分类输出

### 输入

```
平台: truthsocial
内容: "Excellent Poll Numbers. Thank you!"
发布时间: 2026-05-11T01:36:21.695Z
点赞: 9789, 转发: 1871, 回复: 910
标签: []
```

### 分类器输出

```json
{
  "modes": [
    {
      "roast_id": "poison_2026-05-11_001",
      "game_mode": "poison_opinion",
      "prompt": "[POISON SCENARIO]\nTrump just posted on Truth Social: \"Excellent Poll Numbers. Thank you!\" This is a classic Trump poll brag. The weakest point: he doesn't cite any specific poll, leaving the claim unverifiable. Controversy angle: TRUMP_POLL_BRAG. Hook: The post lacks any source or context, making it a hollow boast.",
      "expires_at": "2026-05-13T01:36:21Z"
    },
    {
      "roast_id": "debate_2026-05-11_001",
      "game_mode": "debate",
      "prompt": "[DEBATE SCENARIO]\nTrump posted: \"Excellent Poll Numbers. Thank you!\"\nCore claim: His poll numbers are excellent and he deserves thanks.\nPigugu's provocative stance: Good poll numbers don't mean good leadership — they may just reflect a partisan echo chamber.\nArgument strength: Poll numbers are objective data that resonate with supporters.\nArgument weakness: No specific poll cited — the numbers are unverifiable.",
      "expires_at": "2026-05-13T01:36:21Z"
    }
  ]
}
```

`prediction` 和 `breaking_bomb` 未生成——这条帖没有可验证的预测，也不够紧急。

### Agent 拿到 prompt 后自己发挥（示意，不是管线生成的）

- **毒观点**: "He says 'Excellent Poll Numbers.' Which poll? What sample size? What margin of error? Nothing — just 'Excellent.' What do you think?"
- **来辩**: "Let me defend him. 'Excellent' is an adjective, not a data point. He's describing a feeling, and feelings don't need sources. Go ahead, prove me wrong."

管线只给了游戏场景描述，Agent 自己组织语言。管线没写 Pigugu 的台词。

---

## 7. 部署配置

### 环境变量

| 变量 | 说明 |
|---|---|
| `DEEPSEEK_API_KEY` | DeepSeek API Key |
| `DEEPSEEK_BASE_URL` | (可选) 默认 `https://api.deepseek.com/v1` |

### 推送链路

```
本地 shell / .env
    → GitHub Secret (DEEPSEEK_API_KEY)
    → .github/workflows/deploy.yml (env + Python 替换脚本)
    → k8s/secrets.yaml (占位符 → 实际值)
    → K8s CronJob Pod 环境变量
```

---

## 8. 关于 X/Twitter 来源

X 和 Truth Social 的帖子对分类器完全透明——都走同一条 `trump_social_posts → LLM 分类 → roast_scenarios` 管线。X 爬取恢复后无需额外开发。
