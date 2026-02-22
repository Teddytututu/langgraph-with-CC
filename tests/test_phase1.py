"""tests/test_phase1.py — Phase 1 验证脚本：State & Graph"""
import sys
import os

# 添加项目根目录到 Python 路径
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# ── 测试 1: State 类能正常实例化 ──
from src.graph.state import SubTask, TimeBudget, GraphState

task = SubTask(
    id="test-001", title="测试任务",
    description="这是一个测试", agent_type="coder"
)
assert task.status == "pending", f"默认状态应为 pending，实际为 {task.status}"
assert task.retry_count == 0
print("✅ 测试 1 通过: SubTask 实例化正常")

budget = TimeBudget(total_minutes=30)
assert budget.elapsed_minutes == 0.0
assert budget.is_overtime == False
print("✅ 测试 2 通过: TimeBudget 实例化正常")

# ── 测试 3: Graph 能成功编译 ──
try:
    from src.graph.builder import build_graph
    graph = build_graph()
    print(f"✅ 测试 3 通过: Graph 编译成功，节点列表: {list(graph.nodes.keys())}")
except Exception as e:
    print(f"❌ 测试 3 失败: Graph 编译出错 — {e}")
    sys.exit(1)

# ── 测试 4: 条件边函数可正常调用 ──
from src.graph.edges import route_after_router
test_state = {
    "time_budget": None,
    "phase": "init",
    "subtasks": [],
}
result = route_after_router(test_state)
assert result == "planning", f"init 阶段应路由到 planning，实际为 {result}"
print(f"✅ 测试 4 通过: route_after_router 返回 '{result}'")

print("\n🎉 Phase 1 全部验证通过！")
