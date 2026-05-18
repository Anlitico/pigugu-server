# Roast Scenarios — 爬虫管线 ↔ Agent 共用数据表

## 1. 数据表

```sql
CREATE TABLE IF NOT EXISTS roast_scenarios (
    roast_id        TEXT PRIMARY KEY,                  -- "poison_2026-05-17_001"
    game_mode       TEXT NOT NULL,                     -- poison_opinion | debate | prediction | breaking_bomb
    prompt          TEXT NOT NULL,                     -- L4 context 注入文本（token-limited，建议 ≤500 tokens）
    news_id         TEXT DEFAULT '',                   -- 来源 post_id / news reference
    tags            JSONB DEFAULT '[]',                -- 分类标签，可扩展
    status          TEXT NOT NULL DEFAULT 'active',    -- active | expired
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    expires_at      TIMESTAMPTZ DEFAULT NULL
);

CREATE INDEX IF NOT EXISTS idx_roast_scenarios_mode
    ON roast_scenarios(game_mode, status);
```

## 2. 双方接口

| 谁 | 操作 | 说明 |
|----|------|------|
| **爬虫管线**（你） | `INSERT INTO roast_scenarios` | 分类器生成 `prompt` 文本后写入 |
| **ContextManager**（我） | `SELECT prompt WHERE roast_id = $1` | 用户进入玩法时加载，注入 LLM context 的 L4 层 |

- 你负责：分析帖子 → 生成 `prompt`（游戏场景描述）→ 写入这个表
- 我负责：按 `roast_id` 读取 → 注入 Agent context → 对话结束后标记 `status = 'expired'`
- 此表可新增字段，便于后续数据分析

## 3. Context 注入机制（供参考）

Agent 的 LLM 上下文按以下顺序组装：

```
[system] persona + global rules
[system] user profile
[system] conversation summaries (compressed)
───
[user]   recent chat turns
[user]   roast_scenarios.prompt  ← 你的游戏场景文本，作为 user 角色注入
[user]   in-game chat turns
```

玩法进行中，如果对话过长，`prompt` 会被压缩进 roast summary（原封不动保留 prompt 文本，只压缩 gameplay 部分）。玩法结束后，整个 roast 压缩进对话历史摘要。

## 4. 分类器 LLM 调用

分类器直接复用项目已有的 `core.llm` provider 池，不需单独创建 OpenAI client：

```python
from core.llm import get_llm, Message

llm = get_llm("qwen3.6-plus")
resp = await llm.chat(
    messages=[Message.user(CLASSIFIER_PROMPT)],
    model="qwen3.6-plus",
    response_format={"type": "json_object"},
    temperature=0.1,
)
```

- 推荐模型 `qwen3.6-plus`（类 GPT-4 级别，分类 + 提取够用，成本低于 plus）
- 已封装重试、超时、fallback，不需自己管理 HTTP client
- API key 从环境变量 `DASHSCOPE_US_API_KEY` 读取（provider 池已配置）

## 5. 开发接口约定

双方按以下接口独立开发，互不阻塞：

| 接口 | 提供方 | 消费方 | 说明 |
|------|--------|--------|------|
| `roast_scenarios` 表 | 爬虫管线（你） | ContextManager（我） | 你写入，我读取 |
| `trump_social_posts` 表 | 爬虫（已有） | 分类器（你） | 爬虫 upsert 后触发分类 |
| `ContextManager.end_roast(roast_id)` | ContextManager（我） | 后续流程 | 玩法结束时标记 `status=expired` |

```python
# 爬虫管线侧（你）
from core.llm import get_llm, Message

async def classify_and_store(post: dict) -> None:
    # 1. 调用 LLM 分类 + 生成 prompt
    llm = get_llm("qwen3.6-plus")
    resp = await llm.chat(
        messages=[Message.user(build_classifier_prompt(post))],
        model="qwen3.6-plus",
        response_format={"type": "json_object"},
    )
    data = json.loads(resp.content)
    
    # 2. 写入共用表
    await db.execute(
        "INSERT INTO roast_scenarios (roast_id, game_mode, prompt, news_id, expires_at) "
        "VALUES ($1, $2, $3, $4, $5)",
        data["roast_id"], data["game_mode"], data["prompt"],
        post["id"], data.get("expires_at"),
    )
```

```python
# ContextManager 侧（我）
async def load_roast_prompt(roast_id: str) -> str:
    row = await db.fetchrow(
        "SELECT prompt FROM roast_scenarios WHERE roast_id=$1 AND status='active'",
        roast_id,
    )
    return row["prompt"] if row else ""
```

## 6. 待确认

- `prompt` 的 token 上限建议 5000 tokens（软限制，超限不拒绝写入但 ContextManager 会告警），是否满足业务场景？
- `expires_at` 的过期策略由你决定（48h / 按 mode 不同 / 手动）？
- `game_mode` 的值列表是否定稿（poison_opinion | debate | prediction | breaking_bomb）？
- 是否需要 `metadata JSONB` 字段存放 mode-specific 结构化数据（如 debate 的 weakness/strength），供 Agent 工具调用时查询？
- 分类器用 `qwen3.6-plus` 是否满足精度要求？如需更强推理可换 `qwen-plus`
