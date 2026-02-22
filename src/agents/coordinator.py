"""
协调者 Agent

负责决定协作模式：chain（链式）、parallel（并行）、discussion（讨论式）
"""

from typing import Any, Optional
from pydantic import BaseModel

from .collaboration import CollaborationMode


class TaskAnalysis(BaseModel):
    """任务分析结果"""
    task_type: str = ""
    has_dependencies: bool = False
    requires_consensus: bool = False
    can_parallelize: bool = False
    suggested_mode: CollaborationMode = CollaborationMode.CHAIN
    reasoning: str = ""


class CoordinatorAgent:
    """
    协调者 Agent - 决定协作模式

    职责：
    1. 分析任务特点
    2. 选择最合适的协作模式
    3. 安排 agent 执行顺序
    """

    def __init__(self):
        pass

    def analyze_task(self, task: Any, subtasks: list[Any] = None) -> TaskAnalysis:
        """
        分析任务特点

        Args:
            task: 主任务
            subtasks: 子任务列表

        Returns:
            任务分析结果
        """
        # 基础分析框架
        analysis = TaskAnalysis()

        if subtasks is None:
            subtasks = []

        # 检查依赖关系
        analysis.has_dependencies = self._check_dependencies(subtasks)

        # 检查是否需要共识
        analysis.requires_consensus = self._check_consensus_need(task, subtasks)

        # 检查是否可并行化
        analysis.can_parallelize = not analysis.has_dependencies

        # 选择协作模式
        analysis.suggested_mode = self._choose_mode(analysis)

        return analysis

    def _check_dependencies(self, subtasks: list[Any]) -> bool:
        """检查子任务间是否有依赖关系"""
        for subtask in subtasks:
            if hasattr(subtask, 'dependencies') and subtask.dependencies:
                return True
            if isinstance(subtask, dict) and subtask.get('dependencies'):
                return True
        return False

    def _check_consensus_need(self, task: Any, subtasks: list[Any]) -> bool:
        """检查是否需要达成共识"""
        task_str = str(task).lower()

        # 关键词检测
        consensus_keywords = [
            '协商', '讨论', '共识', '同意', '投票',
            'discuss', 'consensus', 'agree', 'vote',
            '评审', 'review', '决策', 'decide'
        ]

        for keyword in consensus_keywords:
            if keyword in task_str:
                return True

        return False

    def _choose_mode(self, analysis: TaskAnalysis) -> CollaborationMode:
        """选择协作模式"""
        if analysis.requires_consensus:
            return CollaborationMode.DISCUSSION

        if analysis.has_dependencies:
            return CollaborationMode.CHAIN

        return CollaborationMode.PARALLEL

    def choose_collaboration_mode(
        self,
        task: Any,
        agents: list[str],
        subtasks: list[Any] = None
    ) -> CollaborationMode:
        """
        选择协作模式

        Args:
            task: 主任务
            agents: 参与的 agent 列表
            subtasks: 子任务列表

        Returns:
            推荐的协作模式
        """
        if len(agents) <= 1:
            # 单个 agent，默认链式
            return CollaborationMode.CHAIN

        analysis = self.analyze_task(task, subtasks)
        return analysis.suggested_mode

    def plan_execution(
        self,
        task: Any,
        agents: list[str],
        subtasks: list[Any] = None
    ) -> dict:
        """
        规划执行方案

        Args:
            task: 主任务
            agents: 参与的 agent 列表
            subtasks: 子任务列表

        Returns:
            执行计划
        """
        mode = self.choose_collaboration_mode(task, agents, subtasks)
        analysis = self.analyze_task(task, subtasks)

        plan = {
            "mode": mode.value,
            "agents": agents,
            "execution_order": [],
            "reasoning": analysis.reasoning or self._get_reasoning(mode)
        }

        if mode == CollaborationMode.CHAIN:
            plan["execution_order"] = self._plan_chain_order(agents, subtasks)
        elif mode == CollaborationMode.PARALLEL:
            plan["execution_order"] = agents  # 并行执行，顺序不重要
        elif mode == CollaborationMode.DISCUSSION:
            plan["execution_order"] = self._plan_discussion_order(agents)

        return plan

    def _plan_chain_order(self, agents: list[str], subtasks: list[Any]) -> list[str]:
        """规划链式执行顺序"""
        if not subtasks:
            return agents

        # 根据子任务依赖排序
        ordered = []
        remaining = list(agents)

        # 简化实现：保持原顺序
        # 实际应该根据依赖图拓扑排序
        return agents

    def _plan_discussion_order(self, agents: list[str]) -> list[str]:
        """规划讨论顺序"""
        # 讨论模式：先让所有 agent 发表意见
        return agents

    def _get_reasoning(self, mode: CollaborationMode) -> str:
        """获取模式选择的原因"""
        reasons = {
            CollaborationMode.CHAIN: "任务存在依赖关系，需要顺序执行",
            CollaborationMode.PARALLEL: "任务相互独立，可以并行执行提高效率",
            CollaborationMode.DISCUSSION: "任务需要协商达成共识，使用讨论模式"
        }
        return reasons.get(mode, "")


# 协调者的系统提示模板
COORDINATOR_SYSTEM_PROMPT = """你是一个任务协调者。

你的职责是：
1. 分析任务特点
2. 选择最合适的协作模式：
   - chain: 顺序依赖的任务，A → B → C
   - parallel: 可独立执行的任务，同时进行
   - discussion: 需要协商的任务，先讨论后执行

选择依据：
- 有依赖关系 → chain
- 无依赖且独立 → parallel
- 需要共识/评审 → discussion

输出格式（JSON）：
```json
{
  "mode": "chain",
  "reasoning": "选择原因",
  "execution_order": ["agent_1", "agent_2", "agent_3"]
}
```
"""
