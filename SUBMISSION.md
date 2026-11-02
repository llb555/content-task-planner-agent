# 提交说明与思路

> 这份文档对应答题框「写下说明、思路，或文件里放不下的内容」。
> 前面几节是可以直接粘进答题框的正文，后面几节是补充材料。

---

## 一、一句话说明

在**预算**与**截止时间**两个硬约束下，让模型自己决定「下一步做什么」，做不动时**换方案或如实放弃**，
并把每一步的选择理由、工具返回、花了多少钱、哪一步失败了，全部留在可复现的记录里。

仓库：https://github.com/llb555/content-task-planner-agent
运行：`PYTHONPATH=src python -m content_planner run --case normal`
（离线可跑，不需要 key、不需要联网；25 项测试全绿）

---

## 二、我怎么读这道题

题目 100 分，但真正决定分数的不是"能不能生成一份方案"，而是下面四件事。我按这四件事来组织代码：

| 题目原话 | 我的理解 | 落到哪里 |
|---|---|---|
| 「模型根据成本超限、工具错误或部分成功结果，调整计划、选替代方案或停止」 | 下一步的输入必须是**上一步的返回值**，不是输入的档位 | `loop.py` 每轮把 observation 交给决策源 |
| 「不能把失败步骤记为成功」 | 失败要能在状态里被看见，而且不能被后续动作抹掉 | 任务状态 `failed/skipped` + 每轮 `state_after` 快照 |
| 「无状态、无失败处理只能证明完成串行调用」 | 必须有**对照实验**证明重规划是被观测驱动的 | `--case budget15`：只改预算，重规划消失 |
| 「达到上限时返回明确的未完成状态」 | 未完成是一等状态，不是异常，也不是空结果 | 三种上限 + 收尾一致性检查 + 退出码 1 |

一句话：**这道题考的是"钱不够、工具坏了的时候，它做了什么，以及有没有如实说"**，不是考文案生成。

---

## 三、核心思路：把「决策」和「约束」彻底分开

整条链路只有四个角色，边界卡得很死：

```
决策源（Mock / 真实模型）  只回答一个问题：下一步做什么？
        │  一个 JSON 动作
        ▼
循环 loop.py               校验它、执行它、把结果放回去、数着步数
        │
        ▼
工具 tools.py              真实干活；顺便守住"不能超预算保存"
        │
        ▼
状态 models.py             纯数据，可以完整序列化成 checkpoint
```

这样切分的直接好处：

1. **换模型不影响正确性**。模型可以胡说、可以无视规则，但它改不了 `save_plan` 里的预算校验，
   也绕不过 `max_turns`。它能做的只有浪费轮次，然后被上限截断。
2. **换策略不影响约束**。测试里我注入过一个只会返回 `teleport` 的假策略（`test_invalid_actions_are_capped`），
   循环照样在 2 次无效动作后干净地返回未完成。
3. **每层都能单独测**。25 个用例里，有的只测工具（`save_plan` 拒绝超预算方案），有的只测循环（上限），
   有的只测策略（决策表），不需要启动整条链路。

我刻意**没有**用 LangGraph / ADK。理由不是"框架不好"，而是这个项目只有 5 个工具、一个循环，
框架会带来两百行胶水加一层看不见的控制流，而评审要读的恰恰是决策逻辑本身。
题目也说了「框架名称不加分，能解释取舍与修改结果才有价值」——所以我把取舍写在了 `docs/02` 里。

---

## 四、三个真正难的地方

### 难点 1：怎么证明「这是决策」而不是「这是流程」

我第一版写出来就是串行的：`检索 → 估算 → 超了就换便宜的 → 生成 → 保存`。
跑得通、测试也绿，但它和 `if budget < 12: plan = lean` 没有任何区别——这正是题目说的
「仅有固定条件分支不视为完成 ReAct 决策要求」。

转折点是我给自己写验收用例时写下的这两行：

```python
assert estimates[0] == "plan-standard"   # 必须先估贵的
assert estimates[1] == "plan-lean"       # 才换便宜的
```

要满足它，Agent 必须**先拿到 12 这个数字**，再决定换。也就是说决策的输入必须是上一步的返回值。
于是我把 `_target_plan` 改成：**第一次估算永远是运营带来的方案（`brief.proposed_plan`），
即使看起来注定超限也要估一次**；只有观测到 `violations` 之后才允许降级。

改完之后多了一个副产品，我觉得它比任何文档都有说服力：

> 同一份代码，预算 10 时产生 2 个版本的计划（v1 → v2，中间换过方案）；
> 预算 15 时只有 1 个版本，从不重规划。

这条对照实验现在是一个测试（`test_control_case_with_enough_budget_does_not_replan`），
也是一条可以跑出来的运行记录（`examples/runs/budget15/`）。

### 难点 2：重规划时复用任务 id，导致「重跑」被静默跳过

循环的规则是「同 id 的已完成任务保留状态」——这样修订计划不会丢掉已经做完的工作。
但第一版我在换方案时**原地**把 `t2_estimate` 改成估新方案，结果新计划一装上，
`t2_estimate` 就已经是 `done`，永远轮不到它执行。

**trace 看起来完整，实际什么都没跑。** 这类 bug 只有把中间状态也断言出来才抓得到。

改法（`MockPolicy._revise_for_plan`）：

- 触发重规划的旧任务**保留在原位**，状态标 `blocked`，理由写进 `detail`——证据不丢；
- 新的估算任务用**新 id**（`t2b_estimate`），并让后续任务依赖它；
- 这套「重跑要换 id」的规则同时写进 system prompt，让真实模型也遵守。

真实模型确实照做了：`tool-failure-model` 的 v3 计划里，模型自己写道
「旧失败任务 t6 以新 id t8 重跑，保存任务改 id t9 依赖 t8」——我没有提示它，是规则约束的结果。

### 难点 3：预算到底包不包工具费

题目参考实例是「预算 10、初始 12、改 8、保存」。按这个读法，预算只管**方案成本**。
但「调用工具本身也要花钱」在真实业务里同样成立。

我第一版把工具费也算进预算，结果「预算 10、方案 8」最后显示余额 -1.2，和题目的参考实例对不上。
这不是算错，是**口径没写清楚**。

处理方式：**两种口径都实现、都打印、都写文档**。

- 默认 `charge_tools_to_budget=False`：预算约束方案成本，工具费单独统计（与题目一致）；
- 可选 `True`：预算约束「方案成本 + 工具费」。

`trace.md` 里同时出现两行：

```
- 费用合计：8 单位 / 预算 10
- 口径：预算只约束方案成本；全部花费（含工具费）为 11.2 单位
```

需求歧义不该偷偷选一个。把选择摆到台面上，评审能自己核对，比我说"我对了"有用。

---

## 五、为什么要有两个决策源

不是为了炫技，是因为它们各自能证明不同的事：

| | `MockPolicy`（离线） | `ModelPolicy`（真实模型） |
|---|---|---|
| 证明什么 | **机制正确**：约束驱动的重规划、失败处理、上限、续跑 | **模型真的能做这个循环**：看观测、改计划、修正自己的错误 |
| 能不能复现 | 逐字节可复现 | 不能，只能保证结构稳定 |
| 能不能当证据 | 不能证明模型决策质量（题目明确说了） | 不能证明可复现性 |

所以我两个都留，而且用**同一套循环、同一套工具、同一套失败注入**跑。
`--case tool-failure` 在离线模式和真实模型模式下都跑一遍，两份 trace 并排放。

真实模型那四条轨迹里，我最想让人看的是 `missing-budget-model` 的第 4 轮：

> 模型提交了一个计划，其中 `t5` 依赖一个它没有列出来的任务 `t1`。
> 循环拒绝：`INVALID_PLAN: task t5 depends on unknown task t1`。
> 第 5 轮，模型自己把已完成任务显式补回计划里，然后成功。

这比「模型一次做对」更能说明循环真的在守约束——模型会犯错，但错的东西进不了状态。

---

## 六、验收点怎么满足（逐条给证据）

| 题目验收点 | 我怎么做 | 证据文件 |
|---|---|---|
| **正常完成** | 8 轮走到 `completed`，保存 `plan.json` | `examples/runs/normal/trace.md` |
| **缺预算或截止时间** | 不阻止执行，但把缺口写进 `plan.json.gaps` 和 `incomplete_reason` 之外的备注 | `examples/runs/missing-budget/trace.md` |
| **工具失败** | 失败一次→缩小规模重试；失败两次→改计划保留已完成步骤 | `tool-failure/`、`tool-failure-twice/` |
| **工具连续失败到无法完成** | 保存两次失败 → `incomplete`，任务标 `failed`，**不生成 plan.json** | `tool-failure-incomplete/` |
| **预算超限** | 先估 12 观测到超限，再换 8 | `normal/` 第 3–5 轮 |
| **预算超限且无替代** | 12、8 都超 → `incomplete` + 「需要追加预算或放宽截止」 | `no-feasible-plan/` |
| **截止时间超限** | 截止 5 天 → 7 天方案被否 → 5 天方案通过 | `deadline-tight/` |
| **重复执行** | 同一输入两次，动作序列/方案/费用完全一致 | `test_repeated_runs_are_deterministic` |
| **失败步骤续跑（加分）** | checkpoint 里把 `failed` 改回 `pending`，`done` 不动 | `tool-failure-resumed/` |
| **步数/工具调用上限** | `--max-turns 3` / `--max-tool-calls 2` → 明确未完成 | `test_turn_limit_*`、`test_tool_call_limit_*` |
| **ReAct 循环（≥2 条）** | 4 条真实模型轨迹，覆盖正常/超预算/工具失败/无方案 | `examples/runs/*-model/` |

合计：**25 项测试 + 13 组运行记录**（9 组离线 + 4 组真实模型）。

---

## 七、几个具体数字

| 场景 | 轮次 | 工具调用 | 结果 |
|---|---|---|---|
| normal（预算 10） | 8 | 5 | `plan-lean` 8 单位，余额 2 |
| budget15（对照） | 6 | 4 | `plan-standard` 12 单位，**无重规划** |
| tool-failure | 9 | 6 | 失败 1 次后缩到 1 篇成功 |
| tool-failure-twice | 10 | 6 | 计划 v3，脚本 `skipped`，只存框架 |
| no-feasible-plan（预算 5） | 4 | 2 | `incomplete`，否决 12 和 8 |
| deadline-tight（截止 5 天） | 8 | 5 | 7 天方案被否，5 天方案通过 |
| tool-failure-incomplete | 9 | 6 | `incomplete`，保存失败 2 次 |
| tool-failure-resumed | 11 | 7 | 续跑后 `completed`，已完成步骤没重跑 |

模型侧：`normal-model` 8 轮 / 12.9 s，`tool-failure-model` 10 轮 / 17.3 s，
`missing-budget-model` 9 轮，`no-feasible-plan-model` 7 轮。

---

## 八、已知限制（不藏）

1. **素材是合成数据**，6 条手写。检索"命中"只验证流程，不代表检索质量。
2. **模型轨迹依赖本机网关**（`deepseek-account/deepseek-flash`）。token 数记在 `model_calls.json`，
   但换模型或换时间结果会变。仓库里保留的是当时那次运行的原样输出。
3. **模型偶尔会编方案 id**。`no-feasible-plan-model` 里模型试了不存在的 `plan-minimal`，
   被工具以 `UNKNOWN_PLAN` 拒绝——这是设计内的失败路径，不是漏洞，但说明模型需要工具来兜底。
4. **脚本是模板化的**，按方案名 + 渠道 + 序号生成标题和钩子，不是可用文案。
5. **没有并发、没有数据库、没有 Web**。题目明确说这些非必需，我优先把钱花在决策循环和证据链上。
6. **`MockPolicy` 不是模型**。它只能证明"约束驱动的重规划"这个机制成立，
   模型决策质量由那 4 条真实轨迹单独证明。两者不能互相替代。

---

## 九、时间分配（题目建议 3 小时）

| 阶段 | 实际 |
|---|---|
| 读题、定范围、选技术栈 | ~15 min |
| 领域模型 + 工具 + 循环 | ~50 min |
| 两个决策源（Mock + 模型适配器） | ~40 min |
| 测试与调试（含 3 处逻辑返工） | ~45 min |
| 真实模型跑轨迹、调 prompt | ~25 min |
| 文档与运行记录整理 | ~35 min |
| **合计** | **约 3.5 小时** |

超了半小时，主要花在难点 1 和难点 2 的返工上——第一版"能跑"但"证明不了"，改了两轮才达到可验收。

---

## 十、如果继续做

按优先级：

1. **边界值参数化测试**：现在只测了 5/10/15 几个离散预算，7.9 这种边界没覆盖。
2. **同场景多模型对比**（DeepSeek / GLM / Kimi），看决策差异——更能说明"框架不重要，约束和观测才重要"。
3. **细粒度续跑**：现在 draft 任务是整体成功/失败，应该支持只重跑失败的那一篇。
4. **真实 HTTP 工具**（带真实延迟与超时），验证超时路径——现在延迟是模拟的。
5. **多轮协作**：运营看完方案说"预算加 3 单位"，从 checkpoint 继续，而不是重跑。

---

## 附：运行命令速查

```bash
git clone https://github.com/llb555/content-task-planner-agent && cd content-task-planner-agent
pip install -r requirements.txt
export PYTHONPATH=src            # PowerShell: $env:PYTHONPATH="src"

python -m pytest tests -q                          # 25 passed
python -m content_planner list-cases               # 8 个场景
python -m content_planner run --case normal        # 参考实例
python -m content_planner batch                    # 批量重跑全部离线场景
python -m content_planner run --case normal --max-turns 3   # 看上限行为
```

真实模型（可选）：

```bash
export LLM_API_KEY=sk-...
export LLM_BASE_URL=https://api.deepseek.com/v1
export LLM_MODEL=deepseek-chat
python -m content_planner run --policy model --case normal
```
