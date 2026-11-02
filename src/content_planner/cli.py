"""Command line entry point.

    python -m content_planner run    --case normal --policy mock
    python -m content_planner batch  --policy mock
    python -m content_planner run    --case tool-failure --resume examples/runs/.../checkpoint.json
    python -m content_planner compare --case normal
    python -m content_planner list-cases

Exit codes: 0 = completed, 1 = incomplete (explicitly reported), 2 = usage error.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .loop import RunConfig, run_agent, resume_state
from .models import Brief, RunState
from .policy import MockPolicy, ModelPolicy
from .report import render_summary, render_trace_md
from .tools import ToolBox

REPO_ROOT = Path(__file__).resolve().parents[2]
CASES_PATH = REPO_ROOT / "examples" / "cases.json"
RUNS_DIR = REPO_ROOT / "examples" / "runs"


def load_cases() -> dict[str, dict]:
    raw = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    return {case["id"]: case for case in raw["cases"]}


def build_state(case: dict) -> RunState:
    return RunState(
        brief=Brief(
            topic=case["topic"],
            budget_units=case.get("budget_units"),
            deadline_days=case.get("deadline_days"),
            audience=case.get("audience", ""),
            proposed_plan=case.get("proposed_plan"),
        )
    )


def build_toolbox(case: dict, out_dir: Path, clear_failures: bool = False) -> ToolBox:
    fail_on = {} if clear_failures else case.get("fail_on", {})
    return ToolBox(
        fail_on={k: ([v] if isinstance(v, int) else list(v)) for k, v in fail_on.items()},
        out_dir=out_dir,
    )


def build_source(args, case: dict):
    if args.policy == "mock":
        return MockPolicy(preferred=case.get("preferred", "plan-standard"))
    return ModelPolicy()


def run_one(args, case: dict, out_dir: Path, state: RunState | None = None) -> RunState:
    out_dir.mkdir(parents=True, exist_ok=True)
    toolbox = build_toolbox(case, out_dir, clear_failures=getattr(args, "clear_failures", False))
    source = build_source(args, case)
    config = RunConfig(
        max_turns=args.max_turns,
        max_tool_calls=args.max_tool_calls,
        max_invalid_actions=args.max_invalid_actions,
        charge_tools_to_budget=case.get("charge_tools_to_budget", False),
        out_dir=str(out_dir),
    )
    result = run_agent(state or build_state(case), source, toolbox, config)
    state = result.state

    (out_dir / "trace.json").write_text(
        json.dumps(
            {"case": case.get("id", "adhoc"), "title": case.get("title", ""), **state.to_dict()},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (out_dir / "trace.md").write_text(
        render_trace_md(state, title=case.get("title") or case.get("id", "")), encoding="utf-8"
    )
    if hasattr(source, "calls"):
        (out_dir / "model_calls.json").write_text(
            json.dumps(source.calls, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    return state


def cmd_list_cases(_args) -> int:
    cases = load_cases()
    for case in cases.values():
        covers = "、".join(case.get("covers", []))
        print(f"{case['id']:<16} {case['title']}")
        print(f"{'':<16} 覆盖验收点：{covers}")
    return 0


def cmd_run(args) -> int:
    cases = load_cases()
    if args.case:
        case = cases.get(args.case)
        if case is None:
            print(f"unknown case: {args.case}; available: {', '.join(sorted(cases))}", file=sys.stderr)
            return 2
    else:
        if not args.topic:
            print("either --case or --topic is required", file=sys.stderr)
            return 2
        case = {
            "id": args.case_id or "adhoc",
            "topic": args.topic,
            "budget_units": args.budget,
            "deadline_days": args.deadline,
            "audience": args.audience,
            "proposed_plan": args.plan,
            "fail_on": {},
        }

    out_dir = Path(args.out) if args.out else RUNS_DIR / (args.case or "adhoc")

    state = None
    if args.resume:
        state = resume_state(args.resume)
        if args.case and state.brief.topic != case["topic"]:
            print(
                f"warning: checkpoint topic {state.brief.topic!r} differs from case topic {case['topic']!r}",
                file=sys.stderr,
            )

    state = run_one(args, case, out_dir, state)

    print(f"status:  {state.status.value}")
    print(f"plan:    {state.saved_plan_id or '—'}（{state.plan_cost if state.plan_cost is not None else '—'} 单位）")
    print(f"cost:    合计 {state.total_cost:g} / 预算 {state.brief.budget_units if state.brief.budget_units is not None else '未提供'}")
    print(f"spend:   工具 {state.tool_spend:g} 单位")
    print(f"turns:   {state.turns} 轮 / {state.tool_calls} 次工具调用")
    if state.saved_path:
        print(f"saved:   {state.saved_path}")
    if state.incomplete_reason:
        print(f"reason:  {state.incomplete_reason}")
    print(f"trace:   {out_dir / 'trace.md'}")
    return 0 if state.status.value == "completed" else 1


def cmd_batch(args) -> int:
    cases = load_cases()
    ids = args.cases or list(cases)
    rc = 0
    print(f"{'case':<18}{'summary'}")
    print("-" * 100)
    for case_id in ids:
        case = cases.get(case_id)
        if case is None:
            print(f"unknown case: {case_id}", file=sys.stderr)
            rc = 2
            continue
        out_dir = RUNS_DIR / case_id
        state = run_one(args, case, out_dir)
        print(f"{case_id:<18}{render_summary(state)}")
    print("-" * 100)
    print(f"runs written under {RUNS_DIR}")
    return rc


def cmd_compare(args) -> int:
    """Run the same case twice: mock (offline) and the real model."""
    cases = load_cases()
    case = cases.get(args.case)
    if case is None:
        print(f"unknown case: {args.case}", file=sys.stderr)
        return 2
    for policy in ("mock", "model"):
        args.policy = policy
        out_dir = RUNS_DIR / f"{args.case}-{policy}"
        try:
            state = run_one(args, case, out_dir)
        except RuntimeError as exc:
            print(f"[{policy}] 失败：{exc}", file=sys.stderr)
            return 1
        print(f"[{policy}] {render_summary(state)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="content_planner", description="内容任务规划 Agent")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--policy", choices=["mock", "model"], default="mock")
        p.add_argument("--max-turns", type=int, default=14)
        p.add_argument("--max-tool-calls", type=int, default=12)
        p.add_argument("--max-invalid-actions", type=int, default=3)

    run = sub.add_parser("run", help="跑一个场景")
    add_common(run)
    run.add_argument("--case", help="examples/cases.json 中的场景 id")
    run.add_argument("--case-id", help="--topic 模式下写入 trace 的场景名")
    run.add_argument("--topic")
    run.add_argument("--budget", type=float)
    run.add_argument("--deadline", type=int)
    run.add_argument("--audience", default="")
    run.add_argument(
        "--plan",
        help="运营带来的初始方案 id（plan-standard / plan-lean / plan-heavy），先估这个",
    )
    run.add_argument("--out", help="输出目录（默认 examples/runs/<case>）")
    run.add_argument("--resume", help="从 checkpoint.json 续跑")
    run.add_argument(
        "--clear-failures",
        action="store_true",
        help="续跑时不注入工具失败（用于演示「失败步骤续跑」）",
    )

    batch = sub.add_parser("batch", help="批量跑所有场景并写 trace")
    add_common(batch)
    batch.add_argument("--cases", nargs="*", help="只跑这些场景")

    compare = sub.add_parser("compare", help="同一场景跑 mock 与真实模型各一次")
    add_common(compare)
    compare.add_argument("--case", default="normal")

    sub.add_parser("list-cases", help="列出场景")

    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            return cmd_run(args)
        if args.command == "batch":
            return cmd_batch(args)
        if args.command == "compare":
            return cmd_compare(args)
        if args.command == "list-cases":
            return cmd_list_cases(args)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
