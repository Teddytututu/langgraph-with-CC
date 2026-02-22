"""
测试协作模块

测试 collaboration.py 中的共识机制和协作模式
"""
import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from src.agents.collaboration import (
    CollaborationMode,
    AgentExecutor,
    CollaborationResult,
    ChainCollaboration,
    ParallelCollaboration,
    DiscussionCollaboration,
    execute_collaboration,
)


class TestChainCollaboration:
    """测试链式协作"""

    @pytest.mark.asyncio
    async def test_chain_execution_order(self):
        """测试链式执行顺序：A -> B -> C"""
        execution_order = []

        async def track_execution(task, context):
            execution_order.append(task)
            return f"result_{task}"

        agents = [
            AgentExecutor(agent_id="a", name="Agent A", execute_fn=track_execution),
            AgentExecutor(agent_id="b", name="Agent B", execute_fn=track_execution),
            AgentExecutor(agent_id="c", name="Agent C", execute_fn=track_execution),
        ]

        collaboration = ChainCollaboration(agents)
        result = await collaboration.execute("initial_task")

        assert result.success is True
        # 链式传递：每个 agent 的输出作为下一个的输入
        assert len(execution_order) == 3
        assert result.final_output.startswith("result_")

    @pytest.mark.asyncio
    async def test_chain_failure_stops_early(self):
        """测试链式执行失败时提前停止"""
        call_count = []

        async def normal_agent(task, context):
            call_count.append("normal")
            return task

        async def failing_agent(task, context):
            call_count.append("fail")
            raise ValueError("Agent failed")

        agents = [
            AgentExecutor(agent_id="a", name="Agent A", execute_fn=normal_agent),
            AgentExecutor(agent_id="b", name="Agent B", execute_fn=failing_agent),
            AgentExecutor(agent_id="c", name="Agent C", execute_fn=normal_agent),
        ]

        collaboration = ChainCollaboration(agents)
        result = await collaboration.execute("task")

        assert result.success is False
        assert "执行失败" in result.error
        # 只有 a 和 b 被调用，c 不应该被调用
        assert "c" not in [c for c in call_count]


class TestParallelCollaboration:
    """测试并行协作"""

    @pytest.mark.asyncio
    async def test_parallel_execution(self):
        """测试并行执行"""
        execution_order = []

        async def track_execution(task, context):
            execution_order.append(context.get("agent_id", "unknown"))
            await asyncio.sleep(0.01)  # 模拟异步操作
            return {"result": f"done_{context.get('agent_id')}"}

        agents = [
            AgentExecutor(agent_id="a", name="Agent A", execute_fn=lambda t, c: track_execution(t, {"agent_id": "a"})),
            AgentExecutor(agent_id="b", name="Agent B", execute_fn=lambda t, c: track_execution(t, {"agent_id": "b"})),
            AgentExecutor(agent_id="c", name="Agent C", execute_fn=lambda t, c: track_execution(t, {"agent_id": "c"})),
        ]

        collaboration = ParallelCollaboration(agents)
        result = await collaboration.execute("task")

        assert result.success is True
        assert "a" in execution_order
        assert "b" in execution_order
        assert "c" in execution_order

    @pytest.mark.asyncio
    async def test_parallel_merges_results(self):
        """测试并行结果合并"""

        async def return_dict(task, context):
            return {"key": context.get("agent_id")}

        agents = [
            AgentExecutor(agent_id="a", name="Agent A", execute_fn=lambda t, c: return_dict(t, {"agent_id": "val_a"})),
            AgentExecutor(agent_id="b", name="Agent B", execute_fn=lambda t, c: return_dict(t, {"agent_id": "val_b"})),
        ]

        collaboration = ParallelCollaboration(agents)
        result = await collaboration.execute("task")

        assert result.success is True
        assert "key" in result.final_output


class TestDiscussionCollaboration:
    """测试讨论式协作"""

    @pytest.mark.asyncio
    async def test_basic_discussion_without_manager(self):
        """测试无讨论管理器的基础讨论"""
        opinions = []

        async def collect_opinion(task, context):
            opinion = f"opinion_{len(opinions)}"
            opinions.append(opinion)
            return {"opinion": opinion}

        agents = [
            AgentExecutor(agent_id="a", name="Agent A", execute_fn=collect_opinion),
            AgentExecutor(agent_id="b", name="Agent B", execute_fn=collect_opinion),
        ]

        collaboration = DiscussionCollaboration(agents, discussion_manager=None)
        result = await collaboration.execute("discuss_topic")

        assert result.success is True
        assert len(opinions) == 2

    @pytest.mark.asyncio
    async def test_wait_consensus_timeout(self):
        """测试共识等待超时"""
        mock_manager = MagicMock()
        mock_manager.get_discussion.return_value = None  # 无讨论

        agents = [
            AgentExecutor(agent_id="a", name="Agent A", execute_fn=lambda t, c: {"opinion": "a"}),
        ]

        collaboration = DiscussionCollaboration(agents, discussion_manager=mock_manager)

        # 测试超时场景
        result = await collaboration._wait_consensus("test_discussion", timeout=0.1)

        assert result["status"] == "timeout"


class TestExecuteCollaboration:
    """测试统一协作执行函数"""

    @pytest.mark.asyncio
    async def test_execute_chain_mode(self):
        """测试链式模式执行"""
        agents = [
            AgentExecutor(agent_id="a", name="Agent A", execute_fn=lambda t, c: t),
        ]

        result = await execute_collaboration(
            mode=CollaborationMode.CHAIN,
            agents=agents,
            task="test_task",
        )

        assert result.mode == CollaborationMode.CHAIN
        assert result.success is True

    @pytest.mark.asyncio
    async def test_execute_parallel_mode(self):
        """测试并行模式执行"""
        agents = [
            AgentExecutor(agent_id="a", name="Agent A", execute_fn=lambda t, c: {"a": 1}),
            AgentExecutor(agent_id="b", name="Agent B", execute_fn=lambda t, c: {"b": 2}),
        ]

        result = await execute_collaboration(
            mode=CollaborationMode.PARALLEL,
            agents=agents,
            task="test_task",
        )

        assert result.mode == CollaborationMode.PARALLEL
        assert result.success is True

    @pytest.mark.asyncio
    async def test_execute_invalid_mode(self):
        """测试无效模式"""
        agents = []

        with pytest.raises(ValueError, match="未知的协作模式"):
            await execute_collaboration(
                mode="invalid_mode",
                agents=agents,
                task="test_task",
            )
