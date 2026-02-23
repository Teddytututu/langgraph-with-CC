"""
CLI 入口

提供命令行方式运行任务，无需启动 Web 服务。

用法:
    python -m src.main "你的任务描述"
    python -m src.main "你的任务描述" --time 30
"""

import argparse
import asyncio
import json
import sys
import traceback
from datetime import datetime
from pathlib import Path

from src.graph.state import GraphState, TimeBudget
from src.graph.builder import build_graph


async def run_task(task: str, time_minutes: float | None = None) -> dict:
    """
    执行任务

    Args:
        task: 任务描述
        time_minutes: 时间预算（分钟）

    Returns:
        执行结果
    """
    graph = build_graph()

    # 构建初始状态
    initial_state: GraphState = {
        "user_task": task,
        "time_budget": TimeBudget(total_minutes=time_minutes, started_at=datetime.now()) if time_minutes else None,
        "subtasks": [],
        "discussions": {},
        "messages": [],
        "execution_log": [],
        "artifacts": {},
        "phase": "init",
        "iteration": 0,
        "max_iterations": 3,
        "error": None,
        "final_output": None,
    }

    config = {"configurable": {"thread_id": "cli-task"}}

    final_state = None

    print(f"\n{'='*60}")
    print(f"任务: {task}")
    if time_minutes:
        print(f"时间预算: {time_minutes} 分钟")
    print(f"{'='*60}\n")

    try:
        async for event in graph.astream(initial_state, config):
            for node_name, state_update in event.items():
                phase = state_update.get("phase", "")
                print(f"[{node_name}] phase={phase}")

                # 显示子任务进度
                subtasks = state_update.get("subtasks", [])
                for t in subtasks:
                    status_icon = {
                        "pending": "⏳",
                        "running": "🔄",
                        "done": "✅",
                        "failed": "❌",
                        "skipped": "⏭️",
                    }.get(t.status, "❓")
                    print(f"  {status_icon} {t.id}: {t.title}")

                final_state = state_update

                # 检查是否完成
                if state_update.get("final_output"):
                    print(f"\n{'='*60}")
                    print("任务完成!")
                    print(f"{'='*60}")
                    print(state_update["final_output"])
                    return {
                        "success": True,
                        "output": state_update["final_output"],
                        "phase": phase,
                    }

        return {
            "success": True,
            "output": final_state.get("final_output") if final_state else None,
            "phase": final_state.get("phase") if final_state else "unknown",
        }

    except Exception as e:
        # 生成崩溃报告
        crash_report = {
            "error_type": type(e).__name__,
            "error_message": str(e),
            "traceback": traceback.format_exc(),
            "task": task,
            "time": datetime.now().isoformat(),
        }

        # 确保 reports/ 目录存在
        Path("reports").mkdir(exist_ok=True)
        crash_path = Path("reports/crash_report.json")
        with open(crash_path, "w", encoding="utf-8") as f:
            json.dump(crash_report, f, indent=2, ensure_ascii=False)

        print(f"\n❌ 任务失败: {e}")
        print(f"崩溃报告已保存到: {crash_path}")

        return {
            "success": False,
            "error": str(e),
            "crash_report": str(crash_path),
        }


def main():
    """CLI 主入口"""
    parser = argparse.ArgumentParser(
        description="Claude LangGraph 多 Agent 执行器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
    python -m src.main "帮我写一个 Python 爬虫"
    python -m src.main "分析这段代码的性能问题" --time 15
    python -m src.main "设计一个用户认证系统" --time 60
        """,
    )

    parser.add_argument(
        "task",
        help="要执行的任务描述"
    )
    parser.add_argument(
        "--time", "-t",
        type=float,
        default=None,
        help="时间预算（分钟）"
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="以 JSON 格式输出结果"
    )

    args = parser.parse_args()

    # 运行任务
    result = asyncio.run(run_task(args.task, args.time))

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        if not result["success"]:
            sys.exit(1)


if __name__ == "__main__":
    main()
