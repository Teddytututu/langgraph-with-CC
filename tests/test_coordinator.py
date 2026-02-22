"""
测试协调者模块

测试 coordinator.py 中的任务分析和协作模式选择
"""
import pytest
from unittest.mock import MagicMock

from src.agents.coordinator import (
    CoordinatorAgent,
    TaskAnalysis,
    COORDINATOR_SYSTEM_PROMPT,
)
from src.agents.collaboration import CollaborationMode


class TestTaskAnalysis:
    """测试任务分析结果"""

    def test_default_values(self):
        """测试默认值"""
        analysis = TaskAnalysis()
        assert analysis.task_type == ""
        assert analysis.has_dependencies is False
        assert analysis.requires_consensus is False
        assert analysis.can_parallelize is False
        assert analysis.suggested_mode == CollaborationMode.CHAIN

    def test_custom_values(self):
        """测试自定义值"""
        analysis = TaskAnalysis(
            task_type="coding",
            has_dependencies=True,
            requires_consensus=False,
            can_parallelize=False,
            suggested_mode=CollaborationMode.CHAIN,
            reasoning="有依赖关系",
        )
        assert analysis.task_type == "coding"
        assert analysis.has_dependencies is True


class TestCoordinatorAgent:
    """测试协调者 Agent"""

    def setup_method(self):
        """每个测试前初始化"""
        self.coordinator = CoordinatorAgent()

    def test_init(self):
        """测试初始化"""
        assert self.coordinator._decision_history == []
        assert self.coordinator._mode_stats == {"chain": 0, "parallel": 0, "discussion": 0}

    def test_check_dependencies_with_subtasks(self):
        """测试依赖检查 - 有依赖的子任务"""
        subtask1 = MagicMock(id="task_1", dependencies=[])
        subtask2 = MagicMock(id="task_2", dependencies=["task_1"])

        result = self.coordinator._check_dependencies([subtask1, subtask2])
        assert result is True

    def test_check_dependencies_no_deps(self):
        """测试依赖检查 - 无依赖"""
        subtask = MagicMock(id="task_1", dependencies=[])
        result = self.coordinator._check_dependencies([subtask])
        assert result is False

    def test_check_dependencies_dict_subtasks(self):
        """测试依赖检查 - 字典格式的子任务"""
        subtasks = [
            {"id": "task_1", "dependencies": []},
            {"id": "task_2", "dependencies": ["task_1"]},
        ]
        result = self.coordinator._check_dependencies(subtasks)
        assert result is True

    def test_check_consensus_need_keywords(self):
        """测试共识需求检测 - 关键词"""
        # 中文关键词
        assert self.coordinator._check_consensus_need("需要讨论这个问题", []) is True
        assert self.coordinator._check_consensus_need("需要协商", []) is True
        assert self.coordinator._check_consensus_need("评审代码", []) is True

        # 英文关键词
        assert self.coordinator._check_consensus_need("discuss this", []) is True
        assert self.coordinator._check_consensus_need("need consensus", []) is True
        assert self.coordinator._check_consensus_need("review the code", []) is True

    def test_check_consensus_need_no_keywords(self):
        """测试共识需求检测 - 无关键词"""
        assert self.coordinator._check_consensus_need("写一个函数", []) is False
        assert self.coordinator._check_consensus_need("fix the bug", []) is False

    def test_choose_mode_consensus(self):
        """测试模式选择 - 需要共识"""
        analysis = TaskAnalysis(requires_consensus=True)
        mode = self.coordinator._choose_mode(analysis)
        assert mode == CollaborationMode.DISCUSSION

    def test_choose_mode_with_dependencies(self):
        """测试模式选择 - 有依赖"""
        analysis = TaskAnalysis(
            has_dependencies=True,
            requires_consensus=False,
        )
        mode = self.coordinator._choose_mode(analysis)
        assert mode == CollaborationMode.CHAIN

    def test_choose_mode_parallel(self):
        """测试模式选择 - 可并行"""
        analysis = TaskAnalysis(
            has_dependencies=False,
            requires_consensus=False,
            can_parallelize=True,
        )
        mode = self.coordinator._choose_mode(analysis)
        assert mode == CollaborationMode.PARALLEL

    def test_choose_collaboration_mode_single_agent(self):
        """测试协作模式选择 - 单个 agent"""
        mode = self.coordinator.choose_collaboration_mode(
            task="test task",
            agents=["agent_1"],
        )
        assert mode == CollaborationMode.CHAIN
        assert self.coordinator._mode_stats["chain"] == 1

    def test_choose_collaboration_mode_records_history(self):
        """测试协作模式选择 - 记录历史"""
        mode = self.coordinator.choose_collaboration_mode(
            task="test task",
            agents=["agent_1", "agent_2"],
        )

        assert len(self.coordinator._decision_history) == 1
        decision = self.coordinator._decision_history[0]
        assert "task" in decision
        assert decision["agents"] == ["agent_1", "agent_2"]
        assert "mode" in decision
        assert "timestamp" in decision

    def test_analyze_task(self):
        """测试任务分析"""
        subtask = MagicMock(id="task_1", dependencies=[])
        analysis = self.coordinator.analyze_task(
            task="test task",
            subtasks=[subtask],
        )

        assert isinstance(analysis, TaskAnalysis)
        assert analysis.has_dependencies is False

    def test_plan_execution(self):
        """测试执行计划生成"""
        plan = self.coordinator.plan_execution(
            task="test task",
            agents=["agent_1", "agent_2"],
        )

        assert "mode" in plan
        assert "agents" in plan
        assert "execution_order" in plan
        assert "reasoning" in plan


class TestPlanChainOrder:
    """测试链式执行顺序规划（拓扑排序）"""

    def setup_method(self):
        self.coordinator = CoordinatorAgent()

    def test_empty_subtasks(self):
        """测试空子任务列表"""
        result = self.coordinator._plan_chain_order(["a", "b"], [])
        assert result == ["a", "b"]

    def test_no_dependencies(self):
        """测试无依赖关系"""
        subtasks = [
            MagicMock(id="t1", dependencies=[]),
            MagicMock(id="t2", dependencies=[]),
        ]
        result = self.coordinator._plan_chain_order(["a", "b"], subtasks)
        assert len(result) == 2

    def test_with_dependencies(self):
        """测试有依赖关系（拓扑排序）"""
        subtasks = [
            MagicMock(id="t1", dependencies=[]),
            MagicMock(id="t2", dependencies=["t1"]),
            MagicMock(id="t3", dependencies=["t1"]),
        ]
        agents = ["a", "b", "c"]
        result = self.coordinator._plan_chain_order(agents, subtasks)

        # t1 应该在 t2 和 t3 之前
        assert len(result) == 3

    def test_dict_subtasks(self):
        """测试字典格式子任务"""
        subtasks = [
            {"id": "t1", "dependencies": []},
            {"id": "t2", "dependencies": ["t1"]},
        ]
        agents = ["a", "b"]
        result = self.coordinator._plan_chain_order(agents, subtasks)

        assert len(result) == 2


class TestCoordinatorSystemPrompt:
    """测试协调者系统提示"""

    def test_prompt_content(self):
        """测试系统提示内容"""
        assert "协调者" in COORDINATOR_SYSTEM_PROMPT
        assert "chain" in COORDINATOR_SYSTEM_PROMPT
        assert "parallel" in COORDINATOR_SYSTEM_PROMPT
        assert "discussion" in COORDINATOR_SYSTEM_PROMPT
        assert "JSON" in COORDINATOR_SYSTEM_PROMPT
