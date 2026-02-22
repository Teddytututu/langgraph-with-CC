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
        分析任务并决定需要哪些 agent（关键词启发式实现）

        根据任务描述中的关键词推断所需 agent 类型，返回 AgentDefinition 列表。
        真正的 LLM 推断在节点层通过 caller.call("writer_1", ...) 实现。

        Args:
            task_description: 任务描述

        Returns:
            建议的 agent 定义列表
        """
        desc_lower = task_description.lower()
        agents: list[AgentDefinition] = []

        # ── 代码类关键词 ──
        code_keywords = ["代码", "编写", "实现", "开发", "脚本", "函数", "类", "模块",
                         "code", "implement", "develop", "script", "function", "class",
                         "python", "javascript", "typescript", "java", "c++", "rust", "go"]
        if any(k in desc_lower for k in code_keywords):
            agents.append(AgentDefinition(
                name="coder",
                description="编写和修改代码，实现功能需求",
                tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep"],
                system_prompt=(
                    "你是一名高级软件工程师。你的职责是根据需求编写高质量代码，"
                    "遵循最佳实践，包括错误处理、类型注解和单元测试。\n"
                    "任务: " + task_description
                ),
            ))

        # ── 研究类关键词 ──
        research_keywords = ["研究", "调研", "查找", "搜索", "文档", "资料", "了解",
                             "research", "search", "find", "document", "analyze", "study",
                             "survey", "investigate", "explore"]
        if any(k in desc_lower for k in research_keywords):
            agents.append(AgentDefinition(
                name="researcher",
                description="搜索信息、阅读文档、整理研究结果",
                tools=["Read", "Glob", "Grep", "Bash"],
                system_prompt=(
                    "你是一名专业研究员。你的职责是收集和整理信息，"
                    "通过阅读文档和代码来回答问题并提供准确的调研报告。\n"
                    "任务: " + task_description
                ),
            ))

        # ── 写作类关键词 ──
        write_keywords = ["文档", "报告", "撰写", "写作", "说明书", "readme", "注释",
                          "document", "write", "report", "readme", "comment", "describe",
                          "summarize", "记录"]
        if any(k in desc_lower for k in write_keywords):
            agents.append(AgentDefinition(
                name="writer",
                description="撰写文档、报告和说明文字",
                tools=["Read", "Write", "Edit"],
                system_prompt=(
                    "你是一名专业技术写手。你的职责是将复杂的技术内容清晰地表达出来，"
                    "编写易于理解的文档和报告。\n"
                    "任务: " + task_description
                ),
            ))

        # ── 分析类关键词 ──
        analyst_keywords = ["分析", "评估", "对比", "比较", "优化", "性能", "安全",
                            "analyze", "evaluate", "compare", "optimize", "performance",
                            "security", "review", "audit", "diagnose", "数据"]
        if any(k in desc_lower for k in analyst_keywords):
            agents.append(AgentDefinition(
                name="analyst",
                description="数据分析、性能评估和方案对比",
                tools=["Read", "Bash", "Glob", "Grep"],
                system_prompt=(
                    "你是一名高级分析师。你的职责是对数据、代码和系统进行深入分析，"
                    "提供可操作的洞见和改进建议。\n"
                    "任务: " + task_description
                ),
            ))

        # 如果没有匹配，返回通用执行 agent
        if not agents:
            agents.append(AgentDefinition(
                name="executor",
                description="通用任务执行 agent",
                tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep"],
                system_prompt=(
                    "你是一名通用 AI 助手。请根据任务要求完成工作，"
                    "并以清晰的格式返回结果。\n"
                    "任务: " + task_description
                ),
            ))

        return agents

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
