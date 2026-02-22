"""
测试协作模式
"""

import sys
import os
import asyncio
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.agents.collaboration import (
    CollaborationMode,
    AgentExecutor,
    ChainCollaboration,
    ParallelCollaboration,
    DiscussionCollaboration,
    execute_collaboration
)


async def test_chain_collaboration():
    """测试链式协作"""
    print("\n测试链式协作...")

    execution_log = []

    async def agent_a(task, context):
        execution_log.append("A")
        return f"{task} -> A"

    async def agent_b(task, context):
        execution_log.append("B")
        return f"{task} -> B"

    async def agent_c(task, context):
        execution_log.append("C")
        return f"{task} -> C"

    agents = [
        AgentExecutor(agent_id="agent_a", name="A", execute_fn=agent_a),
        AgentExecutor(agent_id="agent_b", name="B", execute_fn=agent_b),
        AgentExecutor(agent_id="agent_c", name="C", execute_fn=agent_c),
    ]

    collaboration = ChainCollaboration(agents)
    result = await collaboration.execute("input")

    print(f"  执行顺序: {' -> '.join(execution_log)}")
    print(f"  最终输出: {result.final_output}")

    assert execution_log == ["A", "B", "C"], "应该按顺序执行"
    assert result.success, "应该成功"
    assert result.mode == CollaborationMode.CHAIN, "应该是链式模式"

    print("✓ 链式协作测试通过")


async def test_parallel_collaboration():
    """测试并行协作"""
    print("\n测试并行协作...")

    async def agent_a(task, context):
        await asyncio.sleep(0.1)  # 模拟处理
        return {"a": "result_a"}

    async def agent_b(task, context):
        await asyncio.sleep(0.1)
        return {"b": "result_b"}

    async def agent_c(task, context):
        await asyncio.sleep(0.1)
        return {"c": "result_c"}

    agents = [
        AgentExecutor(agent_id="agent_a", execute_fn=agent_a),
        AgentExecutor(agent_id="agent_b", execute_fn=agent_b),
        AgentExecutor(agent_id="agent_c", execute_fn=agent_c),
    ]

    collaboration = ParallelCollaboration(agents)

    import time
    start = time.time()
    result = await collaboration.execute("input")
    elapsed = time.time() - start

    print(f"  执行时间: {elapsed:.2f}s (并行应该约 0.1s)")
    print(f"  结果: {result.final_output}")

    # 并行执行应该约 0.1s（而不是 0.3s）
    assert elapsed < 0.2, "应该并行执行（总时间 < 0.2s）"
    assert result.success, "应该成功"
    assert result.mode == CollaborationMode.PARALLEL, "应该是并行模式"

    print("✓ 并行协作测试通过")


async def test_discussion_collaboration():
    """测试讨论式协作"""
    print("\n测试讨论式协作...")

    async def agent_a(task, context):
        return {"opinion": "A 认为应该方案1"}

    async def agent_b(task, context):
        return {"opinion": "B 认为应该方案2"}

    agents = [
        AgentExecutor(agent_id="agent_a", execute_fn=agent_a),
        AgentExecutor(agent_id="agent_b", execute_fn=agent_b),
    ]

    collaboration = DiscussionCollaboration(agents)
    result = await collaboration.execute("讨论任务", {})

    print(f"  模式: {result.mode.value}")
    print(f"  成功: {result.success}")

    assert result.success, "应该成功"
    assert result.mode == CollaborationMode.DISCUSSION, "应该是讨论模式"

    print("✓ 讨论式协作测试通过")


async def test_execute_collaboration():
    """测试通用执行函数"""
    print("\n测试通用执行函数...")

    async def simple_agent(task, context):
        return f"processed: {task}"

    agents = [
        AgentExecutor(agent_id="agent_1", execute_fn=simple_agent),
    ]

    # 测试各种模式
    for mode in [CollaborationMode.CHAIN, CollaborationMode.PARALLEL, CollaborationMode.DISCUSSION]:
        result = await execute_collaboration(mode, agents, "test_task")
        print(f"  {mode.value}: success={result.success}")
        assert result.success, f"{mode.value} 模式应该成功"

    print("✓ 通用执行函数测试通过")


async def main():
    print("=" * 50)
    print("测试协作模式")
    print("=" * 50)

    await test_chain_collaboration()
    await test_parallel_collaboration()
    await test_discussion_collaboration()
    await test_execute_collaboration()

    print("=" * 50)
    print("所有测试通过!")
    print("=" * 50)


if __name__ == "__main__":
    asyncio.run(main())
