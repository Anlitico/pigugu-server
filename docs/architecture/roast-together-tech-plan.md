# Roast Together 技术方案

> 基于 main 分支代码 | 2026-06-11

---

## 一、当前运行流程

```
用户说话 → PigAgent.generate_reply()
  → 加载 context
  → 检查 active roast → tick() → 检查 triggers → 注入 prompt（如有）
  → 拼接系统 prompt + pending prompt → LLM 生成回复
  → add_turn() 写入对话记录
```

Trigger 和 Tool Call 都落在这个流程里：

```
每轮循环：
  ┌─────────────────────────────────────────────────────────┐
  │                                                         │
  │  [Phase 检查]  →  [Trigger 评估]  →  [Prompt 注入]       │
  │       ↑                ↑                                  │
  │   状态机驱动       回合间控制层                            │
  │                                                         │
  │  [LLM 生成回复]  →  [Tool Call]  →  [Phase 转换]          │
  │                         ↑                                │
  │                   回合内控制层                             │
  └─────────────────────────────────────────────────────────┘
```

---

## 二、四个核心改动

### 改 1/4：Phase 状态机（基础设施）

**做什么**：定义对话的阶段和转换规则。

当前：

```
ACTIVE → REVIEW → CLOSED
```

改为：

```
ACTIVE → CLOSING → SETTLED → CLOSED
```

| 状态 | 含义 | AI 行为 | 用户对话 |
|------|------|---------|----------|
| ACTIVE | 对战中 | 接住→推一把→递球 | 正常吐槽 |
| CLOSING | 收尾中 | 宣布 best_take + 埋钩子 | 可以说话，但不影响收尾 |
| SETTLED | 已结算 | 切 Free Chat | 自由闲聊 |
| CLOSED | 已关闭 | 结束 | — |

**为什么需要 CLOSING**：收尾不是瞬间完成的。saturated trigger 触发后，AI 还需要一轮来宣布 best_take 和埋钩子。这轮不能继续 tick（否则可能再次触发 trigger），所以需要一个中间状态来锁定。

**为什么需要 SETTLED**：Tool Call 标记完成后，需要把控制权交还给 Free Chat。SETTLED 是结算和自由对话之间的边界。

```python
# pigagent/roast/types.py
class Phase(StrEnum):
    ACTIVE = "active"
    CLOSING = "closing"     # 新增
    SETTLED = "settled"     # 新增
    CLOSED = "closed"
```

---

### 改 2/4：Prompt（系统层）

**做什么**：告诉 AI 它是谁、目标是什么、怎么表现。

**核心变化**：从「完成任务」变为「达成目标」。

| | 旧 Prompt | 新 Prompt |
|------|------|------|
| 定位 | 完成吐槽任务 | 帮用户说出金句 |
| 角色 | 对手 / 裁判 | 共谋者 / 引导者 |
| 结束 | 5 轮后强制结束 | 话题骂透了自然收尾 |
| 评判 | 宣布谁赢 | 表扬用户的金句 |
| 语气 | 竞技感 | 朋友调侃感 |

```jinja2
# pigagent/roast/prompts/roast_together_system.j2

## GAME MODE: ROAST TOGETHER

### Your Goal
You and the user are roasting a news topic together.
Your goal is to help the user produce the BEST roast line of the session.

### Your Role
- Acknowledge the user's roast, then AMPLIFY it to the next level.
- Pass the topic back: "但我有一个更狠的角度，你敢接吗？"
- Match the user's energy. When they're on fire, push harder.
  When they're struggling, give them an opening.

### How It Ends
When the topic feels exhausted (no new angles, min 3 rounds, max 8):
1. Call out the user's best line naturally in your speech.
2. Leave a hook: "明天有新话题，你敢不敢再来？"
3. Then call the mark_roast_complete tool.

### Tone
- Snarky, like a friend at a bar. NEVER a judge or referee.
- NEVER say "you win" or "you lose" or assign scores mid-game.
- NEVER guilt-trip: no "别走", no "我需要你".
```

---

### 改 3/4：Trigger（回合间控制层）

**做什么**：每轮用户说完后，评估当前状态，决定是否注入额外指令给 LLM。

当前 3 个 trigger → 调整为 4 个：

```python
# pigagent/roast/modes/roast_together.py

triggers = [

    Trigger("user_spicy",
        # 用户状态好 → 加码
        check: turn >= 2 AND user_energy > 0.7
        prompt: "The user is on fire. Amplify. Push harder."
    ),

    Trigger("user_disengaged",
        # 用户敷衍 → 重新激发
        check: 近 3 轮用户消息平均长度 < 20
        prompt: "The user is bored. Challenge them playfully to re-engage."
    ),

    Trigger("roast_saturated",          # 🆕
        # 话题骂透 → 自然收尾
        check: turn >= 3 AND LLM 判断话题已无新角度
        prompt: "Topic exhausted. Wrap up naturally. Call out the user's best take if any."
        affects_phase → CLOSING
    ),

    Trigger("ending_max_turns",
        # 硬上限 8 轮
        check: turn >= 8
        affects_phase → CLOSING
    ),
]
```

**saturated 的判断方式**：在每轮 prompt 末尾让 LLM 评价话题饱和度，tick() 解析这个标记。

```
# 注入到每轮 LLM prompt 末尾的结构化指令
[META] 评估话题饱和度 (1-5)：
  1=还有很多角度可骂  3=基本骂完了  5=一个字都骂不出来了
  只输出: {"saturation": N}
```

`_update_state()` 解析这个值并写入 `extra.saturation`，trigger check 读取它。

---

### 改 4/4：Tool Call（回合内控制层）

**做什么**：AI 在收尾词说完后，自行调用 tool 标记结算完成。

**为什么是 tool 而不是代码自动触发**：收尾是自然语言，只有 AI 自己知道「我说完收尾词了」。让 AI 在 prompt 指令下主动调用 tool，比代码猜测更可靠。

```python
# pigagent/tools/roast.py 新增

def create_roast_complete_tool(redis, pg_pool):
    """Tool: mark_roast_complete"""

    async def handler(args: dict) -> dict:
        user_id = _current_user_id.get()
        state = await RoastState._load_active(user_id, redis)

        if not state or state.phase != Phase.CLOSING:
            return {"settled": False}

        state.phase = Phase.SETTLED
        state.extra["settled"] = True
        state.extra["has_best_take"] = (
            state.extra.get("best_take_energy", 0) > 0.70
        )
        await state.save(redis, pg_pool)

        # 异步通知 App，不阻塞对话
        await event_bus.publish(user_id, {
            "type": "roast_settled",
            "has_best_take": state.extra["has_best_take"],
            "best_take": state.extra.get("best_take", ""),
        })

        return {"settled": True}

    return Tool(
        name="mark_roast_complete",
        description="Call after you finish the closing statement of the roast.",
        parameters={},
        handler=handler,
    )
```

**延迟不是问题**：tool 在 LLM 生成流中调用，Redis 写入 + event publish 在 50ms 内完成，用户感知不到。

---

## 三、四层如何串联

```
         ┌──────────────────────────────────────────────────┐
         │                  Prompt（系统层）                  │
         │        全程有效：告诉 AI 目标、角色、语气           │
         └──────────────────────┬───────────────────────────┘
                                │
         ┌──────────────────────▼───────────────────────────┐
         │                 Trigger（回合间）                  │
         │    每轮用户说完后检查：spicy / disengaged /        │
         │    saturated / max_turns                         │
         │    触发 saturated → phase = CLOSING               │
         └──────────────────────┬───────────────────────────┘
                                │
         ┌──────────────────────▼───────────────────────────┐
         │                Tool Call（回合内）                 │
         │    AI 说完收尾词 → 调用 mark_roast_complete       │
         │    → phase = SETTLED → event_bus 发布结算数据      │
         └──────────────────────┬───────────────────────────┘
                                │
         ┌──────────────────────▼───────────────────────────┐
         │               Free Chat                          │
         │    下一轮 generate_reply 检测到 SETTLED            │
         │    → RoastState.close() → 正常对话                 │
         └──────────────────────────────────────────────────┘
```

PigAgent 每轮的路由逻辑：

```python
# agent.py generate_reply() 中

if active_roast:
    if active_roast.phase == Phase.CLOSING:
        # 收尾中，不 tick，让 AI 说收尾词
        pass
    elif active_roast.phase == Phase.SETTLED:
        # 已结算 → 关闭 roast → 后续走 Free Chat
        await active_roast.close(redis, pg_pool)
    elif active_roast.phase == Phase.ACTIVE:
        # 正常对战 → tick → 检查 trigger
        prompt = await game_mode.tick(state, records=records, redis=redis)
        if prompt:
            # trigger 触发 → 注入 prompt 到下一轮 LLM 调用
```

---

## 四、改动文件清单

| 文件 | 改动 | 对应层 |
|------|------|--------|
| `pigagent/roast/types.py` | Phase 加 CLOSING, SETTLED | 状态机 |
| `pigagent/roast/prompts/roast_together_system.j2` | 重写：目标导向、共谋者定位 | Prompt |
| `pigagent/roast/prompts/roast_together_ending.j2` | 修改：去胜负语气 | Prompt |
| `pigagent/roast/modes/roast_together.py` | triggers 调整、饱和判断、extra 扩展 | Trigger |
| `pigagent/tools/roast.py` | 新增 mark_roast_complete | Tool Call |
| `pigagent/agent.py` | generate_reply 加 CLOSING/SETTLED 路由 | 串联 |

---

## 五、实施顺序

```
Step 1: Phase 状态机
  改 types.py + agent.py 路由（可独立跑通，不影响现有行为）

Step 2: Prompt
  改 roast_together_system.j2 + ending.j2
  （可用现有测试框架跑 agent_test.py 验证对话质量）

Step 3: Trigger
  改 roast_together.py triggers + saturated 判断
  （依赖 Step 1 的 CLOSING 状态）

Step 4: Tool Call
  改 tools/roast.py
  （依赖 Step 1 的 SETTLED 状态 + Step 3 的 saturated trigger）
```

每步独立验证，不阻塞。后续（5 维评分、Conversation 写入、成就引擎）在四层跑通后再加。
