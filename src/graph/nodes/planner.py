"""src/graph/nodes/planner.py — 任务分解节点"""
import asyncio
import json
from collections import defaultdict, deque
from datetime import datetime

from src.graph.state import GraphState, SubTask, ExecutionPolicy
from src.utils.config import get_config
from src.agents.caller import get_caller

PLANNER_SYSTEM_PROMPT = """
你是一个任务规划专家。你的职责是将用户任务分解为用于“系统自检→缺陷修复→修复验证”的复杂子任务图（DAG+条件回环）。

## 核心规则
1. 主旨必须严格限定为：自检系统、定位问题、修复 bug、验证修复效果。
2. 明确禁止：新增功能、需求外扩展、体验优化型改造、与修复无关的重构。
3. 每个子任务必须分配 **3个以上不同领域** 的 knowledge_domains，确保多专家参与讨论；每个节点的讨论轮次目标为 **至少 10 轮**
4. 子任务之间必须构建 **复杂依赖关系**：
   - 不能只是线性链（A→B→C），必须包含 **菱形依赖**（A→B,C→D）、**交叉依赖**（A→C, B→C, C→D,E）
   - 至少有 **2组并行任务** 和 **1个汇聚节点**（多依赖合并）
   - 必须包含 **验证→修复→再验证** 的条件回环结构
5. 子任务数量必须 **≥12 个**；若任务场景客观上无法满足，必须在 description 与 completion_criteria 中显式写明原因与证据
6. 为每个子任务指定最合适的 Agent 类型：
   - coder: 编写/修改代码、脚本
   - researcher: 搜索信息、阅读文档、调研
   - writer: 撰写文档、报告、文案
   - analyst: 数据分析、逻辑推理、方案对比
7. 必须考虑时间预算，并保证每个任务可执行、可验证，且修复任务后面必须有依赖其结果的验证任务

## 依赖图结构示例
```
task-001,002 并行诊断 → task-003 汇聚分析 → task-004,005 并行修复 → task-006 回归验证
                                                                    ↑____________↓ (验证失败则重修)
task-007 总结报告 ← task-006
```

## 输出格式
返回严格的 JSON 数组，每个元素包含：
{"id": "task-001", "title": "简短标题",
 "description": "详细描述，包含具体要求和验收标准",
 "agent_type": "coder",
 "dependencies": [], "priority": 1,
 "estimated_minutes": 10,
 "knowledge_domains": ["domain1", "domain2", "domain3"],
 "completion_criteria": ["标准1", "标准2"]}
"""


def _resolve_policy(state: GraphState) -> ExecutionPolicy:
    policy = state.get("execution_policy")
    if isinstance(policy, ExecutionPolicy):
        return policy
    if isinstance(policy, dict):
        return ExecutionPolicy.model_validate(policy)
    return ExecutionPolicy()


def _policy_prompt(policy: ExecutionPolicy) -> str:
    if not policy.strict_enforcement and not policy.force_complex_graph:
        return ""
    return (
        "\n\n[执行策略约束]\n"
        f"- force_complex_graph={policy.force_complex_graph}\n"
        f"- min_agents_per_node={policy.min_agents_per_node}\n"
        f"- min_discussion_rounds={policy.min_discussion_rounds}\n"
        f"- strict_enforcement={policy.strict_enforcement}\n"
        "- 每个节点至少 3 个 subagents，且至少 10 轮讨论。\n"
        "- 若 strict_enforcement=true，必须返回满足约束的复杂 DAG，不允许降级为线性链。\n"
    )


def _topological_levels(subtasks: list[SubTask]) -> list[list[str]]:
    by_id = {t.id: t for t in subtasks}
    in_deg = {t.id: 0 for t in subtasks}
    children: dict[str, list[str]] = defaultdict(list)

    for t in subtasks:
        for dep in t.dependencies:
            if dep in by_id:
                children[dep].append(t.id)
                in_deg[t.id] += 1

    q = deque([tid for tid, deg in in_deg.items() if deg == 0])
    levels: list[list[str]] = []
    visited = 0

    while q:
        layer = list(q)
        levels.append(layer)
        q.clear()
        for u in layer:
            visited += 1
            for v in children.get(u, []):
                in_deg[v] -= 1
                if in_deg[v] == 0:
                    q.append(v)

    if visited != len(subtasks):
        return []
    return levels


def _is_pure_linear(subtasks: list[SubTask]) -> bool:
    if len(subtasks) <= 1:
        return True
    by_id = {t.id: t for t in subtasks}
    out_deg = {t.id: 0 for t in subtasks}
    in_deg = {t.id: 0 for t in subtasks}
    edge_count = 0

    for t in subtasks:
        for dep in t.dependencies:
            if dep in by_id:
                out_deg[dep] += 1
                in_deg[t.id] += 1
                edge_count += 1

    if edge_count != len(subtasks) - 1:
        return False

    roots = sum(1 for v in in_deg.values() if v == 0)
    sinks = sum(1 for v in out_deg.values() if v == 0)
    branching = any(v > 1 for v in out_deg.values())
    converging = any(v > 1 for v in in_deg.values())
    return roots == 1 and sinks == 1 and not branching and not converging


def _validate_subtasks(subtasks: list[SubTask], policy: ExecutionPolicy) -> tuple[bool, str]:
    if not subtasks:
        return False, "empty_subtasks"

    ids = [t.id for t in subtasks]
    if len(set(ids)) != len(ids):
        return False, "duplicate_task_ids"

    by_id = {t.id: t for t in subtasks}
    for t in subtasks:
        if t.id in t.dependencies:
            return False, f"self_dependency:{t.id}"
        for dep in t.dependencies:
            if dep not in by_id:
                return False, f"missing_dependency:{t.id}->{dep}"

    levels = _topological_levels(subtasks)
    if not levels:
        return False, "dependency_cycle_detected"

    if policy.force_complex_graph or policy.strict_enforcement:
        if len(subtasks) < 12:
            return False, f"subtask_count_below_minimum:{len(subtasks)}"

        for t in subtasks:
            if len(set(t.knowledge_domains or [])) < policy.min_agents_per_node:
                return False, f"insufficient_domains:{t.id}"

        parallel_groups = sum(1 for level in levels if len(level) >= 2)
        indegrees = {t.id: len([d for d in t.dependencies if d in by_id]) for t in subtasks}
        converge_nodes = sum(1 for v in indegrees.values() if v >= 2)

        if parallel_groups < 2:
            return False, f"parallel_groups_insufficient:{parallel_groups}"
        if converge_nodes < 1:
            return False, "missing_converge_node"
        if _is_pure_linear(subtasks):
            return False, "pure_linear_dag"

    return True, ""


def _normalize_domains(subtasks: list[SubTask], min_agents: int) -> list[SubTask]:
    extras = [
        "code_quality", "architecture", "testing", "documentation",
        "performance", "security", "maintainability", "reliability",
    ]
    updated: list[SubTask] = []
    for t in subtasks:
        domains = list(dict.fromkeys(t.knowledge_domains or [t.agent_type]))
        for extra in extras:
            if len(domains) >= min_agents:
                break
            if extra not in domains:
                domains.append(extra)
        updated.append(t.model_copy(update={"knowledge_domains": domains}))
    return updated


def _normalize_dependencies_for_complex_graph(subtasks: list[SubTask]) -> list[SubTask]:
    if len(subtasks) < 6:
        return subtasks

    ordered = sorted(subtasks, key=lambda x: (x.priority, x.id))
    ids = [t.id for t in ordered]
    template: dict[str, list[str]] = {
        ids[0]: [],
        ids[1]: [],
        ids[2]: [ids[0], ids[1]],
        ids[3]: [ids[2]],
        ids[4]: [ids[2]],
        ids[5]: [ids[3], ids[4]],
    }

    for i in range(6, len(ids)):
        prev_id = ids[i - 1]
        anchor_id = ids[2]
        deps = [prev_id]
        if anchor_id != prev_id:
            deps.append(anchor_id)
        template[ids[i]] = deps

    return [t.model_copy(update={"dependencies": template.get(t.id, t.dependencies)}) for t in ordered]


async def planner_node(state: GraphState) -> dict:
    """
    分解用户任务为子任务 DAG

    通过 SubagentCaller 调用 planner subagent 执行任务分解
    """
    config = get_config()
    caller = get_caller()

    budget = state.get("time_budget")
    user_task = state["user_task"]
    policy = _resolve_policy(state)

    # 构建时间预算信息
    time_budget_info = None
    if budget:
        time_budget_info = {
            "total_minutes": budget.total_minutes,
            "remaining_minutes": budget.remaining_minutes,
        }

    planner_task = user_task + _policy_prompt(policy)

    # 直接调用 planner subagent（最多等 120s）
    planner_call_error = ""
    fallback_reason = ""
    try:
        call_result = await asyncio.wait_for(
            caller.call_planner(task=planner_task, time_budget=time_budget_info),
            timeout=120.0,
        )
    except asyncio.TimeoutError:
        import logging as _logging
        planner_call_error = "planner_call_failed: planner SDK timeout"
        _logging.getLogger(__name__).warning("[planner] %s", planner_call_error)
        call_result = {"success": False, "error": planner_call_error}
    except Exception as _pe:
        import logging as _logging
        planner_call_error = f"planner_call_failed: {_pe}"
        _logging.getLogger(__name__).warning("[planner] %s", planner_call_error)
        call_result = {"success": False, "error": planner_call_error}

    if not call_result.get("success") and not planner_call_error:
        planner_call_error = f"planner_call_failed: {call_result.get('error') or 'unknown_error'}"

    # 解析子任务（subagent 调用失败时不伪装 success）
    subtasks = _parse_subtasks_from_result(call_result.get("result"), budget)


    # 严格模式下，先规范化一次
    if subtasks:
        subtasks = _normalize_domains(subtasks, max(1, policy.min_agents_per_node))
        ok, reason = _validate_subtasks(subtasks, policy)
        if not ok and (policy.force_complex_graph or policy.strict_enforcement):
            subtasks = _normalize_dependencies_for_complex_graph(subtasks)
            subtasks = _normalize_domains(subtasks, max(1, policy.min_agents_per_node))
            ok, reason = _validate_subtasks(subtasks, policy)
            if not ok and policy.strict_enforcement:
                import logging as _logging
                fallback_reason = f"planner_subagent_validation_failed:{reason}"
                _logging.getLogger(__name__).warning(
                    "[planner] strict validation failed after subagent output (%s), fallback to local template",
                    reason,
                )
                subtasks = []

    # 如果 subagent 未返回有效结果，生成严格模式可通过的 12 节点 DAG（并行+汇聚+验证闭环）
    # 预算控制：单节点默认 4~8 分钟，避免轮询超时
    if not subtasks:
        base_mins = budget.total_minutes if budget else 60
        task_preview = user_task[:200]
        t_diag = max(4.0, base_mins * 0.06)
        t_fix = max(5.0, base_mins * 0.08)
        t_verify = max(4.0, base_mins * 0.06)
        t_rpt = max(3.0, base_mins * 0.05)
        subtasks = [
            SubTask(
                id="task-001",
                title="静态检查与结构盘点",
                description=f"对代码进行静态检查与模块结构盘点，限定在缺陷定位范围。原始任务：{task_preview}",
                agent_type="analyst",
                dependencies=[],
                priority=1,
                estimated_minutes=t_diag,
                knowledge_domains=["python", "architecture", "debugging"],
                completion_criteria=["输出静态问题清单", "标注高风险模块"],
            ),
            SubTask(
                id="task-002",
                title="运行日志与异常聚类",
                description="分析运行日志与异常类型，形成可复现问题分组。",
                agent_type="researcher",
                dependencies=[],
                priority=1,
                estimated_minutes=t_diag,
                knowledge_domains=["logging", "observability", "reliability"],
                completion_criteria=["异常按类型聚类", "给出复现入口"],
            ),
            SubTask(
                id="task-003",
                title="接口与状态流诊断",
                description="检查 API 与状态机流转，定位错误传播路径。",
                agent_type="analyst",
                dependencies=[],
                priority=1,
                estimated_minutes=t_diag,
                knowledge_domains=["api_testing", "state_machine", "workflow"],
                completion_criteria=["识别错误链路", "输出影响范围"],
            ),
            SubTask(
                id="task-004",
                title="根因汇聚分析 A",
                description="汇聚 task-001 与 task-002 结果，抽取共同根因。",
                agent_type="analyst",
                dependencies=["task-001", "task-002"],
                priority=2,
                estimated_minutes=t_diag,
                knowledge_domains=["root_cause_analysis", "architecture", "testing"],
                completion_criteria=["形成根因假设 A", "列出证据链"],
            ),
            SubTask(
                id="task-005",
                title="根因汇聚分析 B",
                description="汇聚 task-002 与 task-003 结果，抽取共同根因。",
                agent_type="analyst",
                dependencies=["task-002", "task-003"],
                priority=2,
                estimated_minutes=t_diag,
                knowledge_domains=["root_cause_analysis", "performance", "reliability"],
                completion_criteria=["形成根因假设 B", "列出证据链"],
            ),
            SubTask(
                id="task-006",
                title="修复方案汇总与排序",
                description="合并根因 A/B，按风险与可验证性排序修复计划。",
                agent_type="analyst",
                dependencies=["task-004", "task-005"],
                priority=3,
                estimated_minutes=t_diag,
                knowledge_domains=["planning", "risk_assessment", "maintainability"],
                completion_criteria=["形成优先级列表", "每项有验证路径"],
            ),
            SubTask(
                id="task-007",
                title="修复分支一：核心逻辑",
                description="实施核心逻辑缺陷修复，控制变更范围。",
                agent_type="coder",
                dependencies=["task-006"],
                priority=4,
                estimated_minutes=t_fix,
                knowledge_domains=["python", "debugging", "code_quality"],
                completion_criteria=["核心缺陷修复", "变更可回溯"],
            ),
            SubTask(
                id="task-008",
                title="修复分支二：状态一致性",
                description="实施状态一致性相关修复，避免死循环与错误分支。",
                agent_type="coder",
                dependencies=["task-006"],
                priority=4,
                estimated_minutes=t_fix,
                knowledge_domains=["state_machine", "reliability", "python"],
                completion_criteria=["状态流正确", "异常分支受控"],
            ),
            SubTask(
                id="task-009",
                title="修复分支三：交互链路",
                description="实施 API/前后端交互链路缺陷修复。",
                agent_type="coder",
                dependencies=["task-006"],
                priority=4,
                estimated_minutes=t_fix,
                knowledge_domains=["api_design", "frontend", "integration_testing"],
                completion_criteria=["交互链路恢复", "关键接口可用"],
            ),
            SubTask(
                id="task-010",
                title="并行回归验证",
                description="对三个修复分支做并行回归验证，确认无新增回归。",
                agent_type="analyst",
                dependencies=["task-007", "task-008", "task-009"],
                priority=5,
                estimated_minutes=t_verify,
                knowledge_domains=["regression", "quality_assurance", "testing"],
                completion_criteria=["回归通过", "记录失败用例"],
            ),
            SubTask(
                id="task-011",
                title="失败回路复检",
                description="若验证失败，聚焦失败项做复检并给出最小修复建议。",
                agent_type="analyst",
                dependencies=["task-010"],
                priority=6,
                estimated_minutes=t_verify,
                knowledge_domains=["debugging", "root_cause_analysis", "testing"],
                completion_criteria=["失败项根因明确", "给出最小修复路径"],
            ),
            SubTask(
                id="task-012",
                title="结果汇总与证据输出",
                description="输出终端摘要并保存 reports/*.md 与 reports/*.json 证据。",
                agent_type="writer",
                dependencies=["task-010", "task-011"],
                priority=7,
                estimated_minutes=t_rpt,
                knowledge_domains=["documentation", "reporting", "analysis"],
                completion_criteria=["摘要完整", "报告文件已落盘"],
            ),
        ]

    # 最终校验：严格模式不允许静默降级
    ok, reason = _validate_subtasks(subtasks, policy)
    if not ok and policy.strict_enforcement:
        source = planner_call_error or fallback_reason or "planner_validation"
        raise RuntimeError(f"[POLICY_VIOLATION] planner_final_validation_failed: {reason} | source={source}")

    planning_meta = {
        "event": "planning_complete",
        "timestamp": datetime.now().isoformat(),
        "subtask_count": len(subtasks),
        "subagent_called": "planner",
        "policy_strict": policy.strict_enforcement,
        "policy_force_complex_graph": policy.force_complex_graph,
    }
    if planner_call_error:
        planning_meta["planner_call_error"] = planner_call_error
    if fallback_reason:
        planning_meta["fallback_reason"] = fallback_reason

    return {
        "subtasks": subtasks,
        "phase": "budgeting",
        "execution_log": [planning_meta],
    }


def _parse_subtasks_from_result(result_data, budget) -> list[SubTask]:
    """从 subagent 结果中解析子任务"""
    import re as _re
    subtasks = []

    # SDK 可能返回字符串（含 JSON）或列表
    if isinstance(result_data, str):
        # 先尝试去掉 markdown 代码块包装
        cleaned = _re.sub(r'^```(?:json)?\s*', '', result_data.strip(), flags=_re.MULTILINE)
        cleaned = _re.sub(r'```\s*$', '', cleaned.strip(), flags=_re.MULTILINE)
        # 尝试直接解析整个字符串作为 JSON
        try:
            parsed = json.loads(cleaned)
            if isinstance(parsed, list):
                result_data = parsed
            elif isinstance(parsed, dict) and 'subtasks' in parsed:
                result_data = parsed['subtasks']
            elif isinstance(parsed, dict) and 'tasks' in parsed:
                result_data = parsed['tasks']
            else:
                result_data = []
        except json.JSONDecodeError:
            # 从字符串中提取 JSON 数组
            match = _re.search(r'\[.*?\]', cleaned, _re.DOTALL)
            if match:
                try:
                    result_data = json.loads(match.group(0))
                except json.JSONDecodeError:
                    result_data = []
            else:
                result_data = []

    if result_data and isinstance(result_data, list):
        for task_data in result_data:
            if not isinstance(task_data, dict):
                continue
            subtasks.append(SubTask(
                id=task_data.get("id", f"task-{len(subtasks)+1:03d}"),
                title=task_data.get("title", "未命名任务"),
                description=task_data.get("description", ""),
                agent_type=task_data.get("agent_type", "coder"),
                dependencies=task_data.get("dependencies", []),
                priority=task_data.get("priority", 1),
                estimated_minutes=task_data.get("estimated_minutes", 10),
                knowledge_domains=task_data.get("knowledge_domains", []),
                completion_criteria=task_data.get("completion_criteria", []),
            ))

    return subtasks
