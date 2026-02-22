"""src/graph/edges.py — 条件路由函数"""
from datetime import datetime
from src.graph.state import GraphState


def route_after_router(state: GraphState) -> str:
    """Router 之后的路由决策"""
    budget = state.get("time_budget")
    if budget and budget.is_overtime:
        return "timeout"
    phase = state.get("phase", "init")
    subtasks = state.get("subtasks", [])
    if phase == "init" or not subtasks:
        return "planning"
    if all(t.status in ("done", "skipped") for t in subtasks):
        return "complete"
    return "executing"


def route_after_review(state: GraphState) -> str:
    """Reviewer 之后的路由"""
    current = _get_current(state)
    if current and current.status == "done":
        return "pass"
    max_iter = state.get("max_iterations", 3)
    if current and current.retry_count >= max_iter:
        return "pass"      # 强制通过，避免死循环
    return "revise"


def should_continue_or_timeout(state: GraphState) -> str:
    """执行后判断：继续 / 审查 / 超时"""
    if _check_timeout(state):
        return "timeout"
    current = _get_current(state)
    if current and current.status in ("done", "failed"):
        return "review"
    return "continue"


# ── 工具函数 ──
def _get_current(state: GraphState):
    subtasks = state.get("subtasks", [])
    cid = state.get("current_subtask_id")
    if not cid:
        return None
    return next(
        (t for t in subtasks if t.id == cid),
        None,
    )


def _check_timeout(state: GraphState) -> bool:
    budget = state.get("time_budget")
    if not budget or not budget.deadline:
        return False
    return datetime.now() > budget.deadline
