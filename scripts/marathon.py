"""
scripts/marathon.py — 持续自检执行循环（马拉松模式）

行为：
  每一轮：
    1. 向 http://127.0.0.1:8001/api/tasks 提交自检任务（含 subagent + 讨论）
    2. 每 15 秒轮询任务状态，直到 completed / failed
    3. completed → 等待冷却（默认 30s），进入下一轮
    4. failed    → 写 fix_request.json，等 Claude Code 修复后重试本轮
    5. 服务器无响应 → 写 fix_request.json，等修复后继续
    6. 到达总时长限制（默认 10 小时）→ 退出

用法：
  .venv/Scripts/python.exe scripts/marathon.py
  .venv/Scripts/python.exe scripts/marathon.py --hours 5 --task "系统性能压力测试与自检"
  .venv/Scripts/python.exe scripts/marathon.py --hours 10 --cooldown 60
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

# ── 常量 ──────────────────────────────────────────────────────────────
BASE_URL      = "http://127.0.0.1:8001"
FIX_REQUEST   = Path("fix_request.json")
POLL_INTERVAL = 15      # 秒：轮询任务状态间隔
WAIT_POLL     = 5       # 秒：等待修复时轮询间隔
WAIT_TIMEOUT  = 900     # 秒：等 Claude Code 修复的最长时间（15 分钟）
DEFAULT_HOURS = 10
DEFAULT_TASK  = (
    "对系统进行全面自检：检查所有节点运行状态、API 接口可用性、"
    "subagent 调度流程、讨论机制和输出质量，发现问题请记录并提出改进建议"
)
DEFAULT_MINUTES_PER_ROUND = 60   # 每轮时间预算


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _log(msg: str) -> None:
    print(f"[marathon {_ts()}] {msg}", flush=True)


def _api(method: str, path: str, body: dict | None = None) -> dict:
    url = BASE_URL + path
    data = json.dumps(body).encode("utf-8") if body else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _server_ok() -> bool:
    try:
        _api("GET", "/api/system/status")
        return True
    except Exception:
        return False


def _wait_for_fix(reason: str, attempt: int, detail: str) -> None:
    """写 fix_request.json，阻塞等待 Claude Code 修复后删除它。"""
    req = {
        "type":        "fix_request",
        "goal":        f"marathon 自检循环第 {attempt} 轮正常完成",
        "failure":     detail[-4000:],
        "ts":          datetime.now().isoformat(),
        "instruction": (
            f"marathon 循环在第 {attempt} 轮遇到错误：{reason}。\n"
            "请根据 failure 中的错误信息定位并修复代码，"
            "修复完成后删除本文件（fix_request.json）以继续循环。\n"
            "只修复导致本轮失败的问题，不要添加额外功能。"
        ),
    }
    FIX_REQUEST.write_text(json.dumps(req, indent=2, ensure_ascii=False), encoding="utf-8")
    _log(f"已写 fix_request.json，等待 Claude Code 修复...")
    _log(f"原因: {reason}")

    deadline = time.monotonic() + WAIT_TIMEOUT
    ticks = 0
    while FIX_REQUEST.exists():
        if time.monotonic() > deadline:
            _log(f"等待修复超时 {WAIT_TIMEOUT}s，强制进入下一轮")
            FIX_REQUEST.unlink(missing_ok=True)
            return
        if ticks % 12 == 0:
            elapsed = int(time.monotonic() - (deadline - WAIT_TIMEOUT))
            print(f"  ... 等待修复 ({elapsed}s)", flush=True)
        ticks += 1
        time.sleep(WAIT_POLL)

    _log("修复完成（fix_request.json 已删除）")


def _wait_for_idle(timeout: int = 600) -> bool:
    """等待系统变为 idle，最多等待 timeout 秒。返回是否成功空闲。"""
    deadline = time.monotonic() + timeout
    ticks = 0
    while time.monotonic() < deadline:
        try:
            s = _api("GET", "/api/system/status").get("status", "idle")
            if s not in ("running",):
                return True
            if ticks % 4 == 0:
                elapsed = int(time.monotonic() - (deadline - timeout))
                _log(f"  系统仍在运行中，等待空闲... ({elapsed}s)")
        except Exception:
            pass
        ticks += 1
        time.sleep(WAIT_POLL)
    return False


def _submit_task(task_text: str, time_minutes: float) -> str | None:
    """提交任务，返回 task_id。失败返回 None。"""
    # 确保系统空闲再提交，避免多任务并发
    if not _wait_for_idle(timeout=600):
        _log("等待系统空闲超时，跳过本次提交")
        return None
    try:
        resp = _api("POST", "/api/tasks", {"task": task_text, "time_minutes": time_minutes})
        return resp.get("id")
    except Exception as e:
        _log(f"提交任务失败: {e}")
        return None


def _poll_task(task_id: str, deadline: float) -> tuple[str, str]:
    """
    轮询任务状态直到终态或超时。
    返回 (status, error_detail)，status ∈ {completed, failed, timeout}
    """
    last_node = ""
    while time.monotonic() < deadline:
        try:
            t = _api("GET", f"/api/tasks/{task_id}")
            status = t.get("status", "")
            node = t.get("current_node") or ""
            if node and node != last_node:
                _log(f"  → 节点: {node}")
                last_node = node
            if status == "completed":
                return "completed", ""
            if status == "failed":
                err = t.get("error") or t.get("final_output") or str(t)
                return "failed", str(err)
        except Exception as e:
            _log(f"  轮询异常: {e}")
        time.sleep(POLL_INTERVAL)
    return "timeout", f"任务 {task_id} 超时未完成"


def _clear_old_tasks() -> None:
    """清空已结束的旧任务（completed/failed），避免堆积。"""
    try:
        tasks = _api("GET", "/api/tasks").get("tasks", [])
        done = [t for t in tasks if t.get("status") in ("completed", "failed")]
        if done:
            # API 有 DELETE /api/tasks，但要求 idle 状态
            sys_status = _api("GET", "/api/system/status").get("status", "")
            if sys_status == "idle":
                _api("DELETE", "/api/tasks")
                _log(f"已清空 {len(done)} 个旧任务")
    except Exception:
        pass


def run(task_text: str, hours: float, minutes_per_round: float, cooldown: int) -> None:
    total_seconds = hours * 3600
    end_time = datetime.now() + timedelta(seconds=total_seconds)
    round_num = 0
    results: list[dict] = []

    _log(f"马拉松启动：计划运行 {hours}h，约 {int(hours * 60 / minutes_per_round)} 轮")
    _log(f"任务: {task_text[:80]}...")
    _log(f"结束时间: {end_time.strftime('%Y-%m-%d %H:%M:%S')}")
    _log("─" * 60)

    FIX_REQUEST.unlink(missing_ok=True)

    while datetime.now() < end_time:
        round_num += 1
        remaining_h = (end_time - datetime.now()).total_seconds() / 3600
        _log(f"")
        _log(f"═══ 第 {round_num} 轮 | 剩余 {remaining_h:.1f}h ═══")

        # ── 确保服务器在线 ──
        retries = 0
        while not _server_ok():
            retries += 1
            detail = f"第 {round_num} 轮开始时服务器无响应（尝试 {retries} 次）"
            _log(detail)
            _wait_for_fix("服务器无响应", round_num, detail)

        # ── 清理旧任务 ──
        _clear_old_tasks()

        # ── 提交任务 ──
        task_id = None
        while task_id is None:
            task_id = _submit_task(task_text, minutes_per_round)
            if task_id is None:
                _wait_for_fix("任务提交失败", round_num, "POST /api/tasks 返回错误")

        round_start = time.monotonic()
        _log(f"任务已提交: {task_id}，时间预算 {minutes_per_round} 分钟")

        # ── 轮询等待完成 ──
        # 最多等待 round 时间 × 1.5 的宽余
        poll_deadline = time.monotonic() + minutes_per_round * 60 * 1.5
        status, detail = _poll_task(task_id, poll_deadline)
        elapsed_min = (time.monotonic() - round_start) / 60

        if status == "completed":
            _log(f"✓ 第 {round_num} 轮完成（耗时 {elapsed_min:.1f} 分钟）")
            results.append({"round": round_num, "status": "ok", "minutes": f"{elapsed_min:.1f}"})

        elif status in ("failed", "timeout"):
            _log(f"✗ 第 {round_num} 轮{status}（耗时 {elapsed_min:.1f} 分钟）")
            _log(f"  错误: {detail[:200]}")
            results.append({"round": round_num, "status": status, "minutes": f"{elapsed_min:.1f}"})
            _wait_for_fix(f"轮次 {round_num} {status}", round_num, detail)

        # ── 冷却 ──
        if datetime.now() < end_time and cooldown > 0:
            _log(f"冷却 {cooldown}s 后开始下一轮...")
            time.sleep(cooldown)

    # ── 总结 ──
    _log("")
    _log("═" * 60)
    _log(f"马拉松结束，共完成 {round_num} 轮")
    ok = sum(1 for r in results if r["status"] == "ok")
    fail = round_num - ok
    _log(f"成功: {ok}  失败/超时: {fail}")
    for r in results:
        icon = "✓" if r["status"] == "ok" else "✗"
        print(f"  {icon} 第 {r['round']:2d} 轮  {r['status']:8s}  {r['minutes']} min", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="持续自检执行循环（马拉松模式）")
    parser.add_argument("--hours",     type=float, default=DEFAULT_HOURS,
                        help=f"总运行时长（小时），默认 {DEFAULT_HOURS}")
    parser.add_argument("--task",      default=DEFAULT_TASK,
                        help="每轮提交的任务描述")
    parser.add_argument("--minutes",   type=float, default=DEFAULT_MINUTES_PER_ROUND,
                        help=f"每轮时间预算（分钟），默认 {DEFAULT_MINUTES_PER_ROUND}")
    parser.add_argument("--cooldown",  type=int, default=30,
                        help="每轮结束后的冷却秒数，默认 30")
    args = parser.parse_args()

    try:
        run(task_text=args.task, hours=args.hours,
            minutes_per_round=args.minutes, cooldown=args.cooldown)
    except KeyboardInterrupt:
        _log("用户中断")
        FIX_REQUEST.unlink(missing_ok=True)
        sys.exit(0)
