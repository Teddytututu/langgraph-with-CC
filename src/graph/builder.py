"""src/graph/builder.py — 构建 LangGraph StateGraph"""
import logging
import sqlite3

from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver
from src.graph.state import GraphState
from src.graph.nodes.router import router_node
from src.graph.nodes.planner import planner_node
from src.graph.nodes.budget import budget_node
from src.graph.nodes.executor import executor_node
from src.graph.nodes.reviewer import reviewer_node
from src.graph.nodes.reflector import reflector_node
from src.graph.edges import (
    route_after_router,
    route_after_review,
    should_continue_or_timeout,
)

logger = logging.getLogger(__name__)

_DEFAULT_DB_PATH = "checkpoints.db"


def _make_default_checkpointer():
    """创建默认检查点存储器（优先 SqliteSaver，失败则回退到 MemorySaver）"""
    try:
        from langgraph.checkpoint.sqlite import SqliteSaver
        conn = sqlite3.connect(_DEFAULT_DB_PATH, check_same_thread=False)
        return SqliteSaver(conn)
    except (ImportError, sqlite3.Error, OSError) as e:
        logger.warning(f"SqliteSaver 初始化失败，回退到 MemorySaver: {e}")
        return MemorySaver()


def build_graph(checkpointer=None):
    """构建并编译完整的任务执行 Graph"""
    g = StateGraph(GraphState)

    # ── 注册节点 ──
    g.add_node("router",         router_node)
    g.add_node("planner",        planner_node)
    g.add_node("budget_manager", budget_node)
    g.add_node("executor",       executor_node)
    g.add_node("reviewer",       reviewer_node)
    g.add_node("reflector",      reflector_node)

    # ── 入口 ──
    g.add_edge(START, "router")

    # ── 条件路由 ──
    g.add_conditional_edges("router", route_after_router, {
        "planning":  "planner",
        "executing": "executor",
        "reviewing": "reviewer",
        "complete":  END,
        "timeout":   END,
    })
    g.add_edge("planner", "budget_manager")
    g.add_edge("budget_manager", "executor")

    g.add_conditional_edges("executor", should_continue_or_timeout, {
        "review":   "reviewer",
        "timeout":  END,
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
