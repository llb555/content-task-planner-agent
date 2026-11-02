# 02 · 方案设计：调用流程、动态决策与技术取舍

## 1. 整体结构

```
Brief(topic, budget, deadline, audience, proposed_plan)
      │
      ▼
┌──────────────────────────────────────────────────────────┐
│ loop.run_agent(state, decision_source, toolbox, config)   │
│                                                          │
│  while True:                                             │
│    ① 上限检查（turns / tool_calls / invalid_actions）      │
│    ② decision = source.decide(ctx)        ← 模型或 Mock   │
│    ③ 校验：动作合法？task_id 存在？依赖满足？工具匹配？      │
│    ④ observation = toolbox.dispatch(...)  ← 真实执行      │
│    ⑤ 费用入账、任务状态回写、checkpoint                   │
│    ⑥ 把 observation 放进 trace → 回到 ②                  │
└──────────────────────────────────────────────────────────┘
      │
      ▼
RunState → trace.json / trace.md / plan.json / checkpoint.json
```

关键分工：

| 层 | 负责 | 不负责 |
|---|---|---|
| `policy.py` | 选下一步动作、写理由 | 算钱、判依赖、计数 |
| `loop.py` | 校验、执行、状态回写、上限、checkpoint | 决定做什么 |
| `tools.py` | 真实检索/估算/生成/保存 + 预算硬校验 | 决定顺序 |
| `models.py` | 数据结构与序列化 | 任何逻辑 |

这样分工的直接后果：**换一个模型、或者模型完全胡说，都不会污染算术**。

## 2. 五个工具

| 工具 | 参数 | 返回 | 费用（单位） |
|---|---|---|---|
| `search_materials` | `query` | 命中的素材摘要 + 可靠度 | 0.5 |
| `estimate_cost` | `plan_id` | 方案成本 / 周期 / 步骤 + 是否违规 | 0.2 |
| `draft_script` | `plan_id`, `pieces` | 生成的脚本标题/渠道/钩子 | 2.0 × pieces/3 |
| `save_plan` | `plan_id`（brief 由循环注入） | 写盘路径 + 缺口 | 0.3 |
| `list_plan_options` | — | 候选方案目录（免费） | 0 |

失败注入：`ToolBox(fail_on={"draft_script": [1, 2]})` = 第 1、2 次调用失败。
错误码有语义：`TOOL_ERROR:*`（工具挂了）、`UNKNOWN_PLAN`、`BAD_ARGS`、`OVER_BUDGET`、
`OVER_DEADLINE`、`DEPENDENCY_UNMET`、`ALREADY_DONE`、`TASK_TOOL_MISMATCH`。

## 3. 动态决策

### 3.1 循环契约（题目硬要求）

> 至少实现一个"模型选择动作 → 执行工具 → 观察结果 → 模型调整下一步或结束"的循环。

本项目的每一轮都是这个结构，且**下一步真的依赖上一步的返回值**：

- `trace[2]` 估 `plan-standard` → 返回 12 > 预算 10；
- `trace[3]` 因此改成 `plan_tasks`，把估算任务换成 `t2b_estimate` 指向 `plan-lean`；
- 若把预算调成 15，`trace[3]` 不存在（`--case budget15` 只有 v1 计划）。

这不是"固定分支"：分支条件是工具**返回的数字**，不是输入的档位。

### 3.2 MockPolicy 的决策表

按顺序求值，每一行都需要一个具体的 observation 才成立：

| 观测到 | 下一步 |
|---|---|
| 还没有计划 | `plan_tasks`（基础链：检索→估算→生成→保存） |
| 估算结果 `violations` 非空，且还有更便宜的候选 | `plan_tasks` 重规划到便宜方案（旧任务标 `blocked`，新估算换新 id） |
| 估算违规，且已无更便宜候选 | `finish(incomplete)`，列出被否决的方案和建议 |
| 估算工具失败，第 1 次 | 重试同一估算 |
| 估算工具失败，第 2 次 | 降级到保守方案并重估 |
| 脚本失败，第 1 次 | 缩小到 1 篇重试 |
| 脚本失败，第 2 次 | 改计划：脚本任务标 `skipped`，保存任务改依赖估算任务 |
| 保存失败，第 1 次 | 重试 |
| 保存失败，第 2 次 | `finish(incomplete)`，带错误码 |
| 任务链走完 | `finish(completed)` |

### 3.3 ModelPolicy 的边界

- prompt 里给：brief、剩余预算、当前计划、最近 8 步、上一条 observation；
- 模型回：一个 JSON 动作（`action` + `args` + `rationale`）；
- 解析容错：允许 ```json 围栏、允许前后有废话；解析失败重问一次，仍失败则 `finish(incomplete)`；
- **模型无权**：改预算数字、跳依赖、跳过 `save_plan` 直接宣告完成（循环会在收尾时把这种"完成"降级为未完成）。

真实模型跑出来的轨迹里能看到模型自己犯错又自己修正：`missing-budget-model` 的第 4 轮
提交了一个依赖不存在任务（`t5 depends on unknown task t1`）的计划，被循环拒绝（`INVALID_PLAN`），
第 5 轮它把已完成任务显式列回去后成功。这比"模型永远正确"更能说明循环真的在起作用。

## 4. 预算与费用口径

这是本题最容易含糊的地方，所以两种口径都实现、都展示：

- **默认（`charge_tools_to_budget=False`）**：预算约束**方案成本**，工具费用单独统计。
  与题目参考实例一致：预算 10、方案 8 → 余额 2。
- **可选（`charge_tools_to_budget=True`）**：预算约束"方案成本 + 工具费用"。

`trace.md` 里同时出现：

```
- 费用合计：8 单位 / 预算 10
- 口径：预算只约束方案成本；全部花费（含工具费）为 11.2 单位
- 预算余额：2 单位
```

`save_plan` 在写盘前会再校验一次（`OVER_BUDGET` / `OVER_DEADLINE`），所以即使模型执意保存超限方案，
也不会产生"已保存"的结果。

## 5. 任务计划与依赖

- 计划是一个 `TaskPlan`：任务列表 + 版本号 + 修订理由；
- 每个任务绑定一个工具，调用时必须带 `task_id`，且 `task.tool` 必须等于 `action`（否则 `TASK_TOOL_MISMATCH`）；
- 依赖未完成 → `DEPENDENCY_UNMET`，不会执行；
- 已完成任务重复调用 → `ALREADY_DONE`，不重复计费；
- 修订计划时，**同 id 的 done 任务保留状态**，所以重跑必须换新 id——这条规则写在 system prompt 里，
  Mock 与模型都遵守。

## 6. 上限与未完成

| 上限 | 默认 | 触发后 |
|---|---|---|
| `max_turns` | 14 | `incomplete: 达到回合上限（14 轮），任务未完成` |
| `max_tool_calls` | 12 | `incomplete: 达到工具调用上限（12 次），任务未完成` |
| `max_invalid_actions` | 3 | `incomplete: 连续 3 次无效动作，任务未完成` |

收尾还会做一次一致性检查：声明 completed 但没保存 → 降级为未完成；最终支出超预算 → 降级为未完成。

## 7. checkpoint 与续跑

- 每轮结束写 `checkpoint.json`（完整 RunState 的序列化）；
- `resume_state(path)` 把 `failed` / `blocked` 任务改回 `pending`，`done` 保持不动；
- 续跑时已完成步骤不会重跑（测试 `test_resume_reopens_failed_tasks_and_finishes` 断言了这点）。

## 8. 技术取舍

| 选择 | 理由 | 代价 |
|---|---|---|
| 原生实现，不用 LangGraph/ADK | 题目只有 5 个工具、一个循环；框架会带来 200 行胶水和一层不可见的控制流，评审要读的是决策逻辑本身 | 没有现成的持久化/可视化/并发 |
| 单文件循环 + Protocol 决策源 | 换决策源 = 换一个类，测试可以注入任意策略（`test_invalid_actions_are_capped` 里的 `Broken`） | 没有多 Agent 编排能力 |
| 标准库 HTTP 调模型 | 不引入 openai SDK，离线环境也能读代码 | 要自己写重试与错误分类（约 60 行） |
| 数据放 JSON 而非数据库 | 合成数据只有 6 条素材 / 3 个方案，评审可以直接读 | 不适合真实规模 |
| 两种预算口径都实现 | 题目参考实例的口径与"工具费也是钱"的口径不同，含糊会失分 | 多一个字段和一段文档 |
| `trace.md` 与 `trace.json` 双写 | 人读表格看流程，JSON 给机器与测试 | 渲染代码约 150 行 |

## 9. 一次运行的数据流（`--case tool-failure`）

```
turn 1  plan_tasks      → v1: t1_research → t2_estimate → t3_draft → t4_save
turn 2  search_materials(t1)  ok, 1 条素材
turn 3  estimate_cost(t2, plan-standard)  ok, 12 > 预算 10 → violations
turn 4  plan_tasks      → v2: t2_estimate 标 blocked，新增 t2b_estimate(plan-lean)
turn 5  estimate_cost(t2b, plan-lean)  ok, 8 / 5 天
turn 6  draft_script(t3, pieces=3)  ✗ TOOL_ERROR  → t3 = failed, attempts=1
turn 7  draft_script(t3, pieces=1)  ok             → t3 = done, 状态从 failed 回到 done
turn 8  save_plan(t4, plan-lean)  ok               → plan.json
turn 9  finish(completed)
```

第 6 步的失败在 `trace.json` 里是 `ok:false` + `error:"TOOL_ERROR: draft_script"`，
第 6 轮的 `state_after.task_status.t3_draft == "failed"`——失败没有被抹掉。
