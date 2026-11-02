"""The agent loop.

The loop is deliberately dumb about *decisions* and strict about *invariants*:

* a decision source (mock policy or real model adapter) picks exactly one action;
* the loop validates it against the task plan, executes the tool, folds the
  result back into the state, and asks again — the next decision sees the new
  observation, which is the ReAct requirement;
* limits (turns, tool calls, invalid actions, budget) live here, so a
  misbehaving model cannot bypass them;
* every turn is checkpointed, so an interrupted run can be resumed without
  replaying successful steps.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .models import (
    Decision,
    Observation,
    RunState,
    RunStatus,
    Task,
    TaskPlan,
    TaskStatus,
    TraceEntry,
)
from .tools import ToolBox


class DecisionSource(Protocol):
    """Anything that can pick the next action."""

    name: str

    def decide(self, ctx: "Context") -> Decision: ...


@dataclass
class Context:
    """Everything a decision source is allowed to look at."""

    brief: Any
    state: RunState
    last: Observation | None
    turns_left: int
    calls_left: int
    charge_tools_to_budget: bool
    actions: tuple[str, ...]

    @property
    def remaining_budget(self) -> float | None:
        return self.state.remaining

    def to_prompt_dict(self) -> dict[str, Any]:
        """Compact, model-facing view of the same context."""
        return {
            "brief": self.brief.to_dict(),
            "budget_rule": (
                "预算同时包含工具费用" if self.charge_tools_to_budget else "预算只约束方案成本，工具费用单独统计"
            ),
            "remaining_budget": self.remaining_budget,
            "tool_spend": round(self.state.tool_spend, 3),
            "plan_cost": self.state.plan_cost,
            "turns_left": self.turns_left,
            "calls_left": self.calls_left,
            "plan": self.state.plan.to_dict() if self.state.plan else None,
            "notes": self.state.notes,
            "rejected_plans": self.state.rejected_plans,
            "last_observation": self.last.to_dict() if self.last else None,
        }


@dataclass
class RunConfig:
    max_turns: int = 14
    max_tool_calls: int = 12
    max_invalid_actions: int = 3
    charge_tools_to_budget: bool = False
    out_dir: str | None = None
    checkpoint: bool = True


@dataclass
class RunResult:
    state: RunState
    config: RunConfig = field(default_factory=RunConfig)

    @property
    def status(self) -> RunStatus:
        return self.state.status

    def to_dict(self) -> dict[str, Any]:
        return self.state.to_dict()


def _dedupe_key(action: str, args: dict[str, Any]) -> str:
    return action + "|" + json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)


def run_agent(
    state: RunState,
    source: DecisionSource,
    toolbox: ToolBox,
    config: RunConfig | None = None,
) -> RunResult:
    """Drive the state to a terminal status, or stop at an explicit limit."""

    cfg = config or RunConfig()
    state.policy_name = source.name
    state.charge_tools_to_budget = cfg.charge_tools_to_budget
    checkpoint_path = Path(cfg.out_dir) / "checkpoint.json" if cfg.out_dir else None
    if cfg.out_dir:
        Path(cfg.out_dir).mkdir(parents=True, exist_ok=True)

    succeeded: dict[str, Observation] = {}

    while True:
        if state.turns >= cfg.max_turns:
            _finish_incomplete(state, f"达到回合上限（{cfg.max_turns} 轮），任务未完成")
            break
        if state.tool_calls >= cfg.max_tool_calls:
            _finish_incomplete(state, f"达到工具调用上限（{cfg.max_tool_calls} 次），任务未完成")
            break

        ctx = Context(
            brief=state.brief,
            state=state,
            last=state.trace[-1].observation if state.trace else None,
            turns_left=cfg.max_turns - state.turns,
            calls_left=cfg.max_tool_calls - state.tool_calls,
            charge_tools_to_budget=cfg.charge_tools_to_budget,
            actions=("plan_tasks", "finish") + ToolBox.TOOLS,
        )

        decision = source.decide(ctx)
        state.turns += 1

        # ---- terminal action -------------------------------------------
        if decision.action == "finish":
            requested = str(decision.args.get("status", "incomplete"))
            reason = decision.args.get("reason")
            if requested == "completed" and state.saved_path:
                state.status = RunStatus.COMPLETED
                state.incomplete_reason = None
            elif requested == "completed":
                state.status = RunStatus.INCOMPLETE
                state.incomplete_reason = "声明完成但没有任何方案被成功保存，按未完成处理"
            else:
                state.status = RunStatus.INCOMPLETE
                state.incomplete_reason = reason or "决策方主动结束，未说明原因"
            state.trace.append(
                TraceEntry(
                    turn=state.turns,
                    decision=decision,
                    observation=Observation(
                        action="finish",
                        ok=state.status is RunStatus.COMPLETED,
                        summary="任务结束",
                        payload={"status": state.status.value, "reason": state.incomplete_reason},
                        error=(
                            None
                            if state.status is RunStatus.COMPLETED
                            else f"INCOMPLETE: {state.incomplete_reason}"
                        ),
                    ),
                    state_after=state.snapshot(),
                )
            )
            _save_checkpoint(checkpoint_path, state)
            break

        # ---- planning action -------------------------------------------
        if decision.action == "plan_tasks":
            observation = _apply_plan(state, decision)
            state.trace.append(
                TraceEntry(state.turns, decision, observation, state.snapshot())
            )
            if not observation.ok:
                state.invalid_actions += 1
                if state.invalid_actions >= cfg.max_invalid_actions:
                    _finish_incomplete(state, f"连续 {state.invalid_actions} 次无效动作，任务未完成")
                    break
            _save_checkpoint(checkpoint_path, state)
            continue

        # ---- tool action ------------------------------------------------
        state.tool_calls += 1
        observation = _execute_tool(state, decision, toolbox, succeeded)
        state.add_cost(decision.action, observation.cost_units)
        if observation.action == "save_plan" and observation.ok:
            state.saved_path = observation.payload.get("path")
            state.saved_plan_id = observation.payload.get("plan_id")
            state.plan_cost = observation.payload.get("plan_cost_units")
        if not observation.ok and observation.error and observation.error.startswith(
            ("UNKNOWN_ACTION", "BAD_ARGS")
        ):
            state.invalid_actions += 1
        state.trace.append(TraceEntry(state.turns, decision, observation, state.snapshot()))
        _save_checkpoint(checkpoint_path, state)
        if state.invalid_actions >= cfg.max_invalid_actions:
            _finish_incomplete(state, f"连续 {state.invalid_actions} 次无效动作，任务未完成")
            break

    # ---- final consistency checks --------------------------------------
    if state.status is RunStatus.COMPLETED:
        budget = state.brief.budget_units
        spend = state.budget_spend
        if budget is not None and spend > budget:
            state.status = RunStatus.INCOMPLETE
            state.incomplete_reason = f"最终支出 {spend:g} 超出预算 {budget:g}，不计为完成"
        elif not state.saved_path:
            state.status = RunStatus.INCOMPLETE
            state.incomplete_reason = "没有成功保存方案，不计为完成"
    _save_checkpoint(checkpoint_path, state)
    return RunResult(state=state, config=cfg)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _finish_incomplete(state: RunState, reason: str) -> None:
    state.status = RunStatus.INCOMPLETE
    state.incomplete_reason = reason


def _apply_plan(state: RunState, decision: Decision) -> Observation:
    """Install or revise the task plan, keeping the state of surviving tasks."""
    raw_tasks = decision.args.get("tasks")
    reason = str(decision.args.get("reason", decision.rationale or ""))
    if not isinstance(raw_tasks, list) or not raw_tasks:
        return Observation(
            action="plan_tasks",
            ok=False,
            summary="plan_tasks 需要非空的 tasks 列表",
            error="BAD_ARGS: tasks",
        )
    try:
        tasks = [Task.from_dict(t) for t in raw_tasks]
    except (KeyError, TypeError, ValueError) as exc:
        return Observation(
            action="plan_tasks",
            ok=False,
            summary=f"任务定义无法解析：{exc}",
            error=f"BAD_ARGS: {exc}",
        )

    previous = state.plan
    if previous is not None:
        for task in tasks:
            old = previous.by_id(task.id)
            if old is not None and old.status is TaskStatus.DONE:
                task.status = TaskStatus.DONE
                task.cost_units = old.cost_units
                task.attempts = old.attempts
                task.detail = old.detail
            elif old is not None:
                task.attempts = old.attempts
                task.cost_units = old.cost_units

    candidate = TaskPlan(
        version=(previous.version + 1) if previous else 1,
        tasks=tasks,
        revision_reason=reason,
    )
    errors = candidate.validate()
    if errors:
        return Observation(
            action="plan_tasks",
            ok=False,
            summary="任务计划不合法：" + "；".join(errors[:3]),
            error="INVALID_PLAN: " + "; ".join(errors[:3]),
            payload={"errors": errors},
        )

    state.plan = candidate
    if reason:
        state.notes.append(f"计划 v{candidate.version}：{reason}")
    return Observation(
        action="plan_tasks",
        ok=True,
        summary=f"计划 v{candidate.version} 已就绪，共 {len(tasks)} 个任务",
        payload={
            "version": candidate.version,
            "tasks": [t.id for t in candidate.tasks],
            "kept_done": [
                t.id for t in candidate.tasks if t.status is TaskStatus.DONE
            ],
        },
    )


def _execute_tool(
    state: RunState,
    decision: Decision,
    toolbox: ToolBox,
    succeeded: dict[str, Observation],
) -> Observation:
    action = decision.action
    args = dict(decision.args)
    task_id = args.pop("task_id", None)

    if action not in ToolBox.TOOLS:
        return Observation(
            action=action,
            ok=False,
            summary=f"未知动作 {action!r}",
            error=f"UNKNOWN_ACTION: {action}",
            payload={"allowed": list(ToolBox.TOOLS)},
        )

    task: Task | None = None
    if task_id:
        if state.plan is None:
            return Observation(
                action=action,
                ok=False,
                summary="尚未建立任务计划，无法关联 task_id",
                error="NO_PLAN",
            )
        task = state.plan.by_id(str(task_id))
        if task is None:
            return Observation(
                action=action,
                ok=False,
                summary=f"计划中没有任务 {task_id!r}",
                error=f"UNKNOWN_TASK: {task_id}",
            )
        if task.tool != action:
            return Observation(
                action=action,
                ok=False,
                summary=f"任务 {task.id} 绑定的是 {task.tool}，不能执行 {action}",
                error=f"TASK_TOOL_MISMATCH: {task.tool}",
            )
        if task.status is TaskStatus.DONE:
            return Observation(
                action=action,
                ok=False,
                summary=f"任务 {task.id} 已完成，不重复执行",
                error="ALREADY_DONE",
                payload={"task_id": task.id},
            )
        unmet = [
            dep
            for dep in task.depends_on
            if (state.plan.by_id(dep) is None or state.plan.by_id(dep).status is not TaskStatus.DONE)
        ]
        if unmet:
            return Observation(
                action=action,
                ok=False,
                summary=f"任务 {task.id} 的前置任务未完成：{', '.join(unmet)}",
                error=f"DEPENDENCY_UNMET: {','.join(unmet)}",
                payload={"task_id": task.id, "unmet": unmet},
            )

    if action == "save_plan":
        args.setdefault("brief", state.brief)
        args.setdefault("drafts", state.drafts)
        args.setdefault("notes", [n for n in state.notes if not n.startswith("计划 v")][-6:])

    key = _dedupe_key(action, {k: v for k, v in args.items() if k != "brief"})
    if key in succeeded and action != "save_plan":
        previous = succeeded[key]
        return Observation(
            action=action,
            ok=True,
            summary=f"重复调用，复用已有结果（不重复计费）：{previous.summary}",
            payload={**previous.payload, "duplicate": True},
            cost_units=0.0,
        )

    try:
        observation = toolbox.dispatch(action, args)
    except Exception as exc:  # a tool crash must never kill the run
        observation = Observation(
            action=action,
            ok=False,
            summary=f"工具异常：{type(exc).__name__}",
            error=f"TOOL_CRASH: {exc}",
        )

    if observation.ok:
        succeeded[key] = observation
    if observation.action == "draft_script" and observation.ok:
        state.drafts += len(observation.payload.get("drafts", []))

    if task is not None:
        task.attempts += 1
        task.cost_units = round(task.cost_units + observation.cost_units, 3)
        if observation.ok:
            task.status = TaskStatus.DONE
            task.detail = observation.summary
        else:
            task.status = TaskStatus.FAILED
            task.detail = observation.error or observation.summary

    if observation.ok and action == "estimate_cost":
        plan_id = observation.payload.get("plan_id")
        budget = state.brief.budget_units
        cost = float(observation.payload.get("base_cost_units", 0.0))
        deadline = state.brief.deadline_days
        duration = int(observation.payload.get("duration_days", 0))
        over = []
        if budget is not None and cost > budget:
            over.append(f"成本 {cost:g} 超出预算 {budget:g}")
        if deadline is not None and duration > deadline:
            over.append(f"周期 {duration} 天超出截止 {deadline} 天")
        if over:
            state.rejected_plans.append(str(plan_id))
            state.notes.append("；".join(over) + f"（{plan_id}）")
            observation.payload["violations"] = over
            observation.summary += " | 超出约束：" + "；".join(over)

    return observation


def _save_checkpoint(path: Path | None, state: RunState) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(state.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def resume_state(path: str | Path) -> RunState:
    """Load a checkpoint and re-open failed/blocked tasks so they can continue."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    state = RunState.from_dict(raw)
    state.status = RunStatus.INCOMPLETE
    state.incomplete_reason = None
    if state.plan is not None:
        resumed = []
        for task in state.plan.tasks:
            if task.status in (TaskStatus.FAILED, TaskStatus.BLOCKED):
                task.status = TaskStatus.PENDING
                task.detail = (task.detail + " | 续跑重试").strip(" |")
                resumed.append(task.id)
        if resumed:
            state.notes.append("续跑：重新打开未完成任务 " + ", ".join(resumed))
    return state
