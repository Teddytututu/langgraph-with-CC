"""src/graph/edges.py — 条件路由函数"""
import logging
from src.graph.state import GraphState

logger = logging.getLogger(__name__)


def route_after_router(state: GraphState) -> str:
    """Router 之后的路由决策

    路由规则（按优先级）：
    1. phase == init → planning
    2. phase == budgeting → executing（等待预算管理完成）
    3. phase == executing/reviewing → 继续当前阶段
    4. phase == reflecting → executing（reflector 已完成反思）
    5. phase == complete → 保持当前状态
    6. 所有任务完成 → complete
    7. 默认 → executing
    """
    phase = state.get("phase", "init")
    subtasks = state.get("subtasks", [])

    # 显式处理各 phase
    if phase == "init":
        return "planning"
    if phase == "budgeting":
        # 预算管理完成，进入执行
        return "executing"
    if phase == "reviewing":
        current = _get_current(state)
        if current is not None and current.status not in ("done", "failed", "skipped"):
            return "reviewing"
        if _all_terminal(subtasks):
            return "complete"
        return "executing"
    if phase == "reflecting":
        # 反思完成，返回执行
        return "executing"
    if phase == "complete":
        # 终态，保持
        return phase

    # 检查任务完成状态
    if not subtasks:
        return "planning"
    if all(t.status in ("done", "skipped", "failed") for t in subtasks):
        return "complete"

    return "executing"


def route_after_review(state: GraphState) -> str:
    """Reviewer 之后的路由（advisory-only，不作为终止门）。"""
    return "pass"


def _task_dependencies(task) -> list[str]:
    deps = getattr(task, "dependencies", None)
    return [d for d in (deps or []) if d]


def _collect_ready_tasks(subtasks: list) -> list:
    done_ids = {t.id for t in subtasks if t.status in ("done", "skipped")}
    ready = [
        t for t in sorted(subtasks, key=lambda t: t.priority)
        if t.status == "pending" and all(dep in done_ids for dep in _task_dependencies(t))
    ]
    return ready


def should_continue_or_timeout(state: GraphState) -> str:
    """执行后判断：继续 / 审查 / 等待"""
    current = _get_current(state)
    subtasks = state.get("subtasks", [])

    if not current:
        if _all_terminal(subtasks):
            return "review"

        ready_tasks = _collect_ready_tasks(subtasks)
        if ready_tasks:
            return "continue"

        has_pending = any(t.status == "pending" for t in subtasks)
        if has_pending:
            # 有未完成任务但当前无 ready：交由 router/reflect 路径处理阻塞态
            return "wait"

        return "review"

    if current.status in ("done", "failed"):
        return "review"

    return "continue"


# ── 工具函数 ──
def _all_terminal(subtasks: list) -> bool:
    return bool(subtasks) and all(t.status in ("done", "skipped", "failed") for t in subtasks)


def _get_current(state: GraphState):
    subtasks = state.get("subtasks", [])
    cid = state.get("current_subtask_id")
    if not cid:
        return None
    return next(
        (t for t in subtasks if t.id == cid),
        None,
    )
