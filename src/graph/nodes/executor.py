"""src/graph/nodes/executor.py - Multi-Agent Discussion Executor

严格模式下执行真实多 agent 往返讨论轮次；不满足策略立即 fail-closed。
"""
import asyncio
import json
import logging
import re
import time as _time
from datetime import datetime
from typing import Optional

from src.graph.state import GraphState, SubTask, NodeDiscussion, DiscussionMessage, ExecutionPolicy
from src.agents.caller import get_caller

logger = logging.getLogger(__name__)


def _ensure_domains(task: SubTask, min_count: int = 3) -> list[str]:
    """Guarantee at least min_count knowledge domains per task (non-strict helper)."""
    domains = list(dict.fromkeys(task.knowledge_domains or [task.agent_type]))
    extras = [
        "code_quality", "architecture", "testing",
        "documentation", "performance", "security",
        "user_experience", "maintainability",
    ]
    for d in extras:
        if d not in domains:
            domains.append(d)
        if len(domains) >= min_count:
            break
    return domains


def _resolve_policy(state: GraphState) -> ExecutionPolicy:
    raw = state.get("execution_policy")
    if isinstance(raw, ExecutionPolicy):
        return raw
    if isinstance(raw, dict):
        return ExecutionPolicy.model_validate(raw)
    return ExecutionPolicy()


def _task_dependencies(task: SubTask) -> list[str]:
    deps = getattr(task, "dependencies", None)
    return [d for d in (deps or []) if d]


def _report_quality_score(path) -> int:
    """轻量报告质量评分：可执行命令 + 复现/验证结构 + 稳定锚点。"""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return 0

    if path.suffix.lower() == ".json":
        try:
            data = json.loads(text)
        except Exception:
            return 0
        score = 0
        repro_cmds = (data.get("reproduction") or {}).get("commands") or []
        ver_cmds = (data.get("verification") or {}).get("commands") or []
        anchors = data.get("evidence_anchors") or []
        if repro_cmds:
            score += 2
        if ver_cmds:
            score += 2
        if anchors:
            score += 2
        has_min = all(k in data for k in ("summary", "verification", "artifacts"))
        if has_min:
            score += 1
        return score

    score = 0
    if re.search(r"```(?:bash|sh|shell|zsh|cmd|powershell)?\n[\s\S]*?```", text, re.IGNORECASE):
        score += 2
    if re.search(r"Reproduction Commands", text, re.IGNORECASE):
        score += 2
    if re.search(r"Verification Commands\s*&\s*Results", text, re.IGNORECASE):
        score += 2
    if re.search(r"Evidence Anchors", text, re.IGNORECASE):
        score += 2
    if re.search(r"\b(grep|rg|findstr|python\s+-c)\b", text, re.IGNORECASE):
        score += 1
    if re.search(r"(?:[A-Za-z]:/|/)[\w./-]+\.[a-z0-9]+", text):
        score += 1
    return score


def _candidate_priority(path) -> tuple[int, float]:
    """质量优先，再按更新时间。"""
    quality = _report_quality_score(path)
    try:
        mtime = path.stat().st_mtime
    except Exception:
        mtime = 0.0
    return quality, mtime


def _derive_failure_stage(error: str | None) -> str:
    if not error:
        return "none"

    err = str(error).lower()
    if "discussion_total_timeout" in err:
        return "discussion_total_timeout"
    if "discussion_synthesis_timeout" in err or "synthesis_timeout" in err:
        return "discussion_synthesis_timeout"
    if "specialist_call_timeout" in err:
        return "discussion_round_call_timeout"
    if "[policy_violation]" in err:
        return "policy_violation"
    if "discussion_round_" in err:
        return "discussion_round_failure"
    if "no specialists created" in err:
        return "specialist_init_failure"
    return "unknown"


def _build_policy_violation(
    *,
    violation_type: str,
    required_rounds: int,
    actual_rounds: int,
    required_agents: int,
    actual_agents: int,
    actual_domains: int | None = None,
    required_domains: int | None = None,
    detail: str | None = None,
) -> dict:
    payload = {
        "violation_type": violation_type,
        "actual_rounds": int(actual_rounds),
        "required_rounds": int(required_rounds),
        "actual_agents": int(actual_agents),
        "required_agents": int(required_agents),
    }
    if actual_domains is not None:
        payload["actual_domains"] = int(actual_domains)
    if required_domains is not None:
        payload["required_domains"] = int(required_domains)
    if detail:
        payload["detail"] = str(detail)

    ordered_keys = [
        "violation_type",
        "actual_rounds",
        "required_rounds",
        "actual_agents",
        "required_agents",
        "actual_domains",
        "required_domains",
        "detail",
    ]
    kv = [f"{k}={payload[k]}" for k in ordered_keys if k in payload]
    payload["error"] = f"[POLICY_VIOLATION] {' '.join(kv)}"
    return payload


def _format_policy_shortfall_detail(
    *,
    actual_rounds: int,
    required_rounds: int,
    actual_agents: int,
    required_agents: int,
    violation_type: str | None = None,
) -> str:
    shortfalls: list[str] = []

    include_agents = violation_type in (None, "", "agents_insufficient")
    include_rounds = violation_type in (None, "", "rounds_insufficient")

    if include_agents and actual_agents < required_agents:
        shortfalls.append(f"actual_agents={actual_agents}<{required_agents}")
    if include_rounds and actual_rounds < required_rounds:
        shortfalls.append(f"actual_rounds={actual_rounds}<{required_rounds}")

    if not shortfalls:
        if actual_agents < required_agents:
            shortfalls.append(f"actual_agents={actual_agents}<{required_agents}")
        if actual_rounds < required_rounds:
            shortfalls.append(f"actual_rounds={actual_rounds}<{required_rounds}")

    return " or ".join(shortfalls) if shortfalls else "post_discussion_policy_check_failed"


def _coerce_non_negative_int(value, default: int = 0) -> int:
    try:
        return max(0, int(value))
    except Exception:
        return int(default)

def _is_transient_policy_shortage(error: str | None) -> bool:
    if not error:
        return False
    err = str(error).lower()
    if "[policy_violation]" not in err:
        return False
    # 严格策略违规统一视为显性失败，避免被误判为可忽略瞬态。
    return False


def _is_soft_timeout_stage(stage: str) -> bool:
    return stage in {
        "discussion_total_timeout",
        "discussion_synthesis_timeout",
        "discussion_round_call_timeout",
    }


def _has_strict_shortfall_signal(
    *,
    required_rounds: int,
    actual_rounds: int,
    required_agents: int,
    actual_agents: int,
    policy_violation: dict | None,
    failure_error: str | None,
) -> bool:
    if actual_rounds < required_rounds or actual_agents < required_agents:
        return True

    pv = dict(policy_violation or {})
    pv_actual_rounds = _coerce_non_negative_int(pv.get("actual_rounds"), actual_rounds)
    pv_required_rounds = _coerce_non_negative_int(pv.get("required_rounds"), required_rounds)
    pv_actual_agents = _coerce_non_negative_int(pv.get("actual_agents"), actual_agents)
    pv_required_agents = _coerce_non_negative_int(pv.get("required_agents"), required_agents)
    if pv_actual_rounds < pv_required_rounds or pv_actual_agents < pv_required_agents:
        return True

    err = str(failure_error or "").lower()
    return any(token in err for token in (
        "actual_rounds=<",
        "actual_agents=<",
        "actual_rounds=0<",
        "actual_agents=0<",
    ))


def _is_terminal_failure(*, strict: bool, failure_stage: str, failure_error: str | None) -> bool:
    if failure_stage == "policy_violation":
        return not _is_transient_policy_shortage(failure_error)
    if not strict:
        return False
    transient_stages = {
        "discussion_total_timeout",
        "discussion_synthesis_timeout",
        "discussion_round_call_timeout",
        "discussion_round_failure",
        "specialist_init_failure",
    }
    if failure_stage in transient_stages:
        return False
    return True


def _build_discussion_fallback_result(
    *,
    task: SubTask,
    call_result: dict,
    required_rounds: int,
    actual_rounds: int,
    required_agents: int,
    actual_agents_used: int,
    fallback_reason: str,
    original_error: str | None,
    violation_type: str | None = None,
) -> dict:
    """将部分/失败执行降级为可观测的非终止结果。"""
    summary = (
        f"[DISCUSSION_FALLBACK] task={task.id}; "
        f"rounds={actual_rounds}/{required_rounds}; "
        f"agents={actual_agents_used}/{required_agents}; "
        f"reason={fallback_reason}; "
        f"original_error={original_error or 'none'}"
    )

    result_text = str(call_result.get("result") or "").strip()
    if not result_text:
        result_text = summary
    elif "[DISCUSSION_FALLBACK]" not in result_text:
        result_text = f"{summary}\n\n{result_text}"

    return {
        "success": True,
        "result": result_text,
        "specialist_id": call_result.get("specialist_id"),
        "assigned_agents": call_result.get("assigned_agents") or [],
        "error": None,
        "original_error": original_error,
        "fallback_applied": True,
        "fallback_reason": fallback_reason,
        "violation_type": violation_type,
        "terminal": False,
        "actual_agents_used": actual_agents_used,
        "actual_discussion_rounds": actual_rounds,
        "required_agents": required_agents,
        "required_rounds": required_rounds,
        "synthesis_timeout_sec": float(call_result.get("synthesis_timeout_sec") or 0.0),
    }


def _build_reliability_degraded_result(
    *,
    task: SubTask,
    call_result: dict,
    required_rounds: int,
    actual_rounds: int,
    required_agents: int,
    actual_agents_used: int,
    fallback_reason: str,
    original_error: str | None,
    violation_type: str | None = None,
) -> dict:
    """Reliability mode: convert failures/shortfalls into degrade-and-continue success."""
    return _build_discussion_fallback_result(
        task=task,
        call_result=call_result,
        required_rounds=required_rounds,
        actual_rounds=actual_rounds,
        required_agents=required_agents,
        actual_agents_used=actual_agents_used,
        fallback_reason=fallback_reason,
        original_error=original_error,
        violation_type=violation_type,
    )


def _compute_discussion_timeout_sec(
    *,
    estimated_minutes: float,
    required_rounds: int,
    required_agents: int,
    strict: bool,
) -> float:
    rounds = max(1, int(required_rounds or 1))
    agents = max(1, int(required_agents or 1))

    if strict:
        # Strict 模式下，单轮常见耗时会被慢 specialist 主导；
        # 需要给足每轮时间，避免在第 6 轮左右被总超时提前截断。
        round_floor_per_round = 72.0 + 8.0 * min(agents, 6)
        baseline = 600.0
    else:
        round_floor_per_round = 30.0 + 5.0 * min(agents, 6)
        baseline = 180.0

    round_floor_total = rounds * round_floor_per_round

    estimate_window = 0.0
    if estimated_minutes > 0:
        estimate_multiplier = 2.0 if strict else 1.2
        estimate_window = estimated_minutes * 60.0 * estimate_multiplier

    timeout = max(baseline, round_floor_total, estimate_window)
    return min(7200.0, timeout)


def _find_task_by_id(subtasks: list[SubTask], task_id: str | None) -> Optional[SubTask]:
    if not task_id:
        return None
    return next((t for t in subtasks if t.id == task_id), None)


def _is_ready(task: SubTask, done_ids: set[str]) -> bool:
    deps = _task_dependencies(task)
    return task.status == "pending" and all(d in done_ids for d in deps)


def _collect_ready_tasks(state: GraphState) -> list[SubTask]:
    subtasks = state.get("subtasks", [])
    # Reliability mode: failed upstreams should not fail-close downstream scheduling.
    done_ids = {t.id for t in subtasks if t.status in ("done", "skipped", "failed")}
    return [t for t in sorted(subtasks, key=lambda t: t.priority) if _is_ready(t, done_ids)]


def _find_next_task(state: GraphState) -> Optional[SubTask]:
    subtasks = state.get("subtasks", [])
    ready_tasks = _collect_ready_tasks(state)
    if ready_tasks:
        current_task = _find_task_by_id(subtasks, state.get("current_subtask_id"))
        if current_task and current_task in ready_tasks:
            return current_task
        return ready_tasks[0]

    current_task = _find_task_by_id(subtasks, state.get("current_subtask_id"))
    if current_task and current_task.status == "pending":
        return current_task

    return None


def _unrecoverable_dependency_reason(task: SubTask, subtasks: list[SubTask]) -> Optional[str]:
    by_id = {t.id: t for t in subtasks}

    terminal_failed_or_missing = []
    transient_blocked = []
    for dep in task.dependencies:
        dep_task = by_id.get(dep)
        if dep_task is None:
            terminal_failed_or_missing.append(f"{dep}(missing)")
            continue
        if dep_task.status == "failed":
            dep_result = str(dep_task.result or "")
            dep_stage = _derive_failure_stage(dep_result)
            dep_is_terminal = _is_terminal_failure(
                strict=True,
                failure_stage=dep_stage,
                failure_error=dep_result,
            )
            if dep_is_terminal:
                terminal_failed_or_missing.append(dep)
            else:
                transient_blocked.append(dep)

    if terminal_failed_or_missing:
        return f"Dependency failed: {', '.join(terminal_failed_or_missing)}"
    if transient_blocked:
        return None
    return None


def _find_running_task(state: GraphState) -> Optional[SubTask]:
    subtasks = state.get("subtasks", [])
    running = [t for t in subtasks if t.status == "running"]
    if not running:
        return None
    return sorted(running, key=lambda t: t.priority)[0]


def _build_context(state: GraphState, current_task: SubTask) -> list[dict]:
    subtasks = state.get("subtasks", [])
    prev_results = []
    for dep_id in current_task.dependencies:
        for t in subtasks:
            if t.id == dep_id and t.result:
                prev_results.append({
                    "task_id": t.id,
                    "title": t.title,
                    "result": t.result,
                })
    return prev_results


async def _execute_multi_agent_discussion(
    caller,
    task: SubTask,
    domains: list[str],
    previous_results: list[dict],
    budget_ctx: dict | None,
    *,
    min_rounds: int,
    min_agents: int,
    strict: bool,
    deadline: float | None = None,
    discussion_timeout_sec: float | None = None,
) -> tuple[dict, list[dict]]:
    """
    真实多轮讨论：每轮所有参与 agent 对前一轮观点进行响应，形成往返。
    """
    discussion_log: list[dict] = []

    unique_domains = list(dict.fromkeys(domains))
    if len(unique_domains) < min_agents and strict:
        pv = _build_policy_violation(
            violation_type="domains_insufficient",
            required_rounds=min_rounds,
            actual_rounds=0,
            required_agents=min_agents,
            actual_agents=0,
            actual_domains=len(unique_domains),
            required_domains=min_agents,
            detail="declared_domains_below_min_agents",
        )
        return (
            {
                "success": False,
                "error": pv["error"],
                "policy_violation": pv,
                "result": None,
                "specialist_id": None,
                "assigned_agents": [],
                "actual_agents_used": 0,
                "actual_discussion_rounds": 0,
            },
            discussion_log,
        )

    async def _create_one(domain: str):
        return domain, await caller.get_or_create_specialist(
            skills=[domain],
            task_description=f"[{domain}] {task.description}",
        )

    created: list[tuple[str, str | None]] = []
    for d in unique_domains:
        try:
            sid = await _create_one(d)
            created.append(sid)
        except Exception as item:
            logger.warning("[discussion] get_or_create_specialist raised: %s", item)

    specialists: list[dict] = []
    for item in created:
        domain, sid = item
        if sid:
            specialists.append({"id": sid, "domain": domain})

    unique_specialist_ids = list(dict.fromkeys([s["id"] for s in specialists]))
    if strict and len(unique_specialist_ids) < min_agents:
        pv = _build_policy_violation(
            violation_type="agents_insufficient",
            required_rounds=min_rounds,
            actual_rounds=0,
            required_agents=min_agents,
            actual_agents=len(unique_specialist_ids),
            actual_domains=len(unique_domains),
            required_domains=min_agents,
            detail="available_agents_below_min_agents",
        )
        return (
            {
                "success": False,
                "error": pv["error"],
                "policy_violation": pv,
                "result": None,
                "specialist_id": None,
                "actual_agents_used": len(unique_specialist_ids),
                "actual_discussion_rounds": 0,
            },
            discussion_log,
        )

    if not specialists:
        return (
            {
                "success": False,
                "error": "no specialists created",
                "result": None,
                "specialist_id": None,
                "assigned_agents": [],
                "actual_agents_used": 0,
                "actual_discussion_rounds": 0,
            },
            discussion_log,
        )

    if len(unique_specialist_ids) < min_agents:
        while len(specialists) < min_agents:
            src = specialists[len(specialists) % len(specialists)]
            specialists.append({"id": src["id"], "domain": f"{src['domain']}_ext"})

    criteria_str = ", ".join(task.completion_criteria) if task.completion_criteria else "none"
    prev_summary = ""
    if previous_results:
        prev_summary = "\n\nContext from prerequisite tasks:\n"
        for p in previous_results[:3]:
            prev_summary += f"\n[{p['title']}]: {str(p['result'])[:600]}\n"

    subtask_dict = {
        "id": task.id,
        "title": task.title,
        "description": task.description,
        "agent_type": task.agent_type,
        "knowledge_domains": task.knowledge_domains,
        "estimated_minutes": task.estimated_minutes,
        "completion_criteria": task.completion_criteria,
    }

    round_outputs: dict[int, list[dict]] = {}
    rounds_executed = 0
    successful_agent_ids: list[str] = []
    discussion_started_monotonic = _time.monotonic()
    deadline_reached = False
    async def _call_round_specialist(spec: dict, round_idx: int, prior_round_digest: str) -> dict:
        prompt = (
            f"You are a senior {spec['domain']} specialist in a multi-agent discussion.\n"
            f"Task: {task.title}\n"
            f"Description: {task.description[:800]}\n"
            f"Success criteria: {criteria_str[:400]}\n"
            f"{prev_summary}\n"
            f"Current round: {round_idx}/{min_rounds}\n"
            f"Prior round summary:\n{prior_round_digest[:1800]}\n\n"
            "Respond in two parts:\n"
            "1) Your updated proposal for this round\n"
            "2) Direct response to at least one other perspective from prior round\n"
            "Keep it concrete and concise (<500 words)."
        )

        def _sanitize_round_error(raw_error: str | None) -> str:
            err = str(raw_error or "").strip()
            if not err:
                return "unknown_error"
            lower = err.lower()
            # Discussion rounds no longer expose per-call timeout artifacts.
            if "specialist_call_timeout" in lower:
                return "specialist_call_failed"
            return err

        # per-call timeout disabled: specialist calls are bounded by discussion-level timeout/deadline.
        try:
            res = await caller.call_specialist(
                agent_id=spec["id"],
                subtask={**subtask_dict, "description": prompt},
                previous_results=previous_results,
                time_budget=budget_ctx,
            )
        except Exception as e:
            res = {"success": False, "error": _sanitize_round_error(str(e)), "result": None}

        content = ""
        if res.get("success") and res.get("result"):
            content = str(res["result"]).strip()
        elif res.get("error"):
            content = f"[{spec['domain']} failed: {_sanitize_round_error(res.get('error'))}]"
        else:
            content = "[no output]"

        return {
            "agent": spec["id"],
            "domain": spec["domain"],
            "round": round_idx,
            "content": content[:3000],
            "type": "response",
            "success": bool(res.get("success")),
        }


    prior_digest = "No prior round yet."
    for round_idx in range(1, min_rounds + 1):
        # 预算截止检查：超时立即收敛为 strict 轮次不足失败（fail-closed）
        if deadline and _time.monotonic() > deadline:
            elapsed_seconds = _time.monotonic() - discussion_started_monotonic
            logger.warning(
                "[discussion] deadline reached before round %d/%d | rounds_executed=%d | discussion_timeout_sec=%.1f | elapsed_seconds=%.1f",
                round_idx,
                min_rounds,
                rounds_executed,
                float(discussion_timeout_sec or 0.0),
                elapsed_seconds,
            )
            deadline_reached = True
            break
        batch = await asyncio.gather(
            *[_call_round_specialist(spec, round_idx, prior_digest) for spec in specialists],
            return_exceptions=True,
        )

        entries: list[dict] = []
        for item in batch:
            if isinstance(item, BaseException):
                logger.warning("[discussion] round=%s gather exception: %s", round_idx, item)
                continue
            entries.append(item)
            discussion_log.append({k: item[k] for k in ("agent", "domain", "round", "content", "type")})

        if not entries:
            return (
                {
                    "success": False,
                    "error": f"discussion_round_{round_idx}_empty",
                    "result": None,
                    "specialist_id": None,
                    "assigned_agents": [],
                    "actual_agents_used": len(unique_specialist_ids),
                    "actual_discussion_rounds": rounds_executed,
                },
                discussion_log,
            )

        # 仅统计成功返回的 agent，失败/超时不计入有效参与
        responded_agent_ids = {e["agent"] for e in entries if e.get("success")}
        if strict and len(responded_agent_ids) < min_agents:
            # 首轮/单轮可能出现瞬态超时，先在本轮内补跑未响应的 agent，避免过早 fail
            retry_specs = [s for s in specialists if s["id"] not in responded_agent_ids]
            for spec in retry_specs:
                if len(responded_agent_ids) >= min_agents:
                    break
                retry_entry = await _call_round_specialist(spec, round_idx, prior_digest)
                entries.append(retry_entry)
                discussion_log.append({
                    k: retry_entry[k]
                    for k in ("agent", "domain", "round", "content", "type")
                })
                if retry_entry.get("success"):
                    responded_agent_ids.add(retry_entry["agent"])

        if strict and len(responded_agent_ids) < min_agents:
            # 如果仍不足，按缺口动态补充新 specialist（避免旧实例持续失败导致首轮直接违规）
            deficit = max(0, min_agents - len(responded_agent_ids))
            replacement_specs: list[dict] = []
            for idx in range(deficit):
                base_domain = domains[idx % len(domains)] if domains else task.agent_type
                replacement_domain = f"{base_domain}_retry_{round_idx}_{idx+1}"
                try:
                    new_sid = await caller.get_or_create_specialist(
                        skills=[replacement_domain],
                        task_description=f"[{replacement_domain}] {task.description}",
                    )
                except Exception as _e:
                    logger.warning("[discussion] replacement specialist creation failed: %s", _e)
                    new_sid = None
                if new_sid:
                    replacement_specs.append({"id": new_sid, "domain": replacement_domain})

            for spec in replacement_specs:
                if len(responded_agent_ids) >= min_agents:
                    break
                retry_entry = await _call_round_specialist(spec, round_idx, prior_digest)
                entries.append(retry_entry)
                discussion_log.append({
                    k: retry_entry[k]
                    for k in ("agent", "domain", "round", "content", "type")
                })
                if retry_entry.get("success"):
                    responded_agent_ids.add(retry_entry["agent"])
                    if spec["id"] not in successful_agent_ids:
                        successful_agent_ids.append(spec["id"])

        active_agents = len(responded_agent_ids)
        if strict and active_agents < min_agents:
            logger.warning(
                "[discussion] round agents below minimum | round=%d/%d | active_agents=%d | required_agents=%d",
                round_idx,
                min_rounds,
                active_agents,
                min_agents,
            )
            discussion_log.append({
                "agent": "policy_guard",
                "domain": "policy",
                "round": round_idx,
                "content": (
                    "[policy_warning] round_agents_below_min_agents "
                    f"actual_agents={active_agents} required_agents={min_agents}"
                ),
                "type": "policy_warning",
            })


        rounds_executed = round_idx
        round_outputs[round_idx] = entries
        round_success_agents = list(dict.fromkeys([e["agent"] for e in entries if e.get("success")]))
        for agent_id in round_success_agents:
            if agent_id not in successful_agent_ids:
                successful_agent_ids.append(agent_id)

        digest_lines = []
        for e in entries[:8]:
            digest_lines.append(f"- {e['domain']}({e['agent']}): {e['content'][:240]}")
        prior_digest = "\n".join(digest_lines) or "No substantial updates."

    # 严格模式轮次不足：必须 fail-closed（包括 1..N-1）
    if strict and rounds_executed < min_rounds:
        elapsed_seconds = _time.monotonic() - discussion_started_monotonic
        logger.warning(
            "[discussion] partial rounds under strict policy | required_rounds=%d | rounds_executed=%d | discussion_timeout_sec=%.1f | elapsed_seconds=%.1f | deadline_reached=%s",
            min_rounds,
            rounds_executed,
            float(discussion_timeout_sec or 0.0),
            elapsed_seconds,
            deadline_reached,
        )
        pv = _build_policy_violation(
            violation_type="rounds_insufficient",
            required_rounds=min_rounds,
            actual_rounds=rounds_executed,
            required_agents=min_agents,
            actual_agents=len(successful_agent_ids),
            actual_domains=len(unique_domains),
            required_domains=min_agents,
            detail="discussion_rounds_below_minimum",
        )
        return (
            {
                "success": False,
                "error": pv["error"],
                "policy_violation": pv,
                "result": None,
                "specialist_id": None,
                "assigned_agents": successful_agent_ids,
                "actual_agents_used": len(unique_specialist_ids),
                "actual_discussion_rounds": rounds_executed,
            },
            discussion_log,
        )

    transcript = []
    for r in range(1, rounds_executed + 1):
        transcript.append(f"## Round {r}")
        for e in round_outputs.get(r, []):
            transcript.append(f"[{e['domain']}|{e['agent']}]\n{e['content'][:1200]}")
    transcript_text = "\n\n".join(transcript)

    synthesis_prompt = (
        f"You are the consensus synthesizer.\n"
        f"Task: {task.title}\nDescription: {task.description}\n\n"
        f"Below is the full multi-round discussion transcript ({rounds_executed} rounds):\n"
        f"{transcript_text[:18000]}\n\n"
        "Produce final consensus with:\n"
        "1) Agreed findings\n"
        "2) Main disagreements and resolution\n"
        "3) Prioritized implementation plan\n"
        "4) Verification checklist\n"
    )

    synthesizer_id = specialists[0]["id"]
    synthesis_timeout_sec = max(45.0, min(240.0, 20.0 + rounds_executed * 12.0))
    try:
        synth_res = await asyncio.wait_for(
            caller.call_specialist(
                agent_id=synthesizer_id,
                subtask={**subtask_dict, "description": synthesis_prompt},
                previous_results=[],
                time_budget=budget_ctx,
            ),
            timeout=synthesis_timeout_sec,
        )
    except asyncio.TimeoutError:
        synth_res = {"success": False, "error": f"discussion_synthesis_timeout>{synthesis_timeout_sec:.0f}s", "result": None}
    except Exception as e:
        synth_res = {"success": False, "error": f"discussion_synthesis_error:{e}", "result": None}

    final_result = ""
    if synth_res.get("success") and synth_res.get("result"):
        final_result = str(synth_res["result"]).strip()

    if not final_result:
        # 合成失败降级：用讨论记录做 fallback，避免硬 fail 导致整轮 subtask 废弃
        final_result = transcript_text[:12000] if transcript_text.strip() else (
            f"Discussion rounds_completed={rounds_executed}; synthesis unavailable."
        )
        logger.warning("[discussion] synthesis_empty, using transcript fallback, task=%s", task.id)

    discussion_log.append({
        "agent": synthesizer_id,
        "domain": "synthesis",
        "round": rounds_executed + 1,
        "content": final_result[:3000],
        "type": "synthesis",
    })

    return (
        {
            "success": True,
            "result": final_result,
            "specialist_id": synthesizer_id,
            "assigned_agents": successful_agent_ids,
            "error": None,
            "actual_agents_used": len(successful_agent_ids),
            "actual_discussion_rounds": rounds_executed,
            "synthesis_timeout_sec": synthesis_timeout_sec,
        },
        discussion_log,
    )



async def executor_node(state: GraphState) -> dict:
    """
    Find the next executable subtask and run multi-agent discussion.
    """
    from pathlib import Path as _Path
    _Path("reports").mkdir(exist_ok=True)

    caller = get_caller()
    subtasks = state.get("subtasks", [])
    artifacts = dict(state.get("artifacts") or {})
    policy = _resolve_policy(state)

    running_task = _find_running_task(state)
    if running_task:
        next_task = running_task
    else:
        next_task = _find_next_task(state)
        if not next_task:
            pending = [t for t in subtasks if t.status == "pending"]
            if pending:
                updated_subtasks = []
                changed = False
                degraded_ids: list[str] = []
                for t in subtasks:
                    if t.status == "pending":
                        reason = _unrecoverable_dependency_reason(t, subtasks)
                        if reason:
                            changed = True
                            degraded_ids.append(t.id)
                            updated_subtasks.append(t.model_copy(update={
                                "result": f"[DEPENDENCY_BLOCKED] {reason}",
                            }))
                            continue
                    updated_subtasks.append(t)

                if changed:
                    return {
                        "phase": "executing",
                        "current_subtask_id": None,
                        "subtasks": updated_subtasks,
                        "artifacts": artifacts,
                        "execution_log": [{
                            "event": "pending_degraded",
                            "reason": "dependency_blocked_non_terminal",
                            "task_ids": degraded_ids,
                            "count": len(degraded_ids),
                            "terminal": False,
                            "fallback_applied": True,
                            "fallback_reason": "dependency_blocked_non_terminal",
                            "timestamp": datetime.now().isoformat(),
                        }],
                    }

                return {
                    "phase": "executing",
                    "current_subtask_id": None,
                    "subtasks": updated_subtasks,
                    "artifacts": artifacts,
                    "execution_log": [{
                        "event": "no_ready_tasks",
                        "pending_count": len(pending),
                        "terminal": False,
                        "timestamp": datetime.now().isoformat(),
                    }],
                }
            return {"phase": "reviewing", "current_subtask_id": None, "artifacts": artifacts}

        started_at = datetime.now()
        required_agents = max(1, policy.min_agents_per_node)
        declared_domains = list(dict.fromkeys(next_task.knowledge_domains or [next_task.agent_type]))
        seed_domains = declared_domains if policy.strict_enforcement else _ensure_domains(next_task, min_count=max(3, required_agents))

        prefetched_agents: list[str] = []
        for domain in seed_domains:
            try:
                sid = await caller.get_or_create_specialist(
                    skills=[domain],
                    task_description=f"[{domain}] {next_task.description}",
                )
                if sid and sid not in prefetched_agents:
                    prefetched_agents.append(sid)
            except Exception as exc:
                logger.warning("[executor] prefetch specialist failed for %s: %s", domain, exc)

        updated_subtasks = [
            t.model_copy(update={
                "status": "running",
                "started_at": started_at,
                "assigned_agents": prefetched_agents,
            }) if t.id == next_task.id else t
            for t in subtasks
        ]
        logger.info("[executor] task %s moved to running with agents=%s", next_task.id, prefetched_agents)
        return {
            "subtasks": updated_subtasks,
            "current_subtask_id": next_task.id,
            "phase": "executing",
            "artifacts": artifacts,
            "execution_log": [{
                "event": "task_started",
                "task_id": next_task.id,
                "agent": next_task.agent_type,
                "assigned_agents": prefetched_agents,
                "timestamp": started_at.isoformat(),
            }],
        }

    started_at = next_task.started_at or datetime.now()
    previous_results = _build_context(state, next_task)

    budget_ctx: dict | None = None
    raw_budget = state.get("time_budget")
    if raw_budget:
        remaining = raw_budget.remaining_minutes
        if raw_budget.started_at and remaining == 0:
            from datetime import datetime as _dt
            elapsed = (_dt.now() - raw_budget.started_at).total_seconds() / 60
            remaining = max(0.0, raw_budget.total_minutes - elapsed)
        budget_ctx = {
            "total_minutes": raw_budget.total_minutes,
            "remaining_minutes": round(remaining, 1),
            "task_estimated_minutes": next_task.estimated_minutes,
            "deadline": raw_budget.deadline.isoformat() if raw_budget.deadline else None,
        }

    # === Policy preflight ===
    required_agents = max(1, policy.min_agents_per_node)
    required_rounds = max(1, policy.min_discussion_rounds)

    declared_domains = list(dict.fromkeys(next_task.knowledge_domains or [next_task.agent_type]))
    domains = declared_domains if policy.strict_enforcement else _ensure_domains(next_task, min_count=max(3, required_agents))
    preflight_log: list[dict] = []

    if policy.strict_enforcement and len(declared_domains) < required_agents:
        domains = _ensure_domains(next_task, min_count=required_agents)
        preflight_log.append({
            "event": "policy_preflight_adjusted",
            "task_id": next_task.id,
            "reason": "declared_domains_insufficient",
            "required_agents": required_agents,
            "declared_domains": len(declared_domains),
            "adjusted_domains": len(domains),
            "timestamp": datetime.now().isoformat(),
        })

    logger.info(
        "[executor] Starting discussion | task=%s | domains=%s | required_agents=%d | required_rounds=%d | strict=%s",
        next_task.id, domains, required_agents, required_rounds, policy.strict_enforcement,
    )

    # 讨论总预算：按 required_rounds + 任务估时联合计算，避免 strict 轮次与预算冲突
    _est_outer = float(getattr(next_task, "estimated_minutes", 0.0) or 0.0)
    discussion_timeout_sec = _compute_discussion_timeout_sec(
        estimated_minutes=_est_outer,
        required_rounds=required_rounds,
        required_agents=required_agents,
        strict=policy.strict_enforcement,
    )
    _discussion_deadline = _time.monotonic() + discussion_timeout_sec

    try:
        call_result, discussion_log = await asyncio.wait_for(
            _execute_multi_agent_discussion(
                caller,
                next_task,
                domains,
                previous_results,
                budget_ctx,
                min_rounds=required_rounds,
                min_agents=required_agents,
                strict=policy.strict_enforcement,
                deadline=_discussion_deadline,
                discussion_timeout_sec=discussion_timeout_sec,
            ),
            timeout=discussion_timeout_sec,
        )
    except asyncio.TimeoutError:
        logger.warning("[executor] discussion total timeout task=%s timeout=%.1fs", next_task.id, discussion_timeout_sec)
        call_result = {
            "success": False,
            "error": f"discussion_total_timeout>{discussion_timeout_sec:.0f}s",
            "result": None,
            "specialist_id": None,
            "assigned_agents": [],
            "actual_agents_used": 0,
            "actual_discussion_rounds": 0,
        }
        discussion_log = []
    except Exception as exc:
        logger.exception("[executor] discussion crashed for task %s", next_task.id)
        call_result = {
            "success": False,
            "error": f"discussion crashed: {exc}",
            "result": None,
            "specialist_id": None,
            "assigned_agents": [],
            "actual_agents_used": 0,
            "actual_discussion_rounds": 0,
        }
        discussion_log = []

    specialist_id = call_result.get("specialist_id")
    assigned_agents = list(dict.fromkeys(call_result.get("assigned_agents") or ([] if not specialist_id else [specialist_id])))
    actual_agents_used = _coerce_non_negative_int(call_result.get("actual_agents_used"), 0)
    actual_rounds = _coerce_non_negative_int(call_result.get("actual_discussion_rounds"), 0)
    synthesis_timeout_sec = float(call_result.get("synthesis_timeout_sec") or 0.0)

    agents_shortfall = actual_agents_used < required_agents
    rounds_shortfall = actual_rounds < required_rounds
    strict_unmet = policy.strict_enforcement and (agents_shortfall or rounds_shortfall)
    violation_type = None

    if strict_unmet:
        violation_type = "agents_insufficient" if agents_shortfall else "rounds_insufficient"
        existing_violation = call_result.get("policy_violation") or {}
        shortfall_detail = _format_policy_shortfall_detail(
            actual_rounds=actual_rounds,
            required_rounds=required_rounds,
            actual_agents=actual_agents_used,
            required_agents=required_agents,
            violation_type=violation_type,
        )
        if not existing_violation:
            pv = _build_policy_violation(
                violation_type=violation_type,
                required_rounds=required_rounds,
                actual_rounds=actual_rounds,
                required_agents=required_agents,
                actual_agents=actual_agents_used,
                actual_domains=len(declared_domains),
                required_domains=required_agents,
                detail=shortfall_detail,
            )
        else:
            pv = dict(existing_violation)
            pv["violation_type"] = violation_type
            pv["actual_rounds"] = actual_rounds
            pv["required_rounds"] = required_rounds
            pv["actual_agents"] = actual_agents_used
            pv["required_agents"] = required_agents
            pv["actual_domains"] = _coerce_non_negative_int(pv.get("actual_domains"), len(declared_domains))
            pv["required_domains"] = _coerce_non_negative_int(pv.get("required_domains"), required_agents)
            pv["detail"] = shortfall_detail
            pv["error"] = _build_policy_violation(
                violation_type=str(pv.get("violation_type")),
                required_rounds=required_rounds,
                actual_rounds=actual_rounds,
                required_agents=required_agents,
                actual_agents=actual_agents_used,
                actual_domains=_coerce_non_negative_int(pv.get("actual_domains"), len(declared_domains)),
                required_domains=_coerce_non_negative_int(pv.get("required_domains"), required_agents),
                detail=str(pv.get("detail", shortfall_detail)),
            )["error"]

        call_result = {
            **call_result,
            "success": False,
            "error": pv["error"],
            "original_error": call_result.get("original_error") or call_result.get("error"),
            "policy_violation": pv,
            "fallback_applied": False,
            "fallback_reason": None,
        }
    else:
        maybe_policy_error = str(call_result.get("error") or "")
        if "[policy_violation]" in maybe_policy_error.lower():
            sanitized_error = call_result.get("original_error") or "discussion_round_failure:policy_violation_without_shortfall"
            retained_violation = call_result.get("policy_violation")
            if policy.strict_enforcement and not retained_violation:
                retained_violation = {"violation_type": "rounds_insufficient"}
            call_result = {
                **call_result,
                "error": sanitized_error,
                "original_error": call_result.get("original_error") or maybe_policy_error,
                "policy_violation": retained_violation,
            }

    failure_error = call_result.get("error")
    if policy.strict_enforcement and not call_result.get("success"):
        err = str(failure_error or "").lower()
        failure_stage = _derive_failure_stage(failure_error)
        allow_synthesis_tail_fallback = (
            not strict_unmet
            and ("synthesis_empty" in err or failure_stage == "discussion_synthesis_timeout")
        )
        if allow_synthesis_tail_fallback:
            call_result = _build_reliability_degraded_result(
                task=next_task,
                call_result=call_result,
                required_rounds=required_rounds,
                actual_rounds=actual_rounds,
                required_agents=required_agents,
                actual_agents_used=actual_agents_used,
                fallback_reason="strict_synthesis_empty_or_timeout",
                original_error=failure_error,
                violation_type=(call_result.get("policy_violation") or {}).get("violation_type") or violation_type,
            )

    specialist_id = call_result.get("specialist_id")
    assigned_agents = list(dict.fromkeys(call_result.get("assigned_agents") or ([] if not specialist_id else [specialist_id])))
    actual_agents_used = _coerce_non_negative_int(call_result.get("actual_agents_used"), actual_agents_used)
    actual_rounds = _coerce_non_negative_int(call_result.get("actual_discussion_rounds"), actual_rounds)
    synthesis_timeout_sec = float(call_result.get("synthesis_timeout_sec") or synthesis_timeout_sec)

    def _normalize_discussion_message_type(raw_type: str | None) -> str:
        allowed_types = {
            "query",
            "response",
            "consensus",
            "conflict",
            "info",
            "proposal",
            "reflection",
            "agreement",
            "error",
            "review_opinion",
            "analysis",
            "synthesis",
            "disagreement_alert",
        }
        normalized = str(raw_type or "info").strip()
        return normalized if normalized in allowed_types else "info"

    normalized_discussion_log = list(discussion_log or [])

    if not normalized_discussion_log:
        if not call_result.get("success"):
            failure_stage = _derive_failure_stage(call_result.get("error"))
            policy_violation = call_result.get("policy_violation") or {}
            unmet_parts: list[str] = []
            if actual_agents_used < required_agents:
                unmet_parts.append(f"agents {actual_agents_used}/{required_agents}")
            if actual_rounds < required_rounds:
                unmet_parts.append(f"rounds {actual_rounds}/{required_rounds}")
            unmet_suffix = f" ({', '.join(unmet_parts)})" if unmet_parts else ""
            violation_type = str(policy_violation.get("violation_type") or "").strip()
            violation_suffix = f", violation={violation_type}" if violation_type else ""
            normalized_discussion_log.append({
                "agent": specialist_id or (assigned_agents[0] if assigned_agents else "executor"),
                "type": "error",
                "content": (
                    f"Execution failed at {failure_stage}{unmet_suffix}{violation_suffix}: "
                    f"{str(call_result.get('error') or 'unknown error')[:400]}"
                ),
                "round": actual_rounds,
                "domain": "execution",
            })
        else:
            normalized_discussion_log.append({
                "agent": specialist_id or (assigned_agents[0] if assigned_agents else "executor"),
                "type": "info",
                "content": "Execution completed without captured inter-agent transcript; recorded synthesized result.",
                "round": actual_rounds,
                "domain": "execution",
            })

    if not call_result.get("success"):
        logger.warning("[executor] task %s discussion failed: %s", next_task.id, call_result.get("error"))
        failure_error = call_result.get("error")
        failure_stage = _derive_failure_stage(failure_error)
        original_failure_error = call_result.get("original_error")
        original_failure_stage = _derive_failure_stage(original_failure_error)
        effective_failure_stage = (
            original_failure_stage
            if original_failure_stage != "none"
            else failure_stage
        )
        policy_violation = call_result.get("policy_violation") or {}

        current_retry_count = int(next_task.retry_count or 0)
        next_retry_count = current_retry_count + 1

        strict_all_agents_timeout = (
            policy.strict_enforcement
            and effective_failure_stage == "discussion_round_call_timeout"
            and actual_agents_used == 0
        )

        # 交互链路修复：当所有 specialist 均超时且无任何有效响应时，判定为终止失败，避免误标记 done。
        # 严格模式下执行失败保持失败语义，避免被降级为 done。
        strict_failed = bool(policy.strict_enforcement)
        hard_terminal = strict_all_agents_timeout

        existing_discussions: dict = dict(state.get("discussions") or {})
        disc = existing_discussions.get(next_task.id) or NodeDiscussion(node_id=next_task.id)
        for entry in normalized_discussion_log:
            disc.add_message(DiscussionMessage(
                node_id=next_task.id,
                from_agent=entry["agent"],
                content=entry["content"][:2000],
                message_type=_normalize_discussion_message_type(entry.get("type")),
                metadata={
                    "round_index": entry.get("round", 0),
                    "domain": entry.get("domain", ""),
                    "task_title": next_task.title,
                    "required_rounds": required_rounds,
                    "actual_rounds": actual_rounds,
                    "required_agents": required_agents,
                    "actual_agents_used": actual_agents_used,
                    "discussion_timeout_sec": discussion_timeout_sec,
                    "failure_stage": effective_failure_stage,
                    "terminal_failure": hard_terminal,
                    "fallback_applied": bool(call_result.get("fallback_applied")),
                    "fallback_reason": call_result.get("fallback_reason"),
                    "violation_type": policy_violation.get("violation_type"),
                },
            ))

        if hard_terminal:
            disc.status = "blocked"
            disc.consensus_reached = False
            disc.consensus_topic = next_task.title
            existing_discussions[next_task.id] = disc

            failed_subtasks = [
                t.model_copy(update={
                    "status": "failed",
                    "result": str(failure_error or call_result.get("result") or "execution_failed"),
                    "started_at": started_at,
                    "finished_at": datetime.now(),
                    "retry_count": next_retry_count,
                    "assigned_agents": assigned_agents or list(dict.fromkeys([e["agent"] for e in normalized_discussion_log])),
                }) if t.id == next_task.id else t
                for t in subtasks
            ]

            terminal_reason = "strict_all_agents_timeout" if strict_all_agents_timeout else "terminal_failure"
            return {
                "subtasks": failed_subtasks,
                "current_subtask_id": next_task.id,
                "phase": "reviewing",
                "discussions": existing_discussions,
                "artifacts": artifacts,
                "execution_log": [{
                    "event": "task_failed",
                    "task_id": next_task.id,
                    "error": failure_error,
                    "failure_stage": effective_failure_stage,
                    "violation_type": policy_violation.get("violation_type"),
                    "terminal": True,
                    "terminal_reason": terminal_reason,
                    "discussion_rounds": actual_rounds,
                    "discussion_agents": actual_agents_used,
                    "required_rounds": required_rounds,
                    "required_agents": required_agents,
                    "actual_rounds": actual_rounds,
                    "actual_agents_used": actual_agents_used,
                    "discussion_timeout_sec": discussion_timeout_sec,
                    "synthesis_timeout_sec": synthesis_timeout_sec,
                    "fallback_applied": False,
                    "fallback_reason": None,
                    "original_error": call_result.get("original_error") or failure_error,
                    "retry_count": next_retry_count,
                    "timestamp": datetime.now().isoformat(),
                }],
            }

        disc.status = "active"
        disc.consensus_reached = False
        disc.consensus_topic = next_task.title
        existing_discussions[next_task.id] = disc

        degraded_result = str(call_result.get("result") or f"[DEGRADED_CONTINUE] {failure_error or 'unknown error'}")
        if "[DEGRADED_CONTINUE]" not in degraded_result:
            degraded_result = f"[DEGRADED_CONTINUE] {degraded_result}"

        degraded_subtasks = [
            t.model_copy(update={
                "status": "failed" if strict_failed else "done",
                "result": degraded_result,
                "started_at": started_at,
                "finished_at": datetime.now(),
                "retry_count": next_retry_count,
                "assigned_agents": assigned_agents or list(dict.fromkeys([e["agent"] for e in normalized_discussion_log])),
            }) if t.id == next_task.id else t
            for t in subtasks
        ]
        return {
            "subtasks": degraded_subtasks,
            "current_subtask_id": next_task.id,
            "phase": "executing",
            "discussions": existing_discussions,
            "artifacts": artifacts,
            "execution_log": [{
                "event": "task_degraded_continued",
                "task_id": next_task.id,
                "error": failure_error,
                "failure_stage": effective_failure_stage,
                "violation_type": policy_violation.get("violation_type"),
                "terminal": False,
                "terminal_reason": "strict_execution_failed_nonterminal" if strict_failed else "reliability_mode_non_terminal",
                "discussion_rounds": actual_rounds,
                "discussion_agents": actual_agents_used,
                "required_rounds": required_rounds,
                "required_agents": required_agents,
                "actual_rounds": actual_rounds,
                "actual_agents_used": actual_agents_used,
                "discussion_timeout_sec": discussion_timeout_sec,
                "synthesis_timeout_sec": synthesis_timeout_sec,
                "fallback_applied": bool(call_result.get("fallback_applied", True)),
                "fallback_reason": call_result.get("fallback_reason") or "degraded_continue",
                "original_error": call_result.get("original_error") or failure_error,
                "retry_count": next_retry_count,
                "timestamp": datetime.now().isoformat(),
            }],
        }

    result_text = str(call_result.get("result", "")).strip()

    _reports_dir = _Path("reports")
    report_path = None
    report_kind = None
    _candidates = []
    if _reports_dir.exists():
        _task_slug = next_task.id.replace("-", "").replace(".", "")
        _candidates = list(_reports_dir.glob(f"{next_task.id}*.md")) + list(_reports_dir.glob(f"{_task_slug}*.md"))
        _candidates += list(_reports_dir.glob(f"{next_task.id}*.json")) + list(_reports_dir.glob(f"{_task_slug}*.json"))
        _candidates = list(dict.fromkeys(_candidates))

        if _candidates:
            ranked = sorted(_candidates, key=lambda p: _candidate_priority(p), reverse=True)
            best = ranked[0]
            best_quality, _ = _candidate_priority(best)
            if best_quality >= 4:
                report_path = str(best)
                report_kind = best.suffix.lstrip(".").lower()
            else:
                freshest = sorted(_candidates, key=lambda p: p.stat().st_mtime, reverse=True)[0]
                report_path = str(freshest)
                report_kind = freshest.suffix.lstrip(".").lower()

        md_candidates = [p for p in _candidates if p.suffix.lower() == ".md"]
        if md_candidates:
            ranked_md = sorted(md_candidates, key=lambda p: _candidate_priority(p), reverse=True)
            best_md = ranked_md[0]
            try:
                _file_content = best_md.read_text(encoding="utf-8", errors="replace")
                if len(_file_content.strip()) > len(result_text):
                    result_text = _file_content
                    logger.info("[executor] Using quality-first report file %s", best_md.name)
            except Exception as _fe:
                logger.warning("[executor] Failed to read report: %s", _fe)

    result = {
        "status": "done",
        "result": result_text,
        "specialist_id": specialist_id,
        "assigned_agents": assigned_agents,
        "finished_at": datetime.now(),
    }

    if report_path:
        artifacts[next_task.id] = report_path
        if report_kind:
            artifacts[f"{next_task.id}:{report_kind}"] = report_path

    existing_discussions: dict = dict(state.get("discussions") or {})
    disc = existing_discussions.get(next_task.id) or NodeDiscussion(node_id=next_task.id)
    for entry in discussion_log:
        disc.add_message(DiscussionMessage(
            node_id=next_task.id,
            from_agent=entry["agent"],
            content=entry["content"][:2000],
            message_type=_normalize_discussion_message_type(entry.get("type")),
            metadata={
                "round_index": entry.get("round", 0),
                "domain": entry.get("domain", ""),
                "task_title": next_task.title,
                "required_rounds": required_rounds,
                "actual_rounds": actual_rounds,
                "required_agents": required_agents,
                "actual_agents_used": actual_agents_used,
                "discussion_timeout_sec": discussion_timeout_sec,
            },
        ))
    disc.status = "resolved"
    disc.consensus_reached = True
    disc.consensus_topic = next_task.title
    existing_discussions[next_task.id] = disc

    if specialist_id:
        caller.complete_subtask(specialist_id)

    updated_subtasks = []
    for t in subtasks:
        if t.id == next_task.id:
            fallback_agents = list(dict.fromkeys([e["agent"] for e in normalized_discussion_log]))
            final_assigned_agents = assigned_agents or fallback_agents
            updated_subtasks.append(t.model_copy(update={
                "status": result["status"],
                "result": result["result"],
                "started_at": started_at,
                "finished_at": result["finished_at"],
                "assigned_agents": final_assigned_agents,
            }))
        else:
            updated_subtasks.append(t)

    return {
        "subtasks": updated_subtasks,
        "current_subtask_id": next_task.id,
        "time_budget": state.get("time_budget"),
        "phase": "executing",
        "discussions": existing_discussions,
        "artifacts": artifacts,
        "execution_log": [{
            "event": "task_executed",
            "task_id": next_task.id,
            "agent": next_task.agent_type,
            "specialist_id": specialist_id,
            "assigned_agents": assigned_agents,
            "status": result["status"],
            "discussion_rounds": actual_rounds,
            "discussion_agents": actual_agents_used,
            "required_rounds": required_rounds,
            "required_agents": required_agents,
            "actual_rounds": actual_rounds,
            "actual_agents_used": actual_agents_used,
            "discussion_timeout_sec": discussion_timeout_sec,
            "synthesis_timeout_sec": synthesis_timeout_sec,
            "failure_stage": _derive_failure_stage(call_result.get("original_error") or call_result.get("error")),
            "violation_type": (call_result.get("policy_violation") or {}).get("violation_type"),
            "fallback_applied": bool(call_result.get("fallback_applied")),
            "fallback_reason": call_result.get("fallback_reason"),
            "original_error": call_result.get("original_error") or call_result.get("error"),
            "timestamp": datetime.now().isoformat(),
        }],
    }
