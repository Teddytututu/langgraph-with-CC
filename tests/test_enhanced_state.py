"""tests/test_enhanced_state.py — 验证增强状态和讨论库"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# ── 测试 1: 增强状态类实例化 ──
from src.graph.state import SubTask, DynamicNode, DynamicEdge
from src.discussion.types import DiscussionMessage, NodeDiscussion

task = SubTask(
    id="test-001",
    title="测试任务",
    description="这是一个测试",
    agent_type="coder",
    knowledge_domains=["python", "web"],
    assigned_agents=["coder", "researcher"],
    completion_criteria=["代码通过测试", "文档完整"]
)

assert task.knowledge_domains == ["python", "web"]
assert task.assigned_agents == ["coder", "researcher"]
assert task.is_complete() == False
print("✅ 测试 1 通过: SubTask 增强字段正常")

# ── 测试 2: DiscussionMessage ──
msg = DiscussionMessage(
    node_id="test-001",
    from_agent="coder",
    content="我需要了解数据库结构",
    to_agents=["researcher"],
    message_type="query"
)
assert msg.is_broadcast() == False
assert msg.is_for_agent("researcher") == True
print("✅ 测试 2 通过: DiscussionMessage 实例化正常")

# ── 测试 3: NodeDiscussion ──
discussion = NodeDiscussion(node_id="test-001")
discussion.add_message(msg)

assert len(discussion.messages) == 1
assert "coder" in discussion.participants
assert "researcher" in discussion.participants
print("✅ 测试 3 通过: NodeDiscussion 消息添加和参与者追踪正常")

# ── 测试 4: DynamicNode 和 DynamicEdge ──
node = DynamicNode(
    id="node-001",
    name="Test Node",
    node_type="executor",
    knowledge_domains=["api"]
)
edge = DynamicEdge(
    from_node="node-001",
    to_node="node-002"
)
assert node.status == "created"
assert edge.from_node == "node-001"
print("✅ 测试 4 通过: DynamicNode 和 DynamicEdge 实例化正常")

print("\n🎉 增强状态验证全部通过！")
