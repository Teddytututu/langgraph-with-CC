"""src/graph/nodes/router.py — 全局路由节点"""
from datetime import datetime
from src.graph.state import GraphState


async def router_node(state: GraphState) -> dict:
    """判断整体进度，决定下一步"""
    budget = state.get("time_budget")

    # ✅ 纯函数式更新时间
    if budget and budget.started_at:
        elapsed = (
            datetime.now() - budget.started_at
        ).total_seconds() / 60
        remaining = max(0, budget.total_minutes - elapsed)
        budget = budget.model_copy(update={
            "elapsed_minutes": elapsed,
            "remaining_minutes": remaining,
            "is_overtime": remaining <= 0,
        })

    # 全部完成 → 汇总输出
    subtasks = state.get("subtasks", [])
    if subtasks and all(
        t.status in ("done", "skipped") for t in subtasks
    ):
        return {
            "phase": "complete",
            "final_output": _build_final_output(state),
            "time_budget": budget,
        }

    # 超时 → 交付已完成部分
    if budget and budget.is_overtime:
        return {
            "phase": "timeout",
            "final_output": _build_final_output(state, timeout=True),
            "time_budget": budget,
        }

    return {
        "phase": state.get("phase", "init") if subtasks else "init",
        "time_budget": budget,
        "iteration": state.get("iteration", 0) + 1,
    }


def _build_final_output(state: GraphState, timeout: bool = False) -> str:
    """汇总所有子任务结果"""
    lines = []
    if timeout:
        lines.append("⚠️ **时间预算已用尽，以下为已完成部分：**\n")
    else:
        lines.append("✅ **所有任务已完成：**\n")

    subtasks = state.get("subtasks", [])
    for t in subtasks:
        icon = "✅" if t.status == "done" else "❌" if t.status == "failed" else "⏳"
        lines.append(f"### {icon} {t.title}")
        if t.result:
            lines.append(t.result)
        lines.append("")

    budget = state.get("time_budget")
    if budget:
        lines.append(
            f"\n---\n总耗时 {budget.elapsed_minutes:.1f} 分钟 "
            f"/ 预算 {budget.total_minutes:.0f} 分钟"
        )
    return "\n".join(lines)
