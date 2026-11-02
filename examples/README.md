# examples/ · 输入、实际输出与运行记录

每个子目录是一次**真实运行**的产物，不是手写的示例。

| 文件 | 内容 |
|---|---|
| `cases.json` | 8 个场景的输入定义（含失败注入与对照实验） |
| `test-report.txt` | `pytest -v` 的完整输出（25 passed） |
| `runs/<case>/trace.md` | 人读版逐步记录：输入、结论、任务计划、每轮决策与依据、费用合计 |
| `runs/<case>/trace.json` | 机器可读的完整状态（含每轮 `state_after` 快照） |
| `runs/<case>/plan.json` | 真正保存下来的方案（未完成的场景没有这个文件） |
| `runs/<case>/checkpoint.json` | 每轮结束时的状态快照，`--resume` 用它续跑 |
| `runs/<case>/model_calls.json` | 仅 `*-model`：每次模型调用的 token 统计 |

## 运行记录清单

### 离线 Mock（确定性，可复现）

| 场景 | 状态 | 选定方案 | 覆盖的验收点 |
|---|---|---|---|
| [normal](runs/normal/trace.md) | completed | plan-lean（8） | 正常完成、预算超限重规划、ReAct 循环 |
| [missing-budget](runs/missing-budget/trace.md) | completed | plan-standard（12） | 缺预算与截止时间，标注假设与缺口 |
| [tool-failure](runs/tool-failure/trace.md) | completed | plan-lean（8） | 工具失败 → 缩小规模重试 |
| [tool-failure-twice](runs/tool-failure-twice/trace.md) | completed | plan-lean（8） | 连续失败 → 改计划、保留已完成步骤 |
| [tool-failure-incomplete](runs/tool-failure-incomplete/trace.md) | **incomplete** | — | 保存连续失败，失败步骤不记成功 |
| [tool-failure-resumed](runs/tool-failure-resumed/trace.md) | completed | plan-lean（8） | **失败步骤续跑**（加分项） |
| [no-feasible-plan](runs/no-feasible-plan/trace.md) | **incomplete** | — | 预算超限且无替代，返回缺口 |
| [deadline-tight](runs/deadline-tight/trace.md) | completed | plan-lean（8） | 截止时间超限触发重规划 |
| [budget15](runs/budget15/trace.md) | completed | plan-standard（12） | 对照实验：预算够时不重规划 |

### 真实模型（`deepseek-account/deepseek-flash`，经本机 OpenAI 兼容网关）

| 场景 | 状态 | 轮次 | 覆盖的验收点 |
|---|---|---|---|
| [normal-model](runs/normal-model/trace.md) | completed | 8 | 观测到 12 > 10 后自己改计划 |
| [tool-failure-model](runs/tool-failure-model/trace.md) | completed | 10 | 失败后**自己换 id、缩小规模**重试 |
| [missing-budget-model](runs/missing-budget-model/trace.md) | completed | 9 | 提交非法计划被拒 → 下一轮自己修正 |
| [no-feasible-plan-model](runs/no-feasible-plan-model/trace.md) | **incomplete** | 7 | 试遍候选（含不存在的方案）后明确放弃 |

## 复现

```bash
# 离线场景（会重写 runs/<case>/，但 mock 结果逐字节可复现）
PYTHONPATH=src python -m content_planner batch

# 单个场景
PYTHONPATH=src python -m content_planner run --case normal

# 真实模型（需要 LLM_API_KEY；结果不保证逐字节一致）
PYTHONPATH=src python -m content_planner run --policy model --case normal
```

> `*-model/` 里的轨迹是当时那次运行的原样输出。模型有随机性，重跑得到的轮次与措辞可能不同，
> 但关键结构（先估 12 → 换 8、失败后换 id 重试）在 prompt 约束下是稳定的。
