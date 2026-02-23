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
7. 为每个子任务列出所需知识领域（knowledge_domains），如 ["python", "async", "database"]

## 输出格式
返回严格的 JSON 数组，每个元素包含：
{"id": "task-001", "title": "简短标题",
 "description": "详细描述，包含具体要求和验收标准",
 "agent_type": "coder",
 "dependencies": [], "priority": 1,
 "estimated_minutes": 10,
 "knowledge_domains": ["domain1", "domain2"],
 "completion_criteria": ["标准1", "标准2"]}
"""


async def planner_node(state: GraphState) -> dict:
    """
    分解用户任务为子任务 DAG

    通过 SubagentCaller 调用 planner subagent 执行任务分解
    """
    config = get_config()
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

    # 直接调用 planner subagent
    call_result = await caller.call_planner(
        task=user_task,
        time_budget=time_budget_info
    )

    # 检查执行是否成功
    if not call_result.get("success"):
        raise RuntimeError(f"Planner 执行失败: {call_result.get('error')}")

    # 解析子任务
    subtasks = _parse_subtasks_from_result(call_result.get("result"), budget)

    # 如果 subagent 未返回有效结果，生成标准四阶段子任务
    if not subtasks:
        base_mins = budget.total_minutes if budget else 60
        task_preview = user_task[:200]
        subtasks = [
            SubTask(
                id="task-001",
                title="需求分析与方案规划",
                description=f"深入理解任务目标，分析关键约束与风险，制定可行的实施方案。\n原始任务：{task_preview}",
                agent_type="analyst",
                dependencies=[],
                priority=1,
                estimated_minutes=max(5.0, base_mins * 0.15),
                knowledge_domains=["analysis", "planning"],
                completion_criteria=["明确了任务目标与范围", "制定了分步实施方案", "识别了主要风险"],
            ),
            SubTask(
                id="task-002",
                title="核心内容实现",
                description=f"按照方案完成核心功能/内容的实现，确保满足主要需求。\n原始任务：{task_preview}",
                agent_type="coder",
                dependencies=["task-001"],
                priority=2,
                estimated_minutes=max(10.0, base_mins * 0.50),
                knowledge_domains=["implementation"],
                completion_criteria=["完成了所有核心功能", "代码/内容质量达标"],
            ),
            SubTask(
                id="task-003",
                title="质量验证与问题修复",
                description="对实现成果进行全面质量验证，识别并修复存在的问题，确保满足验收标准。",
                agent_type="analyst",
                dependencies=["task-002"],
                priority=3,
                estimated_minutes=max(5.0, base_mins * 0.20),
                knowledge_domains=["testing", "review"],
                completion_criteria=["通过了功能验证", "关键缺陷已修复", "满足验收标准"],
            ),
            SubTask(
                id="task-004",
                title="成果整理与交付输出",
                description="整理所有阶段成果，撰写说明文档，提供完整清晰的最终交付物。",
                agent_type="writer",
                dependencies=["task-003"],
                priority=4,
                estimated_minutes=max(5.0, base_mins * 0.15),
                knowledge_domains=["documentation", "writing"],
                completion_criteria=["交付物完整且清晰", "包含使用说明"],
            ),
        ]

    return {
        "subtasks": subtasks,
        "phase": "budgeting",
        "execution_log": [{
            "event": "planning_complete",
            "timestamp": datetime.now().isoformat(),
            "subtask_count": len(subtasks),
            "subagent_called": "planner",
        }],
    }


def _parse_subtasks_from_result(result_data, budget) -> list[SubTask]:
    """从 subagent 结果中解析子任务"""
    import re as _re
    subtasks = []

    # SDK 可能返回字符串（含 JSON）或列表
    if isinstance(result_data, str):
        # 先尝试去掉 markdown 代码块包装
        cleaned = _re.sub(r'^```(?:json)?\s*', '', result_data.strip(), flags=_re.MULTILINE)
        cleaned = _re.sub(r'```\s*$', '', cleaned.strip(), flags=_re.MULTILINE)
        # 尝试直接解析整个字符串作为 JSON
        try:
            parsed = json.loads(cleaned)
            if isinstance(parsed, list):
                result_data = parsed
            elif isinstance(parsed, dict) and 'subtasks' in parsed:
                result_data = parsed['subtasks']
            elif isinstance(parsed, dict) and 'tasks' in parsed:
                result_data = parsed['tasks']
            else:
                result_data = []
        except json.JSONDecodeError:
            # 从字符串中提取 JSON 数组
            match = _re.search(r'\[.*?\]', cleaned, _re.DOTALL)
            if match:
                try:
                    result_data = json.loads(match.group(0))
                except json.JSONDecodeError:
                    result_data = []
            else:
                result_data = []

    if result_data and isinstance(result_data, list):
        for task_data in result_data:
            if not isinstance(task_data, dict):
                continue
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
