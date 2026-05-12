# 爬虫 → 玩法规则生成管线

> 目标：每轮爬虫入库后，自动分析特朗普新帖，提取与四种玩法相关的关键信息，供主对话 Agent 在实时会话中消费。
>
> **核心原则：管线只做分类+提取，不做对话生成。** 主 Agent 已经完整掌握 Pigugu 人设和每种玩法的脚本——管线只需要告诉它"这条帖适合哪个玩法、帖子里哪部分信息跟这个玩法相关"。

---

## 1. 整体架构

```
[K8s CronJob 每日 10:00 UTC]
            │
            ▼
[爬虫写入 trump_social_posts] ─── 返回本次新插入的 post 列表
            │
            ▼
[玩法分类器] ─── LLM 分析每条新帖：适合哪些玩法？提取该玩法所需的关键信息
            │
            ▼
[写入 gameplay_rules 表] ─── 一条 post 可对应多条 rule（每条一个 mode）
            │
            ▼
[主 Agent 查询] ─── 用户对话时查询当前活跃的 rules，注入系统提示词
```

**核心原则**：爬虫只管数据采集，玩法分类是独立后处理。分类失败不影响爬虫写入。

---

## 2. 新增数据模型

### 2.1 `gameplay_rules` 表

```sql
CREATE TABLE gameplay_rules (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    post_id         UUID NOT NULL REFERENCES trump_social_posts(id) ON DELETE CASCADE,
    game_mode       VARCHAR(20) NOT NULL,  -- 'poison_opinion' | 'debate' | 'prediction' | 'breaking_bomb'

    -- 提取的关键信息 (JSONB，结构随 mode 不同，只含 post 中与该玩法相关的部分)
    rule_payload    JSONB NOT NULL,

    -- 状态管理
    status          VARCHAR(16) NOT NULL DEFAULT 'active',  -- active | expired
    generated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at      TIMESTAMPTZ,  -- 过期时间

    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_gameplay_rules_mode_status ON gameplay_rules(game_mode, status);
CREATE INDEX idx_gameplay_rules_post_id ON gameplay_rules(post_id);
```

### 2.2 `rule_payload` 字段结构（按模式）

**管线只提取 post 中与该玩法有关的事实信息。Agent 自己知道怎么把事实变成对话。**

**毒观点 (`poison_opinion`)** — Agent 需要知道：帖子里哪个点最有争议性：

```json
{
  "post_content": "帖子正文（原始 HTML，Agent 自行渲染语气）",
  "angle": "TRUMP_POLL_BRAG", 
  "hook": "帖子声称民调极好，但未提供任何数据来源"
}
```

| 字段 | 说明 |
|---|---|
| `post_content` | 帖子正文，Agent 直接引用 |
| `angle` | 争议角度标签，Agent 用来选开场策略（如 TRUMP_POLL_BRAG → "他说民调极好..."） |
| `hook` | 一句提取的事实矛盾点，Agent 作为对话入口 |

**来辩 (`debate`)** — Agent 需要知道：帖子的核心主张、以及 Pigugu 应该持什么挑衅立场来激用户反驳：

```json
{
  "post_content": "帖子正文",
  "core_claim": "特朗普声称关税对中国造成巨大伤害",
  "provocative_stance": "替特朗普辩护：关税确实伤害了中国，他说的是事实——伤害程度不重要，方向是对的",
  "post_weakness": "帖子只说'伤害很大'，没有给出具体数据或伤害机制",
  "post_strength": "帖子引用了美国贸易代表办公室的官方声明"
}
```

| 字段 | 说明 |
|---|---|
| `post_content` | 帖子正文 |
| `core_claim` | 从帖子中提取的核心主张（一句话） |
| `provocative_stance` | Pigugu 应该持的争议立场——**永远选用户最可能不同意的那个角度**。不一定是"为特朗普辩护"，而是"让用户想反驳的立场" |
| `post_weakness` | 帖子论点的薄弱之处（Agent 认输信号：用户打中这个点就该认） |
| `post_strength` | 帖子论点的有力之处（Agent 死撑弹药：用户没打中这个点就继续怼） |

**PRD 定义**：来辩 = "Pigugu 主动持一个争议性立场，玩家反驳"。Pigugu 不是固定站特朗普，而是站**最不舒服的那个角度**——有时候是为他辩护（当帖子的观点本身不受欢迎时），有时候是攻击他（当帖子的观点很受他的支持者欢迎时），有时候是替第三方辩护（如政府、大公司）。`post_weakness` 和 `post_strength` 只从帖子本身提取，不引入外部数据。

**预测混乱 (`prediction`)** — Agent 需要知道：帖子里的可验证主张是什么，什么时候验证：

```json
{
  "post_content": "帖子正文",
  "prediction_target": "特朗普是否会在周五前与伊朗达成协议",
  "deadline": "2026-05-16T16:00:00Z",
  "resolution_check": "北京时间 5/16 16:00 前，是否有官方宣布的协议"
}
```

| 字段 | 说明 |
|---|---|
| `post_content` | 帖子正文 |
| `prediction_target` | 帖子里包含的可验证主张（"会不会...""能不能..."） |
| `deadline` | 帖子或上下文隐含的截止时间 |
| `resolution_check` | 揭晓时用什么标准判断预测正确/错误 |

**突发炸弹 (`breaking_bomb`)** — Agent 需要知道：这条帖是不是大事，需要立刻叫用户：

```json
{
  "post_content": "帖子正文",
  "is_urgent": true,
  "urgency_reason": "涉及战争/军事行动的直接表态"
}
```

| 字段 | 说明 |
|---|---|
| `post_content` | 帖子正文 |
| `is_urgent` | 是否触发推送 |
| `urgency_reason` | 一句话原因（Agent 用来决定措辞的紧迫程度） |

---

## 3. 分类器流程

### 3.1 触发时机

爬虫 `upsert_posts()` 之后，拿到本次**新插入**的 post 列表（`updated` 的跳过，不重复分类）。对每条新帖调用 LLM 进行分类。

### 3.2 分类 Prompt（精简版）

```
你是一个内容分类器。下面是一条特朗普在 {platform} 上的社交媒体帖子。

帖子内容：{content}
发布时间：{created_at}
标签：{tags}

请判断这条帖适合 Pigugu 的哪些游戏模式，并为每个适合的模式提取关键信息。

四种模式：
- poison_opinion：帖子有争议性或槽点 → 提取争议点
- debate：帖子包含一个明确的主张/观点 → 提取核心主张、争议性立场（选用户最可能不同意的角度）、帖子本身的论据强弱之处
- prediction：帖子包含可验证的预测或截止日期 → 提取预测目标和截止时间
- breaking_bomb：帖子是重大突发事件 → 标记紧急程度

返回 JSON。只返回适合的模式，不适合的不返回：
{
  "modes": {
    "poison_opinion": { "angle": "...", "hook": "..." },
    "debate": { "core_claim": "...", "provocative_stance": "...", "post_strength": "...", "post_weakness": "..." },
    "prediction": { "prediction_target": "...", "deadline": "...", "resolution_check": "..." },
    "breaking_bomb": { "is_urgent": true, "urgency_reason": "..." }
  }
}
```

### 3.3 过滤与入库

| 模式 | 入库条件 | 过期时间 |
|---|---|---|
| 毒观点 | 总是入库 | 帖子发布后 48h |
| 来辩 | `core_claim` 非空 | 帖子发布后 48h |
| 预测混乱 | `prediction_target` 非空且 `deadline` 在未来 | `deadline` |
| 突发炸弹 | `is_urgent == true` | 帖子发布后 2h |

### 3.4 调度策略

- **同步执行**：爬虫进程内串行调用 LLM 分类，每个 post 一次 API 调用。
- **成本控制**：使用 Qwen 3.5，纯分类任务，token 极少。
- **去重**：`post_id + game_mode` 唯一约束，同一条 post 同一种 mode 不会重复生成。
- **兜底**：LLM 调用失败 → 至少生成一条 `poison_opinion` rule（该模式只需 post 正文，可直接用模板生成，不需要 LLM）。

### 3.5 LLM 提供商与部署配置

**模型**：Qwen 3.5，通过 Qwen-US 服务器调用。

**环境变量**：

| 变量名 | 说明 |
|---|---|
| `QWEN_US_API_KEY` | Qwen-US API Key |
| `QWEN_US_BASE_URL` | Qwen-US 服务地址（OpenAI 兼容格式） |

**推送链路**（与 `BRIGHTDATA_API_KEY` 完全一致）：

```
本地 shell / .env
    → GitHub Secret
    → .github/workflows/deploy.yml (env + Python 替换脚本)
    → k8s/secrets.yaml (占位符)
    → K8s CronJob Pod 环境变量
```

**涉及的文件变更**：

① `k8s/secrets.yaml` — 添加占位符：
```yaml
QWEN_US_API_KEY: "__QWEN_US_API_KEY__"
QWEN_US_BASE_URL: "__QWEN_US_BASE_URL__"
```

② `k8s/crawler-cronjob.yaml` — 注入环境变量：
```yaml
- name: QWEN_US_API_KEY
  valueFrom:
    secretKeyRef:
      name: pigugu-secrets
      key: QWEN_US_API_KEY
- name: QWEN_US_BASE_URL
  valueFrom:
    secretKeyRef:
      name: pigugu-secrets
      key: QWEN_US_BASE_URL
```

③ `.github/workflows/deploy.yml` — 在 `env:` 块添加 secrets，在 Python 替换脚本的 key 列表中添加 `QWEN_US_API_KEY` 和 `QWEN_US_BASE_URL`。

**分类器调用代码**：

```python
import os
from openai import OpenAI

client = OpenAI(
    api_key=os.environ["QWEN_US_API_KEY"],
    base_url=os.environ["QWEN_US_BASE_URL"],
)

response = client.chat.completions.create(
    model="qwen-3.5",
    messages=[{"role": "user", "content": CLASSIFIER_PROMPT}],
    response_format={"type": "json_object"},
    temperature=0.1,
)
```

---


### 4.1 查询

Agent 在构建系统提示词时查询：

```sql
SELECT * FROM gameplay_rules
WHERE status = 'active'
  AND expires_at > now()
ORDER BY generated_at DESC
LIMIT 10;
```

### 4.2 注入方式

查询结果直接注入到 Pigugu 的系统提示词末尾——**原始数据，不做二次加工**：

```
[今日可用的玩法素材]

毒观点 (poison_opinion)：
- angle: TRUMP_POLL_BRAG | hook: 声称民调极好但无数据来源 | post_id: xxx | 帖文: "..."
- angle: TARIFF_BLAME | hook: 将经济问题归咎于关税而非政策 | post_id: xxx | 帖文: "..."

来辩 (debate)：
- core_claim: 关税对中国造成巨大伤害 | pigugu_side: defend_trump | post_id: xxx
  帖文: "..."
  可用事实: [中国出口下降12%, 美国消费者承担90%关税成本]

预测混乱 (prediction)：
- target: 周五前是否达成协议 | deadline: 2026-05-16 | post_id: xxx | 帖文: "..."

突发炸弹 (breaking_bomb)：
- urgency: 战争表态 | post_id: xxx | 帖文: "..."

你的 Pigugu 人设和每种玩法的对话脚本你已经知道。请根据对话节奏自然使用以上素材。
```

**Agent 自己决定的事（管线不管）**：
- 用什么语气开场
- 选哪条素材先抛出
- 怎么回应用户的立场
- 什么时候认输
- 预测揭晓时怎么嘲讽

---

## 5. 实现优先级

| 阶段 | 内容 |
|---|---|
| **Phase 1** | `gameplay_rules` 表 + 迁移 + SQLAlchemy 模型 |
| **Phase 2** | 分类器（LLM prompt 模板 + 解析 + 入库） |
| **Phase 3** | 爬虫管线集成（upsert 后自动触发分类） |
| **Phase 4** | Agent 端（查询活跃 rules + 注入系统提示词） |

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
  "modes": {
    "poison_opinion": {
      "angle": "TRUMP_POLL_BRAG",
      "hook": "声称民调极好，但未引用任何具体数据或来源"
    },
    "debate": {
      "core_claim": "特朗普的民调支持率正在大幅领先",
      "provocative_stance": "替他辩护：'极好'是一种情感表达而非数据声明——他有权对他的支持者说他们想听的话。你来证明他说错了",
      "post_strength": "帖子措辞自信，感谢粉丝暗示有群众基础",
      "post_weakness": "帖子未引用任何具体民调来源、样本量或误差范围——'极好'没有可验证的定义"
    }
  }
}
```

`prediction` 和 `breaking_bomb` 未生成——这条帖没有可验证的预测，也不够紧急。

### Agent 拿到后自己发挥（示意，不是管线生成的）

- **毒观点**: "他说民调'极好'。哪个民调？多少样本？误差多少？什么都没说，就'极好'。你怎么看？"
- **来辩**: "我来替他辩护。'极好'可以是形容词不是数据，他在描述感觉，感觉不需要来源。来，反驳我。"

管线只给了 `angle: TRUMP_POLL_BRAG` 和 `hook`，Agent 自己组织语言。管线没写 Pigugu 的台词。

---

## 7. 关于 X/Twitter 来源

X 和 Truth Social 的帖子对分类器完全透明——都走同一条 `trump_social_posts → LLM 分类 → gameplay_rules` 管线。X 爬取恢复后无需额外开发。
