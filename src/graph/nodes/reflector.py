"""src/graph/nodes/reflector.py — 反思重试节点"""
from src.graph.state import GraphState


REFLECTOR_PROMPT = """
你是一个任务改进专家。分析子任务失败的原因，并给出改进后的任务描述。

改进后的描述应该：
1. 更具体、更有针对性
2. 明确指出上次失败的问题和需要避免的陷阱
3. 提供更清晰的验收标准

只返回改进后的描述文本，不要其他内容。
"""


async def reflector_node(state: GraphState) -> dict:
    """分析失败原因，增强 prompt 后重新分配"""
    subtasks = state.get("subtasks", [])
    cid = state.get("current_subtask_id")
    current = next(
        (t for t in subtasks if t.id == cid),
        None,
    )

    if not current:
        return {"phase": "executing"}

    # 获取最近的审查反馈
    last_review = next(
        (log for log in reversed(state.get("execution_log", []))
         if log.get("event") == "review_complete"
         and log.get("task_id") == current.id),
        None,
    )
    issues = last_review.get("issues", []) if last_review else []

    # TODO: 调用 LLM 进行反思
    # 目前简单地在描述后追加反思信息
    reflection = f"\n\n需要改进的问题: {issues if issues else '无特定问题，请重新执行'}"

    # ✅ 纯函数式更新
    new_description = (
        current.description
        + f"\n\n--- 第 {current.retry_count} 次反思改进 ---\n"
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
        "execution_log": [{
            "event": "reflection_complete",
            "task_id": current.id,
            "retry_count": current.retry_count,
        }],
    }
