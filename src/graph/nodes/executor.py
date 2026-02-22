"""src/graph/nodes/executor.py — 子任务执行调度"""
from datetime import datetime
from src.graph.state import GraphState


async def executor_node(state: GraphState) -> dict:
    """找到下一个可执行的子任务并调度 Agent"""
    subtasks = state.get("subtasks", [])

    # 找到依赖已满足的下一个待执行任务
    next_task = _find_next_task(state)
    if not next_task:
        return {"phase": "reviewing", "current_subtask_id": None}

    # 记录开始时间
    started_at = datetime.now()

    # TODO: 调用对应 Agent 执行
    # 目前模拟执行完成
    result = {
        "status": "done",
        "result": f"任务 {next_task.title} 执行完成（模拟）",
        "finished_at": datetime.now(),
    }

    # ✅ 纯函数式更新子任务状态
    updated_subtasks = []
    for t in subtasks:
        if t.id == next_task.id:
            updated_subtasks.append(t.model_copy(update={
                "status": result["status"],
                "result": result["result"],
                "started_at": started_at,
                "finished_at": result["finished_at"],
            }))
        else:
            updated_subtasks.append(t)

    # ✅ 纯函数式更新时间预算
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
        "execution_log": [{
            "event": "task_executed",
            "task_id": next_task.id,
            "agent": next_task.agent_type,
            "status": result["status"],
            "timestamp": datetime.now().isoformat(),
        }],
    }


def _find_next_task(state: GraphState):
    """找到依赖已满足的下一个待执行任务"""
    subtasks = state.get("subtasks", [])
    done_ids = {t.id for t in subtasks if t.status in ("done", "skipped")}
    for task in sorted(subtasks, key=lambda t: t.priority):
        if task.status == "pending":
            if all(d in done_ids for d in task.dependencies):
                return task
    return None


def _build_context(state: GraphState, current_task) -> dict:
    """收集前序依赖任务的结果"""
    subtasks = state.get("subtasks", [])
    prev_results = []
    for dep_id in current_task.dependencies:
        for t in subtasks:
            if t.id == dep_id and t.result:
                prev_results.append(f"### {t.title}\n{t.result}")
    return {
        "previous_results": "\n\n".join(prev_results) if prev_results else "无",
        "artifacts": state.get("artifacts", {}),
    }
