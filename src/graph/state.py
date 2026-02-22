"""src/graph/state.py — 全局状态定义（兼容 LangGraph 1.0）"""
from __future__ import annotations
import operator
import uuid
from typing import Annotated, Literal, Any
from datetime import datetime
from pydantic import BaseModel, Field
from typing_extensions import TypedDict


# ═══════════════════════════════════════════════════════════════
# 讨论库相关类型
# ═══════════════════════════════════════════════════════════════

class DiscussionMessage(BaseModel):
    """讨论库中的一条消息"""
    id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    node_id: str                    # 所属节点
    from_agent: str                 # 发送者 subagent
    to_agents: list[str] = []       # 接收者（空=广播）
    content: str
    timestamp: datetime = Field(default_factory=datetime.now)
    message_type: Literal["query", "response", "consensus", "conflict", "info"] = "info"
    metadata: dict[str, Any] = {}   # 附加元数据


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
        return self.messages[-n:]


# ═══════════════════════════════════════════════════════════════
# 增强的子任务模型
# ═══════════════════════════════════════════════════════════════

class SubTask(BaseModel):
    """一个被分解出的子任务"""
    id: str                                    # 如 "task-001"
    title: str
    description: str                            # 详细需求 + 验收标准
    agent_type: str                             # coder / researcher / writer / analyst
    dependencies: list[str] = []                # 依赖的其他子任务 id
    priority: int = 1                           # 1=最高
    estimated_minutes: float = 10.0
    status: Literal[
        "pending", "running", "done", "failed", "skipped"
    ] = "pending"
    result: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    retry_count: int = 0

    # 🆕 增强字段
    knowledge_domains: list[str] = []           # 需要的知识领域
    assigned_agents: list[str] = []             # 负责的 subagent（可多个）
    completion_criteria: list[str] = []         # 完成标准

    def is_complete(self) -> bool:
        """判断节点是否完成"""
        return self.status == "done"

    def get_required_knowledge(self) -> list[str]:
        """获取需要的知识领域"""
        return self.knowledge_domains

    def add_agent(self, agent: str) -> None:
        """添加负责的 subagent"""
        if agent not in self.assigned_agents:
            self.assigned_agents.append(agent)


class TimeBudget(BaseModel):
    """用户设定的时间预算"""
    total_minutes: float
    started_at: datetime | None = None
    deadline: datetime | None = None
    elapsed_minutes: float = 0.0
    remaining_minutes: float = 0.0
    is_overtime: bool = False


class GraphState(TypedDict, total=False):
    """LangGraph StateGraph 的核心状态（TypedDict 兼容 LangGraph 1.0）"""
    # 用户输入
    user_task: str
    time_budget: TimeBudget | None

    # 任务分解
    subtasks: list[SubTask]
    current_subtask_id: str | None

    # 🆕 讨论库（按节点 ID 索引）
    discussions: dict[str, NodeDiscussion]

    # 执行追踪
    messages: Annotated[list, operator.add]
    execution_log: Annotated[list[dict], operator.add]
    artifacts: dict[str, str]

    # 流程控制
    phase: Literal[
        "init", "planning", "budgeting", "executing",
        "reviewing", "reflecting", "complete", "timeout"
    ]
    iteration: int
    max_iterations: int
    error: str | None

    # 最终输出
    final_output: str | None


# ═══════════════════════════════════════════════════════════════
# 动态 Graph 相关类型
# ═══════════════════════════════════════════════════════════════

class DynamicNode(BaseModel):
    """动态节点定义"""
    id: str
    name: str
    node_type: str                    # planner / executor / reviewer 等
    knowledge_domains: list[str] = []
    assigned_agents: list[str] = []
    config: dict[str, Any] = {}       # 节点配置

    # 状态
    status: Literal["created", "initialized", "running", "completed", "failed"] = "created"
    created_at: datetime = Field(default_factory=datetime.now)


class DynamicEdge(BaseModel):
    """动态边定义"""
    id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    from_node: str
    to_node: str
    condition: str | None = None      # 条件表达式（可选）
    priority: int = 0                 # 边的优先级
    metadata: dict[str, Any] = {}     # 附加元数据（条件边信息等）
