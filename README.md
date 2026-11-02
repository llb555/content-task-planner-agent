# 内容任务规划 Agent

帮运营人员在**预算**与**截止时间**约束下，产出一份可执行的内容活动方案。

模型（或离线 Mock）每轮只做一个决定：调用哪个工具、带什么参数、什么时候换方案、什么时候放弃。
工具负责真实的素材检索、成本估算、脚本生成与方案保存。预算算术、任务依赖、步数上限全部在代码里，
模型绕不过去。

> **题目**：项目题 2 · 内容任务规划 Agent（100 分）
> **仓库**：https://github.com/llb555/content-task-planner-agent
> **运行方式**：CLI（`python -m content_planner`）
> **依赖**：Python 3.10+，运行时零第三方依赖，测试只需 pytest
>
> 提交的最终 commit SHA 见提交说明（README 自身无法记录包含它的那次 commit 的 SHA）。

---

## 评审入口（3 分钟）

```bash
# 1. 跑测试（25 项，覆盖题目全部验收点 + 续跑 + 模型适配器）
pip install -r requirements.txt
PYTHONPATH=src python -m pytest tests -q

# 2. 跑题目参考实例：预算 10，初始方案估 12，改用 8 的方案
PYTHONPATH=src python -m content_planner run --case normal

# 3. 看这次运行的逐步决策记录（人读版）
cat examples/runs/normal/trace.md
```

不需要 API key，不需要联网。第 2 步退出码 `0` 表示完成、`1` 表示明确未完成，可直接进 CI。

**两条真实模型轨迹**（仓库内已提交，可直接看，不必重跑）：

| 文件 | 场景 | 模型做了什么 |
|---|---|---|
| [examples/runs/normal-model/trace.md](examples/runs/normal-model/trace.md) | 预算 10 | 先估初始方案 `plan-standard` 得 12 超限 → 改计划 → 估 `plan-lean` 得 8 → 生成脚本 → 保存 |
| [examples/runs/tool-failure-model/trace.md](examples/runs/tool-failure-model/trace.md) | 脚本工具首次失败 | 失败后**改计划换新 id**、把 3 篇缩到 2 篇重试，保留已完成的检索与估算 |

---

## 环境与安装

- Python 3.10+（开发用 3.12）
- 运行时只用标准库；`requirements.txt` 里只有 pytest
- 只有 `--policy model` 需要 `LLM_API_KEY`

```bash
git clone <this repo> && cd content-task-planner
pip install -r requirements.txt
export PYTHONPATH=src          # Windows PowerShell: $env:PYTHONPATH="src"
```

## 运行方式

### 离线 Mock（默认，评审用这个）

```bash
# 列出全部场景
PYTHONPATH=src python -m content_planner list-cases

# 跑题目参考实例
PYTHONPATH=src python -m content_planner run --case normal

# 自定义输入
PYTHONPATH=src python -m content_planner run \
  --topic "春季新品" --budget 10 --deadline 7 --audience "校园人群" --plan plan-standard

# 限制步数：达到上限返回明确的未完成状态
PYTHONPATH=src python -m content_planner run --case normal --max-turns 3

# 批量跑全部场景，重写 examples/runs/
PYTHONPATH=src python -m content_planner batch
```

### 真实模型

```bash
export LLM_API_KEY=sk-...
export LLM_BASE_URL=https://api.deepseek.com/v1     # 任何 OpenAI 兼容接口，含本地网关
export LLM_MODEL=deepseek-chat

PYTHONPATH=src python -m content_planner run --policy model --case normal
```

**边界说明**：`ModelPolicy` 只负责把状态渲染成 prompt、把回复解析成一个 JSON 动作。
预算核算、依赖校验、重试计数、步数上限全在 `loop.py` / `tools.py`，模型即使无视规则也只能浪费轮次，
被上限截断，无法产出越界的结果。没设 key 时会明确报错，不会静默回退到 Mock。

### 失败后续跑

```bash
# 第一次：保存工具连续失败，明确返回 incomplete，checkpoint 落在 examples/runs/<case>/checkpoint.json
PYTHONPATH=src python -m content_planner run --case tool-failure-incomplete

# 第二次：从 checkpoint 续跑，只重开失败任务，已完成的检索/估算不重跑
PYTHONPATH=src python -m content_planner run --case tool-failure-incomplete \
  --out examples/runs/tool-failure-resumed \
  --resume examples/runs/tool-failure-incomplete/checkpoint.json \
  --clear-failures
```

---

## 目录

```
src/content_planner/
  models.py     领域类型：Brief / Task / TaskPlan / Observation / TraceEntry / RunState
  tools.py      5 个业务工具 + 失败注入 + 保存前预算硬校验
  loop.py       ReAct 循环、任务依赖校验、预算与步数上限、checkpoint / 续跑
  policy.py     MockPolicy（离线确定性）与 ModelPolicy（OpenAI 兼容适配器）
  report.py     trace.json → trace.md 的人读渲染
  cli.py        run / batch / compare / list-cases
tests/
  conftest.py        自建 tmp_path（见 docs/03 的说明）
  test_acceptance.py 25 项验收测试
examples/
  cases.json         8 个场景定义（含对照实验）
  README.md          每个运行记录的清单与对应验收点
  test-report.txt    pytest 完整输出（25 passed）
  runs/              13 组实际输出：9 组离线 + 4 组真实模型
data/
  materials.json     合成素材、工具费用表、3 个候选方案
docs/
  01-requirements.md  02-solution.md  03-evaluation.md  04-ai-usage.md  05-retrospective.md
```

## 决策循环长什么样

每一轮：`decision source 选一个动作 → 循环校验（依赖/预算/上限）→ 执行工具 → 结果并入状态 → 再选下一步`。

`--case normal` 的真实记录（节选，完整见 [examples/runs/normal/trace.md](examples/runs/normal/trace.md)）：

| # | 决策 | 参数 | 结果 | 后续动作依据 |
|---|---|---|---|---|
| 1 | `plan_tasks` | 4 个任务 | ✅ | 还没有计划，先建任务链 |
| 2 | `search_materials` | `query=春季新品` | ✅ 1 条素材 | 先看有哪些可复用素材 |
| 3 | `estimate_cost` | `plan-standard` | ✅ 12 单位 / 7 天 **超预算 10** | — |
| 4 | `plan_tasks` | 改为 `t2b_estimate` 估 `plan-lean` | ✅ | **观测到超限**，重规划到更省的方案 |
| 5 | `estimate_cost` | `plan-lean` | ✅ 8 单位 / 5 天 | — |
| 6 | `draft_script` | 3 篇 | ✅ | 方案在约束内，生成脚本 |
| 7 | `save_plan` | `plan-lean` | ✅ | 方案确定，保存并核对费用合计 |
| 8 | `finish` | `completed` | ✅ | 任务链走完 |

第 4 步只在第 3 步真的返回了超预算数字时才发生——把预算调成 15（`--case budget15`），同一份代码
不会产生任何重规划。这是"约束驱动"而不是"固定流程"的证据。

## 设计要点

**模型决策，代码守约束。** 循环只做四件事：要一个动作、校验它、执行它、把结果放回去。
所有算术（预算、成本累计、步数）都在循环和工具层，换模型不影响正确性。

**失败是可注入的一等公民。** `fail_on={"draft_script": [1, 2]}` 让前两次脚本生成失败。
策略必须看到 `ok=False` 才降级，而不是按固定顺序走。失败任务在计划里标 `failed`/`skipped`，
`attempts` 计数和错误码都留在 trace 里——失败步骤永远不会被记成成功。

**未完成是显式状态。** 回合上限、工具调用上限、连续无效动作、无可行方案、保存连续失败，
五种情况都返回 `RunStatus.INCOMPLETE` 并带 `incomplete_reason`，退出码 1。

**预算口径写清楚。** 默认口径是"预算只约束方案成本，工具费用单独统计"（与题目参考实例一致：
预算 10、方案 8 → 余额 2）。`trace.md` 同时给出"计入预算的合计"和"全部花费（含工具费）"，
两种算法都摆出来，不靠含糊过关。切换成"工具费也计入预算"只需 `charge_tools_to_budget=True`。

## 已知限制

- 素材、报价、方案成本全部是**合成数据**，只用于验证流程与算术，不代表真实市场
- `MockPolicy` 是确定性策略，用来证明**约束驱动的重规划**，不代表模型决策质量
- 真实模型轨迹是用本机 OpenAI 兼容网关（`deepseek-account/deepseek-flash`）跑出来的，
  `examples/runs/*-model/` 里有 `model_calls.json` 记录每次调用的 token 数；换模型结果可能不同
- 脚本生成是模板化的（按方案名与渠道生成标题和钩子），不生成真实文案
- 无并发、无持久化数据库、无 Web 界面——题目明确说这些非必需

## 文档

| 文件 | 内容 |
|---|---|
| [docs/01-requirements.md](docs/01-requirements.md) | 用户、目标、范围、成功标准、验收点对照 |
| [docs/02-solution.md](docs/02-solution.md) | 调用流程、动态决策、技术取舍 |
| [docs/03-evaluation.md](docs/03-evaluation.md) | 测试预期、实际结果、失败与限制 |
| [docs/04-ai-usage.md](docs/04-ai-usage.md) | AI 辅助内容、我的修改与验证 |
| [docs/05-retrospective.md](docs/05-retrospective.md) | 最难的问题、学习、下一步 |
