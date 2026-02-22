"""src/graph/nodes/executor.py — 子任务执行调度"""
from datetime import datetime
from typing import Optional

from src.graph.state import GraphState, SubTask
from src.agents.caller import get_caller


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

    # 获取或创建专业 subagent
    specialist_id = await caller.get_or_create_specialist(
        skills=next_task.knowledge_domains,
        task_description=next_task.description
    )

    # 调用专业 subagent 执行任务
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
