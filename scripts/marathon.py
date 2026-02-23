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
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

# ── 常量 ──────────────────────────────────────────────────────────────
BASE_URL      = "http://127.0.0.1:8001"
FIX_REQUEST   = Path("fix_request.json")
MARATHON_LOCK = Path("marathon.lock.json")
POLL_INTERVAL = 15      # 秒：轮询任务状态间隔
WAIT_POLL     = 5       # 秒：等待修复时轮询间隔
WAIT_TIMEOUT  = 900     # 秒：等 Claude Code 修复的最长时间（15 分钟）
IDLE_STABLE_SAMPLES = 3  # 连续稳定次数才认为 idle
TRANSIENT_RECHECKS = 3   # 瞬态异常复核次数
DEFAULT_HOURS = 10
DEFAULT_TASK  = (
    "对系统进行全面自检与修复闭环："
    "（Phase 1-诊断）检查所有节点运行状态、API 接口可用性、subagent 调度流程、讨论机制和输出质量；"
    "（Phase 2-修复）对 Phase 1 发现的问题逐一编写修复代码并验证，确保每个问题修复后通过回归测试；"
    "（Phase 3-验证报告）重新运行自检确认修复生效，输出修复前后对比报告并保存到 reports/ 目录。"
    "每个阶段至少3名不同领域专家参与讨论协商，每个子任务进行2轮专家讨论。"
)
DEFAULT_MINUTES_PER_ROUND = 60   # 每轮时间预算
DEFAULT_COOLDOWN = 30            # 每轮冷却秒数
DEFAULT_EXECUTION_POLICY = {
    "force_complex_graph": True,
    "min_agents_per_node": 3,
    "min_discussion_rounds": 2,
    "strict_enforcement": False,
}


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


def _acquire_singleton_lock() -> bool:
    """确保仅一个 marathon 实例运行。

    若已存在存活实例：立即退出当前新实例（不影响已有任务）。
    采用原子创建锁文件，避免并发启动竞态。
    """
    current_pid = os.getpid()
    payload = json.dumps({
        "pid": current_pid,
        "created_at": datetime.now().isoformat(),
    }, ensure_ascii=False)

    while True:
        try:
            fd = os.open(str(MARATHON_LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, payload.encode("utf-8"))
            finally:
                os.close(fd)
            return True
        except FileExistsError:
            # 已有锁：判断是否为存活进程
            try:
                lock_data = json.loads(MARATHON_LOCK.read_text(encoding="utf-8"))
                old_pid = int(lock_data.get("pid", 0))
            except Exception:
                old_pid = 0

            if old_pid and old_pid != current_pid:
                try:
                    os.kill(old_pid, 0)  # 存活检测
                    _log(f"检测到已有 marathon 实例在运行 (pid={old_pid})，当前实例退出")
                    return False
                except OSError:
                    # 僵尸锁，删除后重试获取
                    MARATHON_LOCK.unlink(missing_ok=True)
                    continue

            # 锁损坏或无效 PID，尝试清理并重试
            MARATHON_LOCK.unlink(missing_ok=True)


def _release_singleton_lock() -> None:
    """释放 marathon 单实例锁（仅释放自己创建的锁）。"""
    try:
        if not MARATHON_LOCK.exists():
            return
        data = json.loads(MARATHON_LOCK.read_text(encoding="utf-8"))
        if int(data.get("pid", -1)) == os.getpid():
            MARATHON_LOCK.unlink(missing_ok=True)
    except Exception:
        pass


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


def _wait_for_idle(timeout: int = 600, stable_samples: int = IDLE_STABLE_SAMPLES) -> bool:
    """等待系统彻底空闲（无 running/created/queued 任务），需连续稳定若干次。"""
    deadline = time.monotonic() + timeout
    ticks = 0
    stable_hits = 0
    while time.monotonic() < deadline:
        try:
            sys_resp = _api("GET", "/api/system/status")
            tasks_resp = _api("GET", "/api/tasks")
            sys_status = sys_resp.get("status", "idle")
            tasks = tasks_resp.get("tasks", [])
            active = [t for t in tasks if t.get("status") in ("running", "created", "queued")]
            idle_now = sys_status not in ("running",) and not active
            if idle_now:
                stable_hits += 1
                if stable_hits >= stable_samples:
                    return True
            else:
                stable_hits = 0

            if ticks % 4 == 0:
                elapsed = int(time.monotonic() - (deadline - timeout))
                reasons = []
                if sys_status == "running":
                    reasons.append(f"system={sys_status}")
                if active:
                    reasons.append(f"active_tasks={len(active)}")
                if idle_now:
                    reasons.append(f"stable={stable_hits}/{stable_samples}")
                _log(f"  等待系统空闲... ({elapsed}s) [{', '.join(reasons) if reasons else 'checking'}]")
        except Exception:
            stable_hits = 0
        ticks += 1
        time.sleep(WAIT_POLL)
    return False


def _submit_task(task_text: str, time_minutes: float, execution_policy: dict | None) -> str | None:
    """提交任务，返回 task_id。失败返回 None。"""
    # 确保系统空闲再提交，避免多任务并发
    if not _wait_for_idle(timeout=600):
        _log("等待系统空闲超时，跳过本次提交")
        return None
    try:
        payload = {"task": task_text, "time_minutes": time_minutes}
        if execution_policy is not None:
            payload["execution_policy"] = execution_policy
        resp = _api("POST", "/api/tasks", payload)
        return resp.get("id")
    except Exception as e:
        _log(f"提交任务失败: {e}")
        return None


def _check_transient_task_status(task_id: str, retries: int = TRANSIENT_RECHECKS) -> tuple[str, str]:
    """复核 timeout/unknown 场景，避免把瞬态误判为失败。"""
    last_detail = ""
    for i in range(retries):
        try:
            task_resp = _api("GET", f"/api/tasks/{task_id}")
            status = task_resp.get("status", "")
            if status == "completed":
                return "completed", "recheck: task completed"
            if status in ("failed", "cancelled"):
                err = task_resp.get("error") or task_resp.get("final_output") or str(task_resp)
                return "failed", str(err)
            last_detail = f"recheck#{i + 1}: task status={status or 'unknown'}"
        except urllib.error.HTTPError as e:
            if e.code == 404:
                try:
                    sys_status = _api("GET", "/api/system/status").get("status", "idle")
                    tasks = _api("GET", "/api/tasks").get("tasks", [])
                    active = [x for x in tasks if x.get("status") in ("running", "created", "queued")]
                    if sys_status != "running" and not active:
                        return "completed", "recheck: task record gone after terminal state"
                    last_detail = f"recheck#{i + 1}: task 404 but system={sys_status}, active={len(active)}"
                except Exception as inner_e:
                    last_detail = f"recheck#{i + 1}: task 404 + status probe failed: {inner_e}"
            else:
                last_detail = f"recheck#{i + 1}: HTTPError {e.code}"
        except Exception as e:
            last_detail = f"recheck#{i + 1}: {e}"
        time.sleep(min(POLL_INTERVAL, 5))

    return "transient", last_detail or "transient status unresolved"


def _poll_task(task_id: str, deadline: float) -> tuple[str, str]:
    """
    轮询任务状态直到终态或超时。
    返回 (status, error_detail)，status ∈ {completed, failed, timeout, transient}
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
            if status == "cancelled":
                err = t.get("error") or "task cancelled"
                return "failed", str(err)
            if status and status not in ("created", "queued", "running"):
                return "transient", f"unexpected task status: {status}"
        except urllib.error.HTTPError as e:
            if e.code == 404:
                try:
                    sys_status = _api("GET", "/api/system/status").get("status", "idle")
                    tasks = _api("GET", "/api/tasks").get("tasks", [])
                    active = [x for x in tasks if x.get("status") in ("running", "created", "queued")]
                    if sys_status != "running" and not active:
                        _log(f"  任务 {task_id} 不可见，但系统空闲，按已终态处理")
                        return "completed", "task record cleaned by auto init"
                except Exception as inner_e:
                    _log(f"  404 兜底判定异常: {inner_e}")
            _log(f"  轮询 HTTP 异常: {e}")
        except Exception as e:
            _log(f"  轮询异常: {e}")
        time.sleep(POLL_INTERVAL)
    return "timeout", f"任务 {task_id} 超时未完成"


def _clear_old_tasks() -> None:
    """清空所有已结束的旧任务（completed/failed/cancelled），避免堆积。"""
    try:
        tasks = _api("GET", "/api/tasks").get("tasks", [])
        running = [t for t in tasks if t.get("status") in ("running", "created")]
        if running:
            _log(f"  跳过清空：仍有 {len(running)} 个活跃任务")
            return
        if tasks:
            _api("DELETE", "/api/tasks")
            _log(f"  已清空 {len(tasks)} 个旧任务")
    except Exception as e:
        _log(f"  清空旧任务失败: {e}")


def run(task_text: str, hours: float, minutes_per_round: float, cooldown: int, execution_policy: dict | None) -> None:
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
        if execution_policy:
            _log(f"执行策略: {json.dumps(execution_policy, ensure_ascii=False)}")

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
            task_id = _submit_task(task_text, minutes_per_round, execution_policy)
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

        elif status == "failed":
            _log(f"✗ 第 {round_num} 轮 FAILED（耗时 {elapsed_min:.1f} 分钟）")
            _log(f"  错误: {detail[:200]}")
            results.append({"round": round_num, "status": "failed", "minutes": f"{elapsed_min:.1f}"})
            _wait_for_fix(f"轮次 {round_num} 任务执行失败", round_num, detail)

        elif status in ("timeout", "transient"):
            # timeout/transient 先复核，不直接触发 fix_request
            _log(f"⚠ 第 {round_num} 轮 {status}（耗时 {elapsed_min:.1f} 分钟）")
            _log(f"  初始详情: {detail[:200]}")
            recheck_status, recheck_detail = _check_transient_task_status(task_id)
            if recheck_status == "failed":
                _log(f"  复核结论: hard failed -> {recheck_detail[:200]}")
                results.append({"round": round_num, "status": "failed", "minutes": f"{elapsed_min:.1f}"})
                _wait_for_fix(f"轮次 {round_num} 任务执行失败(复核)", round_num, recheck_detail)
            elif recheck_status == "completed":
                _log("  复核结论: 已完成（瞬态切换窗口）")
                results.append({"round": round_num, "status": "ok", "minutes": f"{elapsed_min:.1f}"})
            else:
                _log("  复核结论: 仍为瞬态/未知，跳过修复进入下一轮")
                results.append({"round": round_num, "status": status, "minutes": f"{elapsed_min:.1f}"})

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
    parser.add_argument("--strict", action="store_true", default=False,
                        help="启用严格执行策略（默认关闭）")
    parser.add_argument("--non-strict", action="store_true",
                        help="关闭严格执行策略，按旧行为提交")
    parser.add_argument("--force-complex-graph", action="store_true", default=True,
                        help="强制复杂依赖图（默认开启）")
    parser.add_argument("--allow-linear-graph", action="store_true",
                        help="允许线性图（将 force_complex_graph 设为 false）")
    parser.add_argument("--min-agents-per-node", type=int, default=3,
                        help="每个节点最少 agent 数（默认 3）")
    parser.add_argument("--min-discussion-rounds", type=int, default=2,
                        help="每个节点最少讨论轮次（默认 2）")
    parser.add_argument("--cooldown", type=int, default=DEFAULT_COOLDOWN,
                        help=f"每轮结束后冷却秒数（默认 {DEFAULT_COOLDOWN}）")
    args = parser.parse_args()

    if not _acquire_singleton_lock():
        sys.exit(0)

    strict_enabled = args.strict and not args.non_strict
    force_complex_graph = args.force_complex_graph and not args.allow_linear_graph
    execution_policy = {
        "force_complex_graph": force_complex_graph,
        "min_agents_per_node": args.min_agents_per_node,
        "min_discussion_rounds": args.min_discussion_rounds,
        "strict_enforcement": strict_enabled,
    } if strict_enabled else None

    try:
        run(
            task_text=args.task,
            hours=args.hours,
            minutes_per_round=args.minutes,
            cooldown=args.cooldown,
            execution_policy=execution_policy,
        )
    except KeyboardInterrupt:
        _log("用户中断")
        FIX_REQUEST.unlink(missing_ok=True)
        sys.exit(0)
    finally:
        _release_singleton_lock()
