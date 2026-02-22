"""src/graph/nodes/base_node.py — 节点基类（含讨论库接口）"""
from __future__ import annotations
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Callable, Awaitable
from src.graph.state import GraphState, SubTask, NodeDiscussion, DiscussionMessage


class BaseNode(ABC):
    """所有节点的基类"""

    def __init__(
        self,
        node_id: str,
        knowledge_domains: list[str] = None,
        assigned_agents: list[str] = None,
    ):
        self.node_id = node_id
        self.knowledge_domains = knowledge_domains or []
        self.assigned_agents = assigned_agents or []
        self._on_message_handlers: list[Callable[[DiscussionMessage], Awaitable[None]]] = []

    @property
    @abstractmethod
    def name(self) -> str:
        """节点名称"""
        pass

    @abstractmethod
    async def execute(self, state: GraphState) -> dict:
        """执行节点逻辑，返回状态更新"""
        pass

    @abstractmethod
    def is_complete(self, state: GraphState) -> bool:
        """判断节点是否完成"""
        pass

    def get_required_knowledge(self) -> list[str]:
        """获取需要的知识领域"""
        return self.knowledge_domains

    def get_assigned_agents(self) -> list[str]:
        """获取负责的 subagent 列表"""
        return self.assigned_agents

    # ── 讨论库接口 ──

    async def post_message(
        self,
        state: GraphState,
        from_agent: str,
        content: str,
        to_agents: list[str] = None,
        message_type: str = "info",
    ) -> DiscussionMessage:
        """在讨论库中发帖"""
        discussions = state.get("discussions", {})

        # 获取或创建讨论库
        if self.node_id not in discussions:
            discussions[self.node_id] = NodeDiscussion(node_id=self.node_id)

        discussion = discussions[self.node_id]

        # 创建消息
        msg = DiscussionMessage(
            node_id=self.node_id,
            from_agent=from_agent,
            to_agents=to_agents or [],
            content=content,
            message_type=message_type,
        )

        # 添加到讨论库
        discussion.add_message(msg)

        # 触发消息处理器
        for handler in self._on_message_handlers:
            await handler(msg)

        return msg

    async def broadcast(
        self,
        state: GraphState,
        from_agent: str,
        content: str,
        message_type: str = "info",
    ) -> DiscussionMessage:
        """广播消息给所有参与者"""
        return await self.post_message(
            state, from_agent, content,
            to_agents=[],  # 空 = 广播
            message_type=message_type,
        )

    async def request_consensus(
        self,
        state: GraphState,
        from_agent: str,
        topic: str,
    ) -> DiscussionMessage:
        """请求达成共识"""
        discussions = state.get("discussions", {})

        if self.node_id in discussions:
            discussion = discussions[self.node_id]
            discussion.consensus_topic = topic
            discussion.consensus_reached = False
            discussion.status = "active"

        return await self.post_message(
            state, from_agent,
            f"[CONSENSUS REQUEST] {topic}",
            message_type="consensus",
        )

    async def confirm_consensus(
        self,
        state: GraphState,
        from_agent: str,
    ) -> DiscussionMessage:
        """确认共识已达成"""
        discussions = state.get("discussions", {})

        if self.node_id in discussions:
            discussion = discussions[self.node_id]
            # 检查是否所有参与者都已确认
            # 简化实现：直接标记为已达成
            discussion.consensus_reached = True
            discussion.status = "resolved"

        return await self.post_message(
            state, from_agent,
            "[CONSENSUS CONFIRMED] ✓",
            message_type="consensus",
        )

    def get_discussion(self, state: GraphState) -> NodeDiscussion | None:
        """获取当前节点的讨论库"""
        discussions = state.get("discussions", {})
        return discussions.get(self.node_id)

    def get_discussion_history(
        self,
        state: GraphState,
        n: int = 10,
    ) -> list[DiscussionMessage]:
        """获取讨论历史"""
        discussion = self.get_discussion(state)
        if discussion:
            return discussion.get_recent_messages(n)
        return []

    def on_message(
        self,
        handler: Callable[[DiscussionMessage], Awaitable[None]],
    ) -> None:
        """注册消息处理器"""
        self._on_message_handlers.append(handler)

    # ── 状态更新辅助方法 ──

    def update_subtask(
        self,
        subtask: SubTask,
        **updates,
    ) -> SubTask:
        """更新子任务（纯函数式）"""
        return subtask.model_copy(update=updates)

    def create_log_entry(
        self,
        event: str,
        **extra,
    ) -> dict:
        """创建日志条目"""
        return {
            "event": event,
            "node_id": self.node_id,
            "timestamp": datetime.now().isoformat(),
            **extra,
        }


class SimpleNode(BaseNode):
    """简单节点实现（用于测试和占位）"""

    def __init__(
        self,
        node_id: str,
        name: str,
        execute_fn: Callable[[GraphState], Awaitable[dict]] = None,
        **kwargs,
    ):
        super().__init__(node_id, **kwargs)
        self._name = name
        self._execute_fn = execute_fn

    @property
    def name(self) -> str:
        return self._name

    async def execute(self, state: GraphState) -> dict:
        if self._execute_fn:
            return await self._execute_fn(state)
        return {}

    def is_complete(self, state: GraphState) -> bool:
        subtasks = state.get("subtasks", [])
        for task in subtasks:
            if task.id == self.node_id:
                return task.status == "done"
        return False
