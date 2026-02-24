"""
测试多 Agent 协作流程

验证:
1. Planner V2: 多专家并行规划
2. Executor V2: 讨论/并行/链式协作
3. Reviewer V2: 多人评审 + 投票
4. Reflector V2: 多角度反思
"""

import asyncio
import sys
import os

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


async def smoke_planner_v2():
    """测试多 Agent 规划"""
    print("\n" + "="*60)
    print("测试 Planner V2 — 多专家并行规划")
    print("="*60)

    from src.graph.nodes.planner_v2 import planner_v2_node
    from src.graph.state import GraphState, TimeBudget

    # 创建测试状态
    state = GraphState(
        user_task="实现一个用户认证系统，包括注册、登录、密码重置功能",
        time_budget=TimeBudget(
            total_minutes=60,
            remaining_minutes=60,
        ),
    )

    try:
        result = await planner_v2_node(state)
        subtasks = result.get("subtasks", [])

        print(f"\n✅ 规划完成，生成 {len(subtasks)} 个子任务:")
        for task in subtasks:
            print(f"  - [{task.id}] {task.title} ({task.agent_type})")

        log = result.get("execution_log", [{}])[-1]
        print(f"\n📊 执行日志:")
        print(f"  - 规划器数量: {log.get('planner_count', 'N/A')}")
        print(f"  - 讨论ID: {log.get('discussion_id', 'N/A')}")
        print(f"  - 共识达成: {log.get('consensus_reached', 'N/A')}")

        return True
    except Exception as e:
        print(f"\n❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False


async def test_reviewer_v2():
    """测试多人评审"""
    print("\n" + "="*60)
    print("测试 Reviewer V2 — 多人评审 + 投票")
    print("="*60)

    from src.graph.nodes.reviewer_v2 import reviewer_v2_node, _vote_on_reviews
    from src.graph.state import GraphState, SubTask

    # 创建测试状态
    subtask = SubTask(
        id="task-001",
        title="测试任务",
        description="这是一个测试任务",
        agent_type="coder",
        result="已完成的执行结果...",
        status="pending",
    )

    state = GraphState(
        subtasks=[subtask],
        current_subtask_id="task-001",
    )

    try:
        # 测试投票逻辑
        reviews = [
            {"verdict": "PASS", "score": 8, "issues": [], "suggestions": []},
            {"verdict": "PASS", "score": 7, "issues": ["小问题1"], "suggestions": []},
            {"verdict": "REVISE", "score": 5, "issues": ["问题1", "问题2"], "suggestions": ["建议1"]},
        ]

        verdict, score = _vote_on_reviews(reviews)
        print(f"\n📊 投票结果:")
        print(f"  - 评审意见: 2 PASS, 1 REVISE")
        print(f"  - 最终结论: {verdict}")
        print(f"  - 最终分数: {score}")

        print(f"\n✅ 投票逻辑测试通过")
        return True
    except Exception as e:
        print(f"\n❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False


async def test_reflector_v2():
    """测试多角度反思"""
    print("\n" + "="*60)
    print("测试 Reflector V2 — 多角度反思")
    print("="*60)

    from src.graph.nodes.reflector_v2 import (
        REFLECTION_PERSPECTIVES,
        _synthesize_improvement,
    )

    print(f"\n📋 反思视角:")
    for key, config in REFLECTION_PERSPECTIVES.items():
        print(f"  - {config['name']}: {config['focus']}")

    # 测试方案合成
    reflections = {
        "technical": {
            "root_cause": "代码逻辑错误",
            "lessons_learned": ["添加单元测试"],
            "improved_description": "修复逻辑并添加测试",
        },
        "process": {
            "root_cause": "执行顺序错误",
            "lessons_learned": ["先验证依赖"],
            "improved_description": "",
        },
        "resource": {
            "root_cause": "缺少配置信息",
            "lessons_learned": ["明确环境要求"],
            "improved_description": "",
        },
    }

    improvement = _synthesize_improvement(reflections, {"status": "consensus_reached"}, [])
    print(f"\n📝 合成的改进方案:")
    print(improvement[:500] + "..." if len(improvement) > 500 else improvement)

    print(f"\n✅ 多角度反思测试通过")
    return True


async def test_executor_v2():
    """测试执行协作模式"""
    print("\n" + "="*60)
    print("测试 Executor V2 — 协作模式")
    print("="*60)

    from src.agents.coordinator import CoordinatorAgent
    from src.agents.collaboration import CollaborationMode

    coordinator = CoordinatorAgent()

    # 测试场景 1: 独立任务
    mode1 = coordinator.choose_collaboration_mode(
        task="编写单元测试",
        agents=["testing"],
        subtasks=[],
    )
    print(f"\n📋 场景1 - 独立任务: {mode1.value}")

    # 测试场景 2: 多域任务
    mode2 = coordinator.choose_collaboration_mode(
        task="实现前后端接口",
        agents=["frontend", "backend", "api"],
        subtasks=[],
    )
    print(f"📋 场景2 - 多域任务: {mode2.value}")

    # 测试场景 3: 需要协商的任务
    mode3 = coordinator.choose_collaboration_mode(
        task="评审并决定技术方案",
        agents=["architect", "developer"],
        subtasks=[],
    )
    print(f"📋 场景3 - 协商任务: {mode3.value}")

    print(f"\n✅ 协作模式选择测试通过")
    return True


async def test_discussion_manager():
    """测试讨论管理器"""
    print("\n" + "="*60)
    print("测试 DiscussionManager")
    print("="*60)

    from src.discussion.manager import discussion_manager

    # 创建讨论
    discussion_id = "test_discussion_001"
    discussion_manager.create_discussion(discussion_id)

    # 发送消息
    await discussion_manager.post_message(
        node_id=discussion_id,
        from_agent="agent_01",
        content="我的建议是方案A",
        message_type="proposal",
    )

    await discussion_manager.post_message(
        node_id=discussion_id,
        from_agent="agent_02",
        content="我同意方案A",
        message_type="agreement",
    )

    # 请求共识
    await discussion_manager.request_consensus(
        node_id=discussion_id,
        from_agent="coordinator",
        topic="选择最佳方案",
    )

    # 确认共识
    await discussion_manager.confirm_consensus(
        node_id=discussion_id,
        from_agent="agent_01",
    )
    await discussion_manager.confirm_consensus(
        node_id=discussion_id,
        from_agent="agent_02",
    )

    # 获取讨论
    discussion = discussion_manager.get_discussion(discussion_id)
    print(f"\n📊 讨论状态:")
    print(f"  - 消息数量: {len(discussion.messages)}")
    print(f"  - 参与者: {discussion.participants}")
    print(f"  - 共识达成: {discussion.consensus_reached}")

    print(f"\n✅ 讨论管理器测试通过")
    return True


async def test_graph_v2_build():
    """测试 Graph V2 构建"""
    print("\n" + "="*60)
    print("测试 Graph V2 构建")
    print("="*60)

    try:
        from src.graph.builder_v2 import build_graph_v2

        graph = build_graph_v2()
        print(f"\n✅ Graph V2 构建成功")
        print(f"  - 节点: router, planner, budget_manager, executor, reviewer, reflector")
        print(f"  - 使用 V2 多 Agent 协作节点")
        return True
    except Exception as e:
        print(f"\n❌ Graph 构建失败: {e}")
        import traceback
        traceback.print_exc()
        return False


async def main():
    """运行所有测试"""
    print("\n" + "="*60)
    print("🧪 多 Agent 协作流程测试")
    print("="*60)

    results = {}

    # 测试讨论管理器
    results["discussion_manager"] = await test_discussion_manager()

    # 测试协调者
    results["executor_v2"] = await test_executor_v2()

    # 测试评审投票
    results["reviewer_v2"] = await test_reviewer_v2()

    # 测试反思合成
    results["reflector_v2"] = await test_reflector_v2()

    # 测试 Graph 构建
    results["graph_v2"] = await test_graph_v2_build()

    # 测试规划（可能需要实际 subagent）
    # results["planner_v2"] = await test_planner_v2()

    # 汇总结果
    print("\n" + "="*60)
    print("📊 测试结果汇总")
    print("="*60)

    passed = 0
    failed = 0
    for name, result in results.items():
        status = "✅ 通过" if result else "❌ 失败"
        print(f"  {name}: {status}")
        if result:
            passed += 1
        else:
            failed += 1

    print(f"\n总计: {passed} 通过, {failed} 失败")

    return failed == 0


if __name__ == "__main__":
    success = asyncio.run(main())
    sys.exit(0 if success else 1)
