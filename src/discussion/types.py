"""src/discussion/types.py — 讨论消息类型"""
from __future__ import annotations
import uuid
from datetime import datetime
from typing import Literal, Any
from pydantic import BaseModel, Field


class DiscussionMessage(BaseModel):
    """讨论库中的一条消息"""
    id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    node_id: str                    # 所属节点
    from_agent: str                 # 发送者 subagent
    to_agents: list[str] = []       # 接收者（空=广播）
    content: str
    timestamp: datetime = Field(default_factory=datetime.now)
    message_type: Literal["query", "response", "consensus", "conflict", "info", "proposal", "reflection", "agreement", "error", "review_opinion"] = "info"
    metadata: dict[str, Any] = {}   # 附加元数据（如附件引用）

    def is_broadcast(self) -> bool:
        """是否为广播消息"""
        return len(self.to_agents) == 0

    def is_for_agent(self, agent: str) -> bool:
        """是否发给指定 agent"""
        return agent in self.to_agents or self.is_broadcast()

    def to_dict(self) -> dict:
        """转换为字典（用于 JSON 序列化）"""
        return {
            "id": self.id,
            "node_id": self.node_id,
            "from_agent": self.from_agent,
            "to_agents": self.to_agents,
            "content": self.content,
            "timestamp": self.timestamp.isoformat(),
            "message_type": self.message_type,
            "metadata": self.metadata,
        }


class NodeDiscussion(BaseModel):
    """节点讨论库"""
    node_id: str
    messages: list[DiscussionMessage] = []
    participants: list[str] = []    # 参与的 subagent 列表
    status: Literal["active", "resolved", "blocked"] = "active"
    consensus_reached: bool = False
    consensus_topic: str | None = None

    def add_message(self, msg: DiscussionMessage) -> None:
        """添加消息到讨论库"""
        self.messages.append(msg)
        # 自动添加发送者到参与者列表
        if msg.from_agent not in self.participants:
            self.participants.append(msg.from_agent)
        # 自动添加接收者到参与者列表
        for agent in msg.to_agents:
            if agent not in self.participants:
                self.participants.append(agent)

    def get_messages_by_agent(self, agent: str) -> list[DiscussionMessage]:
        """获取某个 agent 发送或接收的所有消息"""
        return [
            m for m in self.messages
            if m.from_agent == agent or agent in m.to_agents
        ]

    def get_recent_messages(self, n: int = 10) -> list[DiscussionMessage]:
        """获取最近 n 条消息"""
        return self.messages[-n:] if self.messages else []

    def get_messages_by_type(
        self,
        msg_type: str,
    ) -> list[DiscussionMessage]:
        """获取特定类型的消息"""
        return [m for m in self.messages if m.message_type == msg_type]

    def has_conflict(self) -> bool:
        """是否存在冲突"""
        return any(m.message_type == "conflict" for m in self.messages)

    def get_pending_queries(self) -> list[DiscussionMessage]:
        """获取未回复的查询"""
        queries = {m.id: m for m in self.messages if m.message_type == "query"}
        responses = set()
        for m in self.messages:
            if m.message_type == "response":
                # 假设 metadata 中有 query_id
                if "query_id" in m.metadata:
                    responses.add(m.metadata["query_id"])
        return [q for qid, q in queries.items() if qid not in responses]

    def to_dict(self) -> dict:
        """转换为字典（用于 JSON 序列化）"""
        return {
            "node_id": self.node_id,
            "messages": [m.to_dict() for m in self.messages],
            "participants": self.participants,
            "status": self.status,
            "consensus_reached": self.consensus_reached,
            "consensus_topic": self.consensus_topic,
        }


class DiscussionSummary(BaseModel):
    """讨论摘要（用于前端展示）"""
    node_id: str
    participant_count: int
    message_count: int
    status: str
    last_activity: datetime | None
    has_conflict: bool
    pending_queries: int

    @classmethod
    def from_discussion(cls, discussion: NodeDiscussion) -> "DiscussionSummary":
        """从讨论库创建摘要"""
        last_msg = discussion.messages[-1] if discussion.messages else None
        return cls(
            node_id=discussion.node_id,
            participant_count=len(discussion.participants),
            message_count=len(discussion.messages),
            status=discussion.status,
            last_activity=last_msg.timestamp if last_msg else None,
            has_conflict=discussion.has_conflict(),
            pending_queries=len(discussion.get_pending_queries()),
        )
