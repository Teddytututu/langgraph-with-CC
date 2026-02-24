"""
Planner V2 — 多 Agent 协作规划节点

实现多专家并行规划 + 方案讨论合并
- 并行调用多个 planner subagent
- 通过 DiscussionManager 讨论
- 合并方案生成最终 DAG
"""

import asyncio
import json
from datetime import datetime
from typing import Optional

from src.graph.state import GraphState, SubTask
from src.agents.caller import get_caller
from src.discussion.manager import discussion_manager


# 多规划专家数量
PLANNER_COUNT = 3


async def planner_v2_node(state: GraphState) -> dict:
    """
    多 Agent 协作规划节点

    流程:
    1. 并行调用多个 planner subagent 独立规划
    2. 将各方案发送到 DiscussionManager 讨论
    3. 等待共识或超时后合并方案
    4. 生成最终子任务 DAG
    """
    caller = get_caller()
    budget = state.get("time_budget")
    user_task = state["user_task"]

    # 构建时间预算信息
    time_budget_info = None
    if budget:
        time_budget_info = {
            "total_minutes": budget.total_minutes,
            "remaining_minutes": budget.remaining_minutes,
        }

    # === 阶段1: 并行规划 ===
    plans = await _parallel_planning(caller, user_task, time_budget_info)

    if not plans:
        # 所有规划都失败，使用默认方案
        return _create_fallback_result(state, user_task, budget)

    # === 阶段2: 讨论合并 ===
    discussion_id = f"planning_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    # 将各方案发送到讨论库
    await _submit_plans_for_discussion(discussion_id, plans)

    # 等待共识（带超时）
    consensus = await _wait_for_consensus(discussion_id)

    # === 阶段3: 合并方案 ===
    final_subtasks = _merge_plans(plans, consensus, budget)

    return {
        "subtasks": final_subtasks,
        "phase": "budgeting",
        "execution_log": [{
            "event": "multi_planning_complete",
            "timestamp": datetime.now().isoformat(),
            "planner_count": len(plans),
            "discussion_id": discussion_id,
            "consensus_reached": consensus.get("status") == "consensus_reached",
            "final_subtask_count": len(final_subtasks),
        }],
    }


async def _parallel_planning(caller, task: str, time_budget: dict) -> list[dict]:
    """
    并行调用多个 planner subagent 进行独立规划

    Returns:
        成功的规划结果列表
    """
    async def plan_with_planner(planner_id: str) -> Optional[dict]:
        context = {
            "task": task,
            "time_budget": time_budget,
            "planner_id": planner_id,
        }
        result = await caller.call(planner_id, context)
        if result.get("success"):
            return _parse_plan_result(result.get("result"))
        return None

    # 获取可用的 planner agent
    planner_ids = _get_available_planners(caller)

    # 超时已禁用：让任务自然完成
    results = await asyncio.gather(*[plan_with_planner(pid) for pid in planner_ids])

    # 过滤空结果
    return [r for r in results if r]


def _get_available_planners(caller) -> list[str]:
    """获取可用的 planner agent ID 列表"""
    # 优先使用专用 planner，其次使用通用槽位
    planners = []

    # 尝试使用 planner_1, planner_2, planner_3
    for i in range(1, PLANNER_COUNT + 1):
        planner_id = f"planner_{i}"
        # 检查是否存在该模板
        from src.agents.pool_registry import get_pool
        pool = get_pool()
        if pool.get_template(planner_id):
            planners.append(planner_id)

    # 如果没有专用 planner，使用主 planner 生成多个变体
    if not planners:
        planners = ["planner"]  # 降级为单一 planner

    return planners


def _parse_plan_result(result_data) -> Optional[list[dict]]:
    """从 subagent 结果中解析规划方案"""
    import re

    if isinstance(result_data, str):
        match = re.search(r'\[.*\]', result_data, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                return None
        return None

    if isinstance(result_data, list):
        return result_data

    return None


async def _submit_plans_for_discussion(discussion_id: str, plans: list[dict]):
    """
    将各规划方案提交到 DiscussionManager 讨论

    每个方案作为一个独立的"观点"提交
    """
    discussion_manager.create_discussion(discussion_id)

    for i, plan in enumerate(plans):
        # 提取方案摘要
        summary = _summarize_plan(plan, i + 1)

        await discussion_manager.post_message(
            node_id=discussion_id,
            from_agent=f"planner_{i + 1}",
            content=json.dumps(summary, ensure_ascii=False, indent=2),
            message_type="proposal",
        )

    # 请求共识
    await discussion_manager.request_consensus(
        node_id=discussion_id,
        from_agent="planner_coordinator",
        topic="选择最优规划方案并合并",
    )


def _summarize_plan(plan: list[dict], plan_index: int) -> dict:
    """生成规划方案摘要"""
    return {
        "plan_id": f"plan_{plan_index}",
        "task_count": len(plan),
        "tasks": [
            {
                "id": t.get("id"),
                "title": t.get("title"),
                "agent_type": t.get("agent_type"),
                "estimated_minutes": t.get("estimated_minutes"),
            }
            for t in plan[:5]  # 只显示前5个
        ],
        "total_estimated_minutes": sum(t.get("estimated_minutes", 0) for t in plan),
        "dependency_depth": _calculate_dependency_depth(plan),
    }


def _calculate_dependency_depth(plan: list[dict]) -> int:
    """计算依赖深度（用于评估规划的并行化程度）"""
    if not plan:
        return 0

    # 简化：计算最大依赖链长度
    task_deps = {t.get("id"): t.get("dependencies", []) for t in plan}

    def get_depth(task_id, visited=None):
        if visited is None:
            visited = set()
        if task_id in visited:
            return 0
        visited.add(task_id)

        deps = task_deps.get(task_id, [])
        if not deps:
            return 1
        return 1 + max(get_depth(d, visited.copy()) for d in deps)

    return max(get_depth(t.get("id")) for t in plan) if plan else 0


async def _wait_for_consensus(discussion_id: str) -> dict:
    """
    等待规划共识

    Returns:
        共识结果
    """
    discussion = discussion_manager.get_discussion(discussion_id)
    if not discussion:
        return {"status": "no_discussion"}

    # 模拟共识（实际应通过多轮讨论）
    # 这里简化为：选择第一个方案作为基础
    proposals = [
        msg for msg in discussion.messages
        if msg.message_type == "proposal"
    ]

    if not proposals:
        return {"status": "no_proposals"}

    # 在实际系统中，这里应该：
    # 1. 让多个 reviewer agent 对各方案评分
    # 2. 通过投票或协商选择最优方案
    # 3. 或合并多个方案的优点

    # 简化实现：自动确认共识
    for proposer in set(p.from_agent for p in proposals):
        await discussion_manager.confirm_consensus(
            node_id=discussion_id,
            from_agent=proposer,
        )

    return {
        "status": "consensus_reached",
        "selected_plan_index": 0,  # 选择第一个方案
        "proposals_count": len(proposals),
    }


def _merge_plans(plans: list[list[dict]], consensus: dict, budget) -> list[SubTask]:
    """
    合并多个规划方案

    策略：
    1. 如果达成共识，使用选中的方案
    2. 否则，合并所有方案的优点
    """
    if not plans:
        return []

    # 使用共识选择的方案或第一个方案
    selected_index = consensus.get("selected_plan_index", 0)
    if selected_index < len(plans):
        base_plan = plans[selected_index]
    else:
        base_plan = plans[0]

    # 转换为 SubTask 对象
    subtasks = []
    for task_data in base_plan:
        if not isinstance(task_data, dict):
            continue

        subtask = SubTask(
            id=task_data.get("id", f"task-{len(subtasks)+1:03d}"),
            title=task_data.get("title", "未命名任务"),
            description=task_data.get("description", ""),
            agent_type=task_data.get("agent_type", "coder"),
            dependencies=task_data.get("dependencies", []),
            priority=task_data.get("priority", 1),
            estimated_minutes=task_data.get("estimated_minutes", 10),
            knowledge_domains=task_data.get("knowledge_domains", []),
            completion_criteria=task_data.get("completion_criteria", []),
        )
        subtasks.append(subtask)

    # 尝试从其他方案补充遗漏的任务
    subtasks = _enrich_with_other_plans(subtasks, plans, selected_index, budget)

    return subtasks


def _enrich_with_other_plans(
    base_subtasks: list[SubTask],
    all_plans: list[list[dict]],
    selected_index: int,
    budget
) -> list[SubTask]:
    """
    用其他方案的优点丰富基础方案

    策略：检查其他方案中是否有基础方案遗漏的重要任务
    """
    base_task_ids = {t.id for t in base_subtasks}
    enriched = list(base_subtasks)

    for i, plan in enumerate(all_plans):
        if i == selected_index:
            continue

        for task in plan:
            task_id = task.get("id", "")
            # 如果该任务在基础方案中不存在，且是高优先级，考虑添加
            if task_id and task_id not in base_task_ids:
                if task.get("priority", 1) <= 2:  # 高优先级
                    new_task = SubTask(
                        id=f"{task_id}_alt",
                        title=f"[补充] {task.get('title', '')}",
                        description=task.get("description", "从备选方案补充"),
                        agent_type=task.get("agent_type", "coder"),
                        dependencies=task.get("dependencies", []),
                        priority=task.get("priority", 3) + 1,  # 降低优先级
                        estimated_minutes=task.get("estimated_minutes", 5),
                        knowledge_domains=task.get("knowledge_domains", []),
                        completion_criteria=task.get("completion_criteria", []),
                    )
                    enriched.append(new_task)
                    base_task_ids.add(f"{task_id}_alt")

    return enriched


def _create_fallback_result(state: GraphState, user_task: str, budget) -> dict:
    """创建默认回退结果"""
    subtasks = [
        SubTask(
            id="task-001",
            title="执行完整任务",
            description=user_task,
            agent_type="coder",
            estimated_minutes=(
                budget.total_minutes * 0.8
                if budget else 30
            ),
            knowledge_domains=["general"],
            completion_criteria=["任务已完成"],
        )
    ]

    return {
        "subtasks": subtasks,
        "phase": "budgeting",
        "execution_log": [{
            "event": "planning_fallback",
            "timestamp": datetime.now().isoformat(),
            "reason": "所有规划器执行失败，使用默认方案",
        }],
    }
