"""
写手 Agent

负责为其他 subagent 填充专业知识和配置
"""

from typing import Optional
from pydantic import BaseModel

from .pool_registry import SubagentPool, get_pool


class AgentDefinition(BaseModel):
    """Agent 定义"""
    name: str
    description: str
    tools: list[str] = []
    model: str = "inherit"
    system_prompt: str = ""


class WriterAgent:
    """
    写手 Agent - 为其他 subagent 填充专业知识

    这是固定的 subagent，负责：
    1. 根据任务需求决定需要哪些 subagent
    2. 为选中的 subagent 填充 name, description, 系统提示等
    3. 可选择或创建新的 subagent 槽位
    """

    def __init__(self, pool: SubagentPool = None):
        self.pool = pool or get_pool()

    def define_agent(
        self,
        agent_id: str,
        name: str,
        description: str,
        system_prompt: str = "",
        tools: list[str] = None
    ) -> bool:
        """
        定义一个 agent

        Args:
            agent_id: 目标 agent ID（如 agent_01）
            name: agent 名称
            description: agent 描述（决定何时调用）
            system_prompt: 系统提示
            tools: 可用工具列表

        Returns:
            是否成功
        """
        return self.pool.fill_agent(
            agent_id=agent_id,
            name=name,
            description=description,
            content=system_prompt,
            tools=tools or []
        )

    def create_agent(
        self,
        name: str,
        description: str,
        system_prompt: str = "",
        tools: list[str] = None
    ) -> str:
        """
        创建新的 agent（使用下一个可用槽位）

        Args:
            name: agent 名称
            description: agent 描述
            system_prompt: 系统提示
            tools: 可用工具列表

        Returns:
            新创建的 agent ID
        """
        return self.pool.create_agent_file(
            name=name,
            description=description,
            content=system_prompt,
            tools=tools
        )

    def analyze_task_and_define_agents(self, task_description: str) -> list[AgentDefinition]:
        """
        分析任务并决定需要哪些 agent

        注意：这是一个框架方法，实际的 LLM 调用应该在节点层实现

        Args:
            task_description: 任务描述

        Returns:
            建议的 agent 定义列表
        """
        # 这里只是框架，实际实现需要调用 LLM
        # 返回空列表，等待 LLM 填充
        return []

    def fill_from_definition(self, definition: AgentDefinition, agent_id: str = None) -> str:
        """
        根据定义填充 agent

        Args:
            definition: agent 定义
            agent_id: 目标 agent ID（如果为空，创建新的）

        Returns:
            agent ID
        """
        if agent_id:
            self.define_agent(
                agent_id=agent_id,
                name=definition.name,
                description=definition.description,
                system_prompt=definition.system_prompt,
                tools=definition.tools
            )
            return agent_id
        else:
            return self.create_agent(
                name=definition.name,
                description=definition.description,
                system_prompt=definition.system_prompt,
                tools=definition.tools
            )

    def get_available_slots(self) -> list[str]:
        """获取可用的空槽位"""
        return self.pool.get_available_slots()

    def get_filled_agents(self) -> list[str]:
        """获取已填充的 agent"""
        return self.pool.get_filled_agents()


# 写手 Agent 的系统提示模板
WRITER_SYSTEM_PROMPT = """你是一个专业的 Agent 定义写手。

你的职责是：
1. 分析任务需求
2. 决定需要哪些 subagent
3. 为每个 subagent 填充：
   - name: 唯一标识符（小写字母和连字符）
   - description: 描述何时应该调用此 agent
   - system_prompt: 详细的系统提示，指导 agent 如何工作

输出格式（JSON）：
```json
{
  "agents": [
    {
      "name": "code-reviewer",
      "description": "Review code for quality and best practices. Use proactively after code changes.",
      "system_prompt": "You are a code reviewer...",
      "tools": ["Read", "Grep", "Glob", "Bash"]
    }
  ]
}
```
"""
