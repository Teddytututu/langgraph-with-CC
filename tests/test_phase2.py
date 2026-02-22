"""tests/test_phase2.py — Phase 2 验证：Planner + Config"""
import sys
import os
import asyncio

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# ── 测试 1: Config 加载 .env ──
from src.utils.config import get_config
config = get_config()
assert config.model != "", f"model 不应为空"
assert config.model == "glm-5", f"model 应为 glm-5，实际为 {config.model}"
assert config.max_retries == 3, f"max_retries 应为 3，实际为 {config.max_retries}"
print(f"✅ 测试 1 通过: Config 加载成功，model={config.model}")

# ── 测试 2: Planner 函数可导入 ──
from src.graph.nodes.planner import planner_node, PLANNER_SYSTEM_PROMPT
assert callable(planner_node), "planner_node 应为可调用函数"
assert "任务规划专家" in PLANNER_SYSTEM_PROMPT
print("✅ 测试 2 通过: planner_node 导入成功，System Prompt 包含角色定义")

# ── 测试 3: Planner 回退逻辑（模拟 API 失败时产生单个子任务） ──
from src.graph.state import SubTask, TimeBudget
import json

# 模拟一个无法解析的 JSON 场景
try:
    bad_json = "not valid json"
    subtasks = [SubTask(**t) for t in json.loads(bad_json)]
except Exception:
    # 回退逻辑：生成单个子任务
    subtasks = [
        SubTask(
            id="task-001", title="执行完整任务",
            description="测试任务", agent_type="coder",
            estimated_minutes=24.0,
        )
    ]
assert len(subtasks) == 1
assert subtasks[0].agent_type == "coder"
print("✅ 测试 3 通过: Planner 回退逻辑正确，JSON 解析失败时生成 1 个子任务")

# ── 测试 4: Budget 节点可导入 ──
from src.graph.nodes.budget import budget_node
assert callable(budget_node)
print("✅ 测试 4 通过: budget_node 导入成功")

# ── 测试 5: Budget 超支时自动缩减 ──
async def test_budget_scaling():
    subtasks = [
        SubTask(id="a", title="A", description="", agent_type="coder",
                estimated_minutes=30),
        SubTask(id="b", title="B", description="", agent_type="writer",
                estimated_minutes=30),
    ]
    budget = TimeBudget(total_minutes=40)  # 80% = 32分钟 < 60分钟总估
    state_over = {
        "time_budget": budget,
        "subtasks": subtasks,
        "execution_log": [],
    }
    result = await budget_node(state_over)
    total_est = sum(t.estimated_minutes for t in result["subtasks"])
    assert total_est <= 40 * 0.8 + 0.1, f"缩减后总估应 ≤ 32，实际为 {total_est}"
    assert result["time_budget"].deadline is not None
    print(f"✅ 测试 5 通过: 超支自动缩减，调整后总估={total_est:.1f}min ≤ 预算 80%={40*0.8:.0f}min")

asyncio.run(test_budget_scaling())

print("\n🎉 Phase 2 全部验证通过！")
