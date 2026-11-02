"""Acceptance tests.

Each test maps to one item of the brief's 必须验收 list, the ReAct requirement,
or the step-limit requirement. They run the same loop and the same tools the CLI
uses; nothing is stubbed out.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from content_planner import (
    Brief,
    MockPolicy,
    ModelPolicy,
    RunConfig,
    RunState,
    RunStatus,
    TaskPlan,
    TaskStatus,
    ToolBox,
    resume_state,
    run_agent,
)
from content_planner.policy import _parse_decision

REPO_ROOT = Path(__file__).resolve().parents[1]
CASES = json.loads((REPO_ROOT / "examples" / "cases.json").read_text(encoding="utf-8"))["cases"]
BY_ID = {c["id"]: c for c in CASES}


def run_case(case_id: str, out_dir: Path, **overrides) -> RunState:
    case = {**BY_ID[case_id], **overrides}
    state = RunState(
        brief=Brief(
            topic=case["topic"],
            budget_units=case.get("budget_units"),
            deadline_days=case.get("deadline_days"),
            audience=case.get("audience", ""),
            proposed_plan=case.get("proposed_plan"),
        )
    )
    toolbox = ToolBox(
        fail_on={k: ([v] if isinstance(v, int) else list(v)) for k, v in case.get("fail_on", {}).items()},
        out_dir=out_dir,
    )
    return run_agent(state, MockPolicy(), toolbox, RunConfig(out_dir=str(out_dir))).state


def actions(state: RunState) -> list[str]:
    return [entry.decision.action for entry in state.trace]


# --- 正常完成 -------------------------------------------------------------


def test_normal_case_completes_and_saves(tmp_path):
    state = run_case("normal", tmp_path)

    assert state.status is RunStatus.COMPLETED
    assert state.saved_plan_id == "plan-lean"
    assert state.saved_path and Path(state.saved_path).is_file()


# --- 预算超限：观察到超支后换方案 -----------------------------------------


def test_over_budget_switches_to_a_cheaper_plan(tmp_path):
    state = run_case("normal", tmp_path)

    estimates = [
        (entry.decision.args.get("plan_id"), entry.observation.payload.get("plan_id"))
        for entry in state.trace
        if entry.decision.action == "estimate_cost"
    ]
    assert estimates[0][1] == "plan-standard"
    assert estimates[1][1] == "plan-lean"
    # The switch is visible in the trace, not just in the final result.
    assert state.rejected_plans == ["plan-standard"]
    assert any("超出预算" in note for note in state.notes)
    assert state.plan_cost is not None and state.plan_cost <= 10


def test_control_case_with_enough_budget_does_not_replan(tmp_path):
    """Same input with a 15-unit budget must NOT trigger a revision."""
    state = run_case("budget15", tmp_path)

    assert state.status is RunStatus.COMPLETED
    assert state.saved_plan_id == "plan-standard"
    revisions = [e for e in state.trace if e.decision.action == "plan_tasks"]
    assert len(revisions) == 1, "only the initial plan; no revision should be needed"
    assert state.rejected_plans == []


# --- 缺预算或截止时间 -----------------------------------------------------


def test_missing_budget_still_produces_a_plan(tmp_path):
    state = run_case("missing-budget", tmp_path)

    assert state.status is RunStatus.COMPLETED
    assert state.brief.budget_units is None
    assert state.saved_plan_id is not None
    assert state.saved_path is not None
    saved = json.loads(Path(state.saved_path).read_text(encoding="utf-8"))
    assert saved["gaps"] == ["budget_units", "deadline_days"], "the saved plan must name what was missing"
    assert any("缺" in note for note in state.notes)


def test_missing_deadline_only_is_reported(tmp_path):
    state = run_case("missing-budget", tmp_path, budget_units=10, deadline_days=None)

    assert state.status is RunStatus.COMPLETED
    saved = json.loads(Path(state.saved_path).read_text(encoding="utf-8"))
    assert saved["gaps"] == ["deadline_days"]


# --- 工具失败 -------------------------------------------------------------


def test_tool_failure_is_observed_and_retried_smaller(tmp_path):
    state = run_case("tool-failure", tmp_path)

    drafts = [e for e in state.trace if e.decision.action == "draft_script"]
    assert len(drafts) == 2, "one failed attempt plus one retry"
    assert drafts[0].observation.ok is False
    assert drafts[1].observation.ok is True
    assert drafts[0].decision.args["pieces"] > drafts[1].decision.args["pieces"]
    assert state.status is RunStatus.COMPLETED


def test_two_failures_lead_to_a_revised_plan_that_keeps_earlier_work(tmp_path):
    state = run_case("tool-failure-twice", tmp_path)

    assert state.status is RunStatus.COMPLETED
    draft = state.plan.by_id("t3_draft")
    assert draft.status is TaskStatus.SKIPPED
    assert draft.attempts == 2
    # Everything that worked before the failure is still recorded as done.
    assert state.plan.by_id("t1_research").status is TaskStatus.DONE
    assert state.plan.by_id("t2b_estimate").status is TaskStatus.DONE
    assert state.saved_plan_id == "plan-lean"
    assert state.plan.version == 3


def test_failed_step_is_never_recorded_as_success(tmp_path):
    state = run_case("tool-failure-incomplete", tmp_path)

    assert state.status is RunStatus.INCOMPLETE
    assert state.saved_path is None
    assert state.incomplete_reason and "保存" in state.incomplete_reason
    failed = [e for e in state.trace if not e.observation.ok]
    assert len(failed) >= 2
    assert all(e.observation.error for e in failed)
    assert state.plan.by_id("t4_save").status is TaskStatus.FAILED


# --- 预算超限且无替代 -----------------------------------------------------


def test_no_feasible_plan_returns_incomplete_with_the_gap(tmp_path):
    state = run_case("no-feasible-plan", tmp_path)

    assert state.status is RunStatus.INCOMPLETE
    assert state.saved_path is None
    assert state.incomplete_reason
    assert "没有可行方案" in state.incomplete_reason
    assert "追加预算" in state.incomplete_reason


def test_deadline_constraint_can_also_force_a_replan(tmp_path):
    state = run_case("deadline-tight", tmp_path)

    assert state.status is RunStatus.COMPLETED
    assert state.saved_plan_id == "plan-lean"
    assert "plan-standard" in state.rejected_plans
    assert any("截止" in note for note in state.notes)


# --- 重复执行 -------------------------------------------------------------


def test_repeated_runs_are_deterministic(tmp_path):
    first = run_case("normal", tmp_path / "a")
    second = run_case("normal", tmp_path / "b")

    def shape(state: RunState):
        return [
            (
                e.turn,
                e.decision.action,
                json.dumps(e.decision.args, sort_keys=True, ensure_ascii=False),
                e.observation.ok,
            )
            for e in state.trace
        ]

    assert shape(first) == shape(second)
    assert first.saved_plan_id == second.saved_plan_id
    assert first.total_cost == second.total_cost


# --- ReAct：选择 → 执行 → 观察 → 调整 ------------------------------------


def test_trace_shows_an_observation_driven_revision(tmp_path):
    state = run_case("normal", tmp_path)

    assert actions(state)[:4] == [
        "plan_tasks",
        "search_materials",
        "estimate_cost",
        "plan_tasks",
    ]
    over = state.trace[2]
    assert over.observation.payload["base_cost_units"] > 10
    # The revision exists because of that observation, and says so.
    assert "超出约束" in over.decision.rationale or "超限" in state.trace[3].decision.rationale
    assert state.trace[3].decision.args["tasks"][-1]["id"] == "t2b_estimate"


def test_every_turn_records_the_state_it_left_behind(tmp_path):
    state = run_case("tool-failure", tmp_path)

    for entry in state.trace:
        assert "task_status" in entry.state_after
        assert "total_cost" in entry.state_after
    # The snapshot after the failure shows the task as failed, not done.
    failed_turn = next(e for e in state.trace if not e.observation.ok)
    assert failed_turn.state_after["task_status"]["t3_draft"] == "failed"


# --- 步数与工具调用上限 ---------------------------------------------------


def test_turn_limit_returns_incomplete(tmp_path):
    state = RunState(brief=Brief(topic="春季新品", budget_units=10, deadline_days=7))
    result = run_agent(
        state, MockPolicy(), ToolBox(out_dir=tmp_path), RunConfig(max_turns=3, out_dir=str(tmp_path))
    )

    assert result.state.status is RunStatus.INCOMPLETE
    assert "回合上限" in result.state.incomplete_reason
    assert result.state.turns == 3


def test_tool_call_limit_returns_incomplete(tmp_path):
    state = RunState(brief=Brief(topic="春季新品", budget_units=10, deadline_days=7))
    result = run_agent(
        state,
        MockPolicy(),
        ToolBox(out_dir=tmp_path),
        RunConfig(max_turns=30, max_tool_calls=2, out_dir=str(tmp_path)),
    )

    assert result.state.status is RunStatus.INCOMPLETE
    assert "工具调用上限" in result.state.incomplete_reason
    assert result.state.tool_calls == 2


def test_invalid_actions_are_capped(tmp_path):
    class Broken:
        name = "broken"

        def decide(self, ctx):
            from content_planner import Decision

            return Decision("teleport", {})

    state = RunState(brief=Brief(topic="x", budget_units=10))
    result = run_agent(
        state, Broken(), ToolBox(out_dir=tmp_path), RunConfig(max_invalid_actions=2, out_dir=str(tmp_path))
    )

    assert result.state.status is RunStatus.INCOMPLETE
    assert "无效动作" in result.state.incomplete_reason


# --- 计划结构 -------------------------------------------------------------


def test_plan_validation_rejects_cycles_and_unknown_deps():
    cyclic = TaskPlan(
        tasks=[
            {"id": "a", "name": "a", "tool": "estimate_cost", "depends_on": ["b"]},
            {"id": "b", "name": "b", "tool": "save_plan", "depends_on": ["a"]},
        ]
    )
    errors = cyclic.validate()
    assert any("cycle" in e for e in errors)

    dangling = TaskPlan(tasks=[{"id": "a", "name": "a", "tool": "estimate_cost", "depends_on": ["zz"]}])
    assert any("unknown task" in e for e in dangling.validate())


def test_dependency_violation_is_refused(tmp_path):
    """Calling a task before its dependency is done must fail loudly."""

    class Impatient:
        name = "impatient"
        done = False

        def decide(self, ctx):
            from content_planner import Decision

            if not self.done:
                self.done = True
                return Decision(
                    "plan_tasks",
                    {
                        "tasks": [
                            {"id": "t1", "name": "r", "tool": "search_materials", "depends_on": []},
                            {
                                "id": "t2",
                                "name": "s",
                                "tool": "save_plan",
                                "depends_on": ["t1"],
                            },
                        ]
                    },
                )
            if ctx.state.tool_calls == 0:
                return Decision("save_plan", {"task_id": "t2", "plan_id": "plan-lean"})
            return Decision("finish", {"status": "incomplete", "reason": "done"})

    state = RunState(brief=Brief(topic="x", budget_units=10))
    result = run_agent(
        state, Impatient(), ToolBox(out_dir=tmp_path), RunConfig(out_dir=str(tmp_path))
    )
    first_tool = next(e for e in result.state.trace if e.decision.action == "save_plan")
    assert first_tool.observation.ok is False
    assert "DEPENDENCY_UNMET" in first_tool.observation.error


# --- 费用核算 -------------------------------------------------------------


def test_tool_spend_matches_the_sum_of_observations(tmp_path):
    state = run_case("tool-failure", tmp_path)

    total = sum(e.observation.cost_units for e in state.trace)
    assert state.tool_spend == pytest.approx(total)
    assert state.all_in_cost == pytest.approx(state.tool_spend + (state.plan_cost or 0.0))
    # Default accounting: the brief's budget only constrains the plan cost.
    assert state.total_cost == pytest.approx(state.plan_cost or 0.0)


def test_estimate_matches_the_catalog(tmp_path):
    toolbox = ToolBox(out_dir=tmp_path)
    for plan_id, variant in toolbox.plan_variants.items():
        obs = toolbox.estimate_cost(plan_id)
        assert obs.ok
        assert obs.payload["base_cost_units"] == variant["base_cost_units"]
        assert obs.payload["duration_days"] == variant["duration_days"]


def test_save_plan_refuses_an_over_budget_plan(tmp_path):
    toolbox = ToolBox(out_dir=tmp_path)
    obs = toolbox.save_plan(plan_id="plan-standard", brief=Brief(topic="x", budget_units=5))

    assert obs.ok is False
    assert obs.error == "OVER_BUDGET"
    assert not (tmp_path / "plan.json").exists()


# --- 续跑 -----------------------------------------------------------------


def test_resume_reopens_failed_tasks_and_finishes(tmp_path):
    # First pass: the save tool fails twice, so the run ends incomplete.
    first = run_case("tool-failure-incomplete", tmp_path / "first")
    assert first.status is RunStatus.INCOMPLETE

    checkpoint = tmp_path / "first" / "checkpoint.json"
    assert checkpoint.is_file()

    resumed = resume_state(checkpoint)
    assert resumed.plan.by_id("t4_save").status is TaskStatus.PENDING
    assert resumed.plan.by_id("t1_research").status is TaskStatus.DONE

    # Second pass: no injected failure, so the reopened task completes.
    result = run_agent(
        resumed, MockPolicy(), ToolBox(out_dir=tmp_path / "second"), RunConfig(out_dir=str(tmp_path / "second"))
    )
    assert result.state.status is RunStatus.COMPLETED
    assert result.state.saved_path
    # Successful steps are not re-executed on resume.
    resumed_actions = [e.decision.action for e in result.state.trace[first.turns:]]
    assert "search_materials" not in resumed_actions


# --- 模型适配器（不联网） -------------------------------------------------


def test_model_reply_parsing_accepts_json_and_fences():
    assert _parse_decision('{"action":"finish","args":{"status":"completed"}}').action == "finish"
    fenced = '```json\n{"action":"estimate_cost","args":{"plan_id":"plan-lean"}}\n```'
    assert _parse_decision(fenced).args["plan_id"] == "plan-lean"
    noisy = 'sure, here you go: {"action":"search_materials","args":{"query":"x"}} thanks'
    assert _parse_decision(noisy).action == "search_materials"
    with pytest.raises(ValueError):
        _parse_decision("no json here")


def test_model_policy_requires_a_key(tmp_path, monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    policy = ModelPolicy()
    state = RunState(brief=Brief(topic="x", budget_units=10))
    with pytest.raises(RuntimeError):
        run_agent(state, policy, ToolBox(out_dir=tmp_path), RunConfig(out_dir=str(tmp_path)))


def test_model_policy_builds_a_prompt_with_the_last_observation(tmp_path):
    state = run_case("normal", tmp_path)
    from content_planner.loop import Context

    ctx = Context(
        brief=state.brief,
        state=state,
        last=state.trace[-1].observation,
        turns_left=5,
        calls_left=5,
        charge_tools_to_budget=False,
        actions=("plan_tasks", "finish") + ToolBox.TOOLS,
    )
    messages = ModelPolicy().build_messages(ctx)
    assert messages[0]["role"] == "system"
    assert "plan_tasks" in messages[0]["content"]
    assert "春季新品" in messages[1]["content"]
    assert "last_observation" in messages[1]["content"]
