"""
Reviewer V2 — 多人评审节点

实现 3 个 reviewer 并行评审 + 投票决策
- 并行调用多个 reviewer subagent
- 收集所有评分和意见
- 投票决定 PASS/REVISE
- 合并所有 issues 和 suggestions
"""

import asyncio
import json
from datetime import datetime
from typing import Optional
from statistics import mean

from src.graph.state import GraphState, SubTask
from src.agents.caller import get_caller
from src.discussion.manager import discussion_manager


# 评审专家数量
REVIEWER_COUNT = 3

# 评审超时（秒）
REVIEW_TIMEOUT = 90

# 通过阈值（至少 N 个 reviewer 通过）
PASS_THRESHOLD = 2

# 最低可接受平均分
MIN_ACCEPTABLE_SCORE = 6.0


async def reviewer_v2_node(state: GraphState) -> dict:
    """
    多人评审节点

    流程:
    1. 并行调用多个 reviewer subagent 进行独立评审
    2. 收集所有评审结果
    3. 投票决定最终结论
    4. 合并所有问题和建议
    """
    caller = get_caller()
    subtasks = state.get("subtasks", [])
    cid = state.get("current_subtask_id")

    current = _find_current_subtask(subtasks, cid)
    if not current or not current.result:
        return {"phase": "executing"}

    # === 阶段1: 并行评审 ===
    reviews = await _parallel_review(caller, current)

    if not reviews:
        # 所有评审都失败，默认通过
        return _create_pass_result(state, current, "评审器执行失败，默认通过")

    # === 阶段2: 投票决策 ===
    verdict, final_score = _vote_on_reviews(reviews)

    # === 阶段3: 合并反馈 ===
    merged_issues = _merge_issues(reviews)
    merged_suggestions = _merge_suggestions(reviews)

    # === 阶段4: 讨论确认（可选） ===
    if verdict == "REVISE" and len(reviews) >= 2:
        # 有分歧时发起讨论
        discussion_id = f"review_{current.id}_{datetime.now().strftime('%H%M%S')}"
        await _discuss_review_disagreement(discussion_id, reviews, current)

    # 纯函数式更新
    max_iter = state.get("max_iterations", 3)
    if verdict == "PASS":
        new_status, new_retry = "done", current.retry_count
    elif current.retry_count + 1 >= max_iter:
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

    return {
        "subtasks": updated_subtasks,
        "phase": "reviewing",
        "execution_log": [{
            "event": "multi_review_complete",
            "task_id": current.id,
            "verdict": verdict,
            "final_score": final_score,
            "reviewer_count": len(reviews),
            "pass_count": sum(1 for r in reviews if r.get("verdict") == "PASS"),
            "issues_count": len(merged_issues),
            "suggestions_count": len(merged_suggestions),
            "timestamp": datetime.now().isoformat(),
        }],
    }


async def _parallel_review(caller, task: SubTask) -> list[dict]:
    """
    并行调用多个 reviewer subagent 进行独立评审

    Returns:
        成功的评审结果列表
    """
    async def review_with_reviewer(reviewer_id: str) -> Optional[dict]:
        context = {
            "execution_result": {
                "result": task.result,
                "status": task.status,
                "started_at": task.started_at.isoformat() if task.started_at else None,
                "finished_at": task.finished_at.isoformat() if task.finished_at else None,
            },
            "subtask": {
                "id": task.id,
                "title": task.title,
                "description": task.description,
                "completion_criteria": task.completion_criteria,
            },
            "reviewer_id": reviewer_id,
        }
        result = await caller.call(reviewer_id, context)
        if result.get("success"):
            return _parse_review_result(result.get("result"))
        return None

    # 获取可用的 reviewer agent
    reviewer_ids = _get_available_reviewers()

    # 并行执行
    try:
        results = await asyncio.wait_for(
            asyncio.gather(*[review_with_reviewer(rid) for rid in reviewer_ids]),
            timeout=REVIEW_TIMEOUT
        )
    except asyncio.TimeoutError:
        results = []

    return [r for r in results if r]


def _get_available_reviewers() -> list[str]:
    """获取可用的 reviewer agent ID 列表"""
    reviewers = []

    # 尝试使用 reviewer_1, reviewer_2, reviewer_3
    for i in range(1, REVIEWER_COUNT + 1):
        reviewer_id = f"reviewer_{i}"
        from src.agents.pool_registry import get_pool
        pool = get_pool()
        if pool.get_template(reviewer_id):
            reviewers.append(reviewer_id)

    # 如果没有专用 reviewer，使用主 reviewer
    if not reviewers:
        reviewers = ["reviewer"]

    return reviewers


def _parse_review_result(result_data) -> Optional[dict]:
    """解析评审结果"""
    import re

    if isinstance(result_data, str):
        match = re.search(r'\{.*\}', result_data, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group(0))
            except json.JSONDecodeError:
                return None
        else:
            return None
    elif isinstance(result_data, dict):
        data = result_data
    else:
        return None

    return {
        "verdict": data.get("verdict", "PASS"),
        "score": data.get("score", 7),
        "issues": data.get("issues", []),
        "suggestions": data.get("suggestions", []),
        "details": data.get("details", {}),
    }


def _vote_on_reviews(reviews: list[dict]) -> tuple[str, float]:
    """
    投票决定最终结论

    策略:
    1. 统计 PASS/REVISE 票数
    2. PASS 票数 >= 阈值 → PASS
    3. 平均分 < 最低分数 → REVISE
    4. 否则按多数票决定

    Returns:
        (verdict, final_score)
    """
    if not reviews:
        return ("PASS", 7.0)

    # 统计票数
    pass_count = sum(1 for r in reviews if r.get("verdict") == "PASS")
    revise_count = len(reviews) - pass_count

    # 计算平均分
    scores = [r.get("score", 7) for r in reviews]
    avg_score = mean(scores) if scores else 7.0

    # 投票决策
    if pass_count >= PASS_THRESHOLD and avg_score >= MIN_ACCEPTABLE_SCORE:
        return ("PASS", round(avg_score, 1))

    if avg_score < MIN_ACCEPTABLE_SCORE:
        return ("REVISE", round(avg_score, 1))

    # 按多数票
    if pass_count > revise_count:
        return ("PASS", round(avg_score, 1))
    else:
        return ("REVISE", round(avg_score, 1))


def _merge_issues(reviews: list[dict]) -> list[str]:
    """合并所有问题（去重）"""
    all_issues = []
    seen = set()

    for review in reviews:
        for issue in review.get("issues", []):
            issue_key = issue.lower().strip()[:50]  # 简化去重键
            if issue_key not in seen:
                all_issues.append(issue)
                seen.add(issue_key)

    return all_issues


def _merge_suggestions(reviews: list[dict]) -> list[str]:
    """合并所有建议（去重）"""
    all_suggestions = []
    seen = set()

    for review in reviews:
        for suggestion in review.get("suggestions", []):
            suggestion_key = suggestion.lower().strip()[:50]
            if suggestion_key not in seen:
                all_suggestions.append(suggestion)
                seen.add(suggestion_key)

    return all_suggestions


async def _discuss_review_disagreement(
    discussion_id: str,
    reviews: list[dict],
    task: SubTask
):
    """
    当评审意见分歧时，发起讨论

    分歧场景:
    - PASS 和 REVISE 票数相近
    - 评分差异大（最高分 - 最低分 > 3）
    """
    discussion_manager.create_discussion(discussion_id)

    # 提交各评审意见
    for i, review in enumerate(reviews):
        await discussion_manager.post_message(
            node_id=discussion_id,
            from_agent=f"reviewer_{i + 1}",
            content=json.dumps({
                "verdict": review.get("verdict"),
                "score": review.get("score"),
                "key_issues": review.get("issues", [])[:3],
            }, ensure_ascii=False),
            message_type="review_opinion",
        )

    # 检查分歧程度
    scores = [r.get("score", 7) for r in reviews]
    score_range = max(scores) - min(scores) if scores else 0

    if score_range > 3:
        await discussion_manager.post_message(
            node_id=discussion_id,
            from_agent="review_coordinator",
            content=f"检测到评分分歧：最高 {max(scores)}，最低 {min(scores)}，差异 {score_range}",
            message_type="disagreement_alert",
        )


def _find_current_subtask(subtasks: list[SubTask], cid: Optional[str]) -> Optional[SubTask]:
    """查找当前子任务"""
    return next((t for t in subtasks if t.id == cid), None)


def _create_pass_result(state: GraphState, task: SubTask, reason: str) -> dict:
    """创建通过结果"""
    subtasks = state.get("subtasks", [])

    updated_subtasks = []
    for t in subtasks:
        if t.id == task.id:
            updated_subtasks.append(t.model_copy(update={
                "status": "done",
            }))
        else:
            updated_subtasks.append(t)

    return {
        "subtasks": updated_subtasks,
        "phase": "reviewing",
        "execution_log": [{
            "event": "review_fallback_pass",
            "task_id": task.id,
            "reason": reason,
            "timestamp": datetime.now().isoformat(),
        }],
    }


# === 详细评审维度 ===

class ReviewDimension:
    """评审维度"""

    COMPLETENESS = "completeness"  # 完整性
    CORRECTNESS = "correctness"    # 正确性
    QUALITY = "quality"           # 质量
    MAINTAINABILITY = "maintainability"  # 可维护性
    PERFORMANCE = "performance"   # 性能


def calculate_weighted_score(reviews: list[dict], weights: dict = None) -> float:
    """
    计算加权分数

    可以根据评审者的专业领域给予不同权重
    """
    if not weights:
        # 默认权重：所有评审者权重相等
        weights = {f"reviewer_{i+1}": 1.0 for i in range(len(reviews))}

    total_weight = 0
    weighted_sum = 0

    for i, review in enumerate(reviews):
        weight = weights.get(f"reviewer_{i+1}", 1.0)
        score = review.get("score", 7)
        weighted_sum += score * weight
        total_weight += weight

    return weighted_sum / total_weight if total_weight > 0 else 7.0
