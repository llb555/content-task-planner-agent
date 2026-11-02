"""Decision sources.

Two implementations of the same `decide(ctx) -> Decision` contract:

`MockPolicy`
    Deterministic, offline, zero-dependency. It is *reactive*: every branch is
    selected from the observation that just came back (an over-budget estimate,
    a failed tool, a rejected plan), not from a fixed script. It exists so the
    tests and the offline demo are reproducible, and so a reviewer can read the
    decision logic without a model.

`ModelPolicy`
    A real model behind an OpenAI-compatible `/chat/completions` endpoint. The
    prompt carries the brief, the task plan, the remaining budget and the last
    observation; the reply must be one JSON action. Everything that keeps the
    run honest — budget arithmetic, task dependencies, retry and turn caps — is
    outside this class, so the model cannot bypass it.

Both are driven by the same loop in `loop.py`.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from .loop import Context
from .models import Decision, Observation, Task, TaskPlan, TaskStatus
from .tools import load_catalog


# ---------------------------------------------------------------------------
# shared plan template
# ---------------------------------------------------------------------------

STANDARD_TASKS: list[dict[str, Any]] = [
    {"id": "t1_research", "name": "检索主题素材", "tool": "search_materials", "depends_on": []},
    {
        "id": "t2_estimate",
        "name": "估算首选方案成本",
        "tool": "estimate_cost",
        "depends_on": ["t1_research"],
    },
    {
        "id": "t3_draft",
        "name": "生成内容脚本",
        "tool": "draft_script",
        "depends_on": ["t2_estimate"],
    },
    {
        "id": "t4_save",
        "name": "保存最终方案",
        "tool": "save_plan",
        "depends_on": ["t3_draft"],
    },
]


def _base_tasks() -> list[dict[str, Any]]:
    return [dict(t) for t in STANDARD_TASKS]


def _snapshot_tasks(plan: TaskPlan | None) -> list[dict[str, Any]]:
    """Serialise the current plan back into task dicts the loop can re-parse."""
    if plan is None:
        return _base_tasks()
    out = []
    for task in plan.tasks:
        raw = task.to_dict()
        raw.pop("cost_units", None)
        raw.pop("attempts", None)
        out.append(raw)
    return out


# ---------------------------------------------------------------------------
# offline mock
# ---------------------------------------------------------------------------


@dataclass
class MockPolicy:
    """Deterministic policy whose every branch reads the last observation.

    Branch table (evaluated in order; each needs a specific observation):

    1. no plan yet                       -> plan_tasks
    2. estimate over budget/deadline     -> revise the plan onto a cheaper variant
    3. no cheaper variant fits           -> finish incomplete, name the gap
    4. estimate failed once              -> retry the same estimate
    5. estimate failed twice             -> revise onto the fallback plan
    6. draft failed once                 -> retry with a smaller scope
    7. draft failed twice                -> revise: skip the draft, keep the frame
    8. save failed once                  -> retry
    9. save failed twice                 -> finish incomplete, name the error
    10. plan exhausted                   -> finish completed

    Revisions never reuse an id for a task that must run again: the loop keeps
    `done` for ids that survive, so a re-run needs a fresh id (`t2b_estimate`).
    """

    name: str = "mock"
    preferred: str = "plan-standard"
    fallback: str = "plan-lean"
    pieces: int = 3
    ladder: tuple[str, ...] = ("plan-standard", "plan-lean", "plan-heavy")
    catalog: dict[str, dict[str, Any]] = field(default_factory=load_catalog)

    # -- entry point ------------------------------------------------------

    def decide(self, ctx: Context) -> Decision:
        state = ctx.state
        if state.plan is None:
            return Decision(
                "plan_tasks",
                {"tasks": _base_tasks(), "reason": self._bootstrap_reason(ctx)},
                "还没有任务计划，先建立任务链",
            )

        if ctx.last is not None:
            reaction = self._react(ctx, ctx.last)
            if reaction is not None:
                return reaction

        task = self._pick(state.plan)
        if task is None:
            return Decision(
                "finish",
                {"status": "completed", "reason": "所有任务完成且方案已保存"},
                "任务链已走完",
            )
        if task.status is TaskStatus.FAILED:
            task.status = TaskStatus.PENDING
        return self._act_for(ctx, task)

    # -- plan helpers -----------------------------------------------------

    def _bootstrap_reason(self, ctx: Context) -> str:
        gaps = ctx.brief.missing
        reason = "建立基础任务链：检索 → 估算 → 生成 → 保存"
        if gaps:
            reason += f"；输入缺 {'/'.join(gaps)}，先按首选方案 {self.preferred} 执行并在结果中标注假设"
        return reason

    @staticmethod
    def _pick(plan: TaskPlan) -> Task | None:
        ready = plan.ready()
        if ready:
            return ready[0]
        done = {t.id for t in plan.tasks if t.status is TaskStatus.DONE}
        for task in plan.tasks:
            if task.status is TaskStatus.FAILED and all(d in done for d in task.depends_on):
                return task
        return None

    @staticmethod
    def _task_for(plan: TaskPlan | None, tool: str) -> Task | None:
        """The most recent non-skipped task bound to `tool` (the current one)."""
        if plan is None:
            return None
        for task in reversed(plan.tasks):
            if task.tool == tool and task.status is not TaskStatus.SKIPPED:
                return task
        return None

    def _clean_estimate(self, ctx: Context) -> str | None:
        """The plan id of the most recent estimate that broke no constraint."""
        for entry in reversed(ctx.state.trace):
            obs = entry.observation
            if obs.action == "estimate_cost" and obs.ok and not obs.payload.get("violations"):
                return str(obs.payload.get("plan_id"))
        return None

    def _target_plan(self, ctx: Context) -> str | None:
        """The plan to price next.

        The *first* estimate is deliberately the plan the operator brought
        (`brief.proposed_plan`, else the preferred one) even when the brief looks
        tight: the agent must observe the real number before it can re-plan.
        Jumping straight to a cheap variant would hide the decision this project
        is about.
        """
        clean = self._clean_estimate(ctx)
        if clean:
            return clean
        rejected = set(ctx.state.rejected_plans)
        first = ctx.brief.proposed_plan or self.preferred
        if first not in rejected:
            return first
        for plan_id in self.ladder:
            if plan_id in rejected:
                continue
            if self._fits(plan_id, ctx.brief.budget_units, ctx.brief.deadline_days):
                return plan_id
        return None

    def _fits(self, plan_id: str, budget: float | None, deadline: int | None) -> bool:
        variant = self.catalog.get(plan_id)
        if variant is None:
            return False
        if budget is not None and float(variant["base_cost_units"]) > budget:
            return False
        if deadline is not None and int(variant["duration_days"]) > deadline:
            return False
        return True

    def _cheaper_than(self, plan_id: str, budget: float | None, deadline: int | None) -> str | None:
        order = list(self.ladder)
        if plan_id in order:
            order = order[order.index(plan_id) + 1 :]
        for candidate in order:
            if self._fits(candidate, budget, deadline):
                return candidate
        return None

    # -- reactions --------------------------------------------------------

    def _react(self, ctx: Context, last: Observation) -> Decision | None:
        state = ctx.state
        budget = ctx.brief.budget_units
        deadline = ctx.brief.deadline_days
        plan = state.plan

        # 1-3. an estimate broke a constraint
        if last.action == "estimate_cost" and last.ok and last.payload.get("violations"):
            current = str(last.payload.get("plan_id"))
            cheaper = self._cheaper_than(current, budget, deadline)
            if cheaper is None:
                tried = ", ".join(state.rejected_plans) or current
                return Decision(
                    "finish",
                    {
                        "status": "incomplete",
                        "reason": (
                            f"没有可行方案：{last.summary}。已否决 {tried}；"
                            f"需要追加预算（当前 {budget if budget is not None else '未提供'}）"
                            f"或放宽截止（当前 {deadline if deadline is not None else '未提供'} 天）"
                        ),
                    },
                    "所有候选方案都超约束，明确返回未完成而不是硬保存",
                )
            return Decision(
                "plan_tasks",
                {
                    "tasks": self._revise_for_plan(plan, cheaper, current),
                    "reason": f"{current} 超出约束（{last.summary}），改用更省的 {cheaper}",
                },
                "观测到预算/截止超限，重规划到更省的方案",
            )

        # 4-5. the estimate tool itself failed
        if last.action == "estimate_cost" and not last.ok:
            task = self._task_for(plan, "estimate_cost")
            if task is not None and task.attempts >= 2:
                return Decision(
                    "plan_tasks",
                    {
                        "tasks": self._revise_for_plan(plan, self.fallback, "估算工具失败"),
                        "reason": "成本估算连续失败，改用保守方案并重新估算",
                    },
                    "估算工具连续失败，降级方案后重试",
                )
            return None

        # 6-7. the draft tool failed
        if last.action == "draft_script" and not last.ok:
            task = self._task_for(plan, "draft_script")
            if task is not None and task.attempts >= 2:
                estimate = self._task_for(plan, "estimate_cost")
                tasks = _snapshot_tasks(plan)
                for raw in tasks:
                    if raw["id"] == "t3_draft":
                        raw["name"] = "脚本生成（两次失败，跳过）"
                        raw["status"] = TaskStatus.SKIPPED.value
                        raw["detail"] = last.error or "draft_script failed twice"
                    elif raw["id"] == "t4_save":
                        raw["name"] = "保存方案框架（不含脚本）"
                        raw["depends_on"] = [estimate.id] if estimate else ["t1_research"]
                return Decision(
                    "plan_tasks",
                    {
                        "tasks": tasks,
                        "reason": "脚本生成连续失败，保留已完成的检索与估算，只保存方案框架",
                    },
                    "两次失败后不再硬重试，改计划并保留已完成步骤",
                )
            return None

        # 8-9. saving failed
        if last.action == "save_plan" and not last.ok:
            task = self._task_for(plan, "save_plan")
            if task is not None and task.attempts >= 2:
                return Decision(
                    "finish",
                    {"status": "incomplete", "reason": f"方案保存连续失败：{last.error}"},
                    "保存失败且已重试，明确返回未完成",
                )
            return None

        return None

    def _revise_for_plan(self, plan: TaskPlan | None, new_plan: str, why: str) -> list[dict[str, Any]]:
        """Rebuild the chain around `new_plan`, keeping what already happened.

        The task that triggered the revision stays in the plan as `blocked` with
        the reason attached — dropping it would erase the evidence that the
        agent tried it. The re-run gets a fresh id, because the loop keeps the
        `done` status of surviving ids and would otherwise skip it.
        """
        tasks: list[dict[str, Any]] = []
        old_ids: set[str] = set()
        if plan is not None:
            for task in plan.tasks:
                raw = task.to_dict()
                raw.pop("cost_units", None)
                raw.pop("attempts", None)
                if task.tool == "estimate_cost" and task.status is not TaskStatus.DONE:
                    raw["status"] = TaskStatus.BLOCKED.value
                    raw["detail"] = why
                tasks.append(raw)
                old_ids.add(task.id)

        estimate = self._task_for(plan, "estimate_cost")
        base = estimate.id if estimate else "t2_estimate"
        prefix = base.split("_")[0]
        new_estimate_id = f"{prefix}b_estimate"
        counter = ord("b")
        while new_estimate_id in old_ids:
            counter += 1
            new_estimate_id = f"{prefix}{chr(counter)}_estimate"

        tasks.append(
            {
                "id": new_estimate_id,
                "name": f"估算替代方案 {new_plan}",
                "tool": "estimate_cost",
                "depends_on": ["t1_research"],
                "status": TaskStatus.PENDING.value,
            }
        )
        for raw in tasks:
            if raw["id"] == "t3_draft":
                raw["name"] = f"按 {new_plan} 生成脚本"
                raw["depends_on"] = [new_estimate_id]
                if raw.get("status") == TaskStatus.SKIPPED.value:
                    raw["status"] = TaskStatus.PENDING.value
            elif raw["id"] == "t4_save":
                raw["name"] = f"保存 {new_plan} 方案"
                raw["depends_on"] = ["t3_draft"]
        return tasks

    # -- acting -----------------------------------------------------------

    def _act_for(self, ctx: Context, task: Task) -> Decision:
        budget = ctx.brief.budget_units
        deadline = ctx.brief.deadline_days

        if task.tool == "search_materials":
            return Decision(
                "search_materials",
                {"task_id": task.id, "query": ctx.brief.topic},
                "先看有哪些可复用素材",
            )

        if task.tool == "estimate_cost":
            target = self._target_plan(ctx)
            if target is None:
                return Decision(
                    "finish",
                    {
                        "status": "incomplete",
                        "reason": (
                            "没有满足预算与截止时间的候选方案："
                            f"预算 {budget if budget is not None else '未提供'}，"
                            f"截止 {deadline if deadline is not None else '未提供'} 天"
                        ),
                    },
                    "没有可估算的方案，明确返回未完成",
                )
            return Decision(
                "estimate_cost",
                {"task_id": task.id, "plan_id": target},
                f"核算 {target} 是否在预算与截止时间内",
            )

        if task.tool == "draft_script":
            target = self._clean_estimate(ctx) or self.fallback
            pieces = self.pieces if task.attempts == 0 else 1
            return Decision(
                "draft_script",
                {"task_id": task.id, "plan_id": target, "pieces": pieces},
                "方案在约束内，生成脚本" if task.attempts == 0 else "上次失败，缩小到 1 篇重试",
            )

        if task.tool == "save_plan":
            target = self._clean_estimate(ctx) or self.fallback
            return Decision(
                "save_plan",
                {"task_id": task.id, "plan_id": target},
                "方案确定，保存并核对费用合计",
            )

        return Decision(
            "finish",
            {"status": "incomplete", "reason": f"任务 {task.id} 绑定了未知工具 {task.tool}"},
            "无法执行的任务",
        )


# ---------------------------------------------------------------------------
# real model
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """你是一个内容活动方案的执行 Agent。每一步只返回一个 JSON 动作。

可用动作（action）与参数：
- {"action":"plan_tasks","args":{"tasks":[{"id":"t1","name":"...","tool":"search_materials","depends_on":[]}],"reason":"..."}}
  建立或修订任务计划。tool 只能是 search_materials / estimate_cost / draft_script / save_plan。
  修订计划时，已完成的任务沿用原 id 会被保留；需要重跑的任务请换新 id。
- {"action":"search_materials","args":{"task_id":"t1","query":"关键词"}}
- {"action":"estimate_cost","args":{"task_id":"t2","plan_id":"plan-standard"}}
- {"action":"draft_script","args":{"task_id":"t3","plan_id":"plan-lean","pieces":3}}
- {"action":"save_plan","args":{"task_id":"t4","plan_id":"plan-lean"}}
- {"action":"finish","args":{"status":"completed|incomplete","reason":"..."}}

规则：
1. 必须先用 plan_tasks 建立计划，之后每个工具调用都要带对应的 task_id。
2. brief.proposed_plan 是运营带来的初始方案。第一次 estimate_cost 必须估算它，
   看到真实成本后再决定是否换方案；不要跳过估算直接选便宜方案。
3. 预算与截止时间是硬约束：估算结果超限时必须改选更便宜的方案，或直接返回未完成；不能硬保存。
4. 工具失败要调整：可重试一次（缩小规模），再失败就修订计划并保留已完成步骤。
5. 只有 save_plan 成功后才能 finish 为 completed。
6. 缺少预算或截止时间时仍要给出方案，并在 reason 里写明你采用的假设。
7. 只返回 JSON，不要解释，不要 Markdown 代码块。"""


@dataclass
class ModelPolicy:
    """OpenAI-compatible chat-completions adapter.

    Boundary: this class only builds a prompt and parses the reply. Budget
    arithmetic, dependency checks and every cap live in `loop.py` / `tools.py`,
    so a model that ignores the rules still cannot produce an invalid result —
    it only wastes turns, which the caps then stop.
    """

    name: str = "model"
    model: str = field(default_factory=lambda: os.environ.get("LLM_MODEL", "deepseek-chat"))
    base_url: str = field(
        default_factory=lambda: os.environ.get("LLM_BASE_URL", "https://api.deepseek.com/v1")
    )
    api_key_env: str = "LLM_API_KEY"
    temperature: float = 0.0
    timeout_s: float = 90.0
    max_parse_retries: int = 1

    calls: list[dict[str, Any]] = field(default_factory=list)
    _last_error: str | None = None

    # -- prompt -----------------------------------------------------------

    def build_messages(self, ctx: Context) -> list[dict[str, str]]:
        payload = ctx.to_prompt_dict()
        transcript = [
            {
                "turn": entry.turn,
                "action": entry.decision.action,
                "args": entry.decision.args,
                "ok": entry.observation.ok,
                "summary": entry.observation.summary,
                "error": entry.observation.error,
            }
            for entry in ctx.state.trace[-8:]
        ]
        user = (
            "当前状态（JSON）：\n"
            + json.dumps(payload, ensure_ascii=False, indent=2)
            + "\n\n最近的步骤记录：\n"
            + json.dumps(transcript, ensure_ascii=False, indent=2)
            + "\n\n返回下一个动作的 JSON。"
        )
        return [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ]

    # -- decide -----------------------------------------------------------

    def decide(self, ctx: Context) -> Decision:
        messages = self.build_messages(ctx)
        last_error: str | None = None
        for _ in range(self.max_parse_retries + 1):
            content = self._complete(messages, retry_note=last_error)
            if content is None:
                continue
            try:
                return _parse_decision(content)
            except (ValueError, json.JSONDecodeError) as exc:
                last_error = str(exc)
                messages = messages + [
                    {"role": "assistant", "content": content},
                    {
                        "role": "user",
                        "content": f"上一条回复无法解析为 JSON（{exc}）。只返回一个 JSON 对象。",
                    },
                ]
        return Decision(
            "finish",
            {"status": "incomplete", "reason": f"模型回复无法解析为动作：{last_error}"},
            "模型输出不合法",
        )

    def _complete(self, messages: list[dict[str, str]], retry_note: str | None = None) -> str | None:
        api_key = os.environ.get(self.api_key_env, "")
        if not api_key:
            self._last_error = f"{self.api_key_env} 未设置"
            raise RuntimeError(
                f"{self.api_key_env} is not set. Use --policy mock for the offline demo, "
                "or export the key before using --policy model."
            )
        url = self.base_url.rstrip("/") + "/chat/completions"
        body = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "response_format": {"type": "json_object"},
            "max_tokens": 1200,
        }
        request = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                raw = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            self._last_error = f"HTTP {exc.code}: {detail}"
            self.calls.append({"url": url, "model": self.model, "error": self._last_error})
            raise RuntimeError(f"模型接口返回 HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            self._last_error = f"网络错误: {exc}"
            self.calls.append({"url": url, "model": self.model, "error": self._last_error})
            raise RuntimeError(f"模型接口不可达: {exc}") from exc

        content = raw["choices"][0]["message"].get("content") or ""
        usage = raw.get("usage", {})
        self.calls.append(
            {
                "url": url,
                "model": self.model,
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
                "retry_note": retry_note,
            }
        )
        return content


def _parse_decision(content: str) -> Decision:
    text = content.strip()
    if text.startswith("```"):
        body = text.split("```")
        text = body[1] if len(body) > 1 else text
        if text.lstrip().startswith("json"):
            text = text.lstrip()[4:]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("reply contains no JSON object")
    data = json.loads(text[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("reply JSON is not an object")
    action = str(data.get("action", "")).strip()
    if not action:
        raise ValueError("reply has no action")
    args = data.get("args") or {}
    if not isinstance(args, dict):
        raise ValueError("args must be an object")
    return Decision(
        action=action,
        args=dict(args),
        rationale=str(data.get("rationale") or data.get("reason") or ""),
        raw=content.strip()[:800],
    )
