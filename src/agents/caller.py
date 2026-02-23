"""
Subagent 调用接口

提供统一的 subagent 调用方式，供 Graph 节点使用。
通过 SDK 直接执行，失败时抛出异常。
"""

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

from .subagent_manager import SubagentManager, get_manager, SubagentState
from .pool_registry import SubagentPool, get_pool
from .sdk_executor import SDKExecutor, get_executor, SubagentResult


class SubagentCaller:
    """Subagent 调用器"""

    def __init__(
        self,
        manager: SubagentManager = None,
        pool: SubagentPool = None,
        executor: SDKExecutor = None
    ):
        self.manager = manager or get_manager()
        self.pool = pool or get_pool()
        self.executor = executor or get_executor()

    async def call(self, agent_id: str, context: dict[str, Any]) -> dict[str, Any]:
        """
        调用 subagent 执行任务

        Args:
            agent_id: 要调用的 subagent ID
            context: 传递给 subagent 的上下文

        Returns:
            执行结果
        """
        # 检查 subagent 状态
        state = self.manager.get_state(agent_id)
        if state not in (SubagentState.READY, SubagentState.IN_USE):
            return {
                "success": False,
                "error": f"Subagent {agent_id} 不可用（状态: {state}）",
                "result": None
            }

        # 获取 subagent 模板
        template = self.pool.get_template(agent_id)
        if not template:
            return {
                "success": False,
                "error": f"Subagent {agent_id} 模板不存在",
                "result": None
            }

        # 当模板 content 为空时，使用默认 system_prompt
        system_prompt = template.content
        if not system_prompt:
            system_prompt = f"你是一个专业的 AI 助手，负责执行 {agent_id} 相关任务。请根据上下文完成任务，并以 JSON 格式返回结果。"

        # 标记为使用中
        self.manager.mark_in_use(agent_id)

        try:
            # 使用 SDK 执行器执行
            result: SubagentResult = await self.executor.execute(
                agent_id=agent_id,
                system_prompt=system_prompt,
                context=context,
                tools=template.tools or ["Read", "Write", "Edit", "Bash", "Glob", "Grep"],
                model=template.model,
            )

            if result.success:
                return {
                    "success": True,
                    "agent_id": agent_id,
                    "status": "completed",
                    "result": result.result,
                    "turns": result.turns,
                }
            else:
                # 直接返回错误，不降级
                return {
                    "success": False,
                    "error": result.error,
                    "result": None
                }

        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "result": None
            }

    async def call_planner(self, task: str, time_budget: dict = None) -> dict:
        """调用 planner subagent 进行任务分解"""
        context = {
            "task": task,
            "time_budget": time_budget,
        }
        return await self.call("planner", context)

    async def call_executor(self, subtask: dict, previous_results: list = None) -> dict:
        """调用 executor subagent 执行子任务"""
        context = {
            "subtask": subtask,
            "previous_results": previous_results or [],
        }
        return await self.call("executor", context)

    async def call_reviewer(self, execution_result: dict, subtask: dict) -> dict:
        """调用 reviewer subagent 进行质量审查"""
        context = {
            "execution_result": execution_result,
            "subtask": subtask,
        }
        return await self.call("reviewer", context)

    async def call_reflector(self, failure_context: dict, subtask: dict) -> dict:
        """调用 reflector subagent 进行反思改进"""
        context = {
            "failure_context": failure_context,
            "subtask": subtask,
        }
        return await self.call("reflector", context)

    async def call_specialist(self, agent_id: str, subtask: dict, previous_results: list = None, time_budget: dict = None) -> dict:
        """调用专业 subagent 执行任务

        Args:
            agent_id: 专业 agent ID
            subtask: 子任务信息（应包含 estimated_minutes）
            previous_results: 前序任务结果
            time_budget: 全局时间预算信息（含 remaining_minutes）
        """
        context: dict = {
            "subtask": subtask,
            "previous_results": previous_results or [],
        }
        if time_budget:
            context["time_budget"] = time_budget
        return await self.call(agent_id, context)

    async def get_or_create_specialist(self, skills: list[str], task_description: str) -> Optional[str]:
        """
        获取或创建专业 subagent

        Args:
            skills: 需要的技能列表
            task_description: 任务描述（用于创建新 subagent）

        Returns:
            agent_id 或 None
        """
        # 1. 首先查找已有合适的专业 subagent
        agent_id = self.manager.get_by_skills(skills)
        if agent_id:
            return agent_id

        # 2. 查找 ready 状态的 subagent
        agent_id = self.manager.get_next_ready(skills)
        if agent_id:
            return agent_id

        # 3. 获取空槽位，用 writer 填充
        agent_id = self.manager.get_next_empty()
        if agent_id:
            # 调用 writer 填充这个槽位
            await self._fill_specialist_slot(agent_id, skills, task_description)
            return agent_id

        # 4. 没有空槽位，循环清空最早使用的
        cleared = self.manager.cycle_clear(1)
        if cleared:
            await self._fill_specialist_slot(cleared[0], skills, task_description)
            return cleared[0]

        # 5. 兜底：强制创建新编号槽位（不应走到这里，但防止返回 None 后误用系统 agent）
        new_id = self.pool.create_agent_file(
            name=f"{'+'.join(skills[:2])}-specialist",
            description=f"专注于 {', '.join(skills)} 的执行专家",
            content=f"你是一个专注于 {', '.join(skills)} 领域的执行专家。请严格按照任务描述完成工作，输出完整、具体的结果。",
            tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep"],
        )
        if new_id:
            self.manager.mark_ready(new_id, name=f"{'+'.join(skills[:2])}-specialist", skills=skills)
            return new_id

        return None

    async def _fill_specialist_slot(self, agent_id: str, skills: list[str], task_description: str) -> bool:
        """
        用 writer 填充专业 subagent 槽位

        Args:
            agent_id: 槽位 ID
            skills: 需要的技能
            task_description: 任务描述

        Returns:
            是否成功
        """
        # 标记为填充中
        self.manager.mark_filling(agent_id)

        # 直接本地生成 specialist 模板，无需调用 writer_1（避免额外 SDK 调用和超时风险）
        skills_str = ", ".join(skills) if skills else "通用"
        name = f"{skills[0]}-specialist" if skills else f"specialist-{agent_id}"
        description = f"专注于 {skills_str} 的执行专家"
        system_prompt = (
            f"你是一个专注于 {skills_str} 领域的执行专家。\n"
            "仅执行系统自检、缺陷定位、bug 修复与修复验证，禁止新增功能或需求外扩展。\n"
            "优先使用 Python 生态执行实现、测试、脚本与验证。\n"
            "每个 major task 完成时必须输出终端摘要，并落盘 reports/{subtask_id}.md 与 reports/{subtask_id}.json。\n"
            f"任务上下文：{task_description[:200]}"
        )

        try:
            self.pool.fill_agent(
                agent_id=agent_id,
                name=name,
                description=description,
                content=system_prompt,
                tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep"]
            )
            self.manager.mark_ready(
                agent_id=agent_id,
                name=name,
                description=description,
                skills=skills
            )
            return True
        except Exception as e:
            logger.error(f"填充 specialist 失败: {e}")
            # 兜底：使用最简模板
            try:
                self.pool.fill_agent(
                    agent_id=agent_id,
                    name=f"Specialist-{agent_id}",
                    description=f"专业技能: {skills_str}",
                    content=f"你是一个{skills_str}专家。请根据任务要求完成工作。\n\n任务: {task_description}",
                    tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep"]
                )
                self.manager.mark_ready(agent_id, skills=skills)
            except Exception:
                pass
        return True

    def complete_subtask(self, agent_id: str):
        """标记子任务完成（保留专业知识）"""
        self.manager.mark_subtask_completed(agent_id)

    def complete_task(self, agent_ids: list[str]):
        """标记总任务完成（清空所有配置）"""
        self.manager.mark_task_completed(agent_ids)


# 全局单例
_caller_instance: Optional[SubagentCaller] = None


def get_caller() -> SubagentCaller:
    """获取全局 SubagentCaller 实例"""
    global _caller_instance
    if _caller_instance is None:
        _caller_instance = SubagentCaller()
    return _caller_instance


async def call_subagent(agent_id: str, context: dict[str, Any]) -> dict[str, Any]:
    """便捷函数：调用 subagent"""
    return await get_caller().call(agent_id, context)
