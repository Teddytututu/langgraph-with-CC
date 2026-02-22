"""
SDK 执行器

使用 claude-agent-sdk 直接执行 subagent，避免文件系统轮询。
"""

import os
import json
import asyncio
from typing import Any, Optional
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class SubagentResult:
    """Subagent 执行结果"""
    success: bool
    result: Any = None
    error: str | None = None
    messages: list[dict] = field(default_factory=list)
    turns: int = 0
    cost_usd: float = 0.0
    completed_at: str = ""


class SDKExecutor:
    """
    使用 Claude Agent SDK 执行 subagent

    核心功能：
    1. 直接调用 claude-agent-sdk 执行任务
    2. 支持自定义 API 端点
    3. 管理执行上下文和结果
    """

    def __init__(self):
        self._sdk_available = self._check_sdk()
        self._api_configured = self._check_api_config()

    def _check_sdk(self) -> bool:
        """检查 SDK 是否可用"""
        try:
            import claude_agent_sdk
            return True
        except ImportError:
            return False

    def _check_api_config(self) -> bool:
        """检查 API 是否已配置"""
        # 检查环境变量
        has_key = bool(os.getenv("ANTHROPIC_API_KEY"))
        has_token = bool(os.getenv("ANTHROPIC_AUTH_TOKEN"))
        return has_key or has_token

    @property
    def is_available(self) -> bool:
        """执行器是否可用"""
        return self._sdk_available and self._api_configured

    async def execute(
        self,
        agent_id: str,
        system_prompt: str,
        context: dict[str, Any],
        tools: list[str] = None,
        model: str = None,
        max_turns: int = 20,
        cwd: str = None,
    ) -> SubagentResult:
        """
        执行 subagent

        Args:
            agent_id: Subagent ID
            system_prompt: 系统提示词
            context: 执行上下文（包含任务信息）
            tools: 允许使用的工具列表
            model: 使用的模型
            max_turns: 最大轮次
            cwd: 工作目录

        Returns:
            SubagentResult 执行结果
        """
        if not self._sdk_available:
            return SubagentResult(
                success=False,
                error="claude-agent-sdk 未安装。请运行: pip install claude-agent-sdk"
            )

        if not self._api_configured:
            return SubagentResult(
                success=False,
                error="API 未配置。请设置 ANTHROPIC_API_KEY 或 ANTHROPIC_AUTH_TOKEN 环境变量"
            )

        try:
            from claude_agent_sdk import query, ClaudeAgentOptions

            # 构建任务提示
            task_prompt = self._build_task_prompt(context)

            # 默认工具列表
            if tools is None:
                tools = ["Read", "Write", "Edit", "Bash", "Glob", "Grep"]

            # 配置选项
            options_kwargs = {
                "allowed_tools": tools,
                "system_prompt": system_prompt,
            }

            # 模型配置
            if model and model != "inherit":
                options_kwargs["model"] = model

            # 工作目录
            if cwd:
                options_kwargs["cwd"] = cwd

            options = ClaudeAgentOptions(**options_kwargs)

            # 执行
            result_data = []
            messages = []
            turns = 0

            async for message in query(prompt=task_prompt, options=options):
                turns += 1
                msg_dict = self._parse_message(message)
                messages.append(msg_dict)

                # 收集结果
                if hasattr(message, "result"):
                    result_data = message.result
                elif hasattr(message, "content"):
                    result_data.append(message.content)

            # 构建结果
            final_result = result_data if result_data else messages[-1] if messages else None

            return SubagentResult(
                success=True,
                result=final_result,
                messages=messages,
                turns=turns,
                completed_at=datetime.now().isoformat(),
            )

        except Exception as e:
            return SubagentResult(
                success=False,
                error=f"执行失败: {str(e)}"
            )

    def _build_task_prompt(self, context: dict[str, Any]) -> str:
        """构建任务提示"""
        parts = []

        # 任务描述
        if "task" in context:
            parts.append(f"## 任务\n{context['task']}\n")

        # 子任务信息
        if "subtask" in context:
            subtask = context["subtask"]
            parts.append("## 子任务")
            parts.append(f"- ID: {subtask.get('id', 'unknown')}")
            parts.append(f"- 标题: {subtask.get('title', '')}")
            parts.append(f"- 描述: {subtask.get('description', '')}")
            if subtask.get("agent_type"):
                parts.append(f"- 类型: {subtask['agent_type']}")
            parts.append("")

        # 时间预算
        if "time_budget" in context:
            budget = context["time_budget"]
            parts.append("## 时间预算")
            parts.append(f"- 总时间: {budget.get('total_minutes', 'N/A')} 分钟")
            parts.append(f"- 剩余时间: {budget.get('remaining_minutes', 'N/A')} 分钟")
            parts.append("")

        # 前序结果
        if "previous_results" in context and context["previous_results"]:
            parts.append("## 前序任务结果")
            for prev in context["previous_results"]:
                parts.append(f"### {prev.get('title', '任务')}")
                parts.append(str(prev.get("result", "")))
            parts.append("")

        # 执行结果（用于审查）
        if "execution_result" in context:
            parts.append("## 执行结果")
            result = context["execution_result"]
            parts.append(f"- 状态: {result.get('status', 'unknown')}")
            parts.append(f"- 结果: {result.get('result', '')}")
            parts.append("")

        # 失败上下文（用于反思）
        if "failure_context" in context:
            parts.append("## 失败信息")
            failure = context["failure_context"]
            parts.append(f"- 问题: {failure.get('issues', [])}")
            parts.append(f"- 重试次数: {failure.get('retry_count', 0)}")
            parts.append(f"- 上次结果: {failure.get('last_result', '')}")
            parts.append("")

        # 输出格式要求
        parts.append("## 输出要求")
        parts.append("请按照任务要求输出结果。如果是结构化数据，请使用 JSON 格式。")

        return "\n".join(parts)

    def _parse_message(self, message) -> dict:
        """解析消息为字典"""
        if hasattr(message, "__dict__"):
            return {
                "type": type(message).__name__,
                "content": str(getattr(message, "content", "")),
            }
        return {"raw": str(message)}


class FallbackExecutor:
    """
    降级执行器

    当 SDK 不可用时，返回提示信息，等待外部执行
    """

    def __init__(self, bridge=None):
        from .executor_bridge import get_bridge
        self.bridge = bridge or get_bridge()

    async def execute(
        self,
        agent_id: str,
        system_prompt: str,
        context: dict[str, Any],
        tools: list[str] = None,
        **kwargs
    ) -> SubagentResult:
        """
        创建调用指令，等待外部执行

        这个方法不会真正执行，而是将调用信息写入文件，
        等待 CLAUDE.md 或其他外部系统来执行
        """
        # 创建调用
        call_id = self.bridge.create_call(
            agent_id=agent_id,
            system_prompt=system_prompt,
            context=context,
            tools=tools or [],
        )

        return SubagentResult(
            success=True,
            result={
                "call_id": call_id,
                "status": "pending_external_execution",
                "message": "SDK 不可用，已创建调用指令等待外部执行",
            },
        )

    def check_result(self, call_id: str) -> Optional[SubagentResult]:
        """检查外部执行的结果"""
        result = self.bridge.get_result(call_id)
        if result:
            return SubagentResult(
                success=result.get("success", True),
                result=result.get("result"),
                error=result.get("error"),
                completed_at=result.get("completed_at", ""),
            )
        return None


class HybridExecutor:
    """
    混合执行器

    优先使用 SDK 执行，SDK 不可用时降级到文件方式
    """

    def __init__(self):
        self.sdk_executor = SDKExecutor()
        self.fallback_executor = FallbackExecutor()

    async def execute(
        self,
        agent_id: str,
        system_prompt: str,
        context: dict[str, Any],
        tools: list[str] = None,
        model: str = None,
        **kwargs
    ) -> SubagentResult:
        """执行 subagent"""
        if self.sdk_executor.is_available:
            # SDK 可用，直接执行
            return await self.sdk_executor.execute(
                agent_id=agent_id,
                system_prompt=system_prompt,
                context=context,
                tools=tools,
                model=model,
                **kwargs
            )
        else:
            # SDK 不可用，降级到文件方式
            return await self.fallback_executor.execute(
                agent_id=agent_id,
                system_prompt=system_prompt,
                context=context,
                tools=tools,
                **kwargs
            )

    @property
    def mode(self) -> str:
        """当前执行模式"""
        return "sdk" if self.sdk_executor.is_available else "fallback"


# 全局单例
_executor_instance: Optional[HybridExecutor] = None


def get_executor() -> HybridExecutor:
    """获取全局执行器实例"""
    global _executor_instance
    if _executor_instance is None:
        _executor_instance = HybridExecutor()
    return _executor_instance


async def execute_subagent(
    agent_id: str,
    system_prompt: str,
    context: dict[str, Any],
    **kwargs
) -> SubagentResult:
    """便捷函数：执行 subagent"""
    return await get_executor().execute(
        agent_id=agent_id,
        system_prompt=system_prompt,
        context=context,
        **kwargs
    )
