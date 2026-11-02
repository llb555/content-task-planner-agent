"""Domain model for the content task planning agent.

Everything here is plain data. The loop passes these objects between the decision
source (mock policy or real model adapter) and the tools, and the trace records
them verbatim, so a reviewer can recompute every number without re-running the
model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class TaskStatus(str, Enum):
    """State of one task inside the plan."""

    PENDING = "pending"
    DONE = "done"
    FAILED = "failed"
    BLOCKED = "blocked"
    SKIPPED = "skipped"


class RunStatus(str, Enum):
    """Terminal state of a whole run."""

    COMPLETED = "completed"
    INCOMPLETE = "incomplete"


@dataclass
class Task:
    """One step of the task plan.

    `tool` is the business tool that satisfies this task. The loop refuses to run
    a tool against a task that declares a different one, which is how "step
    dependency and actual state" stays checkable instead of decorative.
    """

    id: str
    name: str
    tool: str
    depends_on: list[str] = field(default_factory=list)
    status: TaskStatus = TaskStatus.PENDING
    attempts: int = 0
    cost_units: float = 0.0
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "tool": self.tool,
            "depends_on": list(self.depends_on),
            "status": self.status.value,
            "attempts": self.attempts,
            "cost_units": round(self.cost_units, 3),
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Task":
        return cls(
            id=str(raw["id"]),
            name=str(raw.get("name", raw["id"])),
            tool=str(raw.get("tool", "")),
            depends_on=[str(d) for d in raw.get("depends_on", [])],
            status=TaskStatus(raw.get("status", TaskStatus.PENDING.value)),
            attempts=int(raw.get("attempts", 0)),
            cost_units=float(raw.get("cost_units", 0.0)),
            detail=str(raw.get("detail", "")),
        )


@dataclass
class TaskPlan:
    """An ordered task graph plus the reason it exists.

    `version` increments every time the agent revises the plan. Revisions keep
    the status of tasks whose ids survive, so a re-plan never loses work that
    already succeeded.
    """

    version: int = 0
    tasks: list[Task] = field(default_factory=list)
    revision_reason: str = ""

    def __post_init__(self) -> None:
        # Accept dicts so a plan can be built straight from a model reply.
        self.tasks = [
            t if isinstance(t, Task) else Task.from_dict(t) for t in self.tasks
        ]

    def by_id(self, task_id: str) -> Task | None:
        for task in self.tasks:
            if task.id == task_id:
                return task
        return None

    def ready(self) -> list[Task]:
        """Pending tasks whose dependencies are all done."""
        done = {t.id for t in self.tasks if t.status is TaskStatus.DONE}
        return [
            t
            for t in self.tasks
            if t.status is TaskStatus.PENDING and all(dep in done for dep in t.depends_on)
        ]

    def validate(self) -> list[str]:
        """Return structural problems; an empty list means the plan is runnable."""
        errors: list[str] = []
        if not self.tasks:
            return ["plan has no tasks"]
        seen: set[str] = set()
        for task in self.tasks:
            if task.id in seen:
                errors.append(f"duplicate task id: {task.id}")
            seen.add(task.id)
        for task in self.tasks:
            for dep in task.depends_on:
                if dep not in seen:
                    errors.append(f"task {task.id} depends on unknown task {dep}")
                if dep == task.id:
                    errors.append(f"task {task.id} depends on itself")
        # Cycle detection (iterative DFS, colouring).
        colour: dict[str, int] = {}
        graph = {t.id: [d for d in t.depends_on if d in seen] for t in self.tasks}

        def visit(node: str, stack: list[str]) -> None:
            colour[node] = 1
            stack.append(node)
            for nxt in graph.get(node, []):
                if colour.get(nxt, 0) == 1:
                    cycle = " -> ".join(stack[stack.index(nxt):] + [nxt])
                    errors.append(f"dependency cycle: {cycle}")
                elif colour.get(nxt, 0) == 0:
                    visit(nxt, stack)
            colour[node] = 2
            stack.pop()

        for task in self.tasks:
            if colour.get(task.id, 0) == 0:
                visit(task.id, [])
        return errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "revision_reason": self.revision_reason,
            "tasks": [t.to_dict() for t in self.tasks],
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "TaskPlan":
        return cls(
            version=int(raw.get("version", 0)),
            revision_reason=str(raw.get("revision_reason", "")),
            tasks=[Task.from_dict(t) for t in raw.get("tasks", [])],
        )


@dataclass
class Observation:
    """Normalised result of one action, as the decision source sees it."""

    action: str
    ok: bool
    summary: str
    payload: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    cost_units: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "action": self.action,
            "ok": self.ok,
            "summary": self.summary,
            "cost_units": round(self.cost_units, 3),
        }
        if self.payload:
            out["payload"] = self.payload
        if self.error:
            out["error"] = self.error
        return out


@dataclass
class Decision:
    """One decision, before it is executed."""

    action: str
    args: dict[str, Any] = field(default_factory=dict)
    rationale: str = ""
    raw: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out = {"action": self.action, "args": self.args, "rationale": self.rationale}
        if self.raw is not None:
            out["raw_reply"] = self.raw
        return out


@dataclass
class TraceEntry:
    """A single decide -> act -> observe turn, with the state it left behind."""

    turn: int
    decision: Decision
    observation: Observation
    state_after: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "turn": self.turn,
            "decision": self.decision.to_dict(),
            "observation": self.observation.to_dict(),
            "state_after": self.state_after,
        }


@dataclass
class Brief:
    """The operator's request."""

    topic: str
    budget_units: float | None = None
    deadline_days: int | None = None
    audience: str = ""
    #: The draft plan the operator arrived with; the agent prices this first.
    proposed_plan: str | None = None

    @property
    def missing(self) -> list[str]:
        gaps = []
        if self.budget_units is None:
            gaps.append("budget_units")
        if self.deadline_days is None:
            gaps.append("deadline_days")
        return gaps

    def to_dict(self) -> dict[str, Any]:
        return {
            "topic": self.topic,
            "budget_units": self.budget_units,
            "deadline_days": self.deadline_days,
            "audience": self.audience,
            "proposed_plan": self.proposed_plan,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Brief":
        return cls(
            topic=str(raw.get("topic", "")),
            budget_units=raw.get("budget_units"),
            deadline_days=raw.get("deadline_days"),
            audience=str(raw.get("audience", "")),
            proposed_plan=raw.get("proposed_plan"),
        )


@dataclass
class RunState:
    """All mutable bookkeeping for one run, serialisable as a checkpoint."""

    brief: Brief = field(default_factory=lambda: Brief(topic=""))
    policy_name: str = "mock"
    #: When true the brief's budget also covers tool fees, not just the plan cost.
    charge_tools_to_budget: bool = False
    plan: TaskPlan | None = None
    tool_spend: float = 0.0
    plan_cost: float | None = None
    drafts: int = 0
    notes: list[str] = field(default_factory=list)
    rejected_plans: list[str] = field(default_factory=list)
    trace: list[TraceEntry] = field(default_factory=list)
    turns: int = 0
    tool_calls: int = 0
    invalid_actions: int = 0
    saved_path: str | None = None
    saved_plan_id: str | None = None
    status: RunStatus = RunStatus.INCOMPLETE
    incomplete_reason: str | None = None
    cost_by_tool: dict[str, float] = field(default_factory=dict)

    # -- derived values ---------------------------------------------------

    @property
    def total_cost(self) -> float:
        """Plan cost plus the tool fees the brief says are chargeable.

        With `charge_tools_to_budget=False` (the default) the brief's budget is a
        content-production budget and tool fees are reported separately, which is
        how the reference example counts it: an 8-unit plan under a 10-unit budget
        leaves 2 units, not 2 minus the tool fees.
        """
        extra = self.tool_spend if self.charge_tools_to_budget else 0.0
        return round(extra + (self.plan_cost or 0.0), 3)

    @property
    def budget_spend(self) -> float:
        """Whatever the brief's budget is measured against (see `total_cost`)."""
        return self.total_cost

    @property
    def all_in_cost(self) -> float:
        """Everything the run actually cost: plan + every tool call."""
        return round(self.tool_spend + (self.plan_cost or 0.0), 3)

    @property
    def remaining(self) -> float | None:
        if self.brief.budget_units is None:
            return None
        return round(self.brief.budget_units - self.budget_spend, 3)

    def add_cost(self, tool: str, cost: float) -> None:
        self.tool_spend = round(self.tool_spend + cost, 3)
        self.cost_by_tool[tool] = round(self.cost_by_tool.get(tool, 0.0) + cost, 3)

    def snapshot(self) -> dict[str, Any]:
        """Compact state recorded next to every observation."""
        return {
            "turns": self.turns,
            "tool_calls": self.tool_calls,
            "tool_spend": round(self.tool_spend, 3),
            "plan_cost": self.plan_cost,
            "total_cost": self.total_cost,
            "budget": self.brief.budget_units,
            "remaining": self.remaining,
            "plan_version": self.plan.version if self.plan else None,
            "task_status": (
                {t.id: t.status.value for t in self.plan.tasks} if self.plan else {}
            ),
            "saved_path": self.saved_path,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "brief": self.brief.to_dict(),
            "policy": self.policy_name,
            "charge_tools_to_budget": self.charge_tools_to_budget,
            "status": self.status.value,
            "incomplete_reason": self.incomplete_reason,
            "plan": self.plan.to_dict() if self.plan else None,
            "costs": {
                "tool_spend": round(self.tool_spend, 3),
                "plan_cost": self.plan_cost,
                "total": self.total_cost,
                "all_in": self.all_in_cost,
                "budget": self.brief.budget_units,
                "remaining": self.remaining,
                "by_tool": dict(self.cost_by_tool),
            },
            "drafts": self.drafts,
            "notes": list(self.notes),
            "rejected_plans": list(self.rejected_plans),
            "saved_plan_path": self.saved_path,
            "saved_plan_id": self.saved_plan_id,
            "turns": self.turns,
            "tool_calls": self.tool_calls,
            "invalid_actions": self.invalid_actions,
            "trace": [t.to_dict() for t in self.trace],
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "RunState":
        state = cls(brief=Brief.from_dict(raw.get("brief", {})))
        state.policy_name = str(raw.get("policy", "mock"))
        state.charge_tools_to_budget = bool(raw.get("charge_tools_to_budget", False))
        state.status = RunStatus(raw.get("status", RunStatus.INCOMPLETE.value))
        state.incomplete_reason = raw.get("incomplete_reason")
        state.plan = TaskPlan.from_dict(raw["plan"]) if raw.get("plan") else None
        costs = raw.get("costs", {})
        state.tool_spend = float(costs.get("tool_spend", 0.0))
        state.plan_cost = costs.get("plan_cost")
        state.cost_by_tool = {k: float(v) for k, v in costs.get("by_tool", {}).items()}
        state.drafts = int(raw.get("drafts", 0))
        state.notes = list(raw.get("notes", []))
        state.rejected_plans = list(raw.get("rejected_plans", []))
        state.saved_path = raw.get("saved_plan_path")
        state.saved_plan_id = raw.get("saved_plan_id")
        state.turns = int(raw.get("turns", 0))
        state.tool_calls = int(raw.get("tool_calls", 0))
        state.invalid_actions = int(raw.get("invalid_actions", 0))
        for entry in raw.get("trace", []):
            state.trace.append(
                TraceEntry(
                    turn=int(entry["turn"]),
                    decision=Decision(
                        action=entry["decision"]["action"],
                        args=dict(entry["decision"].get("args", {})),
                        rationale=str(entry["decision"].get("rationale", "")),
                        raw=entry["decision"].get("raw_reply"),
                    ),
                    observation=Observation(
                        action=entry["observation"]["action"],
                        ok=bool(entry["observation"]["ok"]),
                        summary=str(entry["observation"]["summary"]),
                        payload=dict(entry["observation"].get("payload", {})),
                        error=entry["observation"].get("error"),
                        cost_units=float(entry["observation"].get("cost_units", 0.0)),
                    ),
                    state_after=dict(entry.get("state_after", {})),
                )
            )
        return state
