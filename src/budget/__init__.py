"""src/budget — 时间预算管理工具"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from src.graph.state import TimeBudget


def create_budget(total_minutes: float) -> TimeBudget:
    """创建一个新的时间预算对象"""
    now = datetime.now()
    from datetime import timedelta
    return TimeBudget(
        total_minutes=total_minutes,
        remaining_minutes=total_minutes,
        started_at=now,
        deadline=now + timedelta(minutes=total_minutes),
    )


def update_elapsed(budget: TimeBudget) -> TimeBudget:
    """根据当前时间更新 elapsed/remaining，返回新对象（不可变）"""
    if budget.started_at is None:
        return budget
    elapsed = (datetime.now() - budget.started_at).total_seconds() / 60
    remaining = max(budget.total_minutes - elapsed, 0.0)
    return budget.model_copy(update={
        "elapsed_minutes": round(elapsed, 2),
        "remaining_minutes": round(remaining, 2),
        "is_overtime": remaining <= 0,
    })


def is_overtime(budget: Optional[TimeBudget]) -> bool:
    """检查预算是否已超时"""
    if budget is None:
        return False
    if budget.is_overtime:
        return True
    if budget.started_at:
        elapsed = (datetime.now() - budget.started_at).total_seconds() / 60
        return elapsed >= budget.total_minutes
    return False


def remaining_ratio(budget: Optional[TimeBudget]) -> float:
    """返回剩余时间比例（0.0 ~ 1.0）"""
    if budget is None or budget.total_minutes <= 0:
        return 1.0
    updated = update_elapsed(budget)
    return updated.remaining_minutes / budget.total_minutes


__all__ = [
    "TimeBudget",
    "create_budget",
    "update_elapsed",
    "is_overtime",
    "remaining_ratio",
]
