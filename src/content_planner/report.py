"""Trace rendering.

`trace.json` is the machine-readable record; `trace.md` is the same run as a
reviewable table. Both are written for every run, because a reviewer reading the
Markdown should be able to check the numbers without opening the JSON.
"""

from __future__ import annotations

from typing import Any

from .models import RunState, RunStatus, TaskStatus


def _money(value: float | None) -> str:
    return "—" if value is None else f"{value:g}"


def render_trace_md(state: RunState, *, title: str = "") -> str:
    brief = state.brief
    lines: list[str] = []
    heading = title or f"{brief.topic} / {state.policy_name}"
    lines.append(f"# 运行记录：{heading}")
    lines.append("")
    lines.append("## 输入")
    lines.append("")
    lines.append("| 字段 | 值 |")
    lines.append("|---|---|")
    lines.append(f"| 主题 | {brief.topic} |")
    lines.append(f"| 预算 | {_money(brief.budget_units)} 单位 |")
    lines.append(f"| 截止 | {_money(brief.deadline_days)} 天 |")
    lines.append(f"| 人群 | {brief.audience or '—'} |")
    lines.append(f"| 初始方案 | {brief.proposed_plan or '—（未指定，按默认首选方案）'} |")
    lines.append(f"| 决策源 | `{state.policy_name}` |")
    lines.append(f"| 预算口径 | {'含工具费用' if state.charge_tools_to_budget else '仅方案成本，工具费另计'} |")
    lines.append("")

    lines.append("## 结论")
    lines.append("")
    lines.append(f"- 状态：**{state.status.value}**")
    if state.incomplete_reason:
        lines.append(f"- 未完成原因：{state.incomplete_reason}")
    lines.append(f"- 已保存方案：{state.saved_path or '—'}")
    lines.append(f"- 方案成本：{_money(state.plan_cost)} 单位")
    lines.append(f"- 工具费用：{_money(state.tool_spend)} 单位（{_fmt_by_tool(state.cost_by_tool)}）")
    lines.append(f"- 费用合计：{_money(state.total_cost)} 单位 / 预算 {_money(brief.budget_units)}")
    if state.charge_tools_to_budget:
        lines.append("- 口径：预算同时约束工具费用与方案成本")
    else:
        lines.append(f"- 口径：预算只约束方案成本；全部花费（含工具费）为 {_money(state.all_in_cost)} 单位")
    if state.remaining is not None:
        lines.append(f"- 预算余额：{_money(state.remaining)} 单位")
    lines.append(f"- 轮次 / 工具调用：{state.turns} / {state.tool_calls}")
    lines.append("")

    if state.plan is not None:
        lines.append(f"## 任务计划（v{state.plan.version}）")
        lines.append("")
        if state.plan.revision_reason:
            lines.append(f"修订理由：{state.plan.revision_reason}")
            lines.append("")
        lines.append("| 任务 | 工具 | 依赖 | 状态 | 尝试 | 费用 | 说明 |")
        lines.append("|---|---|---|---|---|---|---|")
        for task in state.plan.tasks:
            icon = {
                TaskStatus.DONE: "✅ done",
                TaskStatus.FAILED: "❌ failed",
                TaskStatus.SKIPPED: "⏭ skipped",
                TaskStatus.BLOCKED: "⛔ blocked",
                TaskStatus.PENDING: "⏳ pending",
            }[task.status]
            lines.append(
                f"| {task.id} {task.name} | `{task.tool}` | {', '.join(task.depends_on) or '—'} "
                f"| {icon} | {task.attempts} | {_money(task.cost_units)} | {task.detail or '—'} |"
            )
        lines.append("")

    lines.append("## 逐步记录")
    lines.append("")
    lines.append("| # | 决策 | 参数 | 结果 | 返回摘要 | 后续动作依据 |")
    lines.append("|---|---|---|---|---|---|")
    for entry in state.trace:
        obs = entry.observation
        args = _short_args(entry.decision.args)
        mark = "✅" if obs.ok else "❌"
        summary = obs.summary.replace("|", "/")
        if obs.error:
            summary += f" (`{obs.error}`)"
        lines.append(
            f"| {entry.turn} | `{entry.decision.action}` | {args} | {mark} | {summary} "
            f"| {entry.decision.rationale or '—'} |"
        )
    lines.append("")

    lines.append("## 费用合计")
    lines.append("")
    lines.append("| 项目 | 单位 |")
    lines.append("|---|---|")
    for tool, cost in sorted(state.cost_by_tool.items()):
        lines.append(f"| 工具 `{tool}` | {_money(cost)} |")
    lines.append(f"| **工具费用小计** | **{_money(state.tool_spend)}** |")
    lines.append(f"| 方案成本（{state.saved_plan_id or '未选定'}） | {_money(state.plan_cost)} |")
    lines.append(f"| **计入预算的合计** | **{_money(state.total_cost)}** |")
    lines.append(f"| 全部花费（含工具费，仅参考） | {_money(state.all_in_cost)} |")
    lines.append("")

    if state.notes:
        lines.append("## 备注与缺口")
        lines.append("")
        for note in state.notes:
            lines.append(f"- {note}")
        lines.append("")
    if state.rejected_plans:
        lines.append(f"被否决的方案：{', '.join(state.rejected_plans)}")
        lines.append("")

    return "\n".join(lines)


def _fmt_by_tool(costs: dict[str, float]) -> str:
    if not costs:
        return "无"
    return "，".join(f"{k} {v:g}" for k, v in sorted(costs.items()))


def _short_args(args: dict[str, Any]) -> str:
    if not args:
        return "—"
    parts = []
    for key, value in args.items():
        if key == "tasks":
            ids = [t.get("id") for t in value if isinstance(t, dict)]
            parts.append(f"tasks=[{', '.join(str(i) for i in ids)}]")
        elif isinstance(value, (dict, list)):
            parts.append(f"{key}=…")
        else:
            text = str(value)
            parts.append(f"{key}={text if len(text) <= 40 else text[:37] + '…'}")
    return ", ".join(parts)


def render_summary(state: RunState) -> str:
    """One-line-per-run summary used by `batch` and the README tables."""
    plan = state.saved_plan_id or "—"
    reason = state.incomplete_reason or ""
    return (
        f"{state.status.value:<11} plan={plan:<14} cost={state.total_cost:<6g} "
        f"budget={_money(state.brief.budget_units):<5} turns={state.turns:<2} {reason}"
    )


def is_ok(state: RunState) -> bool:
    return state.status is RunStatus.COMPLETED
