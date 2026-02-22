"""
测试 SubagentPool 注册表
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.agents.pool_registry import SubagentPool, SubagentTemplate, get_pool, reload_pool


def test_load_templates():
    """测试加载模板"""
    pool = SubagentPool()

    templates = pool.get_all_templates()
    print(f"加载了 {len(templates)} 个模板")

    assert len(templates) > 0, "应该加载至少一个模板"

    # 检查模板结构
    for agent_id, template in list(templates.items())[:3]:
        print(f"  - {agent_id}: name='{template.name}', desc='{template.description[:30]}...'")

    print("✓ 模板加载测试通过")


def test_get_available_slots():
    """测试获取空槽位"""
    pool = SubagentPool()

    slots = pool.get_available_slots()
    print(f"可用槽位: {len(slots)} 个")

    # 因为模板都是空的，应该全部是可用槽位
    all_templates = pool.get_all_templates()
    assert len(slots) == len(all_templates), "空模板应该全部是可用槽位"

    print("✓ 空槽位测试通过")


def test_fill_agent():
    """测试填充 agent"""
    pool = SubagentPool()

    # 填充一个测试 agent
    success = pool.fill_agent(
        agent_id="agent_01",
        name="test-reviewer",
        description="测试用的代码审查 agent",
        content="你是一个代码审查员。",
        tools=["Read", "Grep", "Glob"]
    )

    assert success, "填充应该成功"

    # 验证填充结果
    template = pool.get_template("agent_01")
    assert template is not None, "应该能获取到模板"
    assert template.name == "test-reviewer", "名称应该匹配"
    assert template.is_filled(), "模板应该被标记为已填充"

    # 检查已填充列表
    filled = pool.get_filled_agents()
    assert "agent_01" in filled, "agent_01 应该在已填充列表中"

    # 检查可用槽位减少
    slots = pool.get_available_slots()
    assert "agent_01" not in slots, "agent_01 不应该在可用槽位中"

    print("✓ 填充 agent 测试通过")

    # 清理：恢复空模板
    pool.fill_agent(
        agent_id="agent_01",
        name="",
        description="",
        content="（预留 - 由写手填充）"
    )


def test_create_agent():
    """测试创建新 agent"""
    pool = SubagentPool()

    # 创建一个新 agent
    agent_id = pool.create_agent_file(
        name="new-test-agent",
        description="新创建的测试 agent",
        content="这是一个新创建的 agent。",
        tools=["Read", "Bash"]
    )

    print(f"创建的新 agent ID: {agent_id}")
    assert agent_id, "应该返回有效的 agent ID"

    # 验证创建结果
    template = pool.get_template(agent_id)
    assert template is not None, "应该能获取到新创建的模板"
    assert template.name == "new-test-agent", "名称应该匹配"

    print("✓ 创建 agent 测试通过")

    # 清理
    import os
    file_path = os.path.join(".claude/agents", f"{agent_id}.md")
    if os.path.exists(file_path):
        os.remove(file_path)


def test_find_agents():
    """测试查找 agent"""
    pool = SubagentPool()

    # 先填充一个测试 agent
    pool.fill_agent(
        agent_id="agent_02",
        name="python-expert",
        description="Python 编程专家，处理 Python 相关任务",
        content="你是 Python 专家。"
    )

    # 按名称查找
    found_id = pool.find_by_name("python-expert")
    assert found_id == "agent_02", "应该能按名称找到 agent"

    # 按关键词查找
    results = pool.find_by_description_keyword("Python")
    assert "agent_02" in results, "应该能按关键词找到 agent"

    print("✓ 查找 agent 测试通过")

    # 清理
    pool.fill_agent(
        agent_id="agent_02",
        name="",
        description="",
        content="（预留 - 由写手填充）"
    )


def test_singleton():
    """测试单例模式"""
    pool1 = get_pool()
    pool2 = get_pool()

    assert pool1 is pool2, "应该返回同一个实例"

    print("✓ 单例模式测试通过")


if __name__ == "__main__":
    print("=" * 50)
    print("测试 SubagentPool 注册表")
    print("=" * 50)

    test_load_templates()
    test_get_available_slots()
    test_fill_agent()
    test_create_agent()
    test_find_agents()
    test_singleton()

    print("=" * 50)
    print("所有测试通过!")
    print("=" * 50)
