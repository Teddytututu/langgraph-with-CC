"""src/graph/nodes/executor.py - Multi-Agent Discussion Executor

严格模式下执行真实多 agent 往返讨论轮次；不满足策略立即 fail-closed。
"""
import asyncio
import logging
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


def _find_task_by_id(subtasks: list[SubTask], task_id: str | None) -> Optional[SubTask]:
    if not task_id:
        return None
    return next((t for t in subtasks if t.id == task_id), None)


def _is_ready(task: SubTask, done_ids: set[str]) -> bool:
    deps = _task_dependencies(task)
    return task.status == "pending" and all(d in done_ids for d in deps)


def _collect_ready_tasks(state: GraphState) -> list[SubTask]:
    subtasks = state.get("subtasks", [])
    done_ids = {t.id for t in subtasks if t.status in ("done", "skipped")}
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
    status_by_id = {t.id: t.status for t in subtasks}

    failed_or_missing = []
    for dep in task.dependencies:
        dep_status = status_by_id.get(dep)
        if dep_status is None:
            failed_or_missing.append(f"{dep}(missing)")
        elif dep_status == "failed":
            failed_or_missing.append(dep)

    if failed_or_missing:
        return f"Dependency failed: {', '.join(failed_or_missing)}"
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
) -> tuple[dict, list[dict]]:
    """
    真实多轮讨论：每轮所有参与 agent 对前一轮观点进行响应，形成往返。
    """
    discussion_log: list[dict] = []

    unique_domains = list(dict.fromkeys(domains))
    if len(unique_domains) < min_agents and strict:
        return (
            {
                "success": False,
                "error": f"[POLICY_VIOLATION] domains<{min_agents}",
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
        return (
            {
                "success": False,
                "error": f"[POLICY_VIOLATION] available_agents<{min_agents}",
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

        # 防卡死：为单次 specialist 调用设置硬超时，避免首节点长时间无进展
        # 规则：优先基于任务估时分摊；若无估时则使用保守默认值
        est_minutes = float(getattr(task, "estimated_minutes", 0.0) or 0.0)
        specialist_count = max(1, len(specialists))
        round_count = max(1, int(min_rounds or 1))
        if est_minutes > 0:
            budget_seconds = est_minutes * 60.0
            per_call_timeout = budget_seconds / (round_count * specialist_count)
            per_call_timeout = max(30.0, min(180.0, per_call_timeout))
        else:
            per_call_timeout = 60.0

        try:
            res = await asyncio.wait_for(
                caller.call_specialist(
                    agent_id=spec["id"],
                    subtask={**subtask_dict, "description": prompt},
                    previous_results=previous_results,
                    time_budget=budget_ctx,
                ),
                timeout=per_call_timeout,
            )
        except asyncio.TimeoutError:
            res = {
                "success": False,
                "error": f"specialist_call_timeout>{per_call_timeout:.0f}s",
                "result": None,
            }
        except Exception as e:
            res = {"success": False, "error": str(e), "result": None}

        content = ""
        if res.get("success") and res.get("result"):
            content = str(res["result"]).strip()
        elif res.get("error"):
            content = f"[{spec['domain']} failed: {res['error']}]"
        else:
            content = "[no output]"

        return {
            "agent": spec["id"],
            "domain": spec["domain"],
            "round": round_idx,
            "content": content[:3000],
            "type": "response",
            "success": bool(res.get("success") and res.get("result")),
        }


    prior_digest = "No prior round yet."
    for round_idx in range(1, min_rounds + 1):
        batch = await asyncio.gather(
            *[_call_round_specialist(spec, round_idx, prior_digest) for spec in specialists],
            return_exceptions=True,
        )

        entries: list[dict] = []
        for item in batch:
            if isinstance(item, Exception):
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

        success_agents = len({e["agent"] for e in entries if e.get("success")})
        if strict and success_agents < min_agents:
            return (
                {
                    "success": False,
                    "error": f"[POLICY_VIOLATION] round_{round_idx}_agents<{min_agents}",
                    "result": None,
                    "specialist_id": None,
                    "assigned_agents": [],
                    "actual_agents_used": success_agents,
                    "actual_discussion_rounds": rounds_executed,
                },
                discussion_log,
            )

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

    if strict and rounds_executed < min_rounds:
        return (
            {
                "success": False,
                "error": f"[POLICY_VIOLATION] rounds<{min_rounds}",
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
    try:
        synth_res = await caller.call_specialist(
            agent_id=synthesizer_id,
            subtask={**subtask_dict, "description": synthesis_prompt},
            previous_results=[],
            time_budget=budget_ctx,
        )
    except Exception as e:
        synth_res = {"success": False, "error": str(e), "result": None}

    final_result = ""
    if synth_res.get("success") and synth_res.get("result"):
        final_result = str(synth_res["result"]).strip()

    if not final_result:
        if strict:
            return (
                {
                    "success": False,
                    "error": "[POLICY_VIOLATION] synthesis_empty",
                    "result": None,
                    "specialist_id": None,
                    "assigned_agents": [],
                    "actual_agents_used": len(unique_specialist_ids),
                    "actual_discussion_rounds": rounds_executed,
                },
                discussion_log,
            )
        final_result = transcript_text[:12000]

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
            "actual_agents_used": len(unique_specialist_ids),
            "actual_discussion_rounds": rounds_executed,
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
                for t in subtasks:
                    if t.status == "pending":
                        reason = _unrecoverable_dependency_reason(t, subtasks)
                        if reason:
                            changed = True
                            updated_subtasks.append(t.model_copy(update={
                                "status": "failed",
                                "result": reason,
                                "finished_at": datetime.now(),
                            }))
                            continue
                    updated_subtasks.append(t)

                if changed:
                    return {
                        "phase": "reviewing",
                        "current_subtask_id": None,
                        "subtasks": updated_subtasks,
                        "artifacts": artifacts,
                        "execution_log": [{
                            "event": "pending_fail_closed",
                            "reason": "unrecoverable_dependencies",
                            "failed_count": sum(1 for t in updated_subtasks if t.status == "failed"),
                            "timestamp": datetime.now().isoformat(),
                        }],
                    }

                return {
                    "phase": "reviewing",
                    "current_subtask_id": None,
                    "subtasks": updated_subtasks,
                    "artifacts": artifacts,
                    "execution_log": [{
                        "event": "no_ready_tasks",
                        "pending_count": len(pending),
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
    if policy.strict_enforcement and len(declared_domains) < required_agents:
        error = f"[POLICY_VIOLATION] declared_domains<{required_agents}"
        fail_subtasks = [
            t.model_copy(update={
                "status": "failed",
                "result": error,
                "started_at": started_at,
                "finished_at": datetime.now(),
            }) if t.id == next_task.id else t
            for t in subtasks
        ]
        return {
            "subtasks": fail_subtasks,
            "current_subtask_id": next_task.id,
            "phase": "reviewing",
            "artifacts": artifacts,
            "execution_log": [{
                "event": "policy_violation",
                "task_id": next_task.id,
                "error": error,
                "required_agents": required_agents,
                "declared_domains": len(declared_domains),
                "timestamp": datetime.now().isoformat(),
            }],
        }

    domains = declared_domains if policy.strict_enforcement else _ensure_domains(next_task, min_count=max(3, required_agents))

    logger.info(
        "[executor] Starting discussion | task=%s | domains=%s | required_agents=%d | required_rounds=%d | strict=%s",
        next_task.id, domains, required_agents, required_rounds, policy.strict_enforcement,
    )

    try:
        call_result, discussion_log = await _execute_multi_agent_discussion(
            caller,
            next_task,
            domains,
            previous_results,
            budget_ctx,
            min_rounds=required_rounds,
            min_agents=required_agents,
            strict=policy.strict_enforcement,
        )
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
    actual_agents_used = int(call_result.get("actual_agents_used") or 0)
    actual_rounds = int(call_result.get("actual_discussion_rounds") or 0)

    strict_unmet = (
        policy.strict_enforcement
        and (actual_agents_used < required_agents or actual_rounds < required_rounds)
    )

    if strict_unmet and call_result.get("success"):
        call_result = {
            "success": False,
            "error": (
                f"[POLICY_VIOLATION] actual_agents={actual_agents_used}<{required_agents} "
                f"or actual_rounds={actual_rounds}<{required_rounds}"
            ),
            "result": None,
            "specialist_id": specialist_id,
            "assigned_agents": assigned_agents,
            "actual_agents_used": actual_agents_used,
            "actual_discussion_rounds": actual_rounds,
        }

    if not call_result.get("success"):
        logger.warning("[executor] task %s discussion failed: %s", next_task.id, call_result.get("error"))
        fail_subtasks = [
            t.model_copy(update={
                "status": "failed",
                "result": f"[AGENT_FAIL] {call_result.get('error', 'unknown error')}",
                "started_at": started_at,
                "finished_at": datetime.now(),
            }) if t.id == next_task.id else t
            for t in subtasks
        ]
        return {
            "subtasks": fail_subtasks,
            "current_subtask_id": next_task.id,
            "phase": "reviewing",
            "artifacts": artifacts,
            "execution_log": [{
                "event": "task_failed",
                "task_id": next_task.id,
                "error": call_result.get("error"),
                "discussion_rounds": actual_rounds,
                "discussion_agents": actual_agents_used,
                "required_rounds": required_rounds,
                "required_agents": required_agents,
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
        _candidates = sorted(
            list(_reports_dir.glob(f"{next_task.id}*.md"))
            + list(_reports_dir.glob(f"{_task_slug}*.md"))
            + list(_reports_dir.glob(f"{next_task.id}*.json"))
            + list(_reports_dir.glob(f"{_task_slug}*.json")),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if _candidates:
            report_path = str(_candidates[0])
            report_kind = _candidates[0].suffix.lstrip(".").lower()
        md_candidates = [p for p in _candidates if p.suffix.lower() == ".md"]
        if md_candidates:
            try:
                _file_content = md_candidates[0].read_text(encoding="utf-8", errors="replace")
                if len(_file_content.strip()) > len(result_text):
                    result_text = _file_content
                    logger.info("[executor] Using report file %s", md_candidates[0].name)
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
            message_type=entry.get("type", "info"),
            metadata={
                "round_index": entry.get("round", 0),
                "domain": entry.get("domain", ""),
                "task_title": next_task.title,
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
            fallback_agents = list(dict.fromkeys([e["agent"] for e in discussion_log]))
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
            "timestamp": datetime.now().isoformat(),
        }],
    }
