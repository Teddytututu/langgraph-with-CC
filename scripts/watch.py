"""
scripts/watch.py — 监控守卫进程

Claude Code 在后台运行此脚本，通过 stdout JSON 行接收事件：
  {"event":"signal","type":"crash"|"decision"|"stuck","path":"...","ts":"..."}
  {"event":"server_down","port":8001,"ts":"..."}
  {"event":"server_up","port":8001,"ts":"..."}
  {"event":"heartbeat","server":"ok"|"down","ts":"..."}

Claude Code 读取每一行 JSON，按类型响应。
"""

from __future__ import annotations

import json
import sys
import time
import socket
import argparse
from pathlib import Path
from datetime import datetime

# ── 配置 ──────────────────────────────────────────────────────────────
SIGNAL_FILES = {
    "crash":       Path("crash_report.json"),
    "decision":    Path("decision_request.json"),
    "stuck":       Path("stuck_report.json"),
    "fix_request": Path("fix_request.json"),
}

DEFAULT_PORT       = 8001
POLL_INTERVAL      = 3.0   # 秒：检查信号文件频率
HEARTBEAT_INTERVAL = 30.0  # 秒：打印心跳频率


# ── 工具函数 ──────────────────────────────────────────────────────────
def emit(obj: dict) -> None:
    """向 stdout 输出单行 JSON，并立即 flush。Claude Code 从此读取事件。"""
    obj.setdefault("ts", datetime.now().isoformat())
    print(json.dumps(obj, ensure_ascii=False), flush=True)


def is_port_open(host: str = "127.0.0.1", port: int = DEFAULT_PORT) -> bool:
    """检查 TCP 端口是否可达（服务器是否运行）。"""
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False


def read_signal(path: Path) -> dict:
    """读取信号 JSON 文件内容。"""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        return {"_read_error": str(e)}


# ── 主循环 ────────────────────────────────────────────────────────────
def main(port: int = DEFAULT_PORT) -> None:
    emit({"event": "watch_started", "port": port, "signal_files": [str(p) for p in SIGNAL_FILES.values()]})

    seen: set[str] = set()         # 已报告过的信号文件（避免重复）
    server_was_up: bool | None = None
    last_heartbeat: float = 0.0

    while True:
        now = time.monotonic()

        # ── 检查信号文件 ──────────────────────────────────────────────
        for sig_type, sig_path in SIGNAL_FILES.items():
            if sig_path.exists():
                key = f"{sig_type}:{sig_path.stat().st_mtime}"
                if key not in seen:
                    seen.add(key)
                    emit({
                        "event":   "signal",
                        "type":    sig_type,
                        "path":    str(sig_path),
                        "content": read_signal(sig_path),
                    })
            else:
                # 文件消失 → 清除已见记录，下次出现重新报告
                to_remove = {k for k in seen if k.startswith(f"{sig_type}:")}
                seen -= to_remove

        # ── 检查服务器健康 ────────────────────────────────────────────
        server_up = is_port_open(port=port)
        if server_up != server_was_up:
            event = "server_up" if server_up else "server_down"
            emit({"event": event, "port": port})
            server_was_up = server_up

        # ── 心跳 ──────────────────────────────────────────────────────
        if now - last_heartbeat >= HEARTBEAT_INTERVAL:
            emit({"event": "heartbeat", "server": "ok" if server_up else "down", "port": port})
            last_heartbeat = now

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Monitor signal files and server health")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Server port to watch")
    args = parser.parse_args()

    try:
        main(port=args.port)
    except KeyboardInterrupt:
        emit({"event": "watch_stopped"})
        sys.exit(0)
