"""
scripts/marathon.py — 极简持续自修复循环

三阶段状态机：
1) RUN_ROUND   : 提交任务并轮询到 completed / failed / timeout
2) REPAIR_WAIT : 任意失败统一写 fix_request.json，等待其被删除
3) COOLDOWN    : 冷却 + 失败退避后继续下一轮

默认 forever 运行；除用户中断外不主动退出。
"""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.request
from datetime import datetime
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
BASE_URL = "http://127.0.0.1:8001"
FIX_REQUEST = REPO_ROOT / "fix_request.json"
MARATHON_LOCK = REPO_ROOT / "marathon.lock.json"
TASK_EXPORTS_DIR = REPO_ROOT / "exports" / "tasks"

OFFENDING_COMPLETION_MARKERS = (
    "[DEGRADED_CONTINUE]",
    "specialist_call_timeout",
    "discussion_total_timeout",
    "discussion_synthesis_timeout",
    "[POLICY_VIOLATION]",
)

DEFAULT_TASK = (
    "执行持续自检与自修复闭环：仅允许自检系统、定位缺陷、修复 bug、验证修复，"
    "不得新增需求外功能。任意失败进入 fix_request 修复握手，修复后继续下一轮。"
)


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _emit(event: str, **fields: object) -> None:
    payload = {"event": event, "ts": _now_iso()}
    payload.update(fields)
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def _api(method: str, path: str, body: dict | None = None, timeout: int = 10) -> dict:
    url = BASE_URL + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data is not None else {},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else {}


def _acquire_singleton_lock() -> bool:
    current_pid = os.getpid()
    payload = json.dumps(
        {
            "pid": current_pid,
            "created_at": _now_iso(),
        },
        ensure_ascii=False,
    )

    while True:
        try:
            fd = os.open(str(MARATHON_LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, payload.encode("utf-8"))
            finally:
                os.close(fd)
            return True
        except FileExistsError:
            try:
                lock_data = json.loads(MARATHON_LOCK.read_text(encoding="utf-8"))
                old_pid = int(lock_data.get("pid", 0))
            except Exception:
                old_pid = 0

            if old_pid and old_pid != current_pid:
                try:
                    os.kill(old_pid, 0)
                    _emit("marathon_lock_conflict", pid=old_pid)
                    return False
                except OSError:
                    MARATHON_LOCK.unlink(missing_ok=True)
                    continue

            MARATHON_LOCK.unlink(missing_ok=True)


def _release_singleton_lock() -> None:
    try:
        if not MARATHON_LOCK.exists():
            return
        lock_data = json.loads(MARATHON_LOCK.read_text(encoding="utf-8"))
        if int(lock_data.get("pid", -1)) == os.getpid():
            MARATHON_LOCK.unlink(missing_ok=True)
    except Exception:
        pass


def _write_fix_request(task_text: str, round_no: int, reason: str, detail: str) -> None:
    req = {
        "type": "fix_request",
        "goal": f"marathon 第 {round_no} 轮恢复并完成：{task_text}",
        "failure": (detail or reason)[-4000:],
        "ts": _now_iso(),
        "instruction": (
            "marathon 本轮失败。请根据 failure 中错误信息定位并修复代码，"
            "修复完成后删除 fix_request.json。"
            "只修复导致失败的问题，不要添加额外功能。"
        ),
    }
    FIX_REQUEST.write_text(json.dumps(req, indent=2, ensure_ascii=False), encoding="utf-8")
    _emit("fix_request_written", round=round_no, reason=reason)


def _wait_for_fix(wait_poll: int, wait_timeout: int) -> None:
    started = time.monotonic()
    while FIX_REQUEST.exists():
        elapsed = int(time.monotonic() - started)
        if wait_timeout > 0 and elapsed >= wait_timeout:
            _emit("repair_waiting", elapsed_sec=elapsed, timed_out=True)
            started = time.monotonic()
        else:
            _emit("repair_waiting", elapsed_sec=elapsed)
        time.sleep(wait_poll)


def _submit_task(task_text: str) -> str:
    resp = _api("POST", "/api/tasks", {"task": task_text})
    task_id = resp.get("id")
    if not task_id:
        raise RuntimeError(f"POST /api/tasks 返回缺少 id: {resp}")
    return str(task_id)


def _poll_until_terminal(task_id: str, poll_interval: int, round_timeout: int) -> tuple[str, str]:
    started = time.monotonic()
    while True:
        if round_timeout > 0 and (time.monotonic() - started) >= round_timeout:
            return "timeout", f"task {task_id} exceeded round_timeout={round_timeout}s"

        task = _api("GET", f"/api/tasks/{task_id}")
        status = str(task.get("status", ""))

        if status == "completed":
            return "completed", ""
        if status in ("failed", "cancelled"):
            detail = task.get("error") or task.get("final_output") or json.dumps(task, ensure_ascii=False)
            return "failed", str(detail)

        time.sleep(poll_interval)


def _contains_offending_marker(value: object) -> str | None:
    text = str(value or "")
    text_lower = text.lower()
    for marker in OFFENDING_COMPLETION_MARKERS:
        if marker.lower() in text_lower:
            return marker
    return None


def _extract_offending_subtasks(subtasks: list[dict]) -> list[dict[str, str]]:
    offenders: list[dict[str, str]] = []
    for st in subtasks:
        st_id = str(st.get("id") or "")
        st_status = str(st.get("status") or "")
        if st_status == "failed":
            offenders.append(
                {
                    "subtask_id": st_id,
                    "marker": "status=failed",
                    "status": st_status,
                    "evidence": str(st.get("result") or st.get("result_summary") or "")[:400],
                }
            )
            continue

        for field in ("result", "result_summary", "error"):
            marker = _contains_offending_marker(st.get(field))
            if marker:
                offenders.append(
                    {
                        "subtask_id": st_id,
                        "marker": marker,
                        "status": st_status,
                        "evidence": str(st.get(field) or "")[:400],
                    }
                )
                break
    return offenders


def _read_export_task_payload(task_id: str) -> dict | None:
    json_path = TASK_EXPORTS_DIR / f"{task_id}.json"
    if not json_path.exists():
        return None
    try:
        raw = json_path.read_text(encoding="utf-8")
        return json.loads(raw)
    except Exception:
        return None


def _evaluate_completed_task_truth(task_id: str) -> dict:
    payload = None
    evidence_source = "api"
    fetch_error = ""

    try:
        payload = _api("GET", f"/api/tasks/{task_id}")
    except Exception as e:
        fetch_error = f"{type(e).__name__}: {e}"

    if not isinstance(payload, dict) or not payload.get("subtasks"):
        export_payload = _read_export_task_payload(task_id)
        if export_payload:
            payload = export_payload
            evidence_source = "export"

    if not isinstance(payload, dict):
        reason = "missing_task_detail"
        if fetch_error:
            reason = f"{reason}: {fetch_error}"
        return {
            "truthful": False,
            "reason": reason,
            "evidence": [
                {
                    "subtask_id": task_id,
                    "marker": "task_detail_unavailable",
                    "status": "unknown",
                    "evidence": "unable to load /api/tasks/{id} and exports/tasks/{id}.json",
                }
            ],
            "source": "none",
        }

    subtasks = payload.get("subtasks") or []
    if not isinstance(subtasks, list) or not subtasks:
        return {
            "truthful": False,
            "reason": "missing_subtasks",
            "evidence": [
                {
                    "subtask_id": task_id,
                    "marker": "subtasks_missing",
                    "status": "unknown",
                    "evidence": "completed task payload has no subtasks",
                }
            ],
            "source": evidence_source,
        }

    offenders = _extract_offending_subtasks(subtasks)
    if offenders:
        markers = sorted({item["marker"] for item in offenders if item.get("marker")})
        return {
            "truthful": False,
            "reason": f"completed_with_degraded_evidence:{'|'.join(markers)}",
            "evidence": offenders,
            "source": evidence_source,
        }

    return {
        "truthful": True,
        "reason": "clean_completed",
        "evidence": [],
        "source": evidence_source,
    }


def _backoff_seconds(consecutive_failures: int, base: int, cap: int) -> int:
    if consecutive_failures <= 0:
        return 0
    return min(cap, base * (2 ** (consecutive_failures - 1)))


def run(
    task_text: str,
    poll_interval: int,
    wait_poll: int,
    wait_timeout: int,
    cooldown: int,
    backoff_base: int,
    backoff_max: int,
    round_timeout: int,
) -> None:
    phase = "RUN_ROUND"
    round_no = 0
    consecutive_failures = 0
    current_task_id: str | None = None

    _emit("marathon_started")
    FIX_REQUEST.unlink(missing_ok=True)

    while True:
        if phase == "RUN_ROUND":
            round_no += 1
            current_task_id = None
            round_started = time.monotonic()
            _emit("round_started", round=round_no, consecutive_failures=consecutive_failures)

            try:
                current_task_id = _submit_task(task_text)
                _emit("task_submitted", round=round_no, task_id=current_task_id)

                status, detail = _poll_until_terminal(
                    current_task_id,
                    poll_interval=max(1, poll_interval),
                    round_timeout=max(0, round_timeout),
                )

                if status == "completed":
                    completion_truth = _evaluate_completed_task_truth(current_task_id)
                    if completion_truth.get("truthful"):
                        elapsed = int(time.monotonic() - round_started)
                        _emit(
                            "task_completed",
                            round=round_no,
                            task_id=current_task_id,
                            elapsed_sec=elapsed,
                            completion_source=completion_truth.get("source"),
                        )
                        consecutive_failures = 0
                        phase = "COOLDOWN"
                        continue

                    reason = "completed_untruthful"
                    evidence = completion_truth.get("evidence") or []
                    detail_payload = {
                        "reason": completion_truth.get("reason"),
                        "source": completion_truth.get("source"),
                        "evidence": evidence,
                    }
                    _emit(
                        "completed_rejected",
                        round=round_no,
                        task_id=current_task_id,
                        reason=completion_truth.get("reason"),
                        source=completion_truth.get("source"),
                        evidence_count=len(evidence),
                    )
                    detail_text = json.dumps(detail_payload, ensure_ascii=False)
                else:
                    reason = "task_failed" if status == "failed" else "task_timeout"
                    detail_text = detail

            except Exception as e:
                reason = "runtime_error"
                detail_text = f"{type(e).__name__}: {e}"

            consecutive_failures += 1
            elapsed = int(time.monotonic() - round_started)
            _emit(
                "round_failed",
                round=round_no,
                task_id=current_task_id,
                reason=reason,
                elapsed_sec=elapsed,
                consecutive_failures=consecutive_failures,
            )
            _write_fix_request(task_text, round_no, reason, detail_text)
            phase = "REPAIR_WAIT"
            continue

        if phase == "REPAIR_WAIT":
            # 自驱修复：不再仅死等外部删除 fix_request.json
            if FIX_REQUEST.exists():
                try:
                    fix_data = json.loads(FIX_REQUEST.read_text(encoding="utf-8"))
                except Exception as e:
                    _emit("auto_repair_parse_failed", round=round_no, error=f"{type(e).__name__}: {e}")
                    _wait_for_fix(wait_poll=max(1, wait_poll), wait_timeout=max(0, wait_timeout))
                    _emit("repair_cleared", round=round_no, consecutive_failures=consecutive_failures)
                    phase = "COOLDOWN"
                    continue

                fix_instruction = str(fix_data.get("instruction") or "").strip()
                fix_failure = str(fix_data.get("failure") or "").strip()
                fix_goal = str(fix_data.get("goal") or "").strip()
                repair_task_text = (
                    "紧急修复阶段：根据失败信息直接修改代码并验证。\n"
                    f"Goal: {fix_goal}\n"
                    f"Instruction: {fix_instruction}\n"
                    f"Failure: {fix_failure}"
                )

                try:
                    _emit("auto_repair_started", round=round_no)
                    fix_task_id = _submit_task(repair_task_text)
                    _emit("auto_repair_submitted", round=round_no, task_id=fix_task_id)
                    fix_status, fix_detail = _poll_until_terminal(
                        fix_task_id,
                        poll_interval=max(1, poll_interval),
                        round_timeout=max(0, round_timeout),
                    )
                    if fix_status == "completed":
                        FIX_REQUEST.unlink(missing_ok=True)
                        _emit("auto_repair_completed", round=round_no, task_id=fix_task_id)
                    else:
                        _emit(
                            "auto_repair_failed",
                            round=round_no,
                            task_id=fix_task_id,
                            status=fix_status,
                            detail=str(fix_detail)[:500],
                        )
                except Exception as e:
                    _emit("auto_repair_crashed", round=round_no, error=f"{type(e).__name__}: {e}")

            _wait_for_fix(wait_poll=max(1, wait_poll), wait_timeout=max(0, wait_timeout))
            _emit("repair_cleared", round=round_no, consecutive_failures=consecutive_failures)
            phase = "COOLDOWN"
            continue

        if phase == "COOLDOWN":
            backoff_sec = _backoff_seconds(consecutive_failures, max(1, backoff_base), max(1, backoff_max))
            if backoff_sec > 0:
                _emit(
                    "backoff_sleep",
                    round=round_no,
                    seconds=backoff_sec,
                    consecutive_failures=consecutive_failures,
                )
                time.sleep(backoff_sec)

            if cooldown > 0:
                _emit("cooldown_sleep", round=round_no, seconds=cooldown)
                time.sleep(cooldown)

            phase = "RUN_ROUND"
            continue


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="极简持续自修复 marathon")
    parser.add_argument("--task", default=DEFAULT_TASK, help="每轮提交任务文本")
    parser.add_argument("--poll-interval", type=int, default=15, help="任务轮询间隔秒")
    parser.add_argument("--wait-poll", type=int, default=5, help="修复等待轮询间隔秒")
    parser.add_argument("--wait-timeout", type=int, default=0, help="修复等待超时秒，0=仅打点不跳出")
    parser.add_argument("--cooldown", type=int, default=30, help="每轮冷却秒")
    parser.add_argument("--backoff-base", type=int, default=10, help="失败退避基数秒")
    parser.add_argument("--backoff-max", type=int, default=300, help="失败退避上限秒")
    parser.add_argument("--round-timeout", type=int, default=3600, help="单轮任务超时秒，0=不超时")
    return parser


if __name__ == "__main__":
    parser = _build_parser()
    args = parser.parse_args()

    if not _acquire_singleton_lock():
        raise SystemExit(0)

    try:
        run(
            task_text=args.task,
            poll_interval=args.poll_interval,
            wait_poll=args.wait_poll,
            wait_timeout=args.wait_timeout,
            cooldown=args.cooldown,
            backoff_base=args.backoff_base,
            backoff_max=args.backoff_max,
            round_timeout=args.round_timeout,
        )
    except KeyboardInterrupt:
        _emit("marathon_stopped", reason="keyboard_interrupt")
    finally:
        _release_singleton_lock()
