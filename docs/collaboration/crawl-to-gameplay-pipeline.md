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

**核心原则**：爬虫只管数据采集，玩法分类是独立后处理。分类失败不影响爬虫写入。

---

## 2. 共用数据表 `roast_scenarios`

> 此表由爬虫管线写入，ContextLoader 读取。双方接口约定详见 [roast-scenarios-shared-table.md](./roast-scenarios-shared-table.md)。

```sql
CREATE TABLE IF NOT EXISTS roast_scenarios (
    roast_id        TEXT PRIMARY KEY,                  -- "poison_2026-05-17_001"
    game_mode       TEXT NOT NULL,                     -- poison_opinion | debate | prediction | breaking_bomb
    prompt          TEXT NOT NULL,                     -- L4 context 注入文本（建议 ≤500 tokens）
    news_id         TEXT DEFAULT '',                   -- 来源 post_id
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

**管线生成的是自然语言游戏场景描述，不是结构化 JSON。ContextLoader 直接将此文本注入 Agent 的 L4 上下文。**

**毒观点 (`poison_opinion`)** — 场景文本应包含：帖文内容 + 争议角度 + 矛盾钩子：

```
[毒观点场景]
特朗普刚刚在 Truth Social 发帖："Excellent Poll Numbers. Thank you!"
争议角度：TRUMP_POLL_BRAG — 他声称民调极好，但帖子未引用任何具体数据或来源。
游戏钩子：引导玩家质疑——哪个民调？多少样本？误差多少？什么都没有就"极好"？
```

**来辩 (`debate`)** — 场景文本应包含：帖文内容 + 核心主张 + Pigugu 挑衅立场 + 帖子论据的强弱之处：

```
[来辩场景]
特朗普发帖声称关税对中国造成了巨大伤害，并引用了美国贸易代表办公室的官方声明。
核心主张：关税政策正在成功打击中国经济。
Pigugu 立场：替他辩护——"伤害确实发生了，伤害程度不重要，方向是对的。你来证明方向错了。"
帖子强处：引用了官方机构声明作为背书。
帖子弱处：只说"伤害很大"，没有给出具体伤害数据或机制。
```

**预测混乱 (`prediction`)** — 场景文本应包含：帖文内容 + 预测目标 + 截止时间 + 揭晓标准：

```
[预测场景]
特朗普发帖："We will have a deal with Iran by Friday, mark my words!"
预测目标：特朗普是否会在周五前与伊朗达成协议。
截止时间：2026-05-16T16:00:00Z。
揭晓标准：北京时间 5/16 16:00 前，是否有美伊双方官方宣布的协议。
```

**突发炸弹 (`breaking_bomb`)** — 场景文本应包含：帖文内容 + 紧急程度 + 紧急原因：

```
[突发场景 - 紧急]
特朗普刚刚发帖，直接表态涉及军事行动。
紧急原因：涉及战争/军事行动的直接表态，需立刻通知玩家。
```

---

## 3. 分类器流程

### 3.1 触发时机

爬虫 `upsert_posts()` 之后，拿到本次**新插入**的 post 列表（`updated` 的跳过，不重复分类）。对每条新帖调用 LLM 进行分类。

### 3.2 LLM 调用

分类器使用项目已有的 `core.llm` provider 池，不需要单独创建 HTTP client：

```python
import json
from core.llm import get_llm, Message

async def classify_and_store(post: dict) -> None:
    llm = get_llm("qwen3.6-plus")
    resp = await llm.chat(
        messages=[Message.user(build_classifier_prompt(post))],
        model="qwen3.6-plus",
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

- 推荐模型 `qwen3.6-plus`（分类 + 提取够用，成本低）
- 已封装重试、超时、fallback，API key 从环境变量 `DASHSCOPE_US_API_KEY` 读取

### 3.3 分类 Prompt

```
你是一个内容分类器。下面是一条特朗普在 {platform} 上的社交媒体帖子。

帖子内容：{content}
发布时间：{created_at}
标签：{tags}

请判断这条帖适合 Pigugo 的哪些游戏模式，并为每个适合的模式生成游戏场景文本（prompt）。

四种模式：
- poison_opinion：帖子有争议性或槽点 → 生成毒观点场景（帖文 + 争议角度 + 钩子）
- debate：帖子包含明确主张/观点 → 生成来辩场景（帖文 + 核心主张 + Pigugu 挑衅立场 + 帖子强弱处）
- prediction：帖子包含可验证预测/截止日期 → 生成预测场景（帖文 + 预测目标 + 截止时间 + 揭晓标准）
- breaking_bomb：帖子是重大突发事件 → 生成突发场景（帖文 + 紧急原因）

对每条 scenariol 生成:
- roast_id: "{mode_abbrev}_{date}_{3位序号}" (如 poison_2026-05-17_001)
- prompt: 自然语言游戏场景描述，≤500 tokens，结构见模式说明

返回 JSON。只返回适合的模式，不适合的不返回：
{
  "modes": [
    {
      "roast_id": "poison_2026-05-17_001",
      "game_mode": "poison_opinion",
      "prompt": "[毒观点场景]\n特朗普刚刚在 Truth Social...",
      "expires_at": "2026-05-19T01:36:21Z"
    },
    {
      "roast_id": "debate_2026-05-17_001",
      "game_mode": "debate",
      "prompt": "[来辩场景]\n特朗普发帖声称...",
      "expires_at": "2026-05-19T01:36:21Z"
    }
  ]
}
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
- **成本控制**：使用 `qwen3.6-plus`，纯分类任务，token 极少。
- **去重**：`roast_id` 为主键，同一条 post 同一种 mode 不会重复生成（通过 roast_id 中的日期+序号控制）。
- **兜底**：LLM 调用失败 → 至少生成一条 `poison_opinion` scenario（该模式只需帖文内容，可用模板生成，不需要 LLM）。

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

| 阶段 | 内容 |
|---|---|
| **Phase 1** | `roast_scenarios` 表 + 迁移 |
| **Phase 2** | 分类器（LLM prompt 模板 + 解析 + 入库） |
| **Phase 3** | 爬虫管线集成（upsert 后自动触发分类） |
| **Phase 4** | 联调 ContextLoader（验证 prompt 读取 + L4 注入） |

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
      "prompt": "[毒观点场景]\n特朗普刚刚在 Truth Social 发帖：\"Excellent Poll Numbers. Thank you!\"（点赞 9789，转发 1871）\n争议角度：TRUMP_POLL_BRAG — 他声称民调极好，但帖子未引用任何具体数据或来源。\n游戏钩子：引导玩家质疑——哪个民调？多少样本？误差多少？什么都没有就\"极好\"？",
      "expires_at": "2026-05-13T01:36:21Z"
    },
    {
      "roast_id": "debate_2026-05-11_001",
      "game_mode": "debate",
      "prompt": "[来辩场景]\n特朗普发帖：\"Excellent Poll Numbers. Thank you!\"\n核心主张：特朗普的民调支持率正在大幅领先。\nPigugu 挑衅立场：替他辩护——\"极好\"是一种情感表达而非数据声明，他有权对他的支持者说他们想听的话。你来证明他说错了。\n帖子强处：措辞自信，感谢粉丝暗示有群众基础。\n帖子弱处：未引用任何具体民调来源、样本量或误差范围——\"极好\"没有可验证的定义。",
      "expires_at": "2026-05-13T01:36:21Z"
    }
  ]
}
```

`prediction` 和 `breaking_bomb` 未生成——这条帖没有可验证的预测，也不够紧急。

### Agent 拿到 prompt 后自己发挥（示意，不是管线生成的）

- **毒观点**: "他说民调'极好'。哪个民调？多少样本？误差多少？什么都没说，就'极好'。你怎么看？"
- **来辩**: "我来替他辩护。'极好'可以是形容词不是数据，他在描述感觉，感觉不需要来源。来，反驳我。"

管线只给了游戏场景描述，Agent 自己组织语言。管线没写 Pigugu 的台词。

---

## 7. 关于 X/Twitter 来源

X 和 Truth Social 的帖子对分类器完全透明——都走同一条 `trump_social_posts → LLM 分类 → roast_scenarios` 管线。X 爬取恢复后无需额外开发。
