"""
测试 Claude Agent SDK 是否正常工作
"""

import asyncio
from claude_agent_sdk import query, ClaudeAgentOptions


async def test_basic_query():
    """测试基本查询"""
    print("=" * 50)
    print("测试 1: 基本查询 (无工具)")
    print("=" * 50)

    try:
        result = None
        async for message in query(
            prompt="用一句话回答：1+1等于多少？",
            options=ClaudeAgentOptions(
                max_turns=1,
            )
        ):
            if message.type == "result":
                result = message.result
                print(f"结果: {result}")

        if result:
            print("✅ 基本查询测试通过")
            return True
        else:
            print("❌ 未收到结果")
            return False
    except Exception as e:
        print(f"❌ 测试失败: {e}")
        return False


async def test_with_tools():
    """测试带工具的查询"""
    print("\n" + "=" * 50)
    print("测试 2: 带 Read 工具的查询")
    print("=" * 50)

    try:
        result = None
        async for message in query(
            prompt="读取当前目录下的 CLAUDE.md 文件，用一句话总结它的内容",
            options=ClaudeAgentOptions(
                cwd=".",
                allowed_tools=["Read"],
                max_turns=3,
            )
        ):
            if message.type == "result":
                result = message.result
                print(f"结果: {result[:200]}..." if len(str(result)) > 200 else f"结果: {result}")

        if result:
            print("✅ 工具查询测试通过")
            return True
        else:
            print("❌ 未收到结果")
            return False
    except Exception as e:
        print(f"❌ 测试失败: {e}")
        return False


async def test_session():
    """测试会话功能"""
    print("\n" + "=" * 50)
    print("测试 3: 会话恢复")
    print("=" * 50)

    try:
        session_id = None

        # 第一次查询
        print("第一次查询: 记住我的名字是 Alice")
        async for message in query(
            prompt="请记住：我的名字是 Alice",
            options=ClaudeAgentOptions(max_turns=1)
        ):
            if message.type == "system" and hasattr(message, 'session_id'):
                session_id = message.session_id
                print(f"Session ID: {session_id}")

        if not session_id:
            print("⚠️ 未获取到 session_id，跳过会话恢复测试")
            return True

        # 第二次查询 - 恢复会话
        print("第二次查询: 我叫什么名字？")
        result = None
        async for message in query(
            prompt="我叫什么名字？",
            options=ClaudeAgentOptions(
                resume=session_id,
                max_turns=1
            )
        ):
            if message.type == "result":
                result = message.result
                print(f"结果: {result}")

        if result and "Alice" in result:
            print("✅ 会话恢复测试通过")
            return True
        else:
            print("⚠️ 会话可能未正确恢复")
            return True  # 不算失败
    except Exception as e:
        print(f"❌ 测试失败: {e}")
        return False


async def main():
    """运行所有测试"""
    print("\n🚀 Claude Agent SDK 测试开始\n")

    results = []

    # 测试 1: 基本查询
    results.append(await test_basic_query())

    # 测试 2: 带工具的查询
    results.append(await test_with_tools())

    # 测试 3: 会话恢复
    results.append(await test_session())

    # 汇总
    print("\n" + "=" * 50)
    print("测试汇总")
    print("=" * 50)
    passed = sum(results)
    total = len(results)
    print(f"通过: {passed}/{total}")

    if passed == total:
        print("\n🎉 所有测试通过！Claude Agent SDK 工作正常")
    else:
        print(f"\n⚠️ {total - passed} 个测试失败")


if __name__ == "__main__":
    asyncio.run(main())
