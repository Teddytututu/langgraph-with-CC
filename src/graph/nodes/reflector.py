"""src/graph/nodes/reflector.py — 反思重试节点"""
from datetime import datetime
from typing import Optional

from src.graph.state import GraphState, SubTask
from src.agents.caller import get_caller


async def reflector_node(state: GraphState) -> dict:
    """
    分析失败原因，增强 prompt 后重新分配

    通过 SubagentCaller 调用 reflector subagent 进行反思改进
    SDK 模式下同步执行，降级模式下需要等待
    """
    caller = get_caller()
    subtasks = state.get("subtasks", [])
    cid = state.get("current_subtask_id")

    # 🆕 降级模式：检查是否有等待中的调用
    pending_id = state.get("pending_call_id")
    if pending_id and caller.mode == "fallback":
        result_info = caller.check_result(pending_id)

        if result_info.get("completed"):
            # 有结果了，处理反思结果
            return _handle_reflection_result(state, result_info.get("result"), pending_id)
        else:
            # 还在等待
            return {
                "waiting_for_subagent": True,
                "phase": "waiting",
            }

    current = _find_current_subtask(subtasks, cid)
    if not current:
        return {"phase": "executing"}

    # 获取最近的审查反馈
    last_review = _get_last_review(state, current.id)
    issues = last_review.get("issues", []) if last_review else []

    # 调用 reflector subagent 进行反思（SDK 模式下同步执行）
    call_result = await caller.call_reflector(
        failure_context={
            "issues": issues,
            "original_description": current.description,
            "retry_count": current.retry_count,
            "last_result": current.result,
        },
        subtask={
            "id": current.id,
            "title": current.title,
            "description": current.description,
            "agent_type": current.agent_type,
        }
    )

    # 🆕 降级模式：检查是否需要等待外部执行
    if call_result.get("status") == "pending_execution":
        return {
            "pending_call_id": call_result["call_id"],
            "waiting_for_subagent": True,
            "pending_agent_type": "reflector",
            "phase": "waiting",
            "execution_log": [{
                "event": "reflection_call_created",
                "task_id": current.id,
                "call_id": call_result["call_id"],
                "mode": "fallback",
                "timestamp": datetime.now().isoformat(),
            }],
        }

    # SDK 模式：直接获取结果
    reflection = _parse_reflection_result(call_result, issues)

    # 纯函数式更新
    new_description = (
        current.description
        + f"\n\n--- 第 {current.retry_count + 1} 次反思改进 ---\n"
        + reflection
    )

    updated_subtasks = []
    for t in subtasks:
        if t.id == current.id:
            updated_subtasks.append(t.model_copy(update={
                "description": new_description,
                "status": "pending",
                "result": None,
            }))
        else:
            updated_subtasks.append(t)

    return {
        "subtasks": updated_subtasks,
        "phase": "executing",
        "pending_call_id": None,
        "waiting_for_subagent": False,
        "execution_log": [{
            "event": "reflection_complete",
            "task_id": current.id,
            "retry_count": current.retry_count,
            "subagent_called": "reflector",
            "mode": call_result.get("mode", "sdk"),
            "timestamp": datetime.now().isoformat(),
        }],
    }


def _handle_reflection_result(state: GraphState, result_data, call_id: str) -> dict:
    """处理降级模式的反思结果"""
    subtasks = state.get("subtasks", [])
    cid = state.get("current_subtask_id")

    current = _find_current_subtask(subtasks, cid)
    if not current:
        return {"phase": "executing"}

    # 获取最近的审查反馈
    last_review = _get_last_review(state, current.id)
    issues = last_review.get("issues", []) if last_review else []

    # 解析反思结果
    reflection = f"\n需要改进的问题: {issues if issues else '无特定问题，请重新执行'}"
    if result_data and isinstance(result_data, dict):
        parts = []
        if result_data.get("root_cause"):
            parts.append(f"根本原因: {result_data['root_cause']}")
        if result_data.get("lessons_learned"):
            parts.append(f"经验教训: {', '.join(result_data['lessons_learned'])}")
        if result_data.get("improved_description"):
            parts.append(f"改进方案: {result_data['improved_description']}")
        if parts:
            reflection = "\n".join(parts)

    # 更新描述
    new_description = (
        current.description
        + f"\n\n--- 第 {current.retry_count + 1} 次反思改进 ---\n"
        + reflection
    )

    updated_subtasks = []
    for t in subtasks:
        if t.id == current.id:
            updated_subtasks.append(t.model_copy(update={
                "description": new_description,
                "status": "pending",
                "result": None,
            }))
        else:
            updated_subtasks.append(t)

    return {
        "subtasks": updated_subtasks,
        "phase": "executing",
        "pending_call_id": None,
        "waiting_for_subagent": False,
        "execution_log": [{
            "event": "reflection_complete",
            "task_id": current.id,
            "retry_count": current.retry_count,
            "mode": "fallback",
            "call_id": call_id,
            "timestamp": datetime.now().isoformat(),
        }],
    }


def _find_current_subtask(subtasks: list[SubTask], cid: Optional[str]) -> Optional[SubTask]:
    """查找当前子任务"""
    return next((t for t in subtasks if t.id == cid), None)


def _get_last_review(state: GraphState, task_id: str) -> Optional[dict]:
    """获取指定任务的最近审查反馈"""
    return next(
        (log for log in reversed(state.get("execution_log", []))
         if log.get("event") == "review_complete"
         and log.get("task_id") == task_id),
        None,
    )


def _parse_reflection_result(call_result: dict, issues: list) -> str:
    """解析反思结果"""
    if not call_result.get("success"):
        return f"\n需要改进的问题: {issues if issues else '无特定问题，请重新执行'}"

    result = call_result.get("result")
    if result and isinstance(result, dict):
        improved_description = result.get("improved_description", "")
        root_cause = result.get("root_cause", "")
        lessons = result.get("lessons_learned", [])

        parts = []
        if root_cause:
            parts.append(f"根本原因: {root_cause}")
        if lessons:
            parts.append(f"经验教训: {', '.join(lessons)}")
        if improved_description:
            parts.append(f"改进方案: {improved_description}")

        if parts:
            return "\n".join(parts)

    return f"\n需要改进的问题: {issues if issues else '无特定问题，请重新执行'}"
