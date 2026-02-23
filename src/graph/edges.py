"""src/graph/edges.py — 条件路由函数"""
import logging
from datetime import datetime
from src.graph.state import GraphState

logger = logging.getLogger(__name__)


def route_after_router(state: GraphState) -> str:
    """Router 之后的路由决策

    路由规则（按优先级）：
    1. 超时 → timeout
    2. phase == init → planning
    3. phase == budgeting → executing（等待预算管理完成）
    4. phase == executing/reviewing → 继续当前阶段
    5. phase == reflecting → executing（reflector 已完成反思）
    6. phase == complete/timeout → 保持当前状态
    7. 所有任务完成 → complete
    8. 默认 → executing
    """
    budget = state.get("time_budget")
    if budget and budget.is_overtime:
        return "timeout"

    phase = state.get("phase", "init")
    subtasks = state.get("subtasks", [])

    # 显式处理各 phase
    if phase == "init":
        return "planning"
    if phase == "budgeting":
        # 预算管理完成，进入执行
        return "executing"
    if phase == "reviewing":
        # 审查中，继续到 reviewer
        return "reviewing"
    if phase == "reflecting":
        # 反思完成，返回执行
        return "executing"
    if phase == "complete" or phase == "timeout":
        # 终态，保持
        return phase

    # 检查任务完成状态
    if not subtasks:
        return "planning"
    if all(t.status in ("done", "skipped", "failed") for t in subtasks):
        return "complete"

    return "executing"


def route_after_review(state: GraphState) -> str:
    """Reviewer 之后的路由"""
    current = _get_current(state)
    # 空値防护：无当前任务时直接进入下一个执行循环
    if current is None:
        logger.debug("route_after_review: current is None → pass")
        return "pass"
    if current.status == "done":
        return "pass"
    max_iter = state.get("max_iterations", 3)
    if current.retry_count >= max_iter:
        return "pass"      # 强制通过，避免死循环
    return "revise"


def should_continue_or_timeout(state: GraphState) -> str:
    """执行后判断：继续 / 审查 / 超时"""
    if _check_timeout(state):
        return "timeout"
    current = _get_current(state)
    # 无当前任务（executor 未找到可执行项），进入审查阶段
    if not current:
        return "review"
    if current.status in ("done", "failed"):
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
