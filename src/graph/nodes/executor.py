"""src/graph/nodes/executor.py — 子任务执行调度"""
import asyncio
from datetime import datetime
from typing import Optional

from src.graph.state import GraphState, SubTask
from src.agents.caller import get_caller
from src.agents.coordinator import CoordinatorAgent
from src.agents.collaboration import (
    CollaborationMode, AgentExecutor, execute_collaboration,
)

_coordinator = CoordinatorAgent()


def _compute_timeout(task: SubTask) -> float:
    """计算子任务执行超时时间（秒），取估算时间的 2 倍，最低 120s 最高 1800s"""
    return max(120.0, min(task.estimated_minutes * 120, 1800.0))


async def executor_node(state: GraphState) -> dict:
    """
    找到下一个可执行的子任务并调度 Agent

    通过 SubagentCaller 调用 executor subagent 或专业 subagent 执行任务
    """
    caller = get_caller()
    subtasks = state.get("subtasks", [])

    # 找到依赖已满足的下一个待执行任务
    next_task = _find_next_task(state)
    if not next_task:
        # 检查是否有死锁：有 pending 任务但无法执行
        pending = [t for t in subtasks if t.status == "pending"]
        if pending:
            # 所有 pending 任务的依赖都已失败/无法满足，将它们标记为失败
            updated_subtasks = []
            done_ids = {t.id for t in subtasks if t.status in ("done", "skipped", "failed")}
            for t in subtasks:
                if t.status == "pending" and not all(d in done_ids for d in t.dependencies):
                    updated_subtasks.append(t.model_copy(update={
                        "status": "failed",
                        "result": f"依赖任务失败，无法执行：{t.dependencies}",
                    }))
                else:
                    updated_subtasks.append(t)
            return {"phase": "reviewing", "current_subtask_id": None, "subtasks": updated_subtasks}
        return {"phase": "reviewing", "current_subtask_id": None}

    # 记录开始时间
    started_at = datetime.now()

    # 收集前序依赖任务的结果
    previous_results = _build_context(state, next_task)

    # 使用协调者选择协作模式
    mode = _coordinator.choose_collaboration_mode(
        task=next_task.description,
        agents=next_task.knowledge_domains or [next_task.agent_type],
        subtasks=state.get("subtasks", []),
    )

    timeout = _compute_timeout(next_task)
    specialist_id: Optional[str] = None

    # 并行协作：为每个知识域分配独立的专家
    if mode == CollaborationMode.PARALLEL and len(next_task.knowledge_domains) >= 2:
        call_result = await _execute_parallel(
            caller, next_task, previous_results, timeout
        )
        specialist_id = call_result.get("specialist_id")
    else:
        # 链式或单专家模式
        specialist_id = await caller.get_or_create_specialist(
            skills=next_task.knowledge_domains,
            task_description=next_task.description
        )

        subtask_dict = {
            "id": next_task.id,
            "title": next_task.title,
            "description": next_task.description,
            "agent_type": next_task.agent_type,
            "knowledge_domains": next_task.knowledge_domains,
        }

        try:
            if specialist_id:
                call_result = await asyncio.wait_for(
                    caller.call_specialist(
                        agent_id=specialist_id,
                        subtask=subtask_dict,
                        previous_results=previous_results,
                    ),
                    timeout=timeout,
                )
            else:
                call_result = await asyncio.wait_for(
                    caller.call_executor(
                        subtask=subtask_dict,
                        previous_results=previous_results,
                    ),
                    timeout=timeout,
                )
        except asyncio.TimeoutError:
            raise RuntimeError(
                f"Executor 超时：任务 {next_task.id}（{next_task.title}）"
                f"超过 {timeout:.0f}s 未完成"
            )

    # 检查执行是否成功
    if not call_result.get("success"):
        raise RuntimeError(f"Executor 执行失败: {call_result.get('error')}")

    # 获取结果
    result_data = call_result.get("result")
    result = {
        "status": "done",
        "result": str(result_data) if result_data else f"任务 {next_task.title} 执行完成",
        "specialist_id": specialist_id,
        "finished_at": datetime.now(),
    }

    # 标记专业 subagent 完成（子任务级别）
    if specialist_id:
        caller.complete_subtask(specialist_id)

    # 纯函数式更新子任务状态
    updated_subtasks = []
    for t in subtasks:
        if t.id == next_task.id:
            updated_subtasks.append(t.model_copy(update={
                "status": result["status"],
                "result": result["result"],
                "started_at": started_at,
                "finished_at": result["finished_at"],
                "assigned_agents": [specialist_id] if specialist_id else [],
            }))
        else:
            updated_subtasks.append(t)

    return {
        "subtasks": updated_subtasks,
        "current_subtask_id": next_task.id,
        "time_budget": state.get("time_budget"),
        "phase": "executing",
        "execution_log": [{
            "event": "task_executed",
            "task_id": next_task.id,
            "agent": next_task.agent_type,
            "specialist_id": specialist_id,
            "status": result["status"],
            "timestamp": datetime.now().isoformat(),
        }],
    }


def _find_next_task(state: GraphState) -> Optional[SubTask]:
    """找到依赖已满足的下一个待执行任务"""
    subtasks = state.get("subtasks", [])
    done_ids = {t.id for t in subtasks if t.status in ("done", "skipped")}
    for task in sorted(subtasks, key=lambda t: t.priority):
        if task.status == "pending":
            if all(d in done_ids for d in task.dependencies):
                return task
    return None


def _build_context(state: GraphState, current_task: SubTask) -> list[dict]:
    """收集前序依赖任务的结果"""
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


async def _execute_parallel(caller, task: SubTask, previous_results: list, timeout: float) -> dict:
    """
    并行协作：为每个知识域创建独立专家，并发执行，合并结果

    Returns:
        标准的 call_result 字典（含 success / result 字段）
    """
    domains = task.knowledge_domains
    subtask_dict = {
        "id": task.id,
        "title": task.title,
        "description": task.description,
        "agent_type": task.agent_type,
        "knowledge_domains": task.knowledge_domains,
    }

    async def run_one(domain: str) -> dict:
        sid = await caller.get_or_create_specialist(
            skills=[domain],
            task_description=task.description,
        )
        if sid:
            return await caller.call_specialist(
                agent_id=sid,
                subtask=subtask_dict,
                previous_results=previous_results,
            )
        return await caller.call_executor(
            subtask=subtask_dict,
            previous_results=previous_results,
        )

    try:
        results = await asyncio.wait_for(
            asyncio.gather(*[run_one(d) for d in domains], return_exceptions=False),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        raise RuntimeError(
            f"Executor 并行超时：任务 {task.id}（{task.title}）"
            f"超过 {timeout:.0f}s 未完成"
        )

    # 合并：取第一个成功的结果，失败的结果拼接到末尾
    merged_parts = []
    first_success = None
    for r in results:
        if isinstance(r, dict) and r.get("success"):
            if first_success is None:
                first_success = r
            data = r.get("result")
            if data:
                merged_parts.append(str(data))

    if first_success is None:
        errors = [r.get("error", "unknown") for r in results if isinstance(r, dict)]
        return {"success": False, "error": "; ".join(errors), "result": None}

    merged_result = "\n\n---\n\n".join(merged_parts) if merged_parts else first_success.get("result")
    return {"success": True, "result": merged_result, "specialist_id": first_success.get("agent_id")}
    return prev_results
