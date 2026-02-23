"""
测试讨论管理器模块

测试 discussion/manager.py 中的讨论管理功能
"""
import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock

from src.discussion.manager import DiscussionManager
from src.discussion.types import DiscussionMessage, NodeDiscussion


class TestDiscussionManager:
    """测试讨论管理器"""

    def setup_method(self):
        """每个测试前创建新的管理器"""
        self.manager = DiscussionManager()

    def test_create_discussion(self):
        """测试创建讨论库"""
        discussion = self.manager.create_discussion("node_1")

        assert discussion is not None
        assert discussion.node_id == "node_1"
        assert discussion.status == "active"

    def test_create_discussion_idempotent(self):
        """测试重复创建返回相同实例"""
        d1 = self.manager.create_discussion("node_1")
        d2 = self.manager.create_discussion("node_1")

        assert d1 is d2

    def test_get_discussion(self):
        """测试获取讨论库"""
        self.manager.create_discussion("node_1")
        discussion = self.manager.get_discussion("node_1")

        assert discussion is not None
        assert discussion.node_id == "node_1"

    def test_get_discussion_not_found(self):
        """测试获取不存在的讨论库"""
        discussion = self.manager.get_discussion("nonexistent")
        assert discussion is None

    def test_get_or_create(self):
        """测试获取或创建"""
        d1 = self.manager.get_or_create("node_1")
        d2 = self.manager.get_or_create("node_1")

        assert d1 is d2

    def test_remove_discussion(self):
        """测试移除讨论库"""
        self.manager.create_discussion("node_1")
        result = self.manager.remove_discussion("node_1")

        assert result is True
        assert self.manager.get_discussion("node_1") is None

    def test_remove_discussion_not_found(self):
        """测试移除不存在的讨论库"""
        result = self.manager.remove_discussion("nonexistent")
        assert result is False

    @pytest.mark.asyncio
    async def test_post_message(self):
        """测试发送消息"""
        msg = await self.manager.post_message(
            node_id="node_1",
            from_agent="agent_01",
            content="Hello world",
        )

        assert msg is not None
        assert msg.from_agent == "agent_01"
        assert msg.content == "Hello world"

        # 验证消息已添加到讨论库
        discussion = self.manager.get_discussion("node_1")
        assert len(discussion.messages) == 1

    @pytest.mark.asyncio
    async def test_broadcast(self):
        """测试广播消息"""
        msg = await self.manager.broadcast(
            node_id="node_1",
            from_agent="agent_01",
            content="Broadcast message",
        )

        assert msg.to_agents == []  # 空 = 广播

    @pytest.mark.asyncio
    async def test_query(self):
        """测试发起查询"""
        msg = await self.manager.query(
            node_id="node_1",
            from_agent="agent_01",
            question="What do you think?",
            to_agents=["agent_02", "agent_03"],
        )

        assert msg.message_type == "query"
        assert "query_id" in msg.metadata

    @pytest.mark.asyncio
    async def test_respond(self):
        """测试回复查询"""
        # 先发起查询
        query = await self.manager.query(
            node_id="node_1",
            from_agent="agent_01",
            question="Question?",
            to_agents=["agent_02"],
        )

        # 回复
        response = await self.manager.respond(
            node_id="node_1",
            from_agent="agent_02",
            content="My answer",
            query_id=query.metadata["query_id"],
        )

        assert response.message_type == "response"
        assert response.metadata.get("query_id") == query.metadata["query_id"]


class TestConsensusMechanism:
    """测试共识机制"""

    def setup_method(self):
        self.manager = DiscussionManager()

    @pytest.mark.asyncio
    async def test_request_consensus(self):
        """测试请求共识"""
        msg = await self.manager.request_consensus(
            node_id="node_1",
            from_agent="agent_01",
            topic="Should we use Python?",
        )

        assert msg.message_type == "consensus"
        assert "CONSENSUS REQUEST" in msg.content

        discussion = self.manager.get_discussion("node_1")
        assert discussion.consensus_topic == "Should we use Python?"
        assert discussion.consensus_reached is False

    @pytest.mark.asyncio
    async def test_confirm_consensus_single_participant(self):
        """测试单个参与者确认共识"""
        # 先请求共识
        await self.manager.request_consensus(
            node_id="node_1",
            from_agent="agent_01",
            topic="Topic",
        )

        # 确认共识
        msg = await self.manager.confirm_consensus(
            node_id="node_1",
            from_agent="agent_01",
        )

        assert "CONFIRMED" in msg.content

        discussion = self.manager.get_discussion("node_1")
        # 单个参与者确认后达成共识
        assert discussion.consensus_reached is True

    @pytest.mark.asyncio
    async def test_confirm_consensus_multiple_participants(self):
        """测试多参与者确认共识"""
        # 创建讨论
        await self.manager.post_message(
            node_id="node_1",
            from_agent="agent_01",
            content="First message",
        )
        await self.manager.post_message(
            node_id="node_1",
            from_agent="agent_02",
            content="Second message",
        )

        # 请求共识
        await self.manager.request_consensus(
            node_id="node_1",
            from_agent="agent_01",
            topic="Topic",
        )

        discussion = self.manager.get_discussion("node_1")
        # 添加第二个参与者
        discussion.participants = ["agent_01", "agent_02"]

        # 第一个参与者确认
        await self.manager.confirm_consensus("node_1", "agent_01")
        # 此时还没完全达成（需要所有参与者确认）
        # 但 confirmations 集合已记录 agent_01

        # 第二个参与者确认
        await self.manager.confirm_consensus("node_1", "agent_02")

        # 重新获取讨论状态
        discussion = self.manager.get_discussion("node_1")
        assert discussion.consensus_reached is True

    @pytest.mark.asyncio
    async def test_confirm_consensus_nonexistent_discussion(self):
        """测试确认不存在的讨论"""
        with pytest.raises(ValueError, match="Discussion .* not found"):
            await self.manager.confirm_consensus(
                node_id="nonexistent",
                from_agent="agent_01",
            )


class TestConflictHandling:
    """测试冲突处理"""

    def setup_method(self):
        self.manager = DiscussionManager()

    @pytest.mark.asyncio
    async def test_report_conflict(self):
        """测试报告冲突"""
        msg = await self.manager.report_conflict(
            node_id="node_1",
            from_agent="agent_01",
            conflict_description="Disagreement on approach",
            involved_agents=["agent_02"],
        )

        assert msg.message_type == "conflict"
        assert "CONFLICT" in msg.content

        discussion = self.manager.get_discussion("node_1")
        assert discussion.status == "blocked"

    @pytest.mark.asyncio
    async def test_resolve_conflict(self):
        """测试解决冲突"""
        # 先报告冲突
        await self.manager.report_conflict(
            node_id="node_1",
            from_agent="agent_01",
            conflict_description="Disagreement",
            involved_agents=["agent_02"],
        )

        # 解决冲突
        msg = await self.manager.resolve_conflict(
            node_id="node_1",
            from_agent="agent_01",
            resolution="Decided to use approach A",
        )

        assert "CONFLICT RESOLVED" in msg.content

        discussion = self.manager.get_discussion("node_1")
        assert discussion.status == "resolved"


class TestQueryMethods:
    """测试查询方法"""

    def setup_method(self):
        self.manager = DiscussionManager()

    @pytest.mark.asyncio
    async def test_get_history(self):
        """测试获取历史"""
        # 发送几条消息
        for i in range(5):
            await self.manager.post_message(
                node_id="node_1",
                from_agent="agent_01",
                content=f"Message {i}",
            )

        history = self.manager.get_history("node_1", n=3)

        assert len(history) == 3

    @pytest.mark.asyncio
    async def test_get_history_nonexistent(self):
        """测试获取不存在讨论的历史"""
        history = self.manager.get_history("nonexistent")
        assert history == []

    @pytest.mark.asyncio
    async def test_get_all_discussions(self):
        """测试获取所有讨论"""
        await self.manager.post_message("node_1", "a1", "msg")
        await self.manager.post_message("node_2", "a2", "msg")

        all_discussions = self.manager.get_all_discussions()

        assert len(all_discussions) == 2
        assert "node_1" in all_discussions
        assert "node_2" in all_discussions

    @pytest.mark.asyncio
    async def test_get_active_discussions(self):
        """测试获取活跃讨论"""
        await self.manager.post_message("node_1", "a1", "msg")

        # 创建被阻塞的讨论
        await self.manager.report_conflict("node_2", "a2", "conflict", [])

        active = self.manager.get_active_discussions()
        blocked = self.manager.get_blocked_discussions()

        assert len(active) == 1
        assert active[0].node_id == "node_1"
        assert len(blocked) == 1
        assert blocked[0].node_id == "node_2"


class TestEventHandlers:
    """测试事件处理器"""

    def setup_method(self):
        self.manager = DiscussionManager()

    @pytest.mark.asyncio
    async def test_on_message_handler(self):
        """测试消息处理器"""
        received_messages = []

        async def handler(msg):
            received_messages.append(msg)

        self.manager.on_message(handler)

        await self.manager.post_message("node_1", "agent_01", "Hello")

        assert len(received_messages) == 1
        assert received_messages[0].content == "Hello"

    @pytest.mark.asyncio
    async def test_on_consensus_handler(self):
        """测试共识处理器"""
        consensus_events = []

        async def handler(node_id, topic):
            consensus_events.append((node_id, topic))

        self.manager.on_consensus(handler)

        # 请求并确认共识
        await self.manager.request_consensus("node_1", "a1", "Topic")
        await self.manager.confirm_consensus("node_1", "a1")

        assert len(consensus_events) == 1
        assert consensus_events[0] == ("node_1", "Topic")


class TestExportImport:
    """测试导出/导入"""

    def setup_method(self):
        self.manager = DiscussionManager()

    @pytest.mark.asyncio
    async def test_export_discussions(self):
        """测试导出讨论"""
        await self.manager.post_message("node_1", "a1", "msg1")
        await self.manager.post_message("node_2", "a2", "msg2")

        exported = self.manager.export_discussions()

        assert "node_1" in exported
        assert "node_2" in exported
        assert "messages" in exported["node_1"]

    @pytest.mark.asyncio
    async def test_import_discussions(self):
        """测试导入讨论"""
        data = {
            "node_1": {
                "messages": [
                    {
                        "node_id": "node_1",
                        "from_agent": "a1",
                        "to_agents": [],
                        "content": "imported message",
                        "message_type": "info",
                    }
                ],
                "participants": ["a1"],
                "status": "active",
                "consensus_reached": False,
            }
        }

        self.manager.import_discussions(data)

        discussion = self.manager.get_discussion("node_1")
        assert discussion is not None
        assert len(discussion.messages) == 1
