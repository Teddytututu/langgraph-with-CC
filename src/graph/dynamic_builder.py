"""src/graph/dynamic_builder.py — 动态 Graph 构建器"""
from __future__ import annotations
import uuid
from typing import Callable, Any, Awaitable
from datetime import datetime
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver

from src.graph.state import GraphState, DynamicNode, DynamicEdge
from src.graph.nodes.base_node import BaseNode


class DynamicGraphBuilder:
    """动态 Graph 构建器 - 支持运行时添加/移除节点和边"""

    def __init__(self):
        self._nodes: dict[str, DynamicNode] = {}
        self._edges: dict[str, DynamicEdge] = {}
        self._node_executors: dict[str, Callable[[GraphState], Awaitable[dict]]] = {}
        self._compiled_graph = None
        self._checkpointer = MemorySaver()

    # ── 节点管理 ──

    def add_node(
        self,
        node_id: str,
        name: str,
        executor: Callable[[GraphState], Awaitable[dict]],
        node_type: str = "custom",
        knowledge_domains: list[str] = None,
        assigned_agents: list[str] = None,
        config: dict = None,
    ) -> DynamicNode:
        """动态添加节点"""
        if node_id in self._nodes:
            raise ValueError(f"Node {node_id} already exists")

        node = DynamicNode(
            id=node_id,
            name=name,
            node_type=node_type,
            knowledge_domains=knowledge_domains or [],
            assigned_agents=assigned_agents or [],
            config=config or {},
            status="created",
        )

        self._nodes[node_id] = node
        self._node_executors[node_id] = executor
        self._compiled_graph = None  # 使缓存失效

        return node

    def add_node_from_base(self, base_node: BaseNode) -> DynamicNode:
        """从 BaseNode 实例添加节点"""
        return self.add_node(
            node_id=base_node.node_id,
            name=base_node.name,
            executor=base_node.execute,
            knowledge_domains=base_node.knowledge_domains,
            assigned_agents=base_node.assigned_agents,
        )

    def remove_node(self, node_id: str) -> bool:
        """动态移除节点"""
        if node_id not in self._nodes:
            return False

        # 移除相关的边
        edges_to_remove = [
            eid for eid, e in self._edges.items()
            if e.from_node == node_id or e.to_node == node_id
        ]
        for eid in edges_to_remove:
            del self._edges[eid]

        del self._nodes[node_id]
        del self._node_executors[node_id]
        self._compiled_graph = None

        return True

    def get_node(self, node_id: str) -> DynamicNode | None:
        """获取节点"""
        return self._nodes.get(node_id)

    def get_all_nodes(self) -> list[DynamicNode]:
        """获取所有节点"""
        return list(self._nodes.values())

    def update_node_status(
        self,
        node_id: str,
        status: str,
    ) -> bool:
        """更新节点状态"""
        node = self._nodes.get(node_id)
        if node:
            node.status = status
            return True
        return False

    # ── 边管理 ──

    def add_edge(
        self,
        from_node: str,
        to_node: str,
        condition: str = None,
        priority: int = 0,
    ) -> DynamicEdge:
        """动态添加边"""
        # START 和 END 是 LangGraph 特殊常量，需要特殊处理
        is_from_special = from_node == START or str(from_node) == "__start__"
        is_to_special = to_node == END or str(to_node) == "__end__"

        if not is_from_special and from_node not in self._nodes:
            raise ValueError(f"Source node {from_node} not found")
        if not is_to_special and to_node not in self._nodes:
            raise ValueError(f"Target node {to_node} not found")

        # 标准化节点名称
        from_node_str = "__start__" if is_from_special else from_node
        to_node_str = "__end__" if is_to_special else to_node

        edge = DynamicEdge(
            from_node=from_node_str,
            to_node=to_node_str,
            condition=condition,
            priority=priority,
        )

        self._edges[edge.id] = edge
        self._compiled_graph = None

        return edge

    def add_conditional_edges(
        self,
        from_node: str,
        condition_func: Callable[[GraphState], str],
        targets: dict[str, str],
    ) -> None:
        """添加条件边"""
        # 存储条件边信息（用于重建 Graph）
        edge_id = f"conditional_{from_node}"
        self._edges[edge_id] = DynamicEdge(
            id=edge_id,
            from_node=from_node,
            to_node="conditional",
            condition=str(condition_func),  # 存储条件函数的字符串表示
        )
        self._edges[edge_id].metadata = {
            "type": "conditional",
            "targets": targets,
            "condition_func": condition_func,
        }
        self._compiled_graph = None

    def remove_edge(self, edge_id: str) -> bool:
        """移除边"""
        if edge_id in self._edges:
            del self._edges[edge_id]
            self._compiled_graph = None
            return True
        return False

    def get_all_edges(self) -> list[DynamicEdge]:
        """获取所有边"""
        return list(self._edges.values())

    # ── Graph 构建 ──

    def compile(self, force: bool = False) -> StateGraph:
        """编译为可执行 Graph"""
        if self._compiled_graph and not force:
            return self._compiled_graph

        g = StateGraph(GraphState)

        # 注册所有节点
        for node_id, executor in self._node_executors.items():
            g.add_node(node_id, executor)

        # 添加边
        for edge in self._edges.values():
            if hasattr(edge, 'metadata') and edge.metadata.get("type") == "conditional":
                # 处理条件边
                condition_func = edge.metadata["condition_func"]
                targets = edge.metadata["targets"]
                g.add_conditional_edges(edge.from_node, condition_func, targets)
            elif edge.from_node == START:
                g.add_edge(START, edge.to_node)
            elif edge.to_node == END:
                g.add_edge(edge.from_node, END)
            elif edge.condition:
                # 简单条件边（需要额外的条件函数）
                # 这里简化处理，作为普通边
                g.add_edge(edge.from_node, edge.to_node)
            else:
                g.add_edge(edge.from_node, edge.to_node)

        self._compiled_graph = g.compile(checkpointer=self._checkpointer)
        return self._compiled_graph

    # ── Graph 信息导出 ──

    def to_mermaid(self) -> str:
        """导出为 Mermaid 图形语法（style 声明必须在所有边之后，否则 v11 解析报错）"""
        lines = ["graph TD"]

        # 节点样式映射（只收集，稍后统一追加到边的后面）
        node_styles = {
            "router":         "style {} fill:#1e1b4b,color:#a5b4fc,stroke:#6366f1,stroke-width:2px",
            "planner":        "style {} fill:#0c2340,color:#93c5fd,stroke:#3b82f6,stroke-width:2px",
            "budget":         "style {} fill:#0f2922,color:#6ee7b7,stroke:#10b981,stroke-width:2px",
            "executor":       "style {} fill:#14532d,color:#86efac,stroke:#22c55e,stroke-width:2px",
            "reviewer":       "style {} fill:#3b1500,color:#fcd34d,stroke:#f59e0b,stroke-width:2px",
            "reflector":      "style {} fill:#3b0764,color:#e879f9,stroke:#a21caf,stroke-width:2px",
        }
        deferred_styles = []

        # 添加节点（不在此处输出 style）
        for node in self._nodes.values():
            lines.append(f"    {node.id}[{node.name}]")
            style = node_styles.get(node.node_type, "")
            if style:
                deferred_styles.append(f"    {style.format(node.id)}")

        # 添加边
        for edge in self._edges.values():
            if hasattr(edge, 'metadata') and edge.metadata.get("type") == "conditional":
                targets = edge.metadata.get("targets", {})
                for condition, target in targets.items():
                    if target == END:
                        lines.append(f"    {edge.from_node} -->|{condition}| END")
                    else:
                        lines.append(f"    {edge.from_node} -->|{condition}| {target}")
            elif edge.to_node == END:
                lines.append(f"    {edge.from_node} --> END")
            elif edge.from_node != START:
                lines.append(f"    {edge.from_node} --> {edge.to_node}")
            else:
                lines.append(f"    START --> {edge.to_node}")

        # style 声明放在最后（Mermaid v11 语法要求）
        lines.extend(deferred_styles)

        return "\n".join(lines)

    def to_dict(self) -> dict:
        """导出为字典（用于 API 响应）"""
        return {
            "nodes": [
                {
                    "id": n.id,
                    "name": n.name,
                    "type": n.node_type,
                    "status": n.status,
                    "knowledge_domains": n.knowledge_domains,
                    "assigned_agents": n.assigned_agents,
                }
                for n in self._nodes.values()
            ],
            "edges": [
                {
                    "id": e.id,
                    "from": e.from_node,
                    "to": e.to_node,
                    "condition": e.condition,
                }
                for e in self._edges.values()
                if not (hasattr(e, 'metadata') and e.metadata.get("type") == "conditional")
            ],
            "mermaid": self.to_mermaid(),
        }

    # ── 快捷方法 ──

    def create_standard_workflow(self) -> None:
        """创建标准工作流（router → planner → executor → reviewer）"""
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

        # 添加节点
        self.add_node("router", "Router", router_node, node_type="router")
        self.add_node("planner", "Planner", planner_node, node_type="planner")
        self.add_node("budget_manager", "Budget", budget_node, node_type="budget")
        self.add_node("executor", "Executor", executor_node, node_type="executor")
        self.add_node("reviewer", "Reviewer", reviewer_node, node_type="reviewer")
        self.add_node("reflector", "Reflector", reflector_node, node_type="reflector")

        # 添加边
        self.add_edge(START, "router")
        self.add_conditional_edges("router", route_after_router, {
            "planning": "planner",
            "executing": "executor",
            "complete": END,
            "timeout": END,
        })
        self.add_edge("planner", "budget_manager")
        self.add_edge("budget_manager", "executor")
        self.add_conditional_edges("executor", should_continue_or_timeout, {
            "review": "reviewer",
            "timeout": END,
            "continue": "executor",
        })
        self.add_conditional_edges("reviewer", route_after_review, {
            "pass": "router",
            "revise": "reflector",
        })
        self.add_edge("reflector", "executor")


# 全局动态 Graph 构建器实例
dynamic_graph_builder = DynamicGraphBuilder()
