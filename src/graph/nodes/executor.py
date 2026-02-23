"""src/graph/nodes/executor.py - Multi-Agent Discussion Executor

Each subtask is executed by >=3 domain specialists engaging in >=10 rounds
of discussion, with a final synthesizer producing consensus conclusions.
"""
import asyncio
import logging
from datetime import datetime
from typing import Optional

from src.graph.state import GraphState, SubTask, NodeDiscussion, DiscussionMessage
from src.agents.caller import get_caller

logger = logging.getLogger(__name__)


def _compute_timeout(task: SubTask) -> float:
    """Outer timeout for the 2-phase discussion.

    Inner caps: 150s per specialist (parallel) + 150s synthesizer = 300s wall time.
    Add 200s buffer => need at least 500s.

    Formula: max(500s, min(est_min * 75s, 1200s))
      est=7min => 525s  |  est=8min => 600s  |  est=16min => 1200s (cap)

    Budget math: 6 subtasks x ~600s each = 60 min, well within 90 min poll deadline.
    """
    return max(500.0, min(task.estimated_minutes * 75.0, 1200.0))


def _ensure_domains(task: SubTask, min_count: int = 3) -> list[str]:
    """Guarantee at least min_count knowledge domains per task"""
    domains = list(task.knowledge_domains or [task.agent_type])
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


def _find_next_task(state: GraphState) -> Optional[SubTask]:
    subtasks = state.get("subtasks", [])
    done_ids = {t.id for t in subtasks if t.status in ("done", "skipped")}
    for task in sorted(subtasks, key=lambda t: t.priority):
        if task.status == "pending":
            if all(d in done_ids for d in task.dependencies):
                return task
    return None


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


# ------------------------------------------------------------------ #
#  Multi-Agent Discussion Engine                                      #
# ------------------------------------------------------------------ #

async def _execute_multi_agent_discussion(
    caller,
    task: SubTask,
    domains: list[str],
    previous_results: list[dict],
    budget_ctx: dict | None,
    *,
    min_rounds: int = 10,   # kept as param; each specialist covers this many dimensions
    min_agents: int = 3,
) -> tuple[dict, list[dict]]:
    """
    Multi-specialist discussion with EFFICIENT parallel design.

    Architecture (2 phases total, not 10+ rounds):

    Phase 1 – PARALLEL: each specialist gets ONE SDK call containing a
    comprehensive prompt asking them to analyse the task across `min_rounds`
    analytical dimensions from their domain perspective.

    Phase 2 – SEQUENTIAL: synthesizer merges all specialist outputs into
    a final consensus report.

    This gives "3 agents, 10+ analytical dimensions" while making only
    4 total SDK calls (3 parallel + 1 sequential) instead of 31.

    Returns (call_result, discussion_log)
    where discussion_log has min_agents*min_rounds + 1 conceptual entries.
    """
    discussion_log: list[dict] = []

    # ── Phase 0: create specialists IN PARALLEL ──
    target_domains = domains[:max(min_agents, len(domains))]

    async def _create_one(domain: str):
        return domain, await caller.get_or_create_specialist(
            skills=[domain],
            task_description=f"[{domain}] {task.description}",
        )

    created = await asyncio.gather(*[_create_one(d) for d in target_domains], return_exceptions=True)

    specialists: list[dict] = []
    for item in created:
        if isinstance(item, Exception):
            logger.warning("[discussion] get_or_create_specialist raised: %s", item)
            continue
        domain, sid = item
        if sid:
            specialists.append({"id": sid, "domain": domain})
        else:
            logger.warning("[discussion] cannot create %s specialist", domain)

    if not specialists:
        return (
            {"success": False, "error": "no specialists created", "result": None, "specialist_id": None},
            discussion_log,
        )

    while len(specialists) < min_agents:
        src = specialists[len(specialists) % len(specialists)]
        specialists.append({"id": src["id"], "domain": f"{src['domain']}_ext"})

    subtask_dict = {
        "id": task.id,
        "title": task.title,
        "description": task.description,
        "agent_type": task.agent_type,
        "knowledge_domains": task.knowledge_domains,
        "estimated_minutes": task.estimated_minutes,
        "completion_criteria": task.completion_criteria,
    }
    criteria_str = ", ".join(task.completion_criteria) if task.completion_criteria else "none"

    # Build the 10 analytical dimensions for each specialist
    dimensions = [
        "current state assessment – what exists / what works",
        "gap analysis – what is missing or broken",
        "root cause analysis – why issues exist",
        "risk assessment – potential failure modes",
        "dependency mapping – upstream/downstream impacts",
        "quick wins – changes that improve things immediately",
        "structural improvements – deeper architectural changes",
        "validation approach – how to verify fixes work",
        "implementation roadmap – ordered action steps",
        "monitoring & observability – ongoing health checks",
    ]
    dim_list = "\n".join(f"{i+1}. {d}" for i, d in enumerate(dimensions[:min_rounds]))

    prev_summary = ""
    if previous_results:
        prev_summary = "\n\n=== Results from prerequisite tasks ===\n"
        for p in previous_results[:3]:
            prev_summary += f"\n**{p['title']}**:\n{str(p['result'])[:800]}\n"
        prev_summary += "=== End prerequisite results ===\n"

    # ── Phase 1: fire all specialists in PARALLEL ──
    async def _deep_dive_specialist(spec: dict) -> dict:
        prompt = (
            f"You are a senior **{spec['domain']}** specialist.\n"
            f"Task: {task.title}\n"
            f"Description: {task.description}\n"
            f"Completion criteria: {criteria_str}\n"
            f"{prev_summary}\n"
            f"Please analyse this task across ALL {min_rounds} dimensions below, "
            f"writing a dedicated section for each:\n\n"
            f"{dim_list}\n\n"
            f"Format: use '## Dimension N: <name>' as headers.\n"
            f"Be specific, technical, and actionable throughout. "
            f"Focus on your **{spec['domain']}** expertise.\n\n"
            f"Output your complete {min_rounds}-dimension analysis:"
        )
        try:
            res = await asyncio.wait_for(
                caller.call_specialist(
                    agent_id=spec["id"],
                    subtask={**subtask_dict, "description": prompt},
                    previous_results=previous_results,
                    time_budget=budget_ctx,
                ),
                timeout=150.0,  # 2.5 min per specialist
            )
        except asyncio.TimeoutError:
            logger.warning("[discussion] %s deep-dive timed out (150s)", spec["domain"])
            res = {"success": False, "error": "timed out after 150s", "result": None}
        except Exception as e:
            logger.warning("[discussion] %s deep-dive failed: %s", spec["domain"], e)
            res = {"success": False, "error": str(e), "result": None}

        output = ""
        if res.get("success") and res.get("result"):
            output = str(res["result"]).strip()
        elif res.get("error"):
            output = f"[{spec['domain']} failed: {res['error']}]"
        else:
            output = "[no output]"

        # Split output into per-dimension log entries for visibility
        sections = output.split("## Dimension ")
        if len(sections) > 1:
            for i, sec in enumerate(sections[1:], start=1):
                discussion_log.append({
                    "agent": spec["id"],
                    "domain": spec["domain"],
                    "round": i,
                    "content": ("## Dimension " + sec)[:2000],
                    "type": "analysis",
                })
        else:
            # Fallback: single entry
            discussion_log.append({
                "agent": spec["id"],
                "domain": spec["domain"],
                "round": 1,
                "content": output[:3000],
                "type": "analysis",
            })

        return {"domain": spec["domain"], "id": spec["id"], "output": output}

    logger.info("[discussion] Phase 1: firing %d specialists in parallel", len(specialists))
    specialist_results = await asyncio.gather(
        *[_deep_dive_specialist(spec) for spec in specialists],
        return_exceptions=True,
    )

    # Collect outputs
    outputs_by_domain: dict[str, str] = {}
    for i, res in enumerate(specialist_results):
        domain = specialists[i]["domain"]
        if isinstance(res, Exception):
            logger.warning("[discussion] specialist %s raised exception: %s", domain, res)
            outputs_by_domain[domain] = f"[exception: {res}]"
        else:
            outputs_by_domain[domain] = res.get("output", "[no output]")

    if not any(v and not v.startswith("[") for v in outputs_by_domain.values()):
        return (
            {"success": False, "error": "all specialists failed", "result": None, "specialist_id": None},
            discussion_log,
        )

    # ── Phase 2: synthesizer ──
    perspectives_text = "\n\n".join(
        f"=== {domain.upper()} SPECIALIST ===\n{output[:4000]}"
        for domain, output in outputs_by_domain.items()
    )
    synthesis_prompt = (
        f"You are the consensus synthesizer.\n"
        f"Task: {task.title}\nDescription: {task.description}\n\n"
        f"{perspectives_text}\n\n"
        f"=== END OF SPECIALIST REPORTS ===\n\n"
        f"Synthesise ALL specialist perspectives into one final report:\n"
        f"1. **Consensus findings** – what all specialists agree on\n"
        f"2. **Complementary insights** – unique value from each perspective\n"
        f"3. **Prioritised action plan** – ordered by impact × effort\n"
        f"4. **Implementation details** – specific files, commands, or code to change\n"
        f"5. **Verification checklist** – how to confirm completion\n\n"
        f"Output the complete synthesis report:"
    )

    synthesizer_id = specialists[0]["id"]
    logger.info("[discussion] Phase 2: running synthesizer")
    try:
        synth_res = await asyncio.wait_for(
            caller.call_specialist(
                agent_id=synthesizer_id,
                subtask={**subtask_dict, "description": synthesis_prompt},
                previous_results=[],
                time_budget=budget_ctx,
            ),
            timeout=150.0,  # 2.5 min synthesizer
        )
    except asyncio.TimeoutError:
        logger.warning("[discussion] synthesizer timed out (150s), using concatenation")
        synth_res = {"success": True, "result": "\n\n---\n\n".join(
            f"**[{d}]**\n{o}" for d, o in outputs_by_domain.items()
        )}
    except Exception as e:
        logger.warning("[discussion] synthesizer error: %s", e)
        synth_res = {"success": True, "result": "\n\n---\n\n".join(
            f"**[{d}]**\n{o}" for d, o in outputs_by_domain.items()
        )}

    final_result = ""
    if synth_res.get("success") and synth_res.get("result"):
        final_result = str(synth_res["result"]).strip()
    if not final_result:
        final_result = "\n\n---\n\n".join(
            f"**[{d}]**\n{o}" for d, o in outputs_by_domain.items()
        )

    discussion_log.append({
        "agent": synthesizer_id,
        "domain": "synthesis",
        "round": min_rounds + 1,
        "content": final_result[:3000],
        "type": "synthesis",
    })

    return (
        {
            "success": bool(final_result),
            "result": final_result,
            "specialist_id": specialists[0]["id"],
            "error": None if final_result else "discussion produced no conclusion",
        },
        discussion_log,
    )


# ------------------------------------------------------------------ #
#  Main Executor Node                                                 #
# ------------------------------------------------------------------ #

async def executor_node(state: GraphState) -> dict:
    """
    Find the next executable subtask, run multi-agent discussion.
    Flow: 3+ domain specialists x 10+ rounds -> synthesizer -> consensus
    """
    from pathlib import Path as _Path
    _Path("reports").mkdir(exist_ok=True)

    caller = get_caller()
    subtasks = state.get("subtasks", [])

    next_task = _find_next_task(state)
    if not next_task:
        pending = [t for t in subtasks if t.status == "pending"]
        if pending:
            updated_subtasks = []
            done_ids = {t.id for t in subtasks if t.status in ("done", "skipped", "failed")}
            for t in subtasks:
                if t.status == "pending" and not all(d in done_ids for d in t.dependencies):
                    updated_subtasks.append(t.model_copy(update={
                        "status": "failed",
                        "result": f"Dependency failed: {t.dependencies}",
                    }))
                else:
                    updated_subtasks.append(t)
            return {"phase": "reviewing", "current_subtask_id": None, "subtasks": updated_subtasks}
        return {"phase": "reviewing", "current_subtask_id": None}

    started_at = datetime.now()
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

    # === Core: Multi-Agent Discussion Execution ===
    domains = _ensure_domains(next_task, min_count=3)
    timeout = _compute_timeout(next_task)

    logger.info(
        "[executor] Starting multi-agent discussion | task=%s | domains=%s | experts=%d",
        next_task.id, domains, len(domains),
    )

    try:
        call_result, discussion_log = await asyncio.wait_for(
            _execute_multi_agent_discussion(
                caller, next_task, domains, previous_results, budget_ctx,
                min_rounds=10, min_agents=3,
            ),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        # Don't crash the whole graph — mark this subtask failed and let the graph continue
        logger.warning(
            "[executor] discussion timeout %ss for task %s — marking failed, graph continues",
            timeout, next_task.id,
        )
        call_result = {
            "success": False,
            "error": f"discussion timeout ({timeout:.0f}s)",
            "result": None,
            "specialist_id": None,
        }
        discussion_log = []

    specialist_id = call_result.get("specialist_id")

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
            "execution_log": [{
                "event": "task_failed",
                "task_id": next_task.id,
                "error": call_result.get("error"),
                "discussion_rounds": len(discussion_log),
                "timestamp": datetime.now().isoformat(),
            }],
        }

    result_text = str(call_result.get("result", "")).strip()

    # Try to associate report files
    _reports_dir = _Path("reports")
    if _reports_dir.exists():
        _task_slug = next_task.id.replace("-", "").replace(".", "")
        _candidates = sorted(
            list(_reports_dir.glob(f"{next_task.id}*.md"))
            + list(_reports_dir.glob(f"{_task_slug}*.md")),
            key=lambda p: p.stat().st_mtime, reverse=True,
        )
        if _candidates:
            try:
                _file_content = _candidates[0].read_text(encoding="utf-8", errors="replace")
                if len(_file_content.strip()) > len(result_text):
                    result_text = _file_content
                    logger.info("[executor] Using report file %s", _candidates[0].name)
            except Exception as _fe:
                logger.warning("[executor] Failed to read report: %s", _fe)

    result = {
        "status": "done",
        "result": result_text,
        "specialist_id": specialist_id,
        "finished_at": datetime.now(),
    }

    # Build discussion records
    existing_discussions: dict = dict(state.get("discussions") or {})
    disc = existing_discussions.get(next_task.id) or NodeDiscussion(node_id=next_task.id)
    for entry in discussion_log:
        disc.add_message(DiscussionMessage(
            node_id=next_task.id,
            from_agent=entry["agent"],
            content=entry["content"][:2000],
            message_type=entry.get("type", "info"),
            metadata={
                "round": entry.get("round", 0),
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

    all_agents = list({e["agent"] for e in discussion_log})
    updated_subtasks = []
    for t in subtasks:
        if t.id == next_task.id:
            updated_subtasks.append(t.model_copy(update={
                "status": result["status"],
                "result": result["result"],
                "started_at": started_at,
                "finished_at": result["finished_at"],
                "assigned_agents": all_agents,
            }))
        else:
            updated_subtasks.append(t)

    logger.info(
        "[executor] Task %s done | agents=%d | rounds=%d | result_len=%d",
        next_task.id, len(all_agents), len(discussion_log), len(result_text),
    )

    return {
        "subtasks": updated_subtasks,
        "current_subtask_id": next_task.id,
        "time_budget": state.get("time_budget"),
        "phase": "executing",
        "discussions": existing_discussions,
        "execution_log": [{
            "event": "task_executed",
            "task_id": next_task.id,
            "agent": next_task.agent_type,
            "specialist_id": specialist_id,
            "status": result["status"],
            "discussion_rounds": len(discussion_log),
            "discussion_agents": len(all_agents),
            "timestamp": datetime.now().isoformat(),
        }],
    }