"""
测试 SDK 执行器

验证 subagent 执行流程
"""
import asyncio
import sys
sys.path.insert(0, ".")

from src.agents.sdk_executor import get_executor, execute_subagent
from src.agents.caller import get_caller


async def test_executor_status():
    """测试执行器状态"""
    executor = get_executor()
    print("=" * 50)
    print("执行器状态检查")
    print("=" * 50)
    print(f"SDK 可用: {executor.sdk_executor._sdk_available}")
    print(f"API 配置: {executor.sdk_executor._api_configured}")
    print(f"执行模式: {executor.mode}")
    print()


async def test_caller_status():
    """测试调用器状态"""
    caller = get_caller()
    print("=" * 50)
    print("调用器状态检查")
    print("=" * 50)
    print(f"执行模式: {caller.mode}")
    print()


async def test_simple_call():
    """测试简单调用"""
    print("=" * 50)
    print("测试简单 subagent 调用")
    print("=" * 50)

    caller = get_caller()

    # 测试调用 planner
    result = await caller.call_planner(
        task="写一个 hello world 程序",
        time_budget={"total_minutes": 10}
    )

    print(f"成功: {result.get('success')}")
    print(f"模式: {result.get('mode')}")
    print(f"状态: {result.get('status')}")

    if result.get("result"):
        print(f"结果类型: {type(result.get('result'))}")
        # 如果结果是列表，显示子任务
        if isinstance(result.get("result"), list):
            print("子任务:")
            for task in result.get("result"):
                print(f"  - {task.get('id')}: {task.get('title')}")

    if result.get("error"):
        print(f"错误: {result.get('error')}")

    print()
    return result


async def main():
    """主测试函数"""
    print("\n" + "=" * 60)
    print("执行层测试")
    print("=" * 60 + "\n")

    await test_executor_status()
    await test_caller_status()

    # 注意：实际调用需要 API 可用
    # await test_simple_call()

    print("测试完成！")


if __name__ == "__main__":
    asyncio.run(main())
