"""tests/test_dynamic_graph.py — 验证动态 Graph"""
import sys
import os
import asyncio

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.graph.dynamic_builder import DynamicGraphBuilder


async def test_dynamic_graph():
    builder = DynamicGraphBuilder()

    # ── 测试 1: 添加节点 ──
    async def dummy_executor(state):
        return {"phase": "done"}

    node = builder.add_node(
        node_id="test-node",
        name="Test Node",
        executor=dummy_executor,
        node_type="custom",
        knowledge_domains=["test"],
        assigned_agents=["coder"]
    )
    assert node.id == "test-node"
    assert node.status == "created"
    print("✅ 测试 1 通过: 节点添加成功")

    # ── 测试 2: 添加边 ──
    builder.add_node("node-b", "Node B", dummy_executor)
    edge = builder.add_edge("test-node", "node-b")
    assert edge.from_node == "test-node"
    assert edge.to_node == "node-b"
    print("✅ 测试 2 通过: 边添加成功")

    # ── 测试 3: 移除节点 ──
    builder.add_node("to-remove", "To Remove", dummy_executor)
    removed = builder.remove_node("to-remove")
    assert removed == True
    assert builder.get_node("to-remove") is None
    print("✅ 测试 3 通过: 节点移除成功")

    # ── 测试 4: 创建标准工作流 ──
    builder2 = DynamicGraphBuilder()
    builder2.create_standard_workflow()

    nodes = builder2.get_all_nodes()
    node_ids = [n.id for n in nodes]
    assert "router" in node_ids
    assert "planner" in node_ids
    assert "executor" in node_ids
    assert "reviewer" in node_ids
    print(f"✅ 测试 4 通过: 标准工作流创建成功 - {len(nodes)} 个节点")

    # ── 测试 5: Mermaid 导出 ──
    mermaid = builder2.to_mermaid()
    assert "graph TD" in mermaid
    assert "router" in mermaid
    print("✅ 测试 5 通过: Mermaid 导出成功")

    # ── 测试 6: Graph 编译 ──
    graph = builder2.compile()
    assert graph is not None
    print(f"✅ 测试 6 通过: Graph 编译成功")

    # ── 测试 7: 字典导出 ──
    data = builder2.to_dict()
    assert "nodes" in data
    assert "edges" in data
    assert "mermaid" in data
    print(f"✅ 测试 7 通过: 字典导出成功 - {len(data['nodes'])} 节点")

    print("\n🎉 动态 Graph 验证全部通过！")


asyncio.run(test_dynamic_graph())
