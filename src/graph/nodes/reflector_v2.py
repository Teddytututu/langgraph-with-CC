"""
Reflector V2 — 多角度反思节点

实现多角度反思 + 讨论式改进
- 从技术、流程、资源三个角度分析失败
- 通过 DiscussionManager 协商
- 形成综合改进方案
"""

import asyncio
import json
from datetime import datetime
from typing import Optional

from src.graph.state import GraphState, SubTask
from src.agents.caller import get_caller
from src.discussion.manager import discussion_manager


# 反思超时（秒）
REFLECTION_TIMEOUT = 90


# 反思视角定义
REFLECTION_PERSPECTIVES = {
    "technical": {
        "name": "技术反思者",
        "focus": "代码质量、架构合理性、技术选型",
        "questions": [
            "代码是否存在明显 bug 或逻辑错误？",
            "架构设计是否合理？",
            "是否使用了正确的技术方案？",
            "是否有性能或安全问题？",
        ],
    },
    "process": {
        "name": "流程反思者",
        "focus": "执行步骤、时间分配、依赖管理",
        "questions": [
            "执行步骤是否遗漏或顺序错误？",
            "时间分配是否合理？",
            "依赖任务是否正确完成？",
            "是否缺少必要的验证步骤？",
        ],
    },
    "resource": {
        "name": "资源反思者",
        "focus": "信息充分性、工具可用性、环境配置",
        "questions": [
            "是否有足够的信息完成任务？",
            "工具和环境是否正确配置？",
            "是否缺少必要的依赖或资源？",
            "文档和参考资料是否充分？",
        ],
    },
}


async def reflector_v2_node(state: GraphState) -> dict:
    """
    多角度反思节点

    流程:
    1. 从技术、流程、资源三个角度并行分析
    2. 在 DiscussionManager 中讨论
    3. 共识形成综合改进方案
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

    # === 阶段1: 多角度并行反思 ===
    reflections = await _parallel_reflection(caller, current, issues)

    if not reflections:
        # 所有反思都失败，使用简单改进
        return _create_simple_improvement(state, current, issues)

    # === 阶段2: 讨论协商 ===
    discussion_id = f"reflection_{current.id}_{datetime.now().strftime('%H%M%S')}"

    await _submit_reflections_for_discussion(discussion_id, reflections, current)

    # === 阶段3: 等待共识 ===
    consensus = await _wait_for_consensus(discussion_id, timeout=45.0)

    # === 阶段4: 合成改进方案 ===
    improvement = _synthesize_improvement(reflections, consensus, issues)

    # 纯函数式更新
    new_description = (
        current.description
        + f"\n\n--- 第 {current.retry_count + 1} 次多角度反思改进 ---\n"
        + improvement
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

    # 保持 task/session 作用域，不再写回全局专家模板

    return {
        "subtasks": updated_subtasks,
        "phase": "executing",
        "execution_log": [{
            "event": "multi_reflection_complete",
            "task_id": current.id,
            "retry_count": current.retry_count,
            "perspectives_used": list(reflections.keys()),
            "discussion_id": discussion_id,
            "consensus_reached": consensus.get("status") == "consensus_reached",
            "timestamp": datetime.now().isoformat(),
        }],
    }


async def _parallel_reflection(
    caller,
    task: SubTask,
    issues: list[str]
) -> dict[str, dict]:
    """
    从多个角度并行反思

    Returns:
        视角 -> 反思结果的映射
    """
    async def reflect_from_perspective(perspective: str, config: dict) -> Optional[dict]:
        # 构建视角专属提示
        context = {
            "failure_context": {
                "issues": issues,
                "original_description": task.description,
                "retry_count": task.retry_count,
                "last_result": task.result,
            },
            "subtask": {
                "id": task.id,
                "title": task.title,
                "description": task.description,
                "agent_type": task.agent_type,
            },
            "perspective": {
                "name": config["name"],
                "focus": config["focus"],
                "questions": config["questions"],
            },
        }

        # 尝试使用视角专属的 reflector
        perspective_agent = f"reflector_{perspective}"
        from src.agents.pool_registry import get_pool
        pool = get_pool()

        if pool.get_template(perspective_agent):
            agent_id = perspective_agent
        else:
            agent_id = "reflector"  # 降级为通用 reflector

        result = await caller.call(agent_id, context)
        if result.get("success"):
            return _parse_reflection_result(result.get("result"))
        return None

    # 并行执行三个视角的反思
    tasks = [
        reflect_from_perspective(p, c)
        for p, c in REFLECTION_PERSPECTIVES.items()
    ]

    try:
        results = await asyncio.wait_for(
            asyncio.gather(*tasks),
            timeout=REFLECTION_TIMEOUT
        )
    except asyncio.TimeoutError:
        results = []

    # 组织结果
    reflections = {}
    for i, (perspective, _) in enumerate(REFLECTION_PERSPECTIVES.items()):
        if i < len(results) and results[i]:
            reflections[perspective] = results[i]

    return reflections


def _parse_reflection_result(result_data) -> Optional[dict]:
    """解析反思结果"""
    import re

    if isinstance(result_data, str):
        match = re.search(r'\{.*\}', result_data, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group(0))
            except json.JSONDecodeError:
                return None
        else:
            # 无法解析为 JSON，直接作为文本返回
            return {
                "root_cause": result_data[:500],
                "lessons_learned": [],
                "improved_description": "",
            }
    elif isinstance(result_data, dict):
        data = result_data
    else:
        return None

    return {
        "root_cause": data.get("root_cause", ""),
        "lessons_learned": data.get("lessons_learned", []),
        "improved_description": data.get("improved_description", ""),
        "prevention_measures": data.get("prevention_measures", []),
    }


async def _submit_reflections_for_discussion(
    discussion_id: str,
    reflections: dict[str, dict],
    task: SubTask
):
    """
    将各视角的反思提交到 DiscussionManager 讨论
    """
    discussion_manager.create_discussion(discussion_id)

    for perspective, reflection in reflections.items():
        config = REFLECTION_PERSPECTIVES.get(perspective, {})
        agent_name = config.get("name", perspective)

        await discussion_manager.post_message(
            node_id=discussion_id,
            from_agent=f"reflector_{perspective}",
            content=json.dumps({
                "perspective": agent_name,
                "root_cause": reflection.get("root_cause"),
                "lessons": reflection.get("lessons_learned", [])[:3],
                "suggested_improvement": reflection.get("improved_description", "")[:200],
            }, ensure_ascii=False),
            message_type="reflection",
        )

    # 请求共识
    await discussion_manager.request_consensus(
        node_id=discussion_id,
        from_agent="reflection_coordinator",
        topic="综合各视角分析，形成改进方案",
    )


async def _wait_for_consensus(discussion_id: str, timeout: float = 45.0) -> dict:
    """等待反思共识"""
    discussion = discussion_manager.get_discussion(discussion_id)

    if not discussion:
        return {"status": "no_discussion"}

    # 模拟共识达成（实际应通过多轮讨论）
    # 让各参与者确认共识
    participants = list(set(m.from_agent for m in discussion.messages))

    for participant in participants:
        try:
            await discussion_manager.confirm_consensus(
                node_id=discussion_id,
                from_agent=participant,
            )
        except Exception:
            pass  # 忽略确认失败

    return {
        "status": "consensus_reached",
        "participants": participants,
    }


def _synthesize_improvement(
    reflections: dict[str, dict],
    consensus: dict,
    issues: list[str]
) -> str:
    """
    合成综合改进方案

    将各视角的反思合并为完整的改进描述
    """
    parts = []

    # 收集所有根本原因
    root_causes = []
    for perspective, reflection in reflections.items():
        rc = reflection.get("root_cause")
        if rc:
            config = REFLECTION_PERSPECTIVES.get(perspective, {})
            root_causes.append(f"[{config.get('name', perspective)}] {rc}")

    if root_causes:
        parts.append("## 根本原因分析\n" + "\n".join(f"- {rc}" for rc in root_causes))

    # 收集所有经验教训
    all_lessons = []
    for reflection in reflections.values():
        all_lessons.extend(reflection.get("lessons_learned", []))

    if all_lessons:
        parts.append("## 经验教训\n" + "\n".join(f"- {l}" for l in all_lessons[:5]))

    # 收集预防措施
    all_measures = []
    for reflection in reflections.values():
        all_measures.extend(reflection.get("prevention_measures", []))

    if all_measures:
        parts.append("## 预防措施\n" + "\n".join(f"- {m}" for m in all_measures[:5]))

    # 选择最佳的改进描述
    best_improvement = ""
    best_length = 0
    for reflection in reflections.values():
        imp = reflection.get("improved_description", "")
        if len(imp) > best_length:
            best_improvement = imp
            best_length = len(imp)

    if best_improvement:
        parts.append("## 改进方案\n" + best_improvement)

    # 如果有问题列表，添加到改进方案中
    if issues:
        parts.append("## 需要解决的问题\n" + "\n".join(f"- {i}" for i in issues[:5]))

    return "\n\n".join(parts)


def _find_current_subtask(subtasks: list[SubTask], cid: Optional[str]) -> Optional[SubTask]:
    """查找当前子任务"""
    return next((t for t in subtasks if t.id == cid), None)


def _get_last_review(state: GraphState, task_id: str) -> Optional[dict]:
    """获取指定任务的最近审查反馈"""
    return next(
        (log for log in reversed(state.get("execution_log", []))
         if log.get("event") in ("review_complete", "multi_review_complete")
         and log.get("task_id") == task_id),
        None,
    )


def _create_simple_improvement(state: GraphState, task: SubTask, issues: list[str]) -> dict:
    """创建简单改进结果（当所有反思都失败时）"""
    improvement = f"\n需要改进的问题: {issues if issues else '无特定问题，请重新执行'}"

    new_description = task.description + f"\n\n--- 第 {task.retry_count + 1} 次反思改进 ---\n" + improvement

    subtasks = state.get("subtasks", [])
    updated_subtasks = []
    for t in subtasks:
        if t.id == task.id:
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
            "event": "reflection_fallback",
            "task_id": task.id,
            "timestamp": datetime.now().isoformat(),
        }],
    }


