"""
Executor V2 — 集成讨论协作的执行节点

完整实现三种协作模式:
- Chain: 顺序执行，结果传递
- Parallel: 并行执行，结果合并
- Discussion: 讨论+协商+执行
"""

import asyncio
from datetime import datetime
from typing import Optional

from src.graph.state import GraphState, SubTask
from src.agents.caller import get_caller
from src.agents.coordinator import CoordinatorAgent
from src.agents.collaboration import (
    CollaborationMode, AgentExecutor, execute_collaboration,
)
from src.discussion.manager import discussion_manager


_coordinator = CoordinatorAgent()


from src.graph.nodes.executor import executor_node


async def executor_v2_node(state: GraphState) -> dict:
    """V2 执行入口：复用严格策略执行器，避免约束被绕过。"""
    return await executor_node(state)


async def _execute_with_discussion(
    caller,
    task: SubTask,
    previous_results: list,
) -> dict:
    """
    讨论协作模式执行

    流程:
    1. 创建讨论主题
    2. 各专家发表意见
    3. 协商达成共识
    4. 按共识执行
    """
    domains = task.knowledge_domains or [task.agent_type]
    discussion_id = f"exec_{task.id}_{datetime.now().strftime('%H%M%S')}"

    discussion_manager.create_discussion(discussion_id)

    # 1. 为每个知识域创建专家并发表意见
    agent_executors = []
    opinions = {}

    for domain in domains:
        specialist_id = await caller.get_or_create_specialist(
            skills=[domain],
            task_description=task.description,
        )

        if specialist_id:
            agent_executors.append(AgentExecutor(
                agent_id=specialist_id,
                name=f"Expert-{domain}",
                execute_fn=lambda t, ctx, sid=specialist_id: _execute_specialist(
                    caller, sid, task, previous_results
                ),
            ))

    if not agent_executors:
        # 没有专家可用，降级为普通执行
        return await _fallback_execution(caller, task, previous_results)

    # 2. 各专家发表初步意见（并行）
    async def gather_opinion(agent: AgentExecutor) -> dict:
        try:
            opinion = await agent.execute_fn(task, {})
            return {
                "agent_id": agent.agent_id,
                "opinion": opinion,
                "success": True,
            }
        except Exception as e:
            return {
                "agent_id": agent.agent_id,
                "error": str(e),
                "success": False,
            }

    # 超时已禁用：让任务自然完成
    opinion_results = await asyncio.gather(*[gather_opinion(a) for a in agent_executors])

    # 3. 将意见发送到讨论库
    for opinion_result in opinion_results:
        if opinion_result.get("success"):
            await discussion_manager.post_message(
                node_id=discussion_id,
                from_agent=opinion_result["agent_id"],
                content=str(opinion_result["opinion"])[:500],
                message_type="proposal",
            )
        else:
            await discussion_manager.post_message(
                node_id=discussion_id,
                from_agent=opinion_result["agent_id"],
                content=f"执行出错: {opinion_result['error']}",
                message_type="error",
            )

    # 4. 请求共识
    await discussion_manager.request_consensus(
        node_id=discussion_id,
        from_agent="coordinator",
        topic=f"决定任务 {task.title} 的最佳执行方案",
    )

    # 5. 等待共识（简化：使用第一个成功的意见）
    successful_opinions = [
        o for o in opinion_results if o.get("success")
    ]

    if successful_opinions:
        # 在实际系统中，这里应该等待讨论管理器的共识信号
        # 简化：选择第一个成功的意见
        for participant in set(o["agent_id"] for o in successful_opinions):
            try:
                await discussion_manager.confirm_consensus(
                    node_id=discussion_id,
                    from_agent=participant,
                )
            except Exception:
                pass

        first_success = successful_opinions[0]
        return {
            "success": True,
            "result": first_success["opinion"],
            "specialist_id": first_success["agent_id"],
        }

    # 所有都失败
    errors = [o.get("error", "unknown") for o in opinion_results]
    return {
        "success": False,
        "error": "; ".join(errors),
        "result": None,
    }


async def _execute_parallel_v2(
    caller,
    task: SubTask,
    previous_results: list,
) -> dict:
    """
    并行协作模式执行（改进版）

    流程:
    1. 为每个知识域创建专家
    2. 并行执行
    3. 合并结果
    """
    domains = task.knowledge_domains or [task.agent_type]

    if len(domains) < 2:
        # 单域，使用链式
        return await _execute_chain(caller, task, previous_results)

    subtask_dict = {
        "id": task.id,
        "title": task.title,
        "description": task.description,
        "agent_type": task.agent_type,
        "knowledge_domains": task.knowledge_domains,
    }

    async def run_one(domain: str) -> dict:
        sid = await caller.get_or_create_specialist(
            skills=[domain],
            task_description=task.description,
        )
        if sid:
            return await caller.call_specialist(
                agent_id=sid,
                subtask=subtask_dict,
                previous_results=previous_results,
            )
        return await caller.call_executor(
            subtask=subtask_dict,
            previous_results=previous_results,
        )

    # 超时已禁用：让任务自然完成
    results = await asyncio.gather(*[run_one(d) for d in domains])

    # 合并结果
    merged_parts = []
    first_success = None
    for r in results:
        if isinstance(r, dict) and r.get("success"):
            if first_success is None:
                first_success = r
            data = r.get("result")
            if data:
                merged_parts.append(str(data))

    if first_success is None:
        errors = [r.get("error", "unknown") for r in results if isinstance(r, dict)]
        return {"success": False, "error": "; ".join(errors), "result": None}

    merged_result = "\n\n---\n\n".join(merged_parts) if merged_parts else first_success.get("result")
    return {"success": True, "result": merged_result, "specialist_id": first_success.get("agent_id")}


async def _execute_chain(
    caller,
    task: SubTask,
    previous_results: list,
) -> dict:
    """
    链式协作模式执行

    流程:
    1. 获取或创建专家
    2. 执行任务
    3. 返回结果
    """
    specialist_id = await caller.get_or_create_specialist(
        skills=task.knowledge_domains,
        task_description=task.description,
    )

    subtask_dict = {
        "id": task.id,
        "title": task.title,
        "description": task.description,
        "agent_type": task.agent_type,
        "knowledge_domains": task.knowledge_domains,
    }

    # 超时已禁用：让任务自然完成
    if specialist_id:
        call_result = await caller.call_specialist(
            agent_id=specialist_id,
            subtask=subtask_dict,
            previous_results=previous_results,
        )
    else:
        call_result = await caller.call_executor(
            subtask=subtask_dict,
            previous_results=previous_results,
        )

    return call_result


async def _execute_specialist(
    caller,
    specialist_id: str,
    task: SubTask,
    previous_results: list
) -> dict:
    """执行单个专家任务"""
    subtask_dict = {
        "id": task.id,
        "title": task.title,
        "description": task.description,
        "agent_type": task.agent_type,
        "knowledge_domains": task.knowledge_domains,
    }

    return await caller.call_specialist(
        agent_id=specialist_id,
        subtask=subtask_dict,
        previous_results=previous_results,
    )


async def _fallback_execution(
    caller,
    task: SubTask,
    previous_results: list,
) -> dict:
    """降级执行（当没有专家可用时）"""
    subtask_dict = {
        "id": task.id,
        "title": task.title,
        "description": task.description,
        "agent_type": task.agent_type,
        "knowledge_domains": task.knowledge_domains,
    }

    # 超时已禁用：让任务自然完成
    return await caller.call_executor(
        subtask=subtask_dict,
        previous_results=previous_results,
    )


def _task_dependencies(task: SubTask) -> list[str]:
    deps = getattr(task, "dependencies", None)
    return [d for d in (deps or []) if d]


def _find_task_by_id(subtasks: list[SubTask], task_id: str | None) -> Optional[SubTask]:
    if not task_id:
        return None
    return next((t for t in subtasks if t.id == task_id), None)


def _is_ready(task: SubTask, done_ids: set[str]) -> bool:
    deps = _task_dependencies(task)
    return task.status == "pending" and all(d in done_ids for d in deps)


def _collect_ready_tasks(state: GraphState) -> list[SubTask]:
    subtasks = state.get("subtasks", [])
    done_ids = {t.id for t in subtasks if t.status in ("done", "skipped")}
    return [t for t in sorted(subtasks, key=lambda t: t.priority) if _is_ready(t, done_ids)]


def _find_next_task(state: GraphState) -> Optional[SubTask]:
    """找到依赖已满足的下一个待执行任务"""
    subtasks = state.get("subtasks", [])
    ready_tasks = _collect_ready_tasks(state)
    if ready_tasks:
        current_task = _find_task_by_id(subtasks, state.get("current_subtask_id"))
        if current_task and current_task in ready_tasks:
            return current_task
        return ready_tasks[0]

    # 兼容兜底：当依赖字段缺失或异常时，回退旧 current_subtask_id 语义
    current_task = _find_task_by_id(subtasks, state.get("current_subtask_id"))
    if current_task and current_task.status == "pending":
        return current_task

    return None


def _build_context(state: GraphState, current_task: SubTask) -> list[dict]:
    """收集前序依赖任务的结果"""
    subtasks = state.get("subtasks", [])
    prev_results = []
    for dep_id in current_task.dependencies:
        for t in subtasks:
            if t.id == dep_id and t.result:
                prev_results.append({
                    "task_id": t.id,
                    "title": t.title,
                    "result": t.result,
                })
    return prev_results
