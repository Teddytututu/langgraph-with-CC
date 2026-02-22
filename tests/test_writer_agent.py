"""
测试写手 Agent
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.agents.writer_agent import WriterAgent, AgentDefinition
from src.agents.pool_registry import get_pool, reload_pool


def test_define_agent():
    """测试定义 agent"""
    print("\n测试定义 agent...")

    writer = WriterAgent()
    pool = get_pool()

    # 定义一个新 agent
    success = writer.define_agent(
        agent_id="agent_05",
        name="code-analyzer",
        description="分析代码质量和结构的专家",
        system_prompt="你是一个代码分析专家。分析代码时请注意：1. 代码结构 2. 潜在问题 3. 改进建议",
        tools=["Read", "Grep", "Glob"]
    )

    assert success, "定义应该成功"

    # 验证
    template = pool.get_template("agent_05")
    assert template is not None, "应该能获取到模板"
    assert template.name == "code-analyzer", "名称应该匹配"
    assert "Read" in template.tools, "应该包含 Read 工具"

    print(f"  定义的 agent: {template.name}")
    print(f"  描述: {template.description}")
    print(f"  工具: {template.tools}")

    print("✓ 定义 agent 测试通过")

    # 清理
    pool.fill_agent("agent_05", "", "", "（预留 - 由写手填充）")


def test_create_agent():
    """测试创建新 agent"""
    print("\n测试创建新 agent...")

    writer = WriterAgent()

    # 创建新 agent（自动分配槽位）
    agent_id = writer.create_agent(
        name="test-writer-created",
        description="由写手创建的测试 agent",
        system_prompt="测试用",
        tools=["Read"]
    )

    print(f"  创建的 agent ID: {agent_id}")

    assert agent_id, "应该返回有效的 agent ID"

    # 验证
    template = writer.pool.get_template(agent_id)
    assert template is not None, "应该能获取到模板"
    assert template.name == "test-writer-created", "名称应该匹配"

    print("✓ 创建新 agent 测试通过")

    # 清理
    import os
    file_path = os.path.join(".claude/agents", f"{agent_id}.md")
    if os.path.exists(file_path):
        os.remove(file_path)
    reload_pool()


def test_fill_from_definition():
    """测试从定义填充"""
    print("\n测试从定义填充...")

    writer = WriterAgent()

    # 创建定义
    definition = AgentDefinition(
        name="security-scanner",
        description="安全扫描专家，检查代码中的安全漏洞",
        tools=["Read", "Grep", "Bash"],
        system_prompt="你是一个安全专家。请检查：1. SQL 注入 2. XSS 3. 敏感信息泄露"
    )

    # 填充到指定槽位
    agent_id = writer.fill_from_definition(definition, agent_id="agent_10")

    assert agent_id == "agent_10", "应该返回指定的 agent ID"

    # 验证
    template = writer.pool.get_template("agent_10")
    assert template.name == "security-scanner", "名称应该匹配"

    print(f"  从定义填充: {template.name}")
    print(f"  工具: {template.tools}")

    print("✓ 从定义填充测试通过")

    # 清理
    writer.pool.fill_agent("agent_10", "", "", "（预留 - 由写手填充）")


def test_get_slots():
    """测试获取槽位信息"""
    print("\n测试获取槽位信息...")

    writer = WriterAgent()

    # 获取可用槽位
    available = writer.get_available_slots()
    print(f"  可用槽位: {len(available)} 个")

    # 获取已填充 agent
    filled = writer.get_filled_agents()
    print(f"  已填充: {len(filled)} 个")

    # 填充一个测试
    writer.define_agent("agent_15", "temp-agent", "临时测试", "测试")

    # 再次检查
    available_after = writer.get_available_slots()
    filled_after = writer.get_filled_agents()

    assert len(available_after) == len(available) - 1, "可用槽位应该减少1"
    assert len(filled_after) == len(filled) + 1, "已填充应该增加1"

    print("✓ 获取槽位信息测试通过")

    # 清理
    writer.pool.fill_agent("agent_15", "", "", "（预留 - 由写手填充）")


def main():
    print("=" * 50)
    print("测试写手 Agent")
    print("=" * 50)

    reload_pool()  # 确保从干净状态开始

    test_define_agent()
    test_create_agent()
    test_fill_from_definition()
    test_get_slots()

    print("=" * 50)
    print("所有测试通过!")
    print("=" * 50)


if __name__ == "__main__":
    main()
