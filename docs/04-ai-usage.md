# 04 · AI 辅助内容、我的修改与验证

题目要求"用 AI 完成可运行项目"，同时要能说清哪些是 AI 产出、哪些是我改的、怎么验证的。
这里按"AI 写了什么 / 我改了什么 / 怎么证明改对了"三段来记。

## 1. AI 产出的部分

| 部分 | AI 产出 | 我的处理 |
|---|---|---|
| 领域模型骨架（`models.py`） | Task / TaskPlan / Observation / TraceEntry 的字段设计 | 删掉 `Material`/`PlanVariant` 等未用类型，改成从 JSON 直接读 dict |
| 工具实现（`tools.py`） | 5 个工具的函数体、失败注入、费用表 | 加了 `save_plan` 的预算硬校验、`pieces` 范围校验、相对路径回显 |
| 循环（`loop.py`） | while 循环、上限、checkpoint | 重写状态回写与"声明完成但没保存 → 降级未完成"的收尾检查 |
| 两个策略（`policy.py`） | Mock 的决策表、Model 的 prompt 与解析 | 修了三处逻辑错误（见下） |
| 文档初稿 | README 与 docs 的结构与大部分文字 | 用实跑数据替换了所有示例数字，删掉未实现的能力描述 |
| 测试（`tests/`） | 25 个用例的骨架 | 补了对照实验、续跑、模型适配器三类，改了 2 个断言（见下） |

## 2. 我改掉的三处关键错误

### 2.1 重规划时复用任务 id，导致"重跑"被跳过

AI 初版在换方案时把 `t2_estimate` 原地改成估新方案。但循环的规则是"同 id 的 done 任务保留状态"，
于是新计划一装上，`t2_estimate` 就已经是 `done`，永远轮不到它执行——**trace 看起来完整，实际什么都没做**。

改法（`MockPolicy._revise_for_plan`）：

- 触发重规划的旧任务保留在原位，状态标 `blocked`，理由写进 `detail`（证据不丢）；
- 新的估算任务用新 id（`t2b_estimate`），并让 `t3_draft` / `t4_save` 依赖它；
- 这套"换 id"规则同时写进 system prompt，让模型也遵守（`tool-failure-model` 的 v3 计划就是模型自己换的 id）。

验证：`test_two_failures_lead_to_a_revised_plan_that_keeps_earlier_work` 断言 `plan.version == 3`
且 `t2b_estimate` 为 `done`；`examples/runs/normal/trace.md` 第 4 轮能看到新 id。

### 2.2 "预算超限"被写成了固定分支

AI 初版让 Mock 在预算不足时**直接**选便宜方案，跳过了估算。这样 `rejected_plans` 永远是空的，
评审看不到"先观测 12 再决定"的过程——正好踩中题目说的"仅有固定条件分支不视为完成 ReAct 决策"。

改法：`_target_plan` 规定第一次估算必须是 `brief.proposed_plan`（运营带来的方案），
观测到 `violations` 之后才允许降级。同时加对照组 `--case budget15`：同一份代码、只改预算，
重规划就不发生。

验证：`test_over_budget_switches_to_a_cheaper_plan`（先 12 后 8）、
`test_control_case_with_enough_budget_does_not_replan`（只有 v1 计划）。

### 2.3 预算口径含糊

AI 初版把工具费也算进预算，导致"预算 10、方案 8"最后显示余额 -1.2，与题目参考实例
（预算 10 → 改 8 的方案 → 保存）对不上。这不是算错，是**口径没写清楚**。

改法：默认口径改为"预算只约束方案成本"，另加 `charge_tools_to_budget` 开关支持另一种口径；
`trace.md` 同时打印"计入预算的合计"和"全部花费（含工具费）"。

验证：`test_tool_spend_matches_the_sum_of_observations` 同时断言 `total_cost == plan_cost`
和 `all_in_cost == tool_spend + plan_cost`。

## 3. 我自己写的部分（不是 AI 生成后照抄）

- `loop.py` 的校验顺序：上限 → 动作合法性 → task_id → 工具匹配 → 依赖 → 执行。顺序是我定的，
  因为"先判依赖还是先判工具"会改变错误码，而错误码是 trace 可读性的一部分。
- `report.py` 整个文件：把 RunState 渲染成人读 Markdown，包括费用合计表与任务状态图标。
- `tests/conftest.py` 的 `tmp_path` 替换：本机受限令牌下 pytest 原生 fixture 无法工作，见 docs/03。
- 所有场景定义（`examples/cases.json`）与合成数据（`data/materials.json`）。
- 文档里的所有数字：都来自实跑输出，不是估算。

## 4. 验证方式

| 验证 | 做法 | 结果 |
|---|---|---|
| 单元/集成测试 | 25 个用例，覆盖 8 个场景 + 上限 + 续跑 + 适配器 | 25 passed |
| 人工读 trace | 逐条核对 `trace.md` 的轮次、参数、返回、依据是否自洽 | 见 docs/03 第 2 节 |
| 对照实验 | 只改预算（10 → 15），看重规划是否消失 | 消失，说明是约束驱动 |
| 真实模型 | 4 个场景用真实模型跑通，保留 `model_calls.json` | 见 `examples/runs/*-model/` |
| 越界测试 | 让模型提交非法计划、不存在的方案 id | 被 `INVALID_PLAN` / `UNKNOWN_PLAN` 拒绝，状态未被污染 |
| 密钥检查 | `grep -r "sk-\|Bearer\|gho_" examples/` | 无命中（key 只走环境变量） |

## 5. AI 用得不好的地方（如实记录）

- AI 生成的 README 第一版写了"支持多 Agent 协作""有 Web 界面"——都没实现，我全删了。
  教训：让 AI 写文档时必须给它**代码的实际能力清单**，否则它会按"这类项目通常有什么"来写。
- AI 写的测试第一版大量断言"最终状态"，很少断言"中间状态"。但本题考的是过程，
  所以我把关键断言改成了 `trace[i]` 级别的检查（例如第 6 轮 `task_status.t3_draft == "failed"`）。
- AI 对"预算口径"没有主动提问，直接选了一种实现。这类**需求本身的歧义**必须人来定，
  而且要在文档里写明选择理由。

## 6. 时间记录

| 阶段 | 实际耗时 |
|---|---|
| 读题、定范围、选技术栈 | ~15 min |
| 领域模型 + 工具 + 循环 | ~50 min |
| 两个策略（Mock + 模型适配器） | ~40 min |
| 测试与调试（含 3 处逻辑返工） | ~45 min |
| 真实模型跑轨迹与调 prompt | ~25 min |
| 文档与运行记录整理 | ~35 min |
| **合计** | **约 3.5 小时**（题目建议 3 小时） |

未完成部分见 docs/03 第 4 节与 docs/05。
