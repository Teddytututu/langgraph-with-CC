"""
SDK 执行器

使用 claude-agent-sdk 直接执行 subagent，避免文件系统轮询。
"""

import os
import json
import asyncio
import logging
from typing import Any, Optional
from dataclasses import dataclass, field
from datetime import datetime

# Windows 强制 UTF-8，避免中文传递给 SDK 子进程时乱码
os.environ["PYTHONUTF8"] = "1"
os.environ["PYTHONIOENCODING"] = "utf-8"


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
            options_kwargs: dict = {
                "allowed_tools": tools,
                "permission_mode": "bypassPermissions",
                # 加载用户和项目设置（包含 GLM 路由、CLAUDE.md 等）
                "setting_sources": ["user", "project"],
            }

            # 系统提示：通过 append 追加，不覆盖 Claude Code 默认行为
            if system_prompt:
                options_kwargs["system_prompt"] = {
                    "type": "preset",
                    "preset": "claude_code",
                    "append": system_prompt,
                }

            # 模型配置（不指定则使用用户已配置的默认）
            if model and model not in ("inherit", ""):
                options_kwargs["model"] = model

            # 工作目录
            if cwd:
                options_kwargs["cwd"] = cwd

            options = ClaudeAgentOptions(**options_kwargs)

            # 执行，迭代完所有消息
            result_data: list[str] = []
            messages = []
            turns = 0
            got_result_message = False
            result_message_content: str | None = None

            _log = logging.getLogger("sdk_executor")

            try:
                async for message in query(prompt=task_prompt, options=options):
                    if message is None:
                        continue

                    turns += 1
                    msg_type = type(message).__name__
                    msg_dict = self._parse_message(message)
                    messages.append(msg_dict)

                    if msg_type == "ResultMessage":
                        got_result_message = True
                        # 遍历所有属性，找出非空字符串
                        for attr in ("result", "content", "output", "text", "message"):
                            v = getattr(message, attr, None)
                            if v and isinstance(v, str) and v.strip():
                                result_message_content = v.strip()
                                break
                        # 也检查 content 是 list 的情况
                        if not result_message_content:
                            c = getattr(message, "content", None)
                            if isinstance(c, list):
                                for block in c:
                                    t = getattr(block, "text", None) or (block if isinstance(block, str) else None)
                                    if t and str(t).strip():
                                        result_message_content = str(t).strip()
                                        break
                    elif hasattr(message, "content"):
                        content = message.content
                        if content:
                            if isinstance(content, str) and content.strip():
                                result_data.append(content.strip())
                            elif isinstance(content, list):
                                for block in content:
                                    if hasattr(block, "text") and block.text:
                                        result_data.append(block.text)
            except Exception as stream_err:
                # SDK 有时在拿到 ResultMessage 后仍会抛出进程退出异常，忽略它
                if not got_result_message and not messages:
                    raise stream_err
                # 否则已有足够数据，继续

            # 优先使用 ResultMessage.result，其次是流式收集的内容
            if result_message_content:
                final_result = result_message_content
            elif result_data:
                final_result = "\n".join(result_data)
            else:
                # 从 messages 中找最后一条有实际内容的
                final_result = None
                for msg in reversed(messages):
                    if msg.get("type") == "ResultMessage":
                        v = msg.get("result") or msg.get("raw_result")
                        if v and str(v).strip():
                            final_result = str(v).strip()
                            break
                    else:
                        v = msg.get("content")
                        if v and str(v).strip() and not str(v).startswith("{'type':"):
                            final_result = str(v).strip()
                            break
                _log.warning("All result sources empty for agent=%s turns=%d", agent_id, turns)

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
        msg_type = type(message).__name__
        if msg_type == "ResultMessage":
            # ResultMessage 的实际文字在 .result，不在 .content
            return {
                "type": msg_type,
                "result": str(getattr(message, "result", "") or ""),
                "content": "",   # 保持结构一致
            }
        if hasattr(message, "__dict__"):
            return {
                "type": msg_type,
                "content": str(getattr(message, "content", "") or ""),
            }
        return {"type": "unknown", "content": str(message)}


# 全局单例
_executor_instance: Optional[SDKExecutor] = None


def get_executor() -> SDKExecutor:
    """获取全局执行器实例"""
    global _executor_instance
    if _executor_instance is None:
        _executor_instance = SDKExecutor()

    if not _executor_instance._sdk_available:
        raise RuntimeError(
            "claude-agent-sdk 未安装！\n"
            "请运行: pip install claude-agent-sdk"
        )

    if not _executor_instance._api_configured:
        raise RuntimeError(
            "API 密钥未配置！\n"
            "请设置 ANTHROPIC_API_KEY 或 ANTHROPIC_AUTH_TOKEN 环境变量。"
        )

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
