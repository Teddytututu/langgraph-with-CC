"""
协作模式模块

支持三种协作模式：chain（链式）、parallel（并行）、discussion（讨论式）
"""

import asyncio
from abc import ABC, abstractmethod
from enum import Enum
from typing import Any, Callable, Optional
from pydantic import BaseModel, ConfigDict


class CollaborationMode(Enum):
    """协作模式"""
    CHAIN = "chain"          # A → B → C 顺序执行
    PARALLEL = "parallel"    # A, B, C 同时执行
    DISCUSSION = "discussion"  # 讨论协商后执行


class AgentExecutor(BaseModel):
    """Agent 执行器配置"""
    model_config = ConfigDict(arbitrary_types_allowed=True)

    agent_id: str
    name: str = ""
    execute_fn: Optional[Callable] = None


class CollaborationResult(BaseModel):
    """协作执行结果"""
    mode: CollaborationMode
    success: bool
    results: dict[str, Any] = {}  # agent_id -> result
    final_output: Any = None
    error: str = ""


class BaseCollaboration(ABC):
    """协作模式基类"""

    def __init__(self, agents: list[AgentExecutor]):
        self.agents = agents

    @abstractmethod
    async def execute(self, task: Any, context: dict = None) -> CollaborationResult:
        """执行协作"""
        pass


class ChainCollaboration(BaseCollaboration):
    """
    链式协作：A → B → C

    每个 agent 的输出作为下一个 agent 的输入
    适用于有顺序依赖的任务
    """

    async def execute(self, task: Any, context: dict = None) -> CollaborationResult:
        context = context or {}
        current_input = task
        results = {}

        for agent in self.agents:
            try:
                if agent.execute_fn:
                    result = await agent.execute_fn(current_input, context)
                else:
                    # 默认行为：传递输入
                    result = current_input

                results[agent.agent_id] = result
                current_input = result  # 链式传递

            except Exception as e:
                return CollaborationResult(
                    mode=CollaborationMode.CHAIN,
                    success=False,
                    results=results,
                    error=f"Agent {agent.agent_id} 执行失败: {e}"
                )

        return CollaborationResult(
            mode=CollaborationMode.CHAIN,
            success=True,
            results=results,
            final_output=current_input
        )


class ParallelCollaboration(BaseCollaboration):
    """
    并行协作：A, B, C 同时执行

    所有 agent 同时处理相同的输入，结果合并
    适用于可独立执行的任务
    """

    async def execute(self, task: Any, context: dict = None) -> CollaborationResult:
        context = context or {}
        results = {}

        async def run_agent(agent: AgentExecutor):
            try:
                if agent.execute_fn:
                    return await agent.execute_fn(task, context)
                return task
            except Exception as e:
                return {"error": str(e)}

        # 并行执行所有 agent
        tasks_list = [run_agent(agent) for agent in self.agents]
        outputs = await asyncio.gather(*tasks_list)

        for agent, output in zip(self.agents, outputs):
            results[agent.agent_id] = output

        # 合并结果
        final_output = self._merge_results(outputs)

        return CollaborationResult(
            mode=CollaborationMode.PARALLEL,
            success=True,
            results=results,
            final_output=final_output
        )

    def _merge_results(self, results: list[Any]) -> dict:
        """合并并行结果"""
        merged = {}
        for i, result in enumerate(results):
            if isinstance(result, dict):
                merged.update(result)
            else:
                merged[f"agent_{i}"] = result
        return merged


class DiscussionCollaboration(BaseCollaboration):
    """
    讨论式协作：通过讨论库协商后执行

    适用于需要协商、达成共识的任务
    """

    def __init__(self, agents: list[AgentExecutor], discussion_manager=None):
        super().__init__(agents)
        self.discussion_manager = discussion_manager

    async def execute(self, task: Any, context: dict = None) -> CollaborationResult:
        context = context or {}
        results = {}

        # 如果没有讨论管理器，退化为基础讨论
        if not self.discussion_manager:
            return await self._basic_discussion(task, context)

        # 使用讨论库进行协商
        return await self._managed_discussion(task, context)

    async def _basic_discussion(self, task: Any, context: dict) -> CollaborationResult:
        """基础讨论（无讨论管理器）"""
        # 每个agent发表意见
        opinions = {}
        for agent in self.agents:
            try:
                if agent.execute_fn:
                    opinion = await agent.execute_fn(task, context)
                    opinions[agent.agent_id] = opinion
            except Exception as e:
                opinions[agent.agent_id] = {"error": str(e)}

        # 简单多数投票/合并
        final_output = self._consensus(opinions)

        return CollaborationResult(
            mode=CollaborationMode.DISCUSSION,
            success=True,
            results=opinions,
            final_output=final_output
        )

    async def _managed_discussion(self, task: Any, context: dict) -> CollaborationResult:
        """使用讨论库的讨论"""
        # 创建讨论主题
        discussion_id = f"task_{id(task)}"

        # 各 agent 发表意见到讨论库
        for agent in self.agents:
            if agent.execute_fn:
                try:
                    opinion = await agent.execute_fn(task, context)
                    await self.discussion_manager.post_message(
                        node_id=discussion_id,
                        from_agent=agent.agent_id,
                        content=str(opinion),
                    )
                except Exception as e:
                    await self.discussion_manager.post_message(
                        node_id=discussion_id,
                        from_agent=agent.agent_id,
                        content=f"错误: {e}",
                    )

        # 等待共识（简化实现）
        consensus = await self._wait_consensus(discussion_id)

        return CollaborationResult(
            mode=CollaborationMode.DISCUSSION,
            success=True,
            results={},
            final_output=consensus
        )

    async def _wait_consensus(self, discussion_id: str, timeout: float = 60.0) -> dict:
        """
        等待共识达成

        Args:
            discussion_id: 讨论ID
            timeout: 超时时间（秒）

        Returns:
            共识结果
        """
        if not self.discussion_manager:
            return {"status": "consensus_reached", "discussion_id": discussion_id}

        start_time = asyncio.get_running_loop().time()

        while True:
            discussion = self.discussion_manager.get_discussion(discussion_id)

            if discussion:
                # 检查是否已达成共识
                if discussion.consensus_reached:
                    return {
                        "status": "consensus_reached",
                        "discussion_id": discussion_id,
                        "topic": discussion.consensus_topic,
                        "message_count": len(discussion.messages),
                    }

                # 检查是否被阻塞（冲突）
                if discussion.status == "blocked":
                    return {
                        "status": "blocked",
                        "discussion_id": discussion_id,
                        "error": "讨论被阻塞，存在未解决的冲突",
                    }

                # 检查参与者数量（至少2人参与才算共识）
                if len(discussion.participants) >= 2:
                    # 统计同意消息
                    recent = discussion.get_recent_messages(20)
                    agree_count = sum(
                        1 for m in recent
                        if m.message_type == "consensus" and "CONFIRMED" in m.content
                    )
                    if agree_count >= len(discussion.participants):
                        discussion.consensus_reached = True
                        discussion.status = "resolved"
                        return {
                            "status": "consensus_reached",
                            "discussion_id": discussion_id,
                            "agree_count": agree_count,
                        }

            # 超时检查
            elapsed = asyncio.get_running_loop().time() - start_time
            if elapsed >= timeout:
                return {
                    "status": "timeout",
                    "discussion_id": discussion_id,
                    "error": f"共识超时（{timeout}秒）",
                }

            # 等待一小段时间再检查
            await asyncio.sleep(0.5)

    def _consensus(self, opinions: dict) -> Any:
        """从意见中提取共识"""
        # 简化实现：返回第一个非错误意见
        for agent_id, opinion in opinions.items():
            if isinstance(opinion, dict) and "error" not in opinion:
                return opinion
            elif not isinstance(opinion, dict):
                return opinion
        return opinions


async def execute_collaboration(
    mode: CollaborationMode,
    agents: list[AgentExecutor],
    task: Any,
    context: dict = None,
    discussion_manager=None
) -> CollaborationResult:
    """
    执行协作

    Args:
        mode: 协作模式
        agents: 参与的 agent 列表
        task: 任务输入
        context: 执行上下文
        discussion_manager: 讨论管理器（讨论模式需要）

    Returns:
        协作结果
    """
    if mode == CollaborationMode.CHAIN:
        collaboration = ChainCollaboration(agents)
    elif mode == CollaborationMode.PARALLEL:
        collaboration = ParallelCollaboration(agents)
    elif mode == CollaborationMode.DISCUSSION:
        collaboration = DiscussionCollaboration(agents, discussion_manager)
    else:
        raise ValueError(f"未知的协作模式: {mode}")

    return await collaboration.execute(task, context)
