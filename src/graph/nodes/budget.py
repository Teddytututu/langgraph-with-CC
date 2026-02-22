"""src/graph/nodes/budget.py — 时间预算分配节点"""
from datetime import datetime, timedelta
from src.graph.state import GraphState


async def budget_node(state: GraphState) -> dict:
    """根据用户时间预算，动态分配各子任务的时间"""
    budget = state.get("time_budget")
    subtasks = state.get("subtasks", [])

    if not budget:
        return {"phase": "executing"}

    # 设置开始时间和截止时间（仅在未设置时初始化）
    now = datetime.now()
    update = {}
    if not budget.started_at:
        update["started_at"] = now
    if not budget.deadline:
        start = budget.started_at or now
        update["deadline"] = start + timedelta(minutes=budget.total_minutes)
        update["remaining_minutes"] = budget.total_minutes
    if update:
        budget = budget.model_copy(update=update)

    # 计算可用时间（扣除 20% 审查缓冲）
    available = budget.total_minutes * 0.8
    total_estimated = sum(t.estimated_minutes for t in subtasks)

    # 零除法防护
    if total_estimated <= 0:
        return {
            "subtasks": subtasks,
            "time_budget": budget,
            "phase": "executing",
            "execution_log": [{
                "event": "budget_allocated",
                "deadline": budget.deadline.isoformat() if budget.deadline else None,
                "task_budgets": {},
                "timestamp": now.isoformat(),
            }],
        }

    if total_estimated > available:
        # 按比例缩减每个子任务的预估时间
        scale = available / total_estimated
        subtasks = [
            t.model_copy(update={"estimated_minutes": round(t.estimated_minutes * scale, 1)})
            for t in subtasks
        ]
    elif total_estimated < available * 0.5:
        # 估算太少，按比例放大让 Agent 做得更充分
        scale = (available * 0.7) / total_estimated
        subtasks = [
            t.model_copy(update={"estimated_minutes": round(t.estimated_minutes * scale, 1)})
            for t in subtasks
        ]

    return {
        "subtasks": subtasks,
        "time_budget": budget,
        "phase": "executing",
        "execution_log": [{
            "event": "budget_allocated",
            "deadline": budget.deadline.isoformat(),
            "task_budgets": {
                t.id: t.estimated_minutes for t in subtasks
            },
            "timestamp": now.isoformat(),
        }],
    }
