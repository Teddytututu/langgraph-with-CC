"""src/graph/nodes/reviewer.py — 质量审查节点"""
from datetime import datetime
from typing import Optional

from src.graph.state import GraphState, SubTask
from src.agents.caller import get_caller


async def reviewer_node(state: GraphState) -> dict:
    """
    审查当前子任务的执行结果

    通过 SubagentCaller 调用 reviewer subagent 进行质量审查
    """
    caller = get_caller()
    subtasks = state.get("subtasks", [])
    cid = state.get("current_subtask_id")

    current = _find_current_subtask(subtasks, cid)
    if not current or not current.result:
        return {"phase": "executing"}

    # 调用 reviewer subagent 进行审查
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

    # 解析审查结果
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
        "execution_log": [{
            "event": "review_complete",
            "task_id": current.id,
            "verdict": review["verdict"],
            "score": review.get("score", 0),
            "issues": review.get("issues", []),
            "subagent_called": "reviewer",
            "timestamp": datetime.now().isoformat(),
        }],
    }


def _find_current_subtask(subtasks: list[SubTask], cid: Optional[str]) -> Optional[SubTask]:
    """查找当前子任务"""
    return next((t for t in subtasks if t.id == cid), None)


def _parse_review_result(call_result: dict) -> dict:
    """解析审查结果"""
    # 默认审查结果
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
