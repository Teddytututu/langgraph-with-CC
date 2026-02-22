"""src/graph/nodes/executor.py — 子任务执行调度"""
from datetime import datetime
from typing import Optional

from src.graph.state import GraphState, SubTask
from src.agents.caller import get_caller


async def executor_node(state: GraphState) -> dict:
    """
    找到下一个可执行的子任务并调度 Agent

    通过 SubagentCaller 调用 executor subagent 或专业 subagent 执行任务
    SDK 模式下同步执行，降级模式下需要等待
    """
    caller = get_caller()
    subtasks = state.get("subtasks", [])

    # 🆕 降级模式：检查是否有等待中的调用
    pending_id = state.get("pending_call_id")
    if pending_id and caller.mode == "fallback":
        result_info = caller.check_result(pending_id)

        if result_info.get("completed"):
            # 有结果了，更新子任务状态
            return _handle_execution_result(state, result_info.get("result"), pending_id)
        else:
            # 还在等待
            return {
                "waiting_for_subagent": True,
                "phase": "waiting",
            }

    # 找到依赖已满足的下一个待执行任务
    next_task = _find_next_task(state)
    if not next_task:
        return {"phase": "reviewing", "current_subtask_id": None}

    # 记录开始时间
    started_at = datetime.now()

    # 收集前序依赖任务的结果
    previous_results = _build_context(state, next_task)

    # 获取或创建专业 subagent
    specialist_id = await caller.get_or_create_specialist(
        skills=next_task.knowledge_domains,
        task_description=next_task.description
    )

    # 调用专业 subagent 执行任务（SDK 模式下同步执行）
    if specialist_id:
        call_result = await caller.call_specialist(
            agent_id=specialist_id,
            subtask={
                "id": next_task.id,
                "title": next_task.title,
                "description": next_task.description,
                "agent_type": next_task.agent_type,
                "knowledge_domains": next_task.knowledge_domains,
            },
            previous_results=previous_results
        )
    else:
        # 没有专业 subagent，使用通用 executor
        call_result = await caller.call_executor(
            subtask={
                "id": next_task.id,
                "title": next_task.title,
                "description": next_task.description,
                "agent_type": next_task.agent_type,
                "knowledge_domains": next_task.knowledge_domains,
            },
            previous_results=previous_results
        )

    # 🆕 降级模式：检查是否需要等待外部执行
    if call_result.get("status") == "pending_execution":
        return {
            "pending_call_id": call_result["call_id"],
            "waiting_for_subagent": True,
            "pending_agent_type": "executor",
            "phase": "waiting",
            "current_subtask_id": next_task.id,
            "execution_log": [{
                "event": "execution_call_created",
                "task_id": next_task.id,
                "specialist_id": specialist_id,
                "call_id": call_result["call_id"],
                "mode": "fallback",
                "timestamp": datetime.now().isoformat(),
            }],
        }

    # SDK 模式：直接获取结果
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

    # 纯函数式更新时间预算
    budget = state.get("time_budget")
    if budget and started_at:
        elapsed = (datetime.now() - started_at).total_seconds() / 60
        new_elapsed = budget.elapsed_minutes + elapsed
        new_remaining = max(0, budget.total_minutes - new_elapsed)
        budget = budget.model_copy(update={
            "elapsed_minutes": new_elapsed,
            "remaining_minutes": new_remaining,
            "is_overtime": new_remaining <= 0,
        })

    return {
        "subtasks": updated_subtasks,
        "current_subtask_id": next_task.id,
        "time_budget": budget,
        "phase": "executing",
        "pending_call_id": None,
        "waiting_for_subagent": False,
        "execution_log": [{
            "event": "task_executed",
            "task_id": next_task.id,
            "agent": next_task.agent_type,
            "specialist_id": specialist_id,
            "status": result["status"],
            "mode": call_result.get("mode", "sdk"),
            "timestamp": datetime.now().isoformat(),
        }],
    }


def _handle_execution_result(state: GraphState, result_data, call_id: str) -> dict:
    """处理降级模式的执行结果"""
    subtasks = state.get("subtasks", [])
    current_id = state.get("current_subtask_id")

    # 找到当前子任务
    current_task = next((t for t in subtasks if t.id == current_id), None)
    if not current_task:
        return {"phase": "executing"}

    # 更新子任务状态
    updated_subtasks = []
    for t in subtasks:
        if t.id == current_id:
            updated_subtasks.append(t.model_copy(update={
                "status": "done",
                "result": str(result_data) if result_data else f"任务 {t.title} 执行完成",
                "finished_at": datetime.now(),
            }))
        else:
            updated_subtasks.append(t)

    return {
        "subtasks": updated_subtasks,
        "phase": "reviewing",
        "pending_call_id": None,
        "waiting_for_subagent": False,
        "execution_log": [{
            "event": "task_executed",
            "task_id": current_id,
            "status": "done",
            "mode": "fallback",
            "call_id": call_id,
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
