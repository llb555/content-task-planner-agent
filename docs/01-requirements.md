# 01 · 需求、范围与成功标准

## 1. 用户与目标

**用户**：内容运营（一个人负责一个 campaign，手上只有主题、预算和截止时间）。

**目标**：在预算与截止时间这两个硬约束下，拿到一份**能直接执行**的内容活动方案——
包含选定的方案、每步做什么、脚本草稿、花多少钱、以及"哪些地方是假设、哪些地方没做到"。

**今天的痛点**（也是本项目要覆盖的场景）：

1. 方案成本要试算才知道，试算完往往超预算，需要**换方案再试**；
2. 素材检索、脚本生成、成本核算、保存是四个不同工具，中间任何一个都可能失败；
3. 失败之后不能假装成功，运营需要知道**哪一步失败了、已经做完的部分还在不在**。

## 2. 输入与输出

### 输入（Brief）

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `topic` | str | 是 | 活动主题，用于素材检索 |
| `budget_units` | float \| null | **否** | 预算，单位是题目定义的"模拟单位" |
| `deadline_days` | int \| null | **否** | 截止时间，单位天 |
| `audience` | str | 否 | 目标人群 |
| `proposed_plan` | str \| null | 否 | 运营带来的初始方案 id，Agent 先估它 |

预算与截止时间**允许缺失**（题目验收点之一）。缺失时 Agent 仍要出方案，并把"我假设了什么"写进结果。

### 输出（RunState / trace）

| 交付项 | 落在哪里 | 说明 |
|---|---|---|
| 任务计划 | `trace.json` → `plan.tasks` | 每个任务：工具、依赖、状态、尝试次数、费用 |
| 步骤状态 | `plan.tasks[].status` + 每轮 `state_after.task_status` | `pending / done / failed / skipped / blocked` |
| 工具结果 | `trace[].observation` | 成功与否、摘要、错误码、费用、sim 延迟 |
| 费用合计 | `costs.tool_spend / plan_cost / total / all_in / by_tool` | 两种口径都给 |
| 已保存方案 | `plan.json` + `saved_plan_path` | 只有真的写盘成功才算 |
| 未完成原因 | `incomplete_reason` | 一句话说清缺什么 |

## 3. 范围

**做**：

- 一个 ReAct 循环（模型/策略选动作 → 工具执行 → 观察 → 下一步）
- 5 个业务工具（本地实现 + 合成数据），支持失败注入
- 预算与截止时间的硬校验（在代码里，不在 prompt 里）
- 步数 / 工具调用 / 无效动作三重上限
- checkpoint 与失败步骤续跑
- CLI（题目要求三选一）
- 25 项自动化测试 + 13 组实际运行记录（9 组离线 + 4 组真实模型）

**不做**（题目明确非必需，或主动砍掉）：

- Web 界面、线上部署
- 多 Agent 协作
- 生产级 Runtime（无队列、无并发、无数据库）
- 真实素材库 / 真实报价（全部是合成数据）

## 4. 成功标准

| 编号 | 标准 | 怎么验证 |
|---|---|---|
| S1 | 预算够时正常出方案并保存 | `test_normal_case_completes_and_saves` |
| S2 | 预算不够时**先观测再换方案**，不是一开始就选便宜的 | `test_over_budget_switches_to_a_cheaper_plan` + `trace[3]` 的 rationale |
| S3 | 预算够时不产生多余的重规划（对照实验） | `test_control_case_with_enough_budget_does_not_replan` |
| S4 | 缺预算/截止时间仍出方案，并标注假设 | `test_missing_budget_still_produces_a_plan` |
| S5 | 工具失败被观察、缩小规模重试 | `test_tool_failure_is_observed_and_retried_smaller` |
| S6 | 连续失败时改计划，已完成步骤保留 | `test_two_failures_lead_to_a_revised_plan_that_keeps_earlier_work` |
| S7 | 无可行方案时返回未完成 + 缺口 | `test_no_feasible_plan_returns_incomplete_with_the_gap` |
| S8 | 失败步骤绝不记成成功 | `test_failed_step_is_never_recorded_as_success` |
| S9 | 同一输入重复执行结果一致 | `test_repeated_runs_are_deterministic` |
| S10 | 达到步数/工具调用上限时明确未完成 | `test_turn_limit_*` / `test_tool_call_limit_*` |
| S11 | 失败步骤可续跑 | `test_resume_reopens_failed_tasks_and_finishes` |
| S12 | 至少两条"模型选动作→执行→观察→调整"的真实模型记录 | `examples/runs/normal-model/`、`tool-failure-model/`、`missing-budget-model/`、`no-feasible-plan-model/` |

## 5. 题目验收点 → 本仓库证据对照

| 题目"必须验收" | 场景 | 实际运行记录 | 测试 |
|---|---|---|---|
| 正常完成 | `normal` | `examples/runs/normal/`、`normal-model/` | S1 |
| 缺预算或截止时间 | `missing-budget` | `examples/runs/missing-budget/`、`missing-budget-model/` | S4 |
| 工具失败 | `tool-failure`、`tool-failure-twice`、`tool-failure-incomplete` | 同名目录 + `tool-failure-model/` | S5、S6、S8 |
| 预算超限 | `normal`、`no-feasible-plan`、`deadline-tight` | 同名目录 + `no-feasible-plan-model/` | S2、S7 |
| 重复执行 | 任一场景跑两次 | `test_repeated_runs_are_deterministic`（另见 docs/03 的 byte 级比对） | S9 |
| 失败步骤续跑（加分项） | `tool-failure-incomplete` → `tool-failure-resumed` | `examples/runs/tool-failure-resumed/` | S11 |
| 步数/工具调用上限 | `--max-turns` / `--max-tool-calls` | 测试内联运行 | S10 |

## 6. 术语

- **模拟单位**：题目定义的抽象预算单位，不与人民币挂钩。
- **方案（plan variant）**：`data/materials.json` 里的三个候选执行方案（lean / standard / heavy），
  各自有基础成本与周期。
- **工具费用**：调用检索/估算/生成/保存本身的成本，与方案成本分开统计。
- **预算口径**：默认"预算只约束方案成本"（与题目参考实例一致：预算 10、方案 8 → 余额 2）；
  可切换为"工具费也计入预算"（`RunConfig(charge_tools_to_budget=True)`）。
