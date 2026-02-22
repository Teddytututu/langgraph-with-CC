"""src/discussion/manager.py — 讨论管理器"""
from __future__ import annotations
import uuid
from datetime import datetime
from typing import Callable, Awaitable, Any
from src.discussion.types import DiscussionMessage, NodeDiscussion, DiscussionSummary


class DiscussionManager:
    """讨论管理器 - 管理所有节点的讨论库"""

    def __init__(self):
        self._discussions: dict[str, NodeDiscussion] = {}
        self._on_message_handlers: list[Callable[[DiscussionMessage], Awaitable[None]]] = []
        self._on_consensus_handlers: list[Callable[[str, str], Awaitable[None]]] = []

    # ── 讨论库生命周期 ──

    def create_discussion(self, node_id: str) -> NodeDiscussion:
        """为节点创建讨论库"""
        if node_id in self._discussions:
            return self._discussions[node_id]

        discussion = NodeDiscussion(node_id=node_id)
        self._discussions[node_id] = discussion
        return discussion

    def get_discussion(self, node_id: str) -> NodeDiscussion | None:
        """获取节点的讨论库"""
        return self._discussions.get(node_id)

    def get_or_create(self, node_id: str) -> NodeDiscussion:
        """获取或创建讨论库"""
        return self.get_discussion(node_id) or self.create_discussion(node_id)

    def remove_discussion(self, node_id: str) -> bool:
        """移除讨论库"""
        if node_id in self._discussions:
            del self._discussions[node_id]
            return True
        return False

    # ── 消息操作 ──

    async def post_message(
        self,
        node_id: str,
        from_agent: str,
        content: str,
        to_agents: list[str] = None,
        message_type: str = "info",
        metadata: dict = None,
    ) -> DiscussionMessage:
        """发送消息到讨论库"""
        discussion = self.get_or_create(node_id)

        msg = DiscussionMessage(
            node_id=node_id,
            from_agent=from_agent,
            to_agents=to_agents or [],
            content=content,
            message_type=message_type,
            metadata=metadata or {},
        )

        discussion.add_message(msg)

        # 触发消息处理器
        for handler in self._on_message_handlers:
            await handler(msg)

        return msg

    async def broadcast(
        self,
        node_id: str,
        from_agent: str,
        content: str,
        message_type: str = "info",
    ) -> DiscussionMessage:
        """广播消息给所有参与者"""
        return await self.post_message(
            node_id, from_agent, content,
            to_agents=[],  # 空 = 广播
            message_type=message_type,
        )

    async def query(
        self,
        node_id: str,
        from_agent: str,
        question: str,
        to_agents: list[str],
    ) -> DiscussionMessage:
        """发起查询"""
        msg = await self.post_message(
            node_id, from_agent, question,
            to_agents=to_agents,
            message_type="query",
        )
        # 在元数据中存储查询 ID，方便后续关联回复
        msg.metadata["query_id"] = msg.id
        return msg

    async def respond(
        self,
        node_id: str,
        from_agent: str,
        content: str,
        query_id: str,
    ) -> DiscussionMessage:
        """回复查询"""
        return await self.post_message(
            node_id, from_agent, content,
            message_type="response",
            metadata={"query_id": query_id},
        )

    # ── 共识机制 ──

    async def request_consensus(
        self,
        node_id: str,
        from_agent: str,
        topic: str,
    ) -> DiscussionMessage:
        """请求达成共识"""
        discussion = self.get_or_create(node_id)
        discussion.consensus_topic = topic
        discussion.consensus_reached = False
        discussion.status = "active"

        return await self.post_message(
            node_id, from_agent,
            f"[CONSENSUS REQUEST] {topic}",
            message_type="consensus",
        )

    async def confirm_consensus(
        self,
        node_id: str,
        from_agent: str,
    ) -> DiscussionMessage:
        """
        确认共识

        当参与者确认共识时，记录确认状态。
        只有当所有活跃参与者都确认后，才标记为已达成。
        """
        discussion = self.get_discussion(node_id)
        if not discussion:
            raise ValueError(f"Discussion {node_id} not found")

        # 确保该参与者被记录
        if from_agent not in discussion.participants:
            discussion.participants.append(from_agent)

        # 在元数据中记录确认
        if "confirmations" not in discussion.__dict__:
            discussion.__dict__["confirmations"] = set()
        discussion.__dict__["confirmations"].add(from_agent)

        # 发送确认消息
        msg = await self.post_message(
            node_id, from_agent,
            f"[CONSENSUS CONFIRMED by {from_agent}] ✓",
            message_type="consensus",
        )

        # 检查是否所有参与者都已确认
        confirmations = discussion.__dict__.get("confirmations", set())
        active_participants = set(discussion.participants)

        if active_participants and confirmations >= active_participants:
            # 所有人都确认，标记共识达成
            discussion.consensus_reached = True
            discussion.status = "resolved"

            # 触发共识处理器
            for handler in self._on_consensus_handlers:
                await handler(node_id, discussion.consensus_topic or "")

        return msg

    # ── 冲突处理 ──

    async def report_conflict(
        self,
        node_id: str,
        from_agent: str,
        conflict_description: str,
        involved_agents: list[str],
    ) -> DiscussionMessage:
        """报告冲突"""
        discussion = self.get_or_create(node_id)
        discussion.status = "blocked"

        return await self.post_message(
            node_id, from_agent,
            f"[CONFLICT] {conflict_description}",
            to_agents=involved_agents,
            message_type="conflict",
        )

    async def resolve_conflict(
        self,
        node_id: str,
        from_agent: str,
        resolution: str,
    ) -> DiscussionMessage:
        """解决冲突"""
        discussion = self.get_discussion(node_id)
        if discussion:
            discussion.status = "resolved"

        return await self.post_message(
            node_id, from_agent,
            f"[CONFLICT RESOLVED] {resolution}",
            message_type="info",
        )

    # ── 查询方法 ──

    def get_history(self, node_id: str, n: int = 50) -> list[DiscussionMessage]:
        """获取讨论历史"""
        discussion = self.get_discussion(node_id)
        if discussion:
            return discussion.get_recent_messages(n)
        return []

    def get_all_discussions(self) -> dict[str, NodeDiscussion]:
        """获取所有讨论库"""
        return self._discussions.copy()

    def get_summaries(self) -> list[DiscussionSummary]:
        """获取所有讨论的摘要"""
        return [
            DiscussionSummary.from_discussion(d)
            for d in self._discussions.values()
        ]

    def get_active_discussions(self) -> list[NodeDiscussion]:
        """获取所有活跃的讨论"""
        return [
            d for d in self._discussions.values()
            if d.status == "active"
        ]

    def get_blocked_discussions(self) -> list[NodeDiscussion]:
        """获取所有被阻塞的讨论"""
        return [
            d for d in self._discussions.values()
            if d.status == "blocked"
        ]

    # ── 事件处理器注册 ──

    def on_message(
        self,
        handler: Callable[[DiscussionMessage], Awaitable[None]],
    ) -> None:
        """注册消息处理器"""
        self._on_message_handlers.append(handler)

    def on_consensus(
        self,
        handler: Callable[[str, str], Awaitable[None]],
    ) -> None:
        """注册共识达成处理器"""
        self._on_consensus_handlers.append(handler)

    # ── 导出/导入 ──

    def export_discussions(self) -> dict:
        """导出所有讨论（用于持久化）"""
        return {
            node_id: discussion.to_dict()
            for node_id, discussion in self._discussions.items()
        }

    def import_discussions(self, data: dict) -> None:
        """导入讨论（从持久化恢复）"""
        for node_id, discussion_data in data.items():
            messages = [
                DiscussionMessage(**msg) for msg in discussion_data.get("messages", [])
            ]
            discussion = NodeDiscussion(
                node_id=node_id,
                messages=messages,
                participants=discussion_data.get("participants", []),
                status=discussion_data.get("status", "active"),
                consensus_reached=discussion_data.get("consensus_reached", False),
                consensus_topic=discussion_data.get("consensus_topic"),
            )
            self._discussions[node_id] = discussion


# 全局讨论管理器实例
discussion_manager = DiscussionManager()
