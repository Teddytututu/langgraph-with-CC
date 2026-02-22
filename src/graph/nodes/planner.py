"""src/graph/nodes/planner.py — 任务分解节点"""
import json
from datetime import datetime
from src.graph.state import GraphState, SubTask
from src.utils.config import get_config
from src.agents.caller import get_caller

PLANNER_SYSTEM_PROMPT = """
你是一个任务规划专家。你的职责是将用户的复杂任务分解为可执行的子任务。

## 规则
1. 每个子任务必须是一个 Agent 可以独立完成的原子操作
2. 明确标注子任务之间的依赖关系（哪些必须先完成）
3. 为每个子任务指定最合适的 Agent 类型：
   - coder: 编写/修改代码、脚本
   - researcher: 搜索信息、阅读文档、调研
   - writer: 撰写文档、报告、文案
   - analyst: 数据分析、逻辑推理、方案对比
4. 估算每个子任务的耗时（分钟）
5. 子任务数量控制在 3~10 个，不要过度拆分
6. 必须考虑用户给定的时间预算，合理分配

## 输出格式
返回严格的 JSON 数组，每个元素包含：
{"id": "task-001", "title": "简短标题",
 "description": "详细描述，包含具体要求和验收标准",
 "agent_type": "coder",
 "dependencies": [], "priority": 1,
 "estimated_minutes": 10}
"""


async def planner_node(state: GraphState) -> dict:
    """
    分解用户任务为子任务 DAG

    通过 SubagentCaller 调用 planner subagent 执行任务分解
    SDK 模式下同步执行，降级模式下需要等待
    """
    config = get_config()
    caller = get_caller()

    budget = state.get("time_budget")
    user_task = state["user_task"]

    # 🆕 降级模式：检查是否有等待中的调用
    pending_id = state.get("pending_call_id")
    if pending_id and caller.mode == "fallback":
        result_info = caller.check_result(pending_id)

        if result_info.get("completed"):
            # 有结果了，解析并返回
            subtasks = _parse_subtasks_from_result(result_info.get("result"), budget)
            return {
                "subtasks": subtasks,
                "phase": "budgeting",
                "pending_call_id": None,
                "waiting_for_subagent": False,
                "pending_agent_type": None,
                "execution_log": [{
                    "event": "planning_complete",
                    "timestamp": datetime.now().isoformat(),
                    "subtask_count": len(subtasks),
                    "subagent_called": "planner",
                    "call_id": pending_id,
                    "mode": "fallback",
                }],
            }
        else:
            # 还在等待
            return {
                "waiting_for_subagent": True,
                "phase": "waiting",
            }

    # 构建时间预算信息
    time_budget_info = None
    if budget:
        time_budget_info = {
            "total_minutes": budget.total_minutes,
            "remaining_minutes": budget.remaining_minutes,
        }

    # 调用 planner subagent（SDK 模式下同步执行）
    call_result = await caller.call_planner(
        task=user_task,
        time_budget=time_budget_info
    )

    # 🆕 降级模式：检查是否需要等待外部执行
    if call_result.get("status") == "pending_execution":
        return {
            "pending_call_id": call_result["call_id"],
            "waiting_for_subagent": True,
            "pending_agent_type": "planner",
            "phase": "waiting",
            "execution_log": [{
                "event": "planning_call_created",
                "timestamp": datetime.now().isoformat(),
                "call_id": call_result["call_id"],
                "agent_id": "planner",
                "mode": "fallback",
            }],
        }

    # SDK 模式：直接获取结果
    subtasks = _parse_subtasks_from_result(call_result.get("result"), budget)

    # 如果 subagent 未返回有效结果，创建默认子任务
    if not subtasks:
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
            )
        ]

    return {
        "subtasks": subtasks,
        "phase": "budgeting",
        "pending_call_id": None,
        "waiting_for_subagent": False,
        "pending_agent_type": None,
        "execution_log": [{
            "event": "planning_complete",
            "timestamp": datetime.now().isoformat(),
            "subtask_count": len(subtasks),
            "subagent_called": "planner",
            "mode": call_result.get("mode", "sdk"),
        }],
    }


def _parse_subtasks_from_result(result_data, budget) -> list[SubTask]:
    """从 subagent 结果中解析子任务"""
    subtasks = []

    if result_data and isinstance(result_data, list):
        for task_data in result_data:
            subtasks.append(SubTask(
                id=task_data.get("id", f"task-{len(subtasks)+1:03d}"),
                title=task_data.get("title", "未命名任务"),
                description=task_data.get("description", ""),
                agent_type=task_data.get("agent_type", "coder"),
                dependencies=task_data.get("dependencies", []),
                priority=task_data.get("priority", 1),
                estimated_minutes=task_data.get("estimated_minutes", 10),
                knowledge_domains=task_data.get("knowledge_domains", []),
                completion_criteria=task_data.get("completion_criteria", []),
            ))

    return subtasks
