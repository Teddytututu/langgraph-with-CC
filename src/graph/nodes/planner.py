"""src/graph/nodes/planner.py — 任务分解节点"""
import asyncio
import json
from collections import defaultdict, deque
from datetime import datetime

from src.graph.state import GraphState, SubTask, ExecutionPolicy
from src.utils.config import get_config
from src.agents.caller import get_caller

PLANNER_SYSTEM_PROMPT = """
你是一个任务规划专家。你的职责是将用户的复杂任务分解为具有复杂依赖关系的子任务图（DAG+条件环）。

## 核心规则
1. 每个子任务必须分配 **3个以上不同领域** 的 knowledge_domains，确保多专家参与讨论
2. 子任务之间必须构建 **复杂依赖关系**：
   - 不能只是线性链（A→B→C），必须包含 **菱形依赖**（A→B,C→D）、**交叉依赖**（A→C, B→C, C→D,E）
   - 至少有 **2组并行任务** 和 **1个汇聚节点**（多依赖合并）
   - 必须包含 **验证→修复→再验证** 的条件回环结构
3. 为每个子任务指定最合适的 Agent 类型：
   - coder: 编写/修改代码、脚本
   - researcher: 搜索信息、阅读文档、调研
   - writer: 撰写文档、报告、文案
   - analyst: 数据分析、逻辑推理、方案对比
4. 子任务数量控制在 **6~12 个**
5. 必须考虑时间预算，但要保证每个任务有足够时间让 3+ 专家进行 10+ 轮讨论
6. **必须包含修复类子任务**：对诊断发现的问题编写修复，并有后续验证任务依赖修复结果

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
        if not (6 <= len(subtasks) <= 12):
            return False, f"subtask_count_out_of_range:{len(subtasks)}"

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
    try:
        call_result = await asyncio.wait_for(
            caller.call_planner(task=planner_task, time_budget=time_budget_info),
            timeout=120.0,
        )
    except asyncio.TimeoutError:
        import logging as _logging
        _logging.getLogger(__name__).warning("[planner] SDK call timed out (120s)")
        if policy.strict_enforcement:
            raise RuntimeError("[POLICY_VIOLATION] planner_timeout_under_strict_mode")
        call_result = {"success": False, "error": "planner SDK timeout"}
    except Exception as _pe:
        import logging as _logging
        _logging.getLogger(__name__).warning("[planner] SDK call failed: %s", _pe)
        if policy.strict_enforcement:
            raise RuntimeError(f"[POLICY_VIOLATION] planner_error_under_strict_mode: {_pe}")
        call_result = {"success": False, "error": str(_pe)}

    # 非严格模式保留原有降级逻辑
    if not call_result.get("success") and not policy.strict_enforcement:
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "[planner] subagent 调用失败，启用默认四阶段子任务: %s",
            call_result.get('error')
        )
        call_result = {"success": True, "result": None}
    elif not call_result.get("success") and policy.strict_enforcement:
        raise RuntimeError(f"[POLICY_VIOLATION] planner_call_failed: {call_result.get('error', 'unknown')}")

    # 解析子任务
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
                raise RuntimeError(f"[POLICY_VIOLATION] planner_output_invalid: {reason}")

    # 如果 subagent 未返回有效结果，生成 7 个子任务（并行+汇聚+报告）
    # 预算: 7 任务 × ~5 min/任务 = 35 min + 10 min 开销 ≈ 45 min < 90 min poll deadline
    if not subtasks:
        base_mins = budget.total_minutes if budget else 60
        task_preview = user_task[:200]
        t_diag = max(7.0, base_mins * 0.10)   # 诊断类任务
        t_fix = max(8.0, base_mins * 0.12)   # 修复类任务
        t_rpt = max(4.0, base_mins * 0.08)   # 报告类任务
        subtasks = [
            # ── Phase 1: 并行诊断（菱形展开） ──
            SubTask(
                id="task-001",
                title="代码与流程诊断",
                description=(
                    f"检查代码质量、架构健康、模块耦合、API接口可用性、调度流程。\n原始任务：{task_preview}"
                ),
                agent_type="analyst",
                dependencies=[],
                priority=1,
                estimated_minutes=t_diag,
                knowledge_domains=["python", "architecture", "api_testing", "workflow"],
                completion_criteria=["列出代码缺陷与API异常", "识别高风险模块", "调度流程瓶颈定位"],
            ),
            SubTask(
                id="task-002",
                title="运行时与讨论机制诊断",
                description="检查 subagent 执行效率、多层超时配置、讨论机制健康度、WebSocket 稳定性。",
                agent_type="researcher",
                dependencies=[],
                priority=1,
                estimated_minutes=t_diag,
                knowledge_domains=["performance", "monitoring", "collaboration", "networking"],
                completion_criteria=["subagent 执行过程可观测", "讨论轮次及共识质量确认", "常见超时场景定义"],
            ),
            # ── Phase 2: 汇聚分析（菱形聚合） ──
            SubTask(
                id="task-003",
                title="问题汇聚与修复方案",
                description="汇总 task-001/002 的诊断结果，交叉分析根因，按严重度排序，制定具体可执行的修复方案。",
                agent_type="analyst",
                dependencies=["task-001", "task-002"],
                priority=2,
                estimated_minutes=t_diag,
                knowledge_domains=["root_cause_analysis", "planning", "risk_assessment", "architecture"],
                completion_criteria=["问题按 P0/P1/P2 分级", "每个问题有具体修复方案", "修复风险评估完成"],
            ),
            # ── Phase 3: 修复实施（并行分支） ──
            SubTask(
                id="task-004",
                title="P0/P1 缺陷修复实施",
                description="根据 task-003 方案修复 P0、P1 级代码缺陷和配置问题，每个改动配套单元测试。",
                agent_type="coder",
                dependencies=["task-003"],
                priority=3,
                estimated_minutes=t_fix,
                knowledge_domains=["python", "debugging", "testing", "api_design"],
                completion_criteria=["P0 缺陷全部修复", "P1 主要缺陷修复", "修复代码有测试覆盖"],
            ),
            SubTask(
                id="task-005",
                title="并行修复分支：稳定性与性能",
                description="基于 task-003 的问题清单并行修复稳定性与性能相关问题，形成与 task-004 并行分支。",
                agent_type="coder",
                dependencies=["task-003"],
                priority=3,
                estimated_minutes=t_fix,
                knowledge_domains=["performance", "stability", "profiling", "python"],
                completion_criteria=["关键性能问题已修复", "稳定性缺陷已修复", "修复内容可验证"],
            ),
            # ── Phase 4: 验证闭环 ──
            SubTask(
                id="task-006",
                title="回归验证与集成测试",
                description="对 task-004 / task-005 的修复项进行全面回归：重跑 Phase1 诊断项，确认修复生效且无新回归。",
                agent_type="analyst",
                dependencies=["task-004", "task-005"],
                priority=4,
                estimated_minutes=t_diag,
                knowledge_domains=["testing", "regression", "validation", "quality_assurance"],
                completion_criteria=["已修复项 100% 验证通过", "无新引入回归", "验证结果已记录"],
            ),
            # ── Phase 5: 报告 ──
            SubTask(
                id="task-007",
                title="修复前后对比报告",
                description="生成结构化报告：诊断发现→修复方案→验证结果。保存到 reports/ 。",
                agent_type="writer",
                dependencies=["task-006"],
                priority=5,
                estimated_minutes=t_rpt,
                knowledge_domains=["documentation", "reporting", "analysis", "architecture"],
                completion_criteria=["包含修复前后对比数据", "报告已保存到 reports/", "包含后续建议"],
            ),
        ]

    # 最终校验：严格模式不允许静默降级
    ok, reason = _validate_subtasks(subtasks, policy)
    if not ok and policy.strict_enforcement:
        raise RuntimeError(f"[POLICY_VIOLATION] planner_final_validation_failed: {reason}")

    return {
        "subtasks": subtasks,
        "phase": "budgeting",
        "execution_log": [{
            "event": "planning_complete",
            "timestamp": datetime.now().isoformat(),
            "subtask_count": len(subtasks),
            "subagent_called": "planner",
            "policy_strict": policy.strict_enforcement,
            "policy_force_complex_graph": policy.force_complex_graph,
        }],
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
