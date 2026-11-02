# 03 · 评测：测试预期、实际结果、失败与限制

所有数字都来自本机实跑，原始记录在 `examples/runs/`，测试日志在 `examples/test-report.txt`。

## 1. 测试总览

```
$ PYTHONPATH=src python -m pytest tests -q
25 passed
```

| 项 | 结果 |
|---|---|
| 用例数 | 25 |
| 通过 | 25 |
| 失败 | 0 |
| 耗时 | 0.4–0.9 s（纯离线，无网络、无模型调用） |
| 完整日志 | `examples/test-report.txt` |

> 本机说明：DSH 沙箱的受限令牌下，pytest 自带的 `tmp_path` 用 `mkdtemp(mode=0700)` 建出的目录
> 连测试进程自己都写不进去（`PermissionError`）。`tests/conftest.py` 用 `Path.mkdir()` 重建了同名
> fixture，契约不变（每个测试一个全新可写目录，结束清理），在普通机器上行为一致。

## 2. 验收点逐条对照

| # | 题目验收点 | 场景 | 实际结果 | 证据 |
|---|---|---|---|---|
| 1 | 正常完成 | `normal` | `completed`，选 `plan-lean`（8 单位 / 5 天），`plan.json` 已写 | `examples/runs/normal/trace.md` |
| 2 | 缺预算或截止时间 | `missing-budget` | `completed`，选 `plan-standard`（12 单位 / 7 天），`plan.json.gaps = ["budget_units","deadline_days"]` | `examples/runs/missing-budget/trace.md` |
| 3 | 工具失败 | `tool-failure` | 第 6 轮 `draft_script` 失败（`TOOL_ERROR`），第 7 轮缩到 1 篇成功，最终 `completed` | `examples/runs/tool-failure/trace.md` |
| 3b | 工具连续失败 | `tool-failure-twice` | 失败两次后改计划（v3），脚本任务 `skipped`，只保存方案框架，`completed` | `examples/runs/tool-failure-twice/trace.md` |
| 3c | 保存连续失败 | `tool-failure-incomplete` | 两次保存失败 → `incomplete`，`t4_save = failed`，未生成 `plan.json` | `examples/runs/tool-failure-incomplete/trace.md` |
| 4 | 预算超限 | `normal` | 先估 12（超预算 10），重规划后估 8，通过 | 同上，第 3–5 轮 |
| 4b | 无可行方案 | `no-feasible-plan` | 预算 5 < 最便宜 8 → `incomplete`，理由含"需要追加预算" | `examples/runs/no-feasible-plan/trace.md` |
| 4c | 截止时间超限 | `deadline-tight` | 截止 5 天 → `plan-standard`（7 天）被否 → `plan-lean`（5 天）通过 | `examples/runs/deadline-tight/trace.md` |
| 5 | 重复执行 | 任一场景两次 | trace 结构、动作序列、选定方案、费用完全一致（测试内断言） | `test_repeated_runs_are_deterministic` |
| 6 | 失败步骤续跑（加分） | `tool-failure-incomplete` → `tool-failure-resumed` | 续跑后 `t4_save` 从 `failed` 回到 `pending` 并成功，`completed`；检索/估算没有重跑 | `examples/runs/tool-failure-resumed/trace.md` |
| 7 | 步数/工具调用上限 | 内联 | `max_turns=3` → `incomplete: 达到回合上限（3 轮）`；`max_tool_calls=2` → `incomplete: 达到工具调用上限（2 次）` | `test_turn_limit_*`、`test_tool_call_limit_*` |
| 8 | ReAct 循环 | `normal`、`tool-failure` | 决策→执行→观察→调整，见下节 | `examples/runs/*-model/` |

## 3. 两条以上真实模型轨迹（题目硬要求）

模型：`deepseek-account/deepseek-flash`，通过本机 OpenAI 兼容网关（`http://127.0.0.1:8326/v1`）。
每次调用的 token 数记在同目录 `model_calls.json`。

### 3.1 `normal-model`：预算超限 → 自己改方案（8 轮 / 5 次工具调用，12.9 s）

| 轮 | 动作 | 参数 | 返回 | 下一步依据（模型自述） |
|---|---|---|---|---|
| 1 | `plan_tasks` | t1→t2（仅检索+估算） | v1 就绪 | "先按运营初始方案 plan-standard 检索素材并做真实成本估算；待…" |
| 2 | `search_materials` | `query=春季新品 校园人群 素材` | 0 条命中 | — |
| 3 | `estimate_cost` | `plan-standard` | **12 > 预算 10** | "按规则必须先对运营初始方案做真实成本估算，再决定是否更换方案" |
| 4 | `plan_tasks` | 新增 t3 估 lean，t4 生成，t5 保存 | v2 就绪 | "plan-standard 超出预算，修订计划改选 plan-lean 并先估算真实成本" |
| 5 | `estimate_cost` | `plan-lean` | 8 / 5 天 | — |
| 6 | `draft_script` | `plan-lean`, 3 篇 | 3 篇 | — |
| 7 | `save_plan` | `plan-lean` | 已保存 | — |
| 8 | `finish` | `completed` | — | — |

### 3.2 `tool-failure-model`：工具失败 → 改计划换 id 重试（10 轮 / 6 次调用，17.3 s）

关键 4 轮：

| 轮 | 动作 | 返回 | 模型的自述理由 |
|---|---|---|---|
| 6 | `draft_script` t6, 3 篇 | ✗ `TOOL_ERROR: draft_script` | — |
| 7 | `plan_tasks` | v3 就绪 | "draft_script 首次调用失败（TOOL_ERROR）。按规则重试一次并缩小规模：由 3 条脚本降为 2 条，旧失败任务 t6 以新 id t8 重跑，保存任务改 id t9 依赖 t8。已完成步骤 t1/t2/t5 保留原 id 与结论" |
| 8 | `draft_script` t8, **2 篇** | ✓ 2 篇 | — |
| 9 | `save_plan` t9 | ✓ 已保存 | — |

### 3.3 另外两条

- `missing-budget-model`：模型在第 4 轮提交了一个依赖未列出任务的计划，被循环以
  `INVALID_PLAN: task t5 depends on unknown task t1` 拒绝；第 5 轮它显式补回已完成任务后成功。
  **这是"循环真的在守约束"的最好证据**，比模型一次做对更有说服力。
- `no-feasible-plan-model`：模型先估 12、再估 8，都超预算 5；它又试了一个不存在的 `plan-minimal`
  （返回 `UNKNOWN_PLAN`），最后 `finish(incomplete)` 并给出建议（把预算提到 ≥8 或缩小活动规模）。

四条轨迹覆盖了：正常、超预算重规划、工具失败重规划、信息不足、无可行方案。

## 4. 失败与限制（诚实清单）

| 项 | 现状 | 影响 |
|---|---|---|
| 素材是合成数据 | `data/materials.json` 里 6 条，全部手写 | 检索"命中"只验证流程，不代表检索质量 |
| 模型轨迹依赖本机网关 | 用 `deepseek-account/deepseek-flash` 跑出，token 数在 `model_calls.json` | 换模型/换日期结果可能不同；仓库里保留的是当时输出 |
| 模型偶尔会编方案 id | `no-feasible-plan-model` 第 6 轮试了 `plan-minimal` | 被工具以 `UNKNOWN_PLAN` 拒绝，未污染状态；这是设计内的失败路径 |
| 脚本是模板化的 | 按方案名 + 渠道 + 序号生成标题和钩子 | 不是可用文案 |
| 预算口径有分歧 | 默认"预算只约束方案成本"（与题目参考实例一致），另一种口径用 `charge_tools_to_budget` 开关 | 两种数都打印，评审可自行核对 |
| 无并发/无数据库/无 Web | 题目明确非必需 | 单人单次运行 |
| `MockPolicy` 不是模型 | 确定性策略 | 只能证明"约束驱动的重规划"这一机制，不证明模型决策质量；模型质量由 `*-model/` 轨迹单独证明 |

## 5. 一次失败的真实复盘

`no-feasible-plan`（预算 5）第一版实现里，Mock 直接返回 `incomplete` 而没有先估算——
理由是"反正都不够"。这其实是**把决策藏进了代码**：Agent 没有观测过 12 和 8 这两个数字，
`rejected_plans` 是空的，评审无法核对它凭什么说"没有可行方案"。

改法：`_target_plan` 规定**第一次估算必须是运营带来的方案**（`brief.proposed_plan`），
即使看起来注定超限也要估一次；只有在观测到 `violations` 之后才允许降级或放弃。
现在的 trace 里能看到 12 被拒、8 被拒、然后才 `incomplete`，`rejected_plans` 有内容。

同一个 bug 在模型侧也修了：system prompt 第 2 条明确"第一次 estimate_cost 必须估 proposed_plan"。

## 6. 怎么复核

```bash
PYTHONPATH=src python -m pytest tests -q                 # 25 passed
PYTHONPATH=src python -m content_planner batch           # 重写 examples/runs/（mock）
cat examples/runs/normal/trace.md                        # 人读
python -c "import json;d=json.load(open('examples/runs/normal/trace.json'));print(d['costs'])"
```
