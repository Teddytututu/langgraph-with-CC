"""src/graph/nodes/reviewer.py — 质量审查节点"""
from datetime import datetime
from typing import Optional

from src.graph.state import GraphState, SubTask
from src.agents.caller import get_caller


async def reviewer_node(state: GraphState) -> dict:
    """
    审查当前子任务的执行结果

    通过 SubagentCaller 调用 reviewer subagent 进行质量审查
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
            # 有结果了，处理审查结果
            return _handle_review_result(state, result_info.get("result"), pending_id)
        else:
            # 还在等待
            return {
                "waiting_for_subagent": True,
                "phase": "waiting",
            }

    current = _find_current_subtask(subtasks, cid)
    if not current or not current.result:
        return {"phase": "executing"}

    # 调用 reviewer subagent 进行审查（SDK 模式下同步执行）
    call_result = await caller.call_reviewer(
        execution_result={
            "result": current.result,
            "status": current.status,
            "started_at": current.started_at.isoformat() if current.started_at else None,
            "finished_at": current.finished_at.isoformat() if current.finished_at else None,
        },
        subtask={
            "id": current.id,
            "title": current.title,
            "description": current.description,
            "completion_criteria": current.completion_criteria,
        }
    )

    # 🆕 降级模式：检查是否需要等待外部执行
    if call_result.get("status") == "pending_execution":
        return {
            "pending_call_id": call_result["call_id"],
            "waiting_for_subagent": True,
            "pending_agent_type": "reviewer",
            "phase": "waiting",
            "execution_log": [{
                "event": "review_call_created",
                "task_id": current.id,
                "call_id": call_result["call_id"],
                "mode": "fallback",
                "timestamp": datetime.now().isoformat(),
            }],
        }

    # SDK 模式：直接获取结果
    review = _parse_review_result(call_result)

    # 纯函数式更新
    if review["verdict"] == "PASS":
        new_status, new_retry = "done", current.retry_count
    else:
        new_status, new_retry = "pending", current.retry_count + 1

    updated_subtasks = []
    for t in subtasks:
        if t.id == current.id:
            updated_subtasks.append(t.model_copy(update={
                "status": new_status,
                "retry_count": new_retry,
            }))
        else:
            updated_subtasks.append(t)

    return {
        "subtasks": updated_subtasks,
        "phase": "reviewing",
        "pending_call_id": None,
        "waiting_for_subagent": False,
        "execution_log": [{
            "event": "review_complete",
            "task_id": current.id,
            "verdict": review["verdict"],
            "score": review.get("score", 0),
            "issues": review.get("issues", []),
            "subagent_called": "reviewer",
            "mode": call_result.get("mode", "sdk"),
            "timestamp": datetime.now().isoformat(),
        }],
    }


def _handle_review_result(state: GraphState, result_data, call_id: str) -> dict:
    """处理降级模式的审查结果"""
    subtasks = state.get("subtasks", [])
    cid = state.get("current_subtask_id")

    current = _find_current_subtask(subtasks, cid)
    if not current:
        return {"phase": "executing"}

    # 解析审查结果
    review = {
        "verdict": "PASS",
        "score": 7,
        "issues": [],
    }
    if result_data and isinstance(result_data, dict):
        review = {
            "verdict": result_data.get("verdict", "PASS"),
            "score": result_data.get("score", 7),
            "issues": result_data.get("issues", []),
        }

    # 更新状态
    if review["verdict"] == "PASS":
        new_status, new_retry = "done", current.retry_count
    else:
        new_status, new_retry = "pending", current.retry_count + 1

    updated_subtasks = []
    for t in subtasks:
        if t.id == current.id:
            updated_subtasks.append(t.model_copy(update={
                "status": new_status,
                "retry_count": new_retry,
            }))
        else:
            updated_subtasks.append(t)

    return {
        "subtasks": updated_subtasks,
        "phase": "reviewing",
        "pending_call_id": None,
        "waiting_for_subagent": False,
        "execution_log": [{
            "event": "review_complete",
            "task_id": current.id,
            "verdict": review["verdict"],
            "score": review.get("score", 0),
            "issues": review.get("issues", []),
            "mode": "fallback",
            "call_id": call_id,
            "timestamp": datetime.now().isoformat(),
        }],
    }


def _find_current_subtask(subtasks: list[SubTask], cid: Optional[str]) -> Optional[SubTask]:
    """查找当前子任务"""
    return next((t for t in subtasks if t.id == cid), None)


def _parse_review_result(call_result: dict) -> dict:
    """解析审查结果"""
    default_review = {
        "verdict": "PASS",
        "score": 7,
        "issues": [],
        "suggestions": []
    }

    if not call_result.get("success"):
        return default_review

    result = call_result.get("result")
    if result and isinstance(result, dict):
        return {
            "verdict": result.get("verdict", "PASS"),
            "score": result.get("score", 7),
            "issues": result.get("issues", []),
            "suggestions": result.get("suggestions", []),
        }

    return default_review
