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
        """初始化协调者"""
        self._decision_history: list[dict] = []  # 决策历史记录
        self._mode_stats: dict[str, int] = {  # 模式使用统计
            "chain": 0,
            "parallel": 0,
            "discussion": 0,
        }

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
            mode = CollaborationMode.CHAIN
        else:
            analysis = self.analyze_task(task, subtasks)
            mode = analysis.suggested_mode

        # 记录决策
        self._mode_stats[mode.value] = self._mode_stats.get(mode.value, 0) + 1
        self._decision_history.append({
            "task": str(task)[:100],  # 截断避免过长
            "agents": agents,
            "mode": mode.value,
            "timestamp": __import__('datetime').datetime.now().isoformat(),
        })

        return mode

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
        """
        规划链式执行顺序（拓扑排序）

        根据子任务的依赖关系确定 agent 执行顺序
        """
        if not subtasks:
            return agents

        # 构建依赖图
        task_deps: dict[str, set[str]] = {}
        task_to_agent: dict[str, str] = {}
        task_to_priority: dict[str, float] = {}

        def _subtask_get(st: Any, key: str, default: Any = None):
            if isinstance(st, dict):
                return st.get(key, default)
            return getattr(st, key, default)

        for i, subtask in enumerate(subtasks):
            task_id = _subtask_get(subtask, 'id', f'task_{i}')
            task_id = str(task_id)
            agent = agents[i] if i < len(agents) else agents[-1]
            task_to_agent[task_id] = agent

            deps = _subtask_get(subtask, 'dependencies', []) or []
            task_deps[task_id] = {str(d) for d in deps}

            raw_priority = _subtask_get(subtask, 'priority', None)
            if isinstance(raw_priority, (int, float)):
                task_to_priority[task_id] = float(raw_priority)
            else:
                task_to_priority[task_id] = 0.0

        # Kahn 算法拓扑排序
        in_degree = {t: 0 for t in task_deps}
        for task_id, deps in task_deps.items():
            for dep in deps:
                if dep in in_degree:
                    in_degree[task_id] += 1

        # 入度为0的任务队列
        queue = [t for t, deg in in_degree.items() if deg == 0]
        ordered_tasks: list[str] = []

        while queue:
            # 按优先级排序（高优先级先执行）
            queue.sort(key=lambda t: -task_to_priority.get(t, 0.0))
            current = queue.pop(0)
            ordered_tasks.append(current)

            # 更新依赖此任务的其他任务入度
            for task_id, deps in task_deps.items():
                if current in deps:
                    in_degree[task_id] -= 1
                    if in_degree[task_id] == 0:
                        queue.append(task_id)

        # 映射回 agent 顺序
        ordered_agents = [task_to_agent[t] for t in ordered_tasks if t in task_to_agent]

        # 补充未在 subtasks 中的 agents
        for agent in agents:
            if agent not in ordered_agents:
                ordered_agents.append(agent)

        return ordered_agents

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
  "execution_order": ["agent_01", "agent_02", "agent_03"]
}
```
"""
