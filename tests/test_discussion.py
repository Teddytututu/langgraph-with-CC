"""tests/test_discussion.py — 验证讨论库模块"""
import sys
import os
import asyncio

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.discussion.manager import DiscussionManager
from src.discussion.types import DiscussionSummary


async def test_discussion():
    manager = DiscussionManager()

    # ── 测试 1: 创建讨论库 ──
    discussion = manager.create_discussion("node-001")
    assert discussion.node_id == "node-001"
    assert discussion.status == "active"
    print("✅ 测试 1 通过: 讨论库创建成功")

    # ── 测试 2: 发送消息 ──
    msg = await manager.post_message(
        node_id="node-001",
        from_agent="coder",
        content="我需要帮助理解 API",
        to_agents=["researcher"],
        message_type="query"
    )
    assert msg.content == "我需要帮助理解 API"
    assert msg.from_agent == "coder"
    print("✅ 测试 2 通过: 消息发送成功")

    # ── 测试 3: 广播消息 ──
    broadcast = await manager.broadcast(
        node_id="node-001",
        from_agent="director",
        content="请大家注意截止时间"
    )
    assert broadcast.is_broadcast()
    print("✅ 测试 3 通过: 广播消息成功")

    # ── 测试 4: 共识机制 ──
    await manager.request_consensus("node-001", "coder", "使用 REST API")
    await manager.confirm_consensus("node-001", "researcher")

    discussion = manager.get_discussion("node-001")
    assert discussion.consensus_reached == True
    assert discussion.status == "resolved"
    print("✅ 测试 4 通过: 共识机制正常")

    # ── 测试 5: 冲突处理 ──
    manager2 = DiscussionManager()
    manager2.create_discussion("node-002")
    await manager2.report_conflict(
        node_id="node-002",
        from_agent="coder",
        conflict_description="API 设计有分歧",
        involved_agents=["coder", "analyst"]
    )

    d = manager2.get_discussion("node-002")
    assert d.status == "blocked"
    assert d.has_conflict()
    print("✅ 测试 5 通过: 冲突报告正常")

    # ── 测试 6: 摘要生成 ──
    summary = DiscussionSummary.from_discussion(discussion)
    assert summary.participant_count > 0
    assert summary.message_count > 0
    print(f"✅ 测试 6 通过: 摘要生成成功 - {summary.participant_count} 参与者, {summary.message_count} 消息")

    # ── 测试 7: 导出/导入 ──
    data = manager.export_discussions()
    assert "node-001" in data

    manager3 = DiscussionManager()
    manager3.import_discussions(data)
    assert manager3.get_discussion("node-001") is not None
    print("✅ 测试 7 通过: 导出/导入正常")

    print("\n🎉 讨论库模块验证全部通过！")


asyncio.run(test_discussion())
