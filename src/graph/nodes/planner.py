"""src/graph/nodes/planner.py — 任务分解节点"""
import json
from datetime import datetime
from src.graph.state import GraphState, SubTask
from src.utils.config import get_config

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
    """分解用户任务为子任务 DAG"""
    config = get_config()

    budget = state.get("time_budget")
    user_task = state["user_task"]
    time_info = ""
    if budget:
        time_info = (
            f"\n用户给定的总时间预算：{budget.total_minutes} 分钟。"
            f"请确保所有子任务的预估总耗时不超过此预算的 80%（留 20% 作为审查缓冲）。"
        )

    # TODO: 调用 LLM 分解任务
    # 目前返回一个简单的默认子任务
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
        "execution_log": [{
            "event": "planning_complete",
            "timestamp": datetime.now().isoformat(),
            "subtask_count": len(subtasks),
        }],
    }
