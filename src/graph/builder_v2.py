"""
Graph Builder V2 — 使用多 Agent 协作节点的 Graph 构建

完整的多 Agent 协作流程:
- planner_v2: 多专家并行规划 + 方案讨论合并
- executor_v2: 集成 DiscussionManager 的执行节点
- reviewer_v2: 多人评审 + 投票决策
- reflector_v2: 多角度反思 + 讨论协商
"""

import logging
import sqlite3

from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver
from src.graph.state import GraphState
from src.graph.nodes.router import router_node
from src.graph.nodes.budget import budget_node
from src.graph.edges import (
    route_after_router,
    route_after_review,
    should_continue_or_timeout,
)

# 导入 V2 节点
from src.graph.nodes.planner_v2 import planner_v2_node
from src.graph.nodes.executor_v2 import executor_v2_node
from src.graph.nodes.reviewer_v2 import reviewer_v2_node
from src.graph.nodes.reflector_v2 import reflector_v2_node

logger = logging.getLogger(__name__)

_DEFAULT_DB_PATH = "checkpoints_v2.db"


def _make_default_checkpointer():
    """创建默认检查点存储器"""
    try:
        from langgraph.checkpoint.sqlite import SqliteSaver
        conn = sqlite3.connect(_DEFAULT_DB_PATH, check_same_thread=False)
        return SqliteSaver(conn)
    except (ImportError, sqlite3.Error, OSError) as e:
        logger.warning(f"SqliteSaver 初始化失败，回退到 MemorySaver: {e}")
        return MemorySaver()


def build_graph_v2(checkpointer=None):
    """
    构建并编译使用多 Agent 协作节点的 Graph

    节点协作说明:
    - router: 纯逻辑节点，决定下一步
    - planner_v2: 3 个 planner 并行规划 + 讨论
    - budget_manager: 时间预算分配
    - executor_v2: chain/parallel/discussion 三种模式
    - reviewer_v2: 3 个 reviewer 并行评审 + 投票
    - reflector_v2: 技术/流程/资源 三角度反思
    """
    g = StateGraph(GraphState)

    # ── 注册 V2 节点 ──
    g.add_node("router",         router_node)        # 路由节点保持不变
    g.add_node("planner",        planner_v2_node)    # 多 Agent 规划
    g.add_node("budget_manager", budget_node)        # 预算节点保持不变
    g.add_node("executor",       executor_v2_node)   # 讨论协作执行
    g.add_node("reviewer",       reviewer_v2_node)   # 多人评审
    g.add_node("reflector",      reflector_v2_node)  # 多角度反思

    # ── 入口 ──
    g.add_edge(START, "router")

    # ── 条件路由 ──
    g.add_conditional_edges("router", route_after_router, {
        "planning":  "planner",
        "executing": "executor",
        "reviewing": "reviewer",
        "complete":  END,
    })
    g.add_edge("planner", "budget_manager")
    g.add_edge("budget_manager", "executor")

    g.add_conditional_edges("executor", should_continue_or_timeout, {
        "review":   "reviewer",
        "continue": "executor",
        "wait":     "router",
    })
    g.add_conditional_edges("reviewer", route_after_review, {
        "pass":   "router",
        "revise": "reflector",
    })
    g.add_edge("reflector", "executor")

    # ── 编译 ──
    if checkpointer is None:
        checkpointer = _make_default_checkpointer()
    return g.compile(checkpointer=checkpointer)


def build_graph(checkpointer=None):
    """
    向后兼容的构建函数

    现在默认使用 V2 多 Agent 协作节点
    """
    return build_graph_v2(checkpointer)
