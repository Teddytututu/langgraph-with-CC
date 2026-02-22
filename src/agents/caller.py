"""
Subagent 调用接口

提供统一的 subagent 调用方式，供 Graph 节点使用。
支持两种模式：
1. SDK 模式：直接通过 claude-agent-sdk 执行（推荐）
2. 文件模式：写入 pending_calls.json 等待外部执行
"""

from typing import Any, Optional

from .subagent_manager import SubagentManager, get_manager, SubagentState
from .pool_registry import SubagentPool, get_pool
from .sdk_executor import HybridExecutor, get_executor, SubagentResult


class SubagentCaller:
    """Subagent 调用器"""

    def __init__(
        self,
        manager: SubagentManager = None,
        pool: SubagentPool = None,
        executor: HybridExecutor = None
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

        # 标记为使用中
        self.manager.mark_in_use(agent_id)

        try:
            # 使用执行器执行
            result: SubagentResult = await self.executor.execute(
                agent_id=agent_id,
                system_prompt=template.content,
                context=context,
                tools=template.tools,
                model=template.model,
            )

            # 处理执行结果
            if result.success:
                # 检查是否是降级模式（需要外部执行）
                if isinstance(result.result, dict) and result.result.get("status") == "pending_external_execution":
                    return {
                        "success": True,
                        "call_id": result.result.get("call_id"),
                        "agent_id": agent_id,
                        "status": "pending_execution",
                        "result": None,
                        "mode": "fallback",
                        "call_info": {
                            "agent_id": agent_id,
                            "name": template.name,
                            "description": template.description,
                            "tools": template.tools,
                            "system_prompt": template.content,
                            "context": context,
                        }
                    }

                # SDK 直接执行成功
                return {
                    "success": True,
                    "agent_id": agent_id,
                    "status": "completed",
                    "result": result.result,
                    "mode": "sdk",
                    "turns": result.turns,
                }
            else:
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

    def check_result(self, call_id: str) -> dict[str, Any]:
        """
        检查调用结果（仅用于降级模式）

        Args:
            call_id: 调用 ID

        Returns:
            包含结果状态和数据的字典
        """
        result = self.executor.fallback_executor.check_result(call_id)

        if result is None:
            return {
                "status": "pending",
                "result": None,
                "completed": False
            }

        return {
            "status": "completed",
            "result": result.result,
            "success": result.success,
            "error": result.error,
            "completed": True
        }

    @property
    def mode(self) -> str:
        """当前执行模式"""
        return self.executor.mode

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

    async def call_specialist(self, agent_id: str, subtask: dict, previous_results: list = None) -> dict:
        """调用专业 subagent 执行任务"""
        context = {
            "subtask": subtask,
            "previous_results": previous_results or [],
        }
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

        # 3. 获取空槽位，让写手填充
        agent_id = self.manager.get_next_empty()
        if agent_id:
            # 标记为填充中
            self.manager.mark_filling(agent_id)
            return agent_id

        # 4. 没有空槽位，循环清空最早使用的
        cleared = self.manager.cycle_clear(1)
        if cleared:
            return cleared[0]

        return None

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
