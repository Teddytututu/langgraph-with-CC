"""Python 与 CLAUDE.md 的通信工具

本模块提供 Python 程序与 CLAUDE.md 之间的通信机制，
支持三种唤醒场景：崩溃、决策、卡壳。
"""

import json
from pathlib import Path
from datetime import datetime
from typing import Any


def request_decision(
    question: str,
    options: list[str],
    context: dict[str, Any] | None = None,
    urgency: str = "normal"  # low, normal, high
) -> str:
    """
    请求 CLAUDE.md 做出决策

    Args:
        question: 需要决策的问题
        options: 可选的选项列表
        context: 决策上下文
        urgency: 紧急程度 (low, normal, high)

    Returns:
        请求 ID
    """
    request_id = f"decision-{datetime.now().strftime('%Y%m%d%H%M%S')}"

    request_data = {
        "request_id": request_id,
        "question": question,
        "options": options,
        "context": context or {},
        "urgency": urgency,
        "created_at": datetime.now().isoformat(),
        "status": "pending"
    }

    # 写入请求文件（触发 CLAUDE.md 唤醒）
    path = Path("decision_request.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(request_data, f, indent=2, ensure_ascii=False)

    return request_id


def get_decision_result(timeout: float = 300.0, poll_interval: float = 5.0) -> dict | None:
    """
    获取决策结果

    Args:
        timeout: 超时时间（秒）
        poll_interval: 轮询间隔（秒）

    Returns:
        决策结果字典，如果超时则返回 None
    """
    import time

    result_path = Path("decision_result.json")
    start_time = time.time()

    while time.time() - start_time < timeout:
        if result_path.exists():
            with open(result_path, "r", encoding="utf-8") as f:
                result = json.load(f)
            # 读取后删除结果文件
            result_path.unlink()
            return result
        time.sleep(poll_interval)

    return None


def report_stuck(
    node: str,
    state: dict,
    attempts: list[dict],
    reason: str
) -> str:
    """
    报告卡壳状态

    Args:
        node: 当前卡住的节点名称
        state: 当前图状态
        attempts: 已尝试的操作记录
        reason: 卡壳原因

    Returns:
        报告文件名
    """
    report_data = {
        "node": node,
        "state": state,
        "attempts": attempts,
        "reason": reason,
        "created_at": datetime.now().isoformat(),
    }

    path = Path("stuck_report.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report_data, f, indent=2, ensure_ascii=False)

    return "stuck_report.json"


def report_crash(
    error_type: str,
    error_message: str,
    traceback: str,
    state: dict | None = None,
    node: str | None = None
) -> str:
    """
    报告崩溃信息

    Args:
        error_type: 错误类型
        error_message: 错误消息
        traceback: 完整的 Traceback
        state: 崩溃时的图状态
        node: 崩溃时的节点名称

    Returns:
        报告文件名
    """
    report_data = {
        "error_type": error_type,
        "error_message": error_message,
        "traceback": traceback,
        "state": state,
        "node": node,
        "created_at": datetime.now().isoformat(),
    }

    path = Path("crash_report.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report_data, f, indent=2, ensure_ascii=False)

    return "crash_report.json"


def clear_stuck_report() -> bool:
    """清除卡壳报告"""
    path = Path("stuck_report.json")
    if path.exists():
        path.unlink()
        return True
    return False


def clear_crash_report() -> bool:
    """清除崩溃报告"""
    path = Path("crash_report.json")
    if path.exists():
        path.unlink()
        return True
    return False


def clear_decision_request() -> bool:
    """清除决策请求"""
    path = Path("decision_request.json")
    if path.exists():
        path.unlink()
        return True
    return False
