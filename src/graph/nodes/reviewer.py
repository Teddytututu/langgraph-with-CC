"""src/graph/nodes/reviewer.py — 质量审查节点"""
import json
from src.graph.state import GraphState


REVIEWER_PROMPT = """
你是一个严格的质量审查员。请评估子任务的执行结果。

评估维度：
1. **完整性** — 是否覆盖了任务描述的所有要求
2. **正确性** — 结果是否准确无误
3. **质量** — 代码可读性/文档清晰度/分析深度

输出格式（只返回 JSON）：
{"verdict": "PASS" 或 "REVISE",
 "score": 1-10,
 "issues": ["问题1", "问题2"],
 "suggestions": ["建议1", "建议2"]}

评分指南：
- 8-10 分: PASS，质量优秀
- 6-7 分: PASS，可接受但有改进空间
- 1-5 分: REVISE，需要重做
"""


async def reviewer_node(state: GraphState) -> dict:
    """审查当前子任务的执行结果"""
    subtasks = state.get("subtasks", [])
    cid = state.get("current_subtask_id")
    current = next(
        (t for t in subtasks if t.id == cid),
        None,
    )

    if not current or not current.result:
        return {"phase": "executing"}

    # TODO: 调用 LLM 进行审查
    # 目前默认通过
    review = {"verdict": "PASS", "score": 7,
              "issues": [], "suggestions": []}

    # ✅ 纯函数式更新
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
        }],
    }
