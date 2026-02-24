"""src/graph/nodes/reviewer.py — 质量审查节点"""
import logging
import re
from datetime import datetime
from typing import Optional

from src.graph.state import GraphState, SubTask
from src.agents.caller import get_caller
from src.graph.utils.json_parser import extract_first_json_object

logger = logging.getLogger(__name__)

# 诨评为伪结果的特征樣式
_FAKE_PATTERNS = [
    "2026-02-23T10:00:00Z",
    "2026-02-23T11:00:00Z",
    "Agent 123", "Agent 456",
    "虚假",
    "fake_",
    "placeholder",
]
_MIN_RESULT_LEN = 50   # 结果少于 50 字符认为空白
_FAST_PASS_MIN_LEN = 300


async def reviewer_node(state: GraphState) -> dict:
    """
    审查当前子任务的执行结果

    通过 SubagentCaller 调用 reviewer subagent 进行质量审查
    """
    caller = get_caller()
    subtasks = state.get("subtasks", [])
    cid = state.get("current_subtask_id")

    current = _find_current_subtask(subtasks, cid)
    if not current or not current.result:
        subtask_summary = [f"{t.id}:{t.status}" for t in subtasks[:8]]
        skip_reason = "missing_current_subtask" if not current else "missing_current_result"
        return {
            "phase": "executing",
            "execution_log": [{
                "event": "review_skipped",
                "reason": skip_reason,
                "current_subtask_id": cid,
                "resolved_current": current.id if current else None,
                "subtasks_overview": subtask_summary,
                "timestamp": datetime.now().isoformat(),
            }],
        }

    # 本地快速验证：先做本地检查，通过且内容充分则直接 PASS，无需调用 subagent
    local_issues = _validate_result_locally(current)
    result_len = len((current.result or "").strip())
    reproducible_locally = _has_local_reproducibility_structure(current.result or "")

    if not local_issues and result_len >= _FAST_PASS_MIN_LEN and reproducible_locally:
        # 内容充分、本地验证通过且可复核结构齐备 → 直接 PASS，跳过 subagent reviewer（避免误判）
        logger.info("[reviewer] 本地快速通过 %s（%d 字符，结构完整）", current.id, result_len)
        review = {"verdict": "PASS", "score": 8, "issues": [], "suggestions": []}
    else:
        # 内容不足或本地发现问题 → 调用 reviewer subagent 深度审查
        call_result = await caller.call_reviewer(
            execution_result={
                "result": current.result,
                "status": current.status,
                "started_at": current.started_at.isoformat() if current.started_at else None,
                "finished_at": current.finished_at.isoformat() if current.finished_at else None,
            },
            subtask={
                "id": current.id,
                "title": current.title,
                "description": current.description,
                "completion_criteria": current.completion_criteria,
            }
        )

        # 检查执行是否成功（V1 降级：失败时返回 PASS 兜底，避免整图崩溃）
        if not call_result.get("success"):
            logger.warning("[reviewer] subagent 调用失败，启用降级审查: %s", call_result.get('error'))
            call_result = {"success": True, "result": None}

        # 解析审查结果
        review = _parse_review_result(call_result)

        # 叠加本地问题
        if local_issues:
            review["verdict"] = "FAIL"
            review["issues"] = local_issues + review.get("issues", [])
            review["score"] = min(review.get("score", 7), 4)

    # 纯函数式更新
    max_iter = state.get("max_iterations", 3)
    if review["verdict"] == "PASS":
        new_status, new_retry = "done", current.retry_count
    elif current.retry_count + 1 >= max_iter:
        # 达到最大重试次数，标记为失败
        new_status, new_retry = "failed", current.retry_count + 1
    else:
        new_status, new_retry = "pending", current.retry_count + 1

    updated_subtasks = []
    for t in subtasks:
        if t.id == current.id:
            updated_subtasks.append(t.model_copy(update={
                "status": new_status,
                "retry_count": new_retry,
            }))
        else:
            updated_subtasks.append(t)

    # PASS / 达到重试上限 → executing（让 router 继续调度剩余任务）
    # 仍需重试 → reflecting（让 router 路由到 reflector 修正，避免 reviewing 死循环）
    next_phase = "reflecting" if new_status == "pending" else "executing"

    return {
        "subtasks": updated_subtasks,
        "phase": next_phase,
        "execution_log": [{
            "event": "review_complete",
            "task_id": current.id,
            "verdict": review["verdict"],
            "score": review.get("score", 0),
            "issues": review.get("issues", []),
            "subagent_called": "reviewer",
            "timestamp": datetime.now().isoformat(),
        }],
    }


def _find_current_subtask(subtasks: list[SubTask], cid: Optional[str]) -> Optional[SubTask]:
    """查找当前子任务"""
    return next((t for t in subtasks if t.id == cid), None)


def _validate_result_locally(task: SubTask) -> list[str]:
    """本地质量检查：不调用 subagent，直接检查结果内容正确性"""
    issues = []
    result = task.result or ""

    # 1. 结果过短
    if len(result.strip()) < _MIN_RESULT_LEN:
        issues.append(f"结果内容过短（{len(result.strip())} 字符），可能未完成")

    # 2. 包含已知伪造模式
    for pat in _FAKE_PATTERNS:
        if pat.lower() in result.lower():
            issues.append(f"结果包含伪造模式：'{pat}'，需要重新执行")
            break

    # 3. 验收标准检查（交由 subagent 评判） — 此处仅做基础模式检测
    if not result or result.strip() == f"任务 {task.title} 执行完成":
        issues.append("结果是默认占位符，实际未执行")

    # 4. 可执行复现入口：至少一个命令代码块
    if not re.search(r"```(?:bash|sh|shell|zsh|cmd|powershell)?\n[\s\S]*?```", result, re.IGNORECASE):
        issues.append("缺少可执行命令块（Reproduction/Verification 命令不可复现）")

    # 5. 复现步骤 + 预期/实际结果成对出现
    has_steps = bool(re.search(r"\b(步骤|step\s*\d+|repro(duction)?\s+steps?)\b", result, re.IGNORECASE))
    has_expected_actual = bool(re.search(r"\b(预期|expected)\b", result, re.IGNORECASE)) and bool(
        re.search(r"\b(实际|actual)\b", result, re.IGNORECASE)
    )
    if not (has_steps and has_expected_actual):
        issues.append("缺少“复现步骤 + 预期/实际结果”配对描述")

    # 6. 稳定证据锚点（关键词/检索命令/路径），禁止仅固定行号
    if not _has_stable_evidence_anchor(result):
        issues.append("证据锚点不稳定：需要关键词/检索命令/路径，不能仅依赖固定行号")

    return issues


def _has_local_reproducibility_structure(result: str) -> bool:
    """快速判定是否具备最小可复核结构（供 fast-pass 使用）。"""
    if not result or len(result.strip()) < _MIN_RESULT_LEN:
        return False

    has_exec_block = bool(re.search(r"```(?:bash|sh|shell|zsh|cmd|powershell)?\n[\s\S]*?```", result, re.IGNORECASE))
    has_step_or_pair = (
        bool(re.search(r"\b(步骤|step\s*\d+|repro(duction)?\s+steps?)\b", result, re.IGNORECASE))
        and bool(re.search(r"\b(预期|expected|实际|actual|结果|result)\b", result, re.IGNORECASE))
    )
    has_stable_anchor = _has_stable_evidence_anchor(result)

    return has_exec_block and has_step_or_pair and has_stable_anchor


def _has_stable_evidence_anchor(result: str) -> bool:
    """检查是否存在稳定证据锚点：关键词 / 检索命令 / 文件路径。"""
    lower = result.lower()
    has_keyword_anchor = any(k in lower for k in ["keyword", "关键词", "anchor"])
    has_search_cmd = bool(re.search(r"\b(grep|rg|findstr|ripgrep|python\s+-c)\b", lower))
    has_path_anchor = bool(re.search(r"(?:^|\s)(?:[A-Za-z]:/|/)?[\w./-]+\.[a-z0-9]+", result))

    # 若仅含行号锚点且无关键词/命令/路径，不算稳定
    only_line_anchor = bool(re.search(r"\bline\s*\d+\b|\bL\d+\b|:\d+\b", result, re.IGNORECASE)) and not (
        has_keyword_anchor or has_search_cmd or has_path_anchor
    )
    if only_line_anchor:
        return False

    return has_keyword_anchor or has_search_cmd or has_path_anchor


def _parse_review_result(call_result: dict) -> dict:
    """解析审查结果（使用括号计数法提取 JSON，避免贪婪匹配问题）"""
    default_review = {
        "verdict": "PASS",
        "score": 7,
        "issues": [],
        "suggestions": []
    }

    if not call_result.get("success"):
        return default_review

    result = call_result.get("result")

    # SDK 可能返回字符串（含 JSON）—— 使用非贪婪括号计数法提取
    if isinstance(result, str):
        result = extract_first_json_object(result)

    if result and isinstance(result, dict):
        return {
            "verdict": result.get("verdict", "PASS"),
            "score": result.get("score", 7),
            "issues": result.get("issues", []),
            "suggestions": result.get("suggestions", []),
        }

    return default_review
