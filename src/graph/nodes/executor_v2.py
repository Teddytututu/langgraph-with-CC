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


def _compute_timeout(task: SubTask) -> float:
    """计算子任务执行超时时间（秒）"""
    return max(120.0, min(task.estimated_minutes * 120, 1800.0))


async def executor_v2_node(state: GraphState) -> dict:
    """
    集成讨论协作的执行节点

    流程:
    1. 协调者选择协作模式
    2. 根据模式执行:
       - CHAIN: 顺序执行，结果传递
       - PARALLEL: 并行执行，结果合并
       - DISCUSSION: 通过 DiscussionManager 协商后执行
    3. 返回执行结果
    """
    caller = get_caller()
    subtasks = state.get("subtasks", [])

    # 找到依赖已满足的下一个待执行任务
    next_task = _find_next_task(state)
    if not next_task:
        pending = [t for t in subtasks if t.status == "pending"]
        if pending:
            updated_subtasks = []
            done_ids = {t.id for t in subtasks if t.status in ("done", "skipped", "failed")}
            for t in subtasks:
                if t.status == "pending" and not all(d in done_ids for d in t.dependencies):
                    updated_subtasks.append(t.model_copy(update={
                        "status": "failed",
                        "result": f"依赖任务失败，无法执行：{t.dependencies}",
                    }))
                else:
                    updated_subtasks.append(t)
            return {"phase": "reviewing", "current_subtask_id": None, "subtasks": updated_subtasks}
        return {"phase": "reviewing", "current_subtask_id": None}

    started_at = datetime.now()
    previous_results = _build_context(state, next_task)

    # 使用协调者选择协作模式
    mode = _coordinator.choose_collaboration_mode(
        task=next_task.description,
        agents=next_task.knowledge_domains or [next_task.agent_type],
        subtasks=state.get("subtasks", []),
    )

    timeout = _compute_timeout(next_task)

    # 根据协作模式执行
    if mode == CollaborationMode.DISCUSSION:
        call_result = await _execute_with_discussion(
            caller, next_task, previous_results, timeout
        )
    elif mode == CollaborationMode.PARALLEL:
        call_result = await _execute_parallel_v2(
            caller, next_task, previous_results, timeout
        )
    else:  # CHAIN
        call_result = await _execute_chain(
            caller, next_task, previous_results, timeout
        )

    # 检查执行是否成功
    if not call_result.get("success"):
        raise RuntimeError(f"Executor 执行失败: {call_result.get('error')}")

    result_data = call_result.get("result")
    result_text = str(result_data).strip() if result_data else ""
    if not result_text:
        raise RuntimeError(f"Executor 执行失败: 子代理未返回有效结果（task={next_task.id}）")

    result = {
        "status": "done",
        "result": result_text,
        "specialist_id": call_result.get("specialist_id"),
        "collaboration_mode": mode.value,
        "finished_at": datetime.now(),
    }

    # 标记专业 subagent 完成
    if result.get("specialist_id"):
        caller.complete_subtask(result["specialist_id"])

    # 更新子任务状态
    updated_subtasks = []
    for t in subtasks:
        if t.id == next_task.id:
            updated_subtasks.append(t.model_copy(update={
                "status": result["status"],
                "result": result["result"],
                "started_at": started_at,
                "finished_at": result["finished_at"],
                "assigned_agents": [result["specialist_id"]] if result.get("specialist_id") else [],
            }))
        else:
            updated_subtasks.append(t)

    return {
        "subtasks": updated_subtasks,
        "current_subtask_id": next_task.id,
        "time_budget": state.get("time_budget"),
        "phase": "executing",
        "execution_log": [{
            "event": "task_executed_v2",
            "task_id": next_task.id,
            "agent": next_task.agent_type,
            "collaboration_mode": mode.value,
            "specialist_id": result.get("specialist_id"),
            "status": result["status"],
            "timestamp": datetime.now().isoformat(),
        }],
    }


async def _execute_with_discussion(
    caller,
    task: SubTask,
    previous_results: list,
    timeout: float
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
        return await _fallback_execution(caller, task, previous_results, timeout)

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

    try:
        opinion_results = await asyncio.wait_for(
            asyncio.gather(*[gather_opinion(a) for a in agent_executors]),
            timeout=timeout * 0.6,  # 留 40% 时间用于协商
        )
    except asyncio.TimeoutError:
        raise RuntimeError(f"讨论协作超时：任务 {task.id}")

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
    timeout: float
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
        return await _execute_chain(caller, task, previous_results, timeout)

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

    try:
        results = await asyncio.wait_for(
            asyncio.gather(*[run_one(d) for d in domains]),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        raise RuntimeError(f"并行执行超时：任务 {task.id}")

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
    timeout: float
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

    try:
        if specialist_id:
            call_result = await asyncio.wait_for(
                caller.call_specialist(
                    agent_id=specialist_id,
                    subtask=subtask_dict,
                    previous_results=previous_results,
                ),
                timeout=timeout,
            )
        else:
            call_result = await asyncio.wait_for(
                caller.call_executor(
                    subtask=subtask_dict,
                    previous_results=previous_results,
                ),
                timeout=timeout,
            )
    except asyncio.TimeoutError:
        raise RuntimeError(f"链式执行超时：任务 {task.id}")

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
    timeout: float
) -> dict:
    """降级执行（当没有专家可用时）"""
    subtask_dict = {
        "id": task.id,
        "title": task.title,
        "description": task.description,
        "agent_type": task.agent_type,
        "knowledge_domains": task.knowledge_domains,
    }

    try:
        return await asyncio.wait_for(
            caller.call_executor(
                subtask=subtask_dict,
                previous_results=previous_results,
            ),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        raise RuntimeError(f"降级执行超时：任务 {task.id}")


def _find_next_task(state: GraphState) -> Optional[SubTask]:
    """找到依赖已满足的下一个待执行任务"""
    subtasks = state.get("subtasks", [])
    done_ids = {t.id for t in subtasks if t.status in ("done", "skipped")}
    for task in sorted(subtasks, key=lambda t: t.priority):
        if task.status == "pending":
            if all(d in done_ids for d in task.dependencies):
                return task
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
