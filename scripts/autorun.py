"""
scripts/autorun.py — 目标驱动的自我修复执行循环

原理：
  1. 用户给出「目标」+ 「验证命令」（exit 0 = 目标已达成）
  2. 可选：先后台运行「启动命令」（如启动服务器）
  3. 运行验证命令
     ├─ 通过 → 打印成功，退出
     └─ 失败 → 把失败输出写入 fix_request.json，等 Claude Code 修复
  4. Claude Code 修复后删除 fix_request.json → 重试验证
  5. 无限循环，直到验证通过

用法示例：

  # 目标：所有测试通过
  .venv\Scripts\python.exe scripts/autorun.py \
      --goal "所有单元测试通过" \
      --verify ".venv\Scripts\python.exe -m pytest tests/ -x -q"

  # 目标：服务器正常响应
  .venv\Scripts\python.exe scripts/autorun.py \
      --goal "服务器在 8001 端口正常运行" \
      --run ".venv\Scripts\python.exe -X utf8 -m uvicorn src.web.api:app --port 8001 --host 127.0.0.1" \
      --verify "python -c \"import urllib.request; urllib.request.urlopen('http://127.0.0.1:8001/api/system/status', timeout=5)\""
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from threading import Thread

# ── 常量 ──────────────────────────────────────────────────────────────
FIX_REQUEST  = Path("fix_request.json")   # 写给 Claude Code 的修复请求
WAIT_POLL    = 3.0    # 秒：等待修复时的轮询间隔
WAIT_TIMEOUT = 600.0  # 秒：等 Claude Code 修复的最长时间（10 分钟）
ROOT         = Path(__file__).parent.parent  # 项目根目录


def _read_wait_timeout(default_value: float = WAIT_TIMEOUT) -> float:
    raw = str(os.environ.get("AUTORUN_WAIT_TIMEOUT_SEC", "")).strip()
    if not raw:
        return default_value
    try:
        parsed = float(raw)
        if parsed <= 0:
            return default_value
        return parsed
    except Exception:
        return default_value


def _read_verify_timeout(default_value: int = 60) -> int:
    raw = str(os.environ.get("AUTORUN_VERIFY_TIMEOUT_SEC", "")).strip()
    if not raw:
        return default_value
    try:
        parsed = int(float(raw))
        if parsed <= 0:
            return default_value
        return parsed
    except Exception:
        return default_value


def _ts() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _log(msg: str) -> None:
    print(f"\n[autorun {_ts()}] {msg}", flush=True)


def _run(cmd: str, timeout: int = 60) -> tuple[int, str]:
    """运行 shell 命令，返回 (returncode, stdout+stderr 合并)。"""
    result = subprocess.run(
        cmd,
        shell=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        cwd=str(ROOT),
    )
    return result.returncode, (result.stdout + result.stderr).strip()


def _start_background(cmd: str) -> subprocess.Popen:
    """后台启动长驻进程，实时转发其 stdout/stderr 到本终端。"""
    proc = subprocess.Popen(
        cmd,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(ROOT),
    )

    def _fwd(stream):
        for line in stream:
            print(f"  [bg] {line.rstrip()}", flush=True)

    Thread(target=_fwd, args=(proc.stdout,), daemon=True).start()
    Thread(target=_fwd, args=(proc.stderr,), daemon=True).start()
    return proc


def _write_fix_request(goal: str, attempt: int, failure: str) -> None:
    """写 fix_request.json，触发 Claude Code §2.2 修复协议。"""
    req = {
        "type":        "fix_request",
        "goal":        goal,
        "attempt":     attempt,
        "failure":     failure[-4000:],   # 末尾 4000 字符
        "ts":          _ts(),
        "instruction": (
            "验证命令执行失败，请根据 failure 中的错误信息定位并修复代码，"
            "修复完成后删除本文件（fix_request.json）以让循环继续。"
            "只修复导致验证失败的问题，不要添加额外功能。"
        ),
    }
    FIX_REQUEST.write_text(json.dumps(req, indent=2, ensure_ascii=False), encoding="utf-8")
    _log(f"已写 fix_request.json（第 {attempt} 次），等待 Claude Code 修复...")
    _log(f"失败摘要:\n{failure[-600:]}")


def _wait_for_fix(wait_timeout_sec: float) -> bool:
    """阻塞直到 Claude Code 删除 fix_request.json；超时返回 False。"""
    deadline = time.monotonic() + wait_timeout_sec
    ticks = 0
    while FIX_REQUEST.exists():
        if time.monotonic() > deadline:
            _log(f"等待修复超时 {int(wait_timeout_sec)}s，保持 fix_request.json，等待下一轮")
            return False
        if ticks % 10 == 0:
            elapsed = int(time.monotonic() - (deadline - wait_timeout_sec))
            print(f"  ... 等待修复中 ({elapsed}s / {int(wait_timeout_sec)}s)", flush=True)
        ticks += 1
        time.sleep(WAIT_POLL)
    _log("fix_request.json 已消失 → 修复完成，重新验证")
    return True


def run_loop(goal: str, verify_cmd: str, run_cmd: str | None, max_attempts: int) -> None:
    attempt = 0
    proc: subprocess.Popen | None = None
    verify_timeout_sec = _read_verify_timeout(60)
    wait_timeout_sec = _read_wait_timeout(WAIT_TIMEOUT)

    FIX_REQUEST.unlink(missing_ok=True)   # 清理上次遗留

    _log(f"目标   : {goal}")
    _log(f"验证   : {verify_cmd}")
    _log(f"验证超时: {verify_timeout_sec}s（可用 AUTORUN_VERIFY_TIMEOUT_SEC 覆盖）")
    _log(f"等待修复: {int(wait_timeout_sec)}s（可用 AUTORUN_WAIT_TIMEOUT_SEC 覆盖）")
    if run_cmd:
        _log(f"启动   : {run_cmd}")
        proc = _start_background(run_cmd)
        time.sleep(3)   # 等服务就绪

    while max_attempts == 0 or attempt < max_attempts:
        attempt += 1
        _log(f"{'═'*50}")
        _log(f"第 {attempt} 次验证")

        # 若后台进程已挂，重启
        if proc is not None and proc.poll() is not None:
            _log(f"后台服务已退出（code={proc.returncode}），重启...")
            proc = _start_background(run_cmd)   # type: ignore[arg-type]
            time.sleep(3)

        try:
            code, output = _run(verify_cmd, timeout=verify_timeout_sec)
        except subprocess.TimeoutExpired:
            code, output = 1, f"验证命令超时（{verify_timeout_sec}s）"
        except Exception as e:
            code, output = 1, f"运行验证命令时异常: {e}"

        if code == 0:
            _log(f"✓ 目标达成！（第 {attempt} 次验证通过）")
            if output:
                print(output, flush=True)
            break

        # 验证失败 → 请求修复 → 等待
        _write_fix_request(goal, attempt, output)
        if not _wait_for_fix(wait_timeout_sec):
            _log("fix_request 仍存在，保持等待，不推进到下一次验证")
            continue

    else:
        _log(f"已达到最大尝试次数 {max_attempts}，停止")

    if proc is not None and proc.poll() is None:
        proc.terminate()
    FIX_REQUEST.unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="目标驱动的自我修复执行循环",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--goal",         required=True, help="自然语言描述的目标（帮助 Claude Code 理解上下文）")
    parser.add_argument("--verify",       required=True, help="验证命令：exit 0=成功，非0=需修复")
    parser.add_argument("--run",          default=None,  help="可选：后台启动命令（如 uvicorn）")
    parser.add_argument("--max-attempts", type=int, default=0, help="最大重试次数，0=无限")
    args = parser.parse_args()

    try:
        run_loop(goal=args.goal, verify_cmd=args.verify,
                 run_cmd=args.run, max_attempts=args.max_attempts)
    except KeyboardInterrupt:
        _log("用户中断")
        FIX_REQUEST.unlink(missing_ok=True)
        sys.exit(0)

