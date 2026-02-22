"""src/graph/state.py — 全局状态定义（兼容 LangGraph 1.0）"""
from __future__ import annotations
import operator
from typing import Annotated, Literal
from datetime import datetime
from pydantic import BaseModel
from typing_extensions import TypedDict


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
