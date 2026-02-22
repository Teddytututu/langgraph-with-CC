"""src/graph/nodes/reflector.py — 反思重试节点"""
from datetime import datetime
from typing import Optional

from src.graph.state import GraphState, SubTask
from src.agents.caller import get_caller
from src.agents.pool_registry import get_pool


async def reflector_node(state: GraphState) -> dict:
    """
    分析失败原因，增强 prompt 后重新分配

    通过 SubagentCaller 调用 reflector subagent 进行反思改进
    """
    caller = get_caller()
    subtasks = state.get("subtasks", [])
    cid = state.get("current_subtask_id")

    current = _find_current_subtask(subtasks, cid)
    if not current:
        return {"phase": "executing"}

    # 获取最近的审查反馈
    last_review = _get_last_review(state, current.id)
    issues = last_review.get("issues", []) if last_review else []

    # 调用 reflector subagent 进行反思
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

    # 检查执行是否成功
    if not call_result.get("success"):
        raise RuntimeError(f"Reflector 执行失败: {call_result.get('error')}")

    # 解析反思结果
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

    # 同步更新专家 subagent 的 system_prompt，使其从失败中学习
    _update_specialist_prompts(current, reflection)

    return {
        "subtasks": updated_subtasks,
        "phase": "executing",
        "execution_log": [{
            "event": "reflection_complete",
            "task_id": current.id,
            "retry_count": current.retry_count,
            "subagent_called": "reflector",
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
    import json
    import re

    if not call_result.get("success"):
        return f"\n需要改进的问题: {issues if issues else '无特定问题，请重新执行'}"

    result = call_result.get("result")

    # SDK 可能返回字符串（含 JSON）
    if isinstance(result, str):
        match = re.search(r'\{.*\}', result, re.DOTALL)
        if match:
            try:
                result = json.loads(match.group(0))
            except json.JSONDecodeError:
                # 无法解析为 JSON，直接作为改进描述返回
                return f"\n改进建议:\n{result}"

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


def _update_specialist_prompts(task: SubTask, reflection: str) -> None:
    """
    将反思结果追加到关联专家的 system_prompt，使其从失败中学习

    Args:
        task: 刚完成反思的子任务
        reflection: 反思文本
    """
    if not task.assigned_agents:
        return

    pool = get_pool()
    note = (
        f"\n\n## 经验补丁（自动注入，来源于第 {task.retry_count + 1} 次反思）\n"
        f"任务: {task.title}\n"
        f"{reflection}\n"
        f"---\n"
    )

    for agent_id in task.assigned_agents:
        template = pool.get_template(agent_id)
        if template and template.content:
            updated_content = template.content + note
            pool.fill_agent(
                agent_id=agent_id,
                name=template.name or agent_id,
                description=template.description or "",
                content=updated_content,
                tools=template.tools or [],
            )
