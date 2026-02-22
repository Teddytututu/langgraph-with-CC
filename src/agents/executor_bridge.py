"""
执行层桥接器

管理 subagent 调用指令和结果，通过文件系统与 CLAUDE.md 通信。
"""

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional, Any
from threading import Lock


class ExecutorBridge:
    """
    执行层桥接器 - 管理 subagent 调用指令和结果

    核心职责：
    1. 创建调用指令，写入 pending_calls.json
    2. 读取已完成的执行结果 from completed_calls.json
    3. 管理调用的生命周期
    """

    PENDING_FILE = ".claude/pending_calls.json"
    COMPLETED_FILE = ".claude/completed_calls.json"

    def __init__(self, base_dir: str = "."):
        self.base_dir = Path(base_dir)
        self.pending_path = self.base_dir / self.PENDING_FILE
        self.completed_path = self.base_dir / self.COMPLETED_FILE
        self._lock = Lock()
        self._call_contexts: dict[str, dict] = {}  # 缓存调用上下文
        self._ensure_files()

    def _ensure_files(self):
        """确保文件存在"""
        self.pending_path.parent.mkdir(parents=True, exist_ok=True)

        if not self.pending_path.exists():
            self._write_json(self.pending_path, {"calls": []})

        if not self.completed_path.exists():
            self._write_json(self.completed_path, {"results": []})

    def _read_json(self, path: Path) -> dict:
        """读取 JSON 文件"""
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, FileNotFoundError):
            return {"calls": []} if "pending" in str(path) else {"results": []}

    def _write_json(self, path: Path, data: dict):
        """写入 JSON 文件"""
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def create_call(
        self,
        agent_id: str,
        system_prompt: str,
        context: dict[str, Any],
        tools: list[str] = None,
        model: str = "inherit"
    ) -> str:
        """
        创建调用指令，返回 call_id

        Args:
            agent_id: 要调用的 subagent ID
            system_prompt: 系统提示词
            context: 执行上下文（任务信息等）
            tools: 可用工具列表
            model: 使用的模型

        Returns:
            调用 ID（用于后续查询结果）
        """
        call_id = f"call-{uuid.uuid4().hex[:8]}"

        call_record = {
            "call_id": call_id,
            "agent_id": agent_id,
            "system_prompt": system_prompt,
            "context": context,
            "tools": tools or [],
            "model": model,
            "created_at": datetime.now().isoformat(),
            "status": "pending"
        }

        with self._lock:
            data = self._read_json(self.pending_path)
            data["calls"].append(call_record)
            self._write_json(self.pending_path, data)

            # 缓存调用上下文
            self._call_contexts[call_id] = {
                "agent_id": agent_id,
                "context": context,
            }

        return call_id

    def get_pending_calls(self) -> list[dict]:
        """获取所有待执行的调用"""
        with self._lock:
            data = self._read_json(self.pending_path)
            return [c for c in data.get("calls", []) if c.get("status") == "pending"]

    def get_result(self, call_id: str) -> Optional[dict]:
        """
        获取指定调用的执行结果

        Args:
            call_id: 调用 ID

        Returns:
            执行结果，如果未完成则返回 None
        """
        with self._lock:
            data = self._read_json(self.completed_path)
            for result in data.get("results", []):
                if result.get("call_id") == call_id:
                    return result
        return None

    def get_all_results(self) -> list[dict]:
        """获取所有已完成的执行结果"""
        with self._lock:
            data = self._read_json(self.completed_path)
            return data.get("results", [])

    def mark_completed(
        self,
        call_id: str,
        result: Any,
        success: bool = True,
        error: str = None
    ) -> bool:
        """
        标记调用已完成（由 CLAUDE.md 执行后调用）

        Args:
            call_id: 调用 ID
            result: 执行结果
            success: 是否成功
            error: 错误信息（如果失败）

        Returns:
            是否成功标记
        """
        with self._lock:
            # 1. 从 pending 中移除
            pending_data = self._read_json(self.pending_path)
            original_call = None
            new_calls = []
            for call in pending_data.get("calls", []):
                if call.get("call_id") == call_id:
                    original_call = call
                else:
                    new_calls.append(call)

            if not original_call:
                return False

            pending_data["calls"] = new_calls
            self._write_json(self.pending_path, pending_data)

            # 2. 添加到 completed
            completed_data = self._read_json(self.completed_path)
            completed_data["results"].append({
                "call_id": call_id,
                "agent_id": original_call.get("agent_id"),
                "success": success,
                "result": result,
                "error": error,
                "completed_at": datetime.now().isoformat(),
            })
            self._write_json(self.completed_path, completed_data)

            # 3. 清理缓存
            self._call_contexts.pop(call_id, None)

        return True

    def has_pending_calls(self) -> bool:
        """检查是否有待执行的调用"""
        return len(self.get_pending_calls()) > 0

    def get_next_pending_call(self) -> Optional[dict]:
        """获取下一个待执行的调用（FIFO）"""
        pending = self.get_pending_calls()
        return pending[0] if pending else None

    def get_call_context(self, call_id: str) -> Optional[dict]:
        """获取调用的上下文信息"""
        return self._call_contexts.get(call_id)

    def clear_completed_results(self, max_age_hours: int = 24):
        """
        清理过期的已完成结果

        Args:
            max_age_hours: 最大保留时间（小时）
        """
        with self._lock:
            data = self._read_json(self.completed_path)
            cutoff = datetime.now().timestamp() - (max_age_hours * 3600)

            new_results = []
            for result in data.get("results", []):
                try:
                    completed_at = datetime.fromisoformat(result.get("completed_at", ""))
                    if completed_at.timestamp() > cutoff:
                        new_results.append(result)
                except (ValueError, TypeError):
                    # 保留无法解析时间的结果
                    new_results.append(result)

            data["results"] = new_results
            self._write_json(self.completed_path, data)

    def get_status(self) -> dict:
        """获取桥接器状态摘要"""
        return {
            "pending_count": len(self.get_pending_calls()),
            "completed_count": len(self.get_all_results()),
            "cached_contexts": len(self._call_contexts),
        }


# 全局单例
_bridge_instance: Optional[ExecutorBridge] = None


def get_bridge() -> ExecutorBridge:
    """获取全局 ExecutorBridge 实例"""
    global _bridge_instance
    if _bridge_instance is None:
        _bridge_instance = ExecutorBridge()
    return _bridge_instance


def reset_bridge():
    """重置桥接器实例（主要用于测试）"""
    global _bridge_instance
    _bridge_instance = None
