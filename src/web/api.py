"""src/web/api.py — FastAPI 路由"""
from __future__ import annotations
import asyncio
import json
import logging
import os
import re
import traceback

logger = logging.getLogger(__name__)
_UNSET = object()

# 防止后台 Task 被 GC 回收 —— asyncio 不持有强引用
_background_tasks: set[asyncio.Task] = set()


def _fire(coro):
    """创建后台 Task 并保持强引用直到完成"""
    t = asyncio.create_task(coro)
    _background_tasks.add(t)
    t.add_done_callback(_background_tasks.discard)
    return t
from datetime import datetime
from typing import Optional, Any
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, model_validator

from src.graph.state import GraphState, SubTask, TimeBudget, ExecutionPolicy
from src.graph.builder import build_graph
from src.graph.dynamic_builder import DynamicGraphBuilder
from src.discussion.manager import DiscussionManager, discussion_manager
from src.agents.sdk_executor import get_executor
from scripts import init_project


# ── 请求体模型（必须在模块层定义，FastAPI 才能正确解析 body） ──

class ExecutionPolicyPayload(BaseModel):
    """执行约束策略请求体"""
    force_complex_graph: bool = False
    min_agents_per_node: int = 1
    min_discussion_rounds: int = 1
    strict_enforcement: bool = False

    @model_validator(mode="after")
    def validate_strict_policy(self):
        if self.strict_enforcement:
            if not self.force_complex_graph:
                raise ValueError("strict_enforcement=true requires force_complex_graph=true")
            if self.min_agents_per_node < 3:
                raise ValueError("strict_enforcement=true requires min_agents_per_node>=3")
            if self.min_discussion_rounds < 10:
                raise ValueError("strict_enforcement=true requires min_discussion_rounds>=10")
        return self


class TaskCreate(BaseModel):
    """创建任务请求"""
    task: str
    time_minutes: Optional[float] = None
    execution_policy: Optional[ExecutionPolicyPayload] = None


class MessagePost(BaseModel):
    """发送消息请求"""
    from_agent: str
    content: str
    to_agents: list[str] = Field(default_factory=list)
    message_type: str = "info"


class TaskIntervene(BaseModel):
    """实时干预请求"""
    instruction: str  # 注入到运行中任务的指令


class SubtaskUpdate(BaseModel):
    """子任务编辑请求"""
    title: Optional[str] = None
    description: Optional[str] = None
    agent_type: Optional[str] = None
    priority: Optional[int] = None
    estimated_minutes: Optional[float] = None
    knowledge_domains: Optional[list[str]] = None


class ChatRequest(BaseModel):
    """与监控AI对话请求"""
    message: str
    history: list[dict] = Field(default_factory=list)  # [{"role": "user"|"assistant", "content": str}]


# 持久化文件路径
_STATE_FILE = Path("app_state.json")
_REPORTS_DIR = Path("reports")
_EXPORTS_DIR = Path("exports") / "tasks"
_SAFE_REPORT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _build_cors_origins() -> list[str]:
    raw = os.getenv("WEB_ALLOWED_ORIGINS", "")
    if raw.strip():
        return [o.strip() for o in raw.split(",") if o.strip()]
    return [
        "http://localhost:8001",
        "http://127.0.0.1:8001",
    ]


# 全局状态
class AppState:
    def __init__(self):
        self.tasks: dict[str, dict] = {}
        self.graph_builder = DynamicGraphBuilder()
        self.discussion_manager = discussion_manager
        self.active_websockets: list[WebSocket] = []
        self.system_status: str = "idle"
        self.current_node: str = ""
        self.current_task_id: Optional[str] = None
        self.intervention_queues: dict[str, list[str]] = {}
        self.terminal_log: list[dict] = []
        self.running_task_handles: dict[str, asyncio.Task] = {}
        self.start_lock = asyncio.Lock()
        self.post_init_lock = asyncio.Lock()
        self.post_init_done_task_ids: set[str] = set()
        self.state_lock = asyncio.Lock()
        self.state_rev: int = 0
        self._dirty: bool = False  # 标记是否有未保存的变更

    def append_terminal_log(self, entry: dict):
        self.terminal_log.append(entry)
        if len(self.terminal_log) > 500:
            self.terminal_log = self.terminal_log[-500:]
        self._dirty = True

    def mark_dirty(self):
        self._dirty = True

    def _bump_state_rev_unlocked(self):
        self.state_rev += 1

    def _snapshot_state_unlocked(self) -> dict[str, Any]:
        return {
            "status": self.system_status,
            "current_node": self.current_node,
            "current_task_id": self.current_task_id,
            "tasks_count": len(self.tasks),
            "running_tasks": len([t for t in self.tasks.values() if t.get("status") == "running"]),
            "state_rev": self.state_rev,
        }

    def _set_task_and_system_state_unlocked(
        self,
        task_id: str,
        *,
        task_status: Any = _UNSET,
        system_status: Any = _UNSET,
        current_node: Any = _UNSET,
        current_task_id: Any = _UNSET,
        error: Any = _UNSET,
        finished_at: Any = _UNSET,
        result: Any = _UNSET,
    ) -> dict:
        task = self.tasks.get(task_id)
        if not task:
            return {}

        changed = False

        def set_if_diff(container: dict, key: str, value: Any):
            nonlocal changed
            if container.get(key) != value:
                container[key] = value
                changed = True

        if task_status is not _UNSET:
            set_if_diff(task, "status", task_status)
        if error is not _UNSET:
            if error is None:
                if "error" in task:
                    task.pop("error", None)
                    changed = True
            else:
                set_if_diff(task, "error", error)
        if finished_at is not _UNSET:
            if finished_at is None:
                if "finished_at" in task:
                    task.pop("finished_at", None)
                    changed = True
            else:
                set_if_diff(task, "finished_at", finished_at)
        if result is not _UNSET:
            if result is None:
                if "result" in task:
                    task.pop("result", None)
                    changed = True
            else:
                set_if_diff(task, "result", result)

        if system_status is not _UNSET and self.system_status != system_status:
            self.system_status = system_status
            changed = True
        if current_node is not _UNSET and self.current_node != current_node:
            self.current_node = current_node
            changed = True
        if current_task_id is not _UNSET and self.current_task_id != current_task_id:
            self.current_task_id = current_task_id
            changed = True

        if changed:
            self._bump_state_rev_unlocked()
            self.mark_dirty()

        return self._snapshot_state_unlocked()

    def save_to_disk(self):
        """把核心状态序列化到磁盘"""
        try:
            data = {
                "tasks": self.tasks,
                "system_status": self.system_status,
                "current_node": self.current_node,
                "current_task_id": self.current_task_id,
                "terminal_log": self.terminal_log[-300:],
                "state_rev": self.state_rev,
            }
            _STATE_FILE.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            self._dirty = False
        except Exception as e:
            logger.warning("State persist failed: %s", e)

    def load_from_disk(self):
        """从磁盘恢复状态"""
        if not _STATE_FILE.exists():
            return
        try:
            data = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
            self.tasks = data.get("tasks", {})
            self.system_status = data.get("system_status", "idle")
            self.state_rev = int(data.get("state_rev", 0) or 0)
            # 重启后正在运行中的任务实际已停止，标为 failed
            for t in self.tasks.values():
                if t.get("status") == "running":
                    t["status"] = "failed"
                    t["error"] = "服务器重启，任务中断"
            if self.system_status == "running":
                self.system_status = "idle"
            self.current_node = ""
            self.current_task_id = data.get("current_task_id") or None
            self.terminal_log = data.get("terminal_log", [])
            # 注入一条重启提示日志
            self.terminal_log.append({
                "task_id": "",
                "line": f"⚡ 服务器已重启，已恢复 {len(self.tasks)} 个历史任务",
                "level": "warn",
                "ts": datetime.now().strftime("%H:%M:%S"),
            })
        except Exception as e:
            logger.warning("State load failed: %s", e)

    async def broadcast(self, event: str, data: dict):
        """广播事件到所有连接的 WebSocket"""
        message = json.dumps({"event": event, "data": data}, ensure_ascii=False)
        for ws in self.active_websockets[:]:
            try:
                await ws.send_text(message)
            except (ConnectionError, RuntimeError, OSError):
                if ws in self.active_websockets:
                    self.active_websockets.remove(ws)


app_state = AppState()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    # 启动时初始化标准工作流并恢复磁盘状态
    app_state.graph_builder.create_standard_workflow()
    app_state.load_from_disk()

    # 后台定时保存（每 5 秒）
    async def _periodic_save():
        while True:
            await asyncio.sleep(5)
            if app_state._dirty:
                app_state.save_to_disk()

    save_task = asyncio.create_task(_periodic_save())
    yield
    # 关闭时保存最终状态
    app_state.save_to_disk()
    save_task.cancel()
    app_state.active_websockets.clear()


def create_app() -> FastAPI:
    """创建 FastAPI 应用"""
    app = FastAPI(
        title="Claude LangGraph System",
        description="多 Agent 协作系统 Web 管理后台",
        version="2.0.0",
        lifespan=lifespan,
    )

    # CORS 配置
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_build_cors_origins(),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 静态文件
    app.mount("/static", StaticFiles(directory="src/web/static"), name="static")

    # 注册路由
    register_routes(app)

    return app


def register_routes(app: FastAPI):
    """注册 API 路由"""

    async def _start_task_internal(task_id: str, *, force: bool = False, source: str = "manual") -> dict:
        """统一任务启动入口（带互斥）。"""
        async with app_state.start_lock:
            async with app_state.state_lock:
                task = app_state.tasks.get(task_id)
                if task is None:
                    raise HTTPException(status_code=404, detail="Task not found")

                current_status = task.get("status")
                if current_status == "running":
                    if task_id not in app_state.running_task_handles:
                        handle = _fire(run_task(task_id))
                        app_state.running_task_handles[task_id] = handle
                    return {
                        "status": "running",
                        "task_id": task_id,
                        "source": source,
                        "state": app_state._snapshot_state_unlocked(),
                        "task": task,
                    }

                if current_status in TERMINAL_STATUSES:
                    return {
                        "status": current_status,
                        "task_id": task_id,
                        "source": source,
                        "state": app_state._snapshot_state_unlocked(),
                        "task": task,
                    }

                active_task_id = app_state.current_task_id
                active_task = app_state.tasks.get(active_task_id) if active_task_id else None
                if (
                    app_state.system_status == "running"
                    and active_task
                    and active_task.get("status") == "running"
                    and active_task_id != task_id
                ):
                    if not force:
                        task["status"] = "queued"
                        app_state._bump_state_rev_unlocked()
                        app_state.mark_dirty()
                        return {
                            "status": "queued",
                            "task_id": task_id,
                            "source": source,
                            "state": app_state._snapshot_state_unlocked(),
                            "task": task,
                        }
                    raise HTTPException(status_code=409, detail=f"Task already running: {active_task_id}")

                task["status"] = "running"
                task.pop("error", None)
                task.pop("finished_at", None)
                snapshot = app_state._set_task_and_system_state_unlocked(
                    task_id,
                    task_status="running",
                    system_status="running",
                    current_task_id=task_id,
                    current_node="",
                    error=None,
                    finished_at=None,
                )

            await app_state.broadcast("system_status_changed", {
                **snapshot,
                "task_id": task_id,
                "task": task.get("task", ""),
                "source": source,
                "ts": datetime.now().isoformat(),
            })
            await app_state.broadcast("task_started", {
                "id": task_id,
                "source": source,
                "state_rev": snapshot.get("state_rev"),
                "ts": datetime.now().isoformat(),
            })

            handle = _fire(run_task(task_id))
            app_state.running_task_handles[task_id] = handle
            return {
                "status": "running",
                "task_id": task_id,
                "source": source,
                "state": snapshot,
                "task": task,
            }

    # ── 页面路由 ──

    @app.get("/", response_class=HTMLResponse)
    async def index():
        """主页"""
        return FileResponse("src/web/static/index.html")

    # ── 任务 API ──

    def _is_task_normalize_enabled() -> bool:
        raw = os.getenv("WEB_TASK_NORMALIZE_ENABLED", "1").strip().lower()
        return raw not in {"0", "false", "off", "no"}

    def _extract_json_object(raw_text: str) -> dict[str, Any]:
        text = (raw_text or "").strip()
        if not text:
            raise ValueError("empty sdk output")

        fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text, flags=re.IGNORECASE)
        if fence_match:
            text = fence_match.group(1).strip()

        try:
            parsed = json.loads(text)
            if not isinstance(parsed, dict):
                raise ValueError("sdk output is not a JSON object")
            return parsed
        except Exception:
            pass

        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("json object not found in sdk output")

        parsed = json.loads(text[start:end + 1])
        if not isinstance(parsed, dict):
            raise ValueError("sdk output is not a JSON object")
        return parsed

    async def _normalize_task_text_via_sdk(raw_task: str) -> dict[str, Any]:
        original = raw_task or ""
        compact = original.strip()

        fallback = {
            "normalized_task": original,
            "raw_task": original,
            "format_meta": {
                "transformed": False,
                "reason": "fallback_to_raw",
            },
        }

        if not compact:
            fallback["format_meta"]["reason"] = "empty_input"
            return fallback

        if not _is_task_normalize_enabled():
            fallback["format_meta"]["reason"] = "disabled_by_config"
            return fallback

        system_prompt = (
            "你是任务描述整理器。只做重组与轻度润色，不改变用户意图，不新增不存在的约束。\n"
            "请将输入任务整理成结构化 Markdown，优先使用这些标题并按需省略空章节：\n"
            "## Goal\n## Scope\n## Constraints\n## Acceptance Criteria\n## Deliverables\n\n"
            "输出必须是 JSON 对象且仅包含以下字段：\n"
            "{\"normalized_task\": string, \"transformed\": boolean, \"reason\": string}\n"
            "- normalized_task: 整理后的 Markdown\n"
            "- transformed: 仅当结构明显优化时为 true\n"
            "- reason: 简短说明（如 structured_sections / already_structured）\n"
            "不要输出 JSON 之外的任何内容。"
        )

        prompt = (
            "请整理以下任务输入，并按要求返回 JSON：\n"
            "----- RAW TASK START -----\n"
            f"{original}\n"
            "----- RAW TASK END -----"
        )

        try:
            executor = get_executor()
            result = await asyncio.wait_for(
                executor.execute(
                    agent_id="task_input_normalizer",
                    system_prompt=system_prompt,
                    context={"task": prompt},
                    tools=[],
                    max_turns=4,
                ),
                timeout=25,
            )

            if not result.success:
                fallback["format_meta"]["reason"] = f"sdk_failed:{(result.error or 'unknown')[:120]}"
                return fallback

            parsed = _extract_json_object(str(result.result or ""))
            normalized_task = str(parsed.get("normalized_task") or "").strip()
            if not normalized_task:
                fallback["format_meta"]["reason"] = "empty_normalized_output"
                return fallback

            transformed = bool(parsed.get("transformed")) and normalized_task != original
            reason = str(parsed.get("reason") or ("structured_sections" if transformed else "already_structured"))

            return {
                "normalized_task": normalized_task,
                "raw_task": original,
                "format_meta": {
                    "transformed": transformed,
                    "reason": reason[:200],
                },
            }
        except asyncio.TimeoutError:
            fallback["format_meta"]["reason"] = "sdk_timeout"
            return fallback
        except Exception as e:
            fallback["format_meta"]["reason"] = f"sdk_exception:{str(e)[:120]}"
            return fallback

    @app.post("/api/tasks")
    async def create_task(req: TaskCreate):
        """创建新任务"""
        import uuid
        from pathlib import Path as _Path
        task_id = str(uuid.uuid4())[:8]

        # 清空上一次任务的 reports/ 文件，避免旧报告污染新任务视图
        _reports_dir = _Path("reports")
        if _reports_dir.exists():
            for _f in _reports_dir.iterdir():
                if _f.is_file() and _f.suffix in (".md", ".json", ".txt"):
                    try:
                        _f.unlink()
                    except Exception:
                        pass

        policy_data = req.execution_policy.model_dump() if req.execution_policy else None
        normalized_payload = await _normalize_task_text_via_sdk(req.task)

        task_data = {
            "id": task_id,
            "task": normalized_payload["normalized_task"],
            "task_raw": normalized_payload["raw_task"],
            "task_format_meta": normalized_payload["format_meta"],
            "time_minutes": req.time_minutes,
            "execution_policy": policy_data,
            "status": "created",
            "created_at": datetime.now().isoformat(),
            "subtasks": [],
            "discussions": {},
        }

        async with app_state.state_lock:
            app_state.tasks[task_id] = task_data
            app_state._bump_state_rev_unlocked()
            app_state.mark_dirty()
            create_snapshot = app_state._snapshot_state_unlocked()
            created_payload = {**task_data, "state_rev": create_snapshot["state_rev"]}

        await app_state.broadcast("task_created", created_payload)

        # 自动启动：创建后立即触发执行（若系统忙则进入 queued）
        async def _auto_start():
            async with app_state.state_lock:
                task = app_state.tasks.get(task_id)
                if not task:
                    return
                if task.get("status") != "created":
                    return
                active_task_id = app_state.current_task_id
                active_task = app_state.tasks.get(active_task_id) if active_task_id else None
                if (
                    app_state.system_status == "running"
                    and active_task
                    and active_task.get("status") == "running"
                ):
                    task["status"] = "queued"
                    app_state._bump_state_rev_unlocked()
                    app_state.mark_dirty()
                    deferred_payload = {
                        "id": task_id,
                        "reason": "system_busy",
                        "state_rev": app_state.state_rev,
                        "ts": datetime.now().isoformat(),
                    }
                else:
                    deferred_payload = None

            if deferred_payload:
                await app_state.broadcast("task_start_deferred", deferred_payload)
                return

            result = await _start_task_internal(task_id, source="auto")
            if result.get("status") == "queued":
                await app_state.broadcast("task_start_deferred", {
                    "id": task_id,
                    "reason": "system_busy",
                    "state_rev": result.get("state", {}).get("state_rev"),
                    "ts": datetime.now().isoformat(),
                })

        _fire(_auto_start())

        return {
            "id": task_id,
            "status": "created",
            "state_rev": create_snapshot["state_rev"],
        }

    def _normalize_assigned_agents(value: Any, specialist_id: Any = None) -> list[str]:
        """标准化 assigned_agents：始终返回 list[str]，并兼容旧 specialist_id。"""
        raw = value
        if raw is None and specialist_id:
            raw = [specialist_id]
        elif raw is None:
            raw = []

        if isinstance(raw, (list, tuple, set)):
            return [str(v).strip() for v in raw if str(v).strip()]

        text = str(raw).strip()
        return [text] if text else []

    def _normalize_subtask_item_for_api(st: dict[str, Any]) -> dict[str, Any]:
        """标准化单个 subtask 快照，保证关键字段结构稳定。"""
        item = dict(st)

        deps = item.get("dependencies", item.get("depends_on", []))
        if isinstance(deps, (list, tuple, set)):
            item["dependencies"] = [str(d).strip() for d in deps if str(d).strip()]
        elif deps:
            dep_text = str(deps).strip()
            item["dependencies"] = [dep_text] if dep_text else []
        else:
            item["dependencies"] = []

        item["id"] = item.get("id") or ""
        item["title"] = item.get("title") or ""
        item["status"] = item.get("status") or "pending"
        item["agent_type"] = item.get("agent_type") or ""
        item["assigned_agents"] = _normalize_assigned_agents(
            item.get("assigned_agents"),
            specialist_id=item.get("specialist_id"),
        )
        return item

    def _normalize_subtasks_for_api(subtasks: Any) -> list[dict[str, Any]]:
        normalized_subtasks: list[dict[str, Any]] = []
        for st in subtasks or []:
            if not isinstance(st, dict):
                continue
            normalized_subtasks.append(_normalize_subtask_item_for_api(st))
        return normalized_subtasks

    def _normalize_task_for_api(task: dict[str, Any]) -> dict[str, Any]:
        """返回 API 安全快照：标准化 subtasks 结构（含 assigned_agents/dependencies）。"""
        normalized = dict(task)
        normalized["subtasks"] = _normalize_subtasks_for_api(normalized.get("subtasks") or [])
        return normalized

    @app.get("/api/tasks")
    async def list_tasks():
        """获取任务列表"""
        async with app_state.state_lock:
            return {
                "tasks": [_normalize_task_for_api(t) for t in app_state.tasks.values()],
                "count": len(app_state.tasks),
                "state_rev": app_state.state_rev,
            }

    @app.delete("/api/tasks")
    async def clear_all_tasks(force: bool = False):
        """清空所有任务及子任务，重置系统状态。force=true 时强制清空（用于服务器重启后清除僵尸任务）"""
        async with app_state.state_lock:
            running = [t for t in app_state.tasks.values() if t.get("status") == "running"]
            if running and not force:
                raise HTTPException(status_code=409, detail="任务正在运行，无法清空")
            app_state.tasks.clear()
            app_state.current_task_id = None
            app_state.current_node = ""
            app_state.system_status = "idle"
            app_state.terminal_log.clear()
            app_state.intervention_queues.clear()
            app_state._bump_state_rev_unlocked()
            app_state.mark_dirty()
            snapshot = app_state._snapshot_state_unlocked()

        await app_state.broadcast("tasks_cleared", {"state_rev": snapshot["state_rev"]})
        await app_state.broadcast("system_status_changed", {
            **snapshot,
            "source": "tasks_cleared",
            "ts": datetime.now().isoformat(),
        })
        return {"status": "cleared", "state_rev": snapshot["state_rev"]}

    @app.get("/api/tasks/{task_id}")
    async def get_task(task_id: str):
        """获取任务详情"""
        if task_id not in app_state.tasks:
            raise HTTPException(status_code=404, detail="Task not found")
        return _normalize_task_for_api(app_state.tasks[task_id])

    @app.patch("/api/tasks/{task_id}/subtasks/{subtask_id}")
    async def update_subtask(task_id: str, subtask_id: str, req: SubtaskUpdate):
        """编辑子任务字段（title / description / agent_type / priority / estimated_minutes）"""
        if task_id not in app_state.tasks:
            raise HTTPException(status_code=404, detail="Task not found")
        task = app_state.tasks[task_id]
        subtasks = task.get("subtasks", [])
        target = next((s for s in subtasks if s["id"] == subtask_id), None)
        if target is None:
            raise HTTPException(status_code=404, detail="Subtask not found")

        updates = req.model_dump(exclude_none=True)
        target.update(updates)
        app_state.mark_dirty()

        await app_state.broadcast("task_progress", {
            "task_id": task_id,
            "subtasks": _normalize_subtasks_for_api(subtasks),
        })
        return target

    @app.delete("/api/tasks/{task_id}")
    async def cancel_task(task_id: str):
        """取消/停止指定任务"""
        async with app_state.state_lock:
            task = app_state.tasks.get(task_id)
            if task is None:
                raise HTTPException(status_code=404, detail="Task not found")

            old_status = task.get("status")
            if old_status != "running":
                return {"status": task.get("status", "cancelled"), "task_id": task_id}

            now_iso = datetime.now().isoformat()
            snapshot = app_state._set_task_and_system_state_unlocked(
                task_id,
                task_status="cancelled",
                error="用户取消",
                finished_at=now_iso,
                system_status="idle",
                current_node="",
                current_task_id=None,
            )

            running_handle = app_state.running_task_handles.pop(task_id, None)

        if running_handle and not running_handle.done():
            running_handle.cancel()

        await app_state.broadcast("task_cancelled", {
            "id": task_id,
            "previous_status": old_status,
            "state_rev": snapshot.get("state_rev"),
            "ts": datetime.now().isoformat(),
        })
        await app_state.broadcast("system_status_changed", {
            **snapshot,
            "task_id": task_id,
            "source": "cancel_task",
            "ts": datetime.now().isoformat(),
        })

        return {
            "status": "cancelled",
            "task_id": task_id,
            "state_rev": snapshot.get("state_rev"),
        }

    @app.post("/api/tasks/{task_id}/intervene")
    async def intervene_task(task_id: str, req: TaskIntervene):
        """向运行中的任务注入实时指令"""
        if task_id not in app_state.tasks:
            raise HTTPException(status_code=404, detail="Task not found")

        task = app_state.tasks[task_id]
        if task.get("status") != "running":
            raise HTTPException(
                status_code=409,
                detail=f"Task {task_id} is not running (current status: {task.get('status')})",
            )

        # 追加到干预队列（run_task 在下一个节点间隙消费）
        if task_id not in app_state.intervention_queues:
            app_state.intervention_queues[task_id] = []
        app_state.intervention_queues[task_id].append(req.instruction)

        # 记录到任务历史
        if "interventions" not in task:
            task["interventions"] = []
        entry = {"content": req.instruction, "timestamp": datetime.now().isoformat()}
        task["interventions"].append(entry)

        await app_state.broadcast("task_intervened", {
            "task_id": task_id,
            "instruction": req.instruction,
            "timestamp": entry["timestamp"],
            "echoed_to_terminal": True,
        })

        terminal_entry = {
            "task_id": task_id,
            "line": f"[USER] $ {req.instruction}",
            "level": "input",
            "ts": datetime.now().strftime("%H:%M:%S"),
        }
        app_state.append_terminal_log(terminal_entry)
        await app_state.broadcast("terminal_output", terminal_entry)

        return {"status": "queued", "timestamp": entry["timestamp"]}

    @app.post("/api/chat")
    async def chat_with_monitor(req: ChatRequest):
        """与监控AI对话（立即返回，结果通过 WebSocket 推送）"""

        running = [t for t in app_state.tasks.values() if t.get("status") == "running"]
        task_summaries = "\n".join(
            f"  - [{t['status']}] {t['id']}: {t['task'][:60]}"
            for t in list(app_state.tasks.values())[-8:]
        ) or "  （暂无任务）"

        state_context = f"""[web对话上下文]
当前系统状态: {app_state.system_status}
当前节点: {app_state.current_node or '无'}
运行中任务: {len(running)} 个
任务列表（最近8条）:
{task_summaries}
---
用户通过 Web UI 发来消息，请以 CLAUDE.md supervisor 身份回答。"""

        history_lines = ""
        for h in req.history[-6:]:
            role_label = "用户" if h.get("role") == "user" else "助手"
            history_lines += f"{role_label}: {h.get('content', '')}\n"

        full_prompt = f"{state_context}\n\n{history_lines}用户: {req.message}\n助手:"

        async def _run_chat():
            executor = get_executor()
            try:
                result = await executor.execute(
                    agent_id="monitor_chat",
                    system_prompt="",
                    context={"task": full_prompt},
                    tools=[],
                    max_turns=5,
                )
                if not result.success:
                    reply = f"⚠️ 执行错误: {result.error or '未知错误'}"
                else:
                    reply = (result.result or "（无回复内容）").strip()
            except Exception as e:
                reply = f"⚠️ 请求失败: {str(e)[:200]}"

            await app_state.broadcast("chat_reply", {
                "role": "assistant",
                "content": reply,
                "ts": datetime.now().isoformat(),
            })

        _fire(_run_chat())
        return {"status": "thinking"}

    @app.post("/api/tasks/{task_id}/start")
    async def start_task(task_id: str):
        """启动任务执行"""
        result = await _start_task_internal(task_id, source="manual")
        return {"status": result["status"], "task_id": result["task_id"]}

    TERMINAL_STATUSES = {"completed", "failed", "cancelled"}
    ACTIVE_STATUSES = {"running", "created", "queued"}

    def _event_meta(
        *,
        task_id: str,
        node_id: str = "",
        phase: str = "",
        summary: str = "",
        verification: str = "",
        report_path: str = "",
        timestamp: Optional[str] = None,
    ) -> dict[str, Any]:
        return {
            "task_id": task_id,
            "node_id": node_id,
            "phase": phase,
            "summary": summary,
            "verification": verification,
            "report_path": report_path,
            "timestamp": timestamp or datetime.now().isoformat(),
        }

    async def _emit_discussion_message(
        *,
        task_id: str,
        node_id: str,
        message: Any,
        emit_terminal: bool = True,
    ):
        msg_dict = message.to_dict() if hasattr(message, "to_dict") else dict(message or {})
        await app_state.broadcast("discussion_message", {
            "task_id": task_id,
            "node_id": node_id,
            "message": msg_dict,
        })

        if not emit_terminal:
            return

        speaker = msg_dict.get("from_agent") or "system"
        content = (msg_dict.get("content") or "").replace("\n", " ").strip()
        if len(content) > 80:
            content = f"{content[:80]}…"

        terminal_entry = {
            "task_id": task_id,
            "line": f"[DISCUSSION][{node_id}][{speaker}] {content or '(empty)'}",
            "level": "info",
            "ts": datetime.now().strftime("%H:%M:%S"),
        }
        app_state.append_terminal_log(terminal_entry)
        await app_state.broadcast("terminal_output", terminal_entry)

    def _collect_reports_manifest() -> list[str]:
        if not _REPORTS_DIR.exists() or not _REPORTS_DIR.is_dir():
            return []
        items: list[str] = []
        for f in sorted(_REPORTS_DIR.iterdir()):
            if f.is_file() and f.suffix in (".md", ".json", ".txt"):
                items.append(f.name)
        return items

    def _build_task_export_payload(task_id: str) -> dict[str, Any]:
        task = app_state.tasks.get(task_id, {})
        subtasks_payload = []
        for st in task.get("subtasks", []):
            subtasks_payload.append({
                "id": st.get("id"),
                "title": st.get("title"),
                "status": st.get("status"),
                "agent_type": st.get("agent_type"),
                "result_summary": (st.get("result") or "")[:300],
            })

        result = task.get("result")
        if not result:
            result = task.get("error") or ""

        return {
            "task_id": task_id,
            "status": task.get("status"),
            "task": task.get("task"),
            "created_at": task.get("created_at"),
            "finished_at": task.get("finished_at"),
            "result": result,
            "subtasks": subtasks_payload,
            "reports_manifest": _collect_reports_manifest(),
            "exported_at": datetime.now().isoformat(),
        }

    def _render_task_export_markdown(payload: dict[str, Any]) -> str:
        lines = [
            f"# Task Export: {payload.get('task_id', '')}",
            "",
            f"- status: {payload.get('status', '')}",
            f"- created_at: {payload.get('created_at', '')}",
            f"- finished_at: {payload.get('finished_at', '')}",
            "",
            "## Task",
            payload.get("task", "") or "",
            "",
            "## Result",
            payload.get("result", "") or "",
            "",
            "## Subtasks",
        ]

        subtasks = payload.get("subtasks", []) or []
        if not subtasks:
            lines.append("- (none)")
        else:
            for st in subtasks:
                lines.append(
                    f"- {st.get('id', '')} | {st.get('title', '')} | {st.get('status', '')} | {st.get('agent_type', '')}"
                )
                summary = st.get("result_summary", "") or ""
                if summary:
                    lines.append(f"  - summary: {summary}")

        lines.append("")
        lines.append("## Reports Manifest")
        reports_manifest = payload.get("reports_manifest", []) or []
        if not reports_manifest:
            lines.append("- (none)")
        else:
            for name in reports_manifest:
                lines.append(f"- {name}")

        lines.append("")
        lines.append(f"_exported_at: {payload.get('exported_at', '')}_")
        return "\n".join(lines).strip() + "\n"

    async def _export_task_result(task_id: str, retries: int = 3) -> bool:
        last_error = ""
        for attempt in range(1, retries + 1):
            try:
                payload = _build_task_export_payload(task_id)
                if not payload.get("task_id") or not payload.get("status"):
                    raise RuntimeError("task payload incomplete")

                _EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
                json_path = _EXPORTS_DIR / f"{task_id}.json"
                md_path = _EXPORTS_DIR / f"{task_id}.md"
                json_path.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                md_path.write_text(_render_task_export_markdown(payload), encoding="utf-8")
                return True
            except Exception as e:
                last_error = str(e)
                logger.warning(
                    "Task export failed (task=%s, attempt=%s/%s): %s",
                    task_id,
                    attempt,
                    retries,
                    e,
                )
                if attempt < retries:
                    await asyncio.sleep(0.5)

        await app_state.broadcast("post_init_blocked", {
            "task_id": task_id,
            "reason": "export_failed",
            "error": last_error[:300],
        })
        return False

    def _has_active_tasks(exclude_task_id: Optional[str] = None) -> bool:
        for tid, t in app_state.tasks.items():
            if exclude_task_id and tid == exclude_task_id:
                continue
            if t.get("status") in ACTIVE_STATUSES:
                return True
        return False

    async def _schedule_post_task_init(task_id: str):
        async with app_state.post_init_lock:
            if task_id in app_state.post_init_done_task_ids:
                return

            # marathon 运行期间不执行 post-task init（避免清空正在轮转的任务）
            if Path("marathon.lock.json").exists():
                logger.info("post_task_init skipped: marathon.lock.json present")
                await app_state.broadcast("post_init_skipped", {
                    "task_id": task_id,
                    "reason": "marathon_running",
                })
                return

            task = app_state.tasks.get(task_id)
            if not task or task.get("status") not in TERMINAL_STATUSES:
                return

            exported = await _export_task_result(task_id)
            if not exported:
                return

            if _has_active_tasks(exclude_task_id=task_id):
                await app_state.broadcast("post_init_skipped", {
                    "task_id": task_id,
                    "reason": "active_tasks_present",
                })
                return

            try:
                init_summary = await asyncio.to_thread(init_project.run_full_init, dry=False)
            except Exception as e:
                logger.exception("Auto init failed for task %s", task_id)
                await app_state.broadcast("post_init_failed", {
                    "task_id": task_id,
                    "error": str(e)[:300],
                })
                return

            app_state.post_init_done_task_ids.add(task_id)
            async with app_state.state_lock:
                app_state.tasks.clear()
                app_state.intervention_queues.clear()
                app_state.current_task_id = None
                app_state.current_node = ""
                app_state.system_status = "idle"
                app_state.terminal_log.clear()
                app_state._bump_state_rev_unlocked()
                app_state.mark_dirty()
                app_state.save_to_disk()
                snapshot = app_state._snapshot_state_unlocked()

            await app_state.broadcast("post_init_completed", {
                "task_id": task_id,
                "summary": init_summary,
                "state_rev": snapshot["state_rev"],
            })
            await app_state.broadcast("system_status_changed", {
                **snapshot,
                "task_id": task_id,
                "source": "auto_init",
                "ts": datetime.now().isoformat(),
            })

    async def run_task(task_id: str):
        """执行任务（后台）"""
        task = app_state.tasks[task_id]
        oscillation_window: list[tuple[str, str, str, tuple]] = []
        oscillation_warned = False

        def is_cancelled() -> bool:
            current = app_state.tasks.get(task_id)
            return not current or current.get("status") == "cancelled"

        async def emit(
            line: str,
            level: str = "info",
            *,
            node_id: str = "",
            phase: str = "",
            summary: str = "",
            verification: str = "",
            report_path: str = "",
        ):
            """broadcast 一行终端输出并持久化到 terminal_log"""
            meta = _event_meta(
                task_id=task_id,
                node_id=node_id,
                phase=phase,
                summary=summary,
                verification=verification,
                report_path=report_path,
            )
            entry = {
                "task_id": task_id,
                "line": line,
                "level": level,
                "ts": datetime.now().strftime("%H:%M:%S"),
                **meta,
            }
            app_state.append_terminal_log(entry)
            await app_state.broadcast("terminal_output", entry)

        try:
            graph = app_state.graph_builder.compile()
            await emit(f"▶ 任务已启动: {task['task'][:60]}", "start")

            if is_cancelled():
                await emit("⊘ 任务已取消，停止执行", "warn")
                return

            execution_policy = None
            raw_policy = task.get("execution_policy")
            if isinstance(raw_policy, dict):
                execution_policy = ExecutionPolicy.model_validate(raw_policy)

            initial_state: GraphState = {
                "user_task": task["task"],
                "time_budget": TimeBudget(total_minutes=task["time_minutes"], started_at=datetime.now()) if task["time_minutes"] else None,
                "execution_policy": execution_policy,
                "subtasks": [],
                "discussions": {},
                "messages": [],
                "execution_log": [],
                "artifacts": {},
                "phase": "init",
                "iteration": 0,
                "max_iterations": 3,
                "error": None,
                "final_output": None,
            }

            config = {"configurable": {"thread_id": task_id}}

            async for event in graph.astream(initial_state, config):
                if is_cancelled():
                    await emit("⊘ 检测到取消信号，提前终止", "warn")
                    break

                for node_name, state_update in event.items():
                    if is_cancelled():
                        await emit("⊘ 节点处理前检测到取消信号", "warn")
                        break
                    # 更新当前节点（与 state_rev 原子前进）
                    async with app_state.state_lock:
                        final_node = "executor" if node_name == "budget_manager" else node_name
                        app_state.current_node = final_node
                        task["current_node"] = final_node
                        app_state._bump_state_rev_unlocked()
                        app_state.mark_dirty()
                        snapshot = app_state._snapshot_state_unlocked()

                    await app_state.broadcast("node_changed", {
                        "task_id": task_id,
                        "node": final_node,
                        "state_rev": snapshot["state_rev"],
                        "ts": datetime.now().isoformat(),
                        **_event_meta(task_id=task_id, node_id=final_node, phase=state_update.get("phase", "")),
                    })
                    await emit(
                        f"\u25b6 [{node_name.upper()}] phase={state_update.get('phase','')}",
                        "node",
                        node_id=final_node,
                        phase=state_update.get("phase", ""),
                    )

                    # 轻量震荡检测：phase 交替且子任务摘要长期不变（仅告警，不拦截）
                    phase = state_update.get("phase", "")
                    current_cid = state_update.get("current_subtask_id") or ""
                    subtask_digest = tuple(
                        sorted(
                            (
                                t.id,
                                t.status,
                                tuple(getattr(t, "dependencies", []) or []),
                            )
                            for t in state_update.get("subtasks", [])
                        )
                    )
                    oscillation_window.append((node_name, phase, current_cid, subtask_digest))
                    if len(oscillation_window) > 8:
                        oscillation_window.pop(0)

                    if len(oscillation_window) == 8 and not oscillation_warned:
                        phases = [item[1] for item in oscillation_window]
                        digests = [item[3] for item in oscillation_window]
                        unique_pairs = {(phases[i], phases[i + 1]) for i in range(len(phases) - 1)}
                        alternating = len(set(phases[-6:])) <= 2 and len(unique_pairs) <= 2
                        digest_stalled = len(set(digests[-6:])) == 1
                        if alternating and digest_stalled:
                            oscillation_warned = True
                            await emit(
                                (
                                    f"⚠ 震荡告警: node/phase 在 {list(dict.fromkeys(phases[-6:]))} 间反复切换，"
                                    f"current={current_cid or '-'}，subtask_digest 长时间无变化"
                                ),
                                "warn",
                            )

                    # 解析 execution_log 中的事件
                    for log_entry in state_update.get("execution_log", []):
                        ev = log_entry.get("event", "")
                        if ev == "planning_complete":
                            await emit(
                                f"  ✓ 规划完成: {log_entry.get('subtask_count')} 个子任务",
                                "success"
                            )
                        elif ev == "task_executed":
                            await emit(
                                f"  ✓ {log_entry.get('task_id')} 执行完成 [専家={log_entry.get('specialist_id','-')}]",
                                "success"
                            )
                        elif ev == "reflection_complete":
                            await emit(
                                f"  ↺ {log_entry.get('task_id')} 反思重试 #retry={log_entry.get('retry_count',0)+1}",
                                "warn"
                            )

                    existing_subtasks = {
                        str(s.get("id")): s
                        for s in (task.get("subtasks") or [])
                        if isinstance(s, dict) and s.get("id")
                    }

                    # 广播状态更新
                    progress_subtasks = _normalize_subtasks_for_api([
                        {
                            "id": t.id,
                            "title": t.title,
                            "status": t.status,
                            "agent_type": t.agent_type,
                            "assigned_agents": getattr(t, "assigned_agents", None),
                            "dependencies": (
                                getattr(t, "dependencies", None)
                                or getattr(t, "depends_on", None)
                                or (existing_subtasks.get(str(t.id), {}).get("dependencies") or [])
                            ),
                        }
                        for t in state_update.get("subtasks", [])
                    ])

                    await app_state.broadcast("task_progress", {
                        "task_id": task_id,
                        "node": node_name,
                        "phase": state_update.get("phase", ""),
                        "subtasks": progress_subtasks,
                        "result": state_update.get("final_output"),
                        **_event_meta(
                            task_id=task_id,
                            node_id=node_name,
                            phase=state_update.get("phase", ""),
                            summary=(state_update.get("final_output") or "")[:240],
                        ),
                    })

                    # 子任务状态变化时推送名单
                    if "subtasks" in state_update:
                        task["subtasks"] = _normalize_subtasks_for_api([
                            {
                                "id": t.id,
                                "title": t.title,
                                "description": t.description,
                                "agent_type": t.agent_type,
                                "assigned_agents": getattr(t, "assigned_agents", None),
                                "status": t.status,
                                "result": t.result,
                                "dependencies": (
                                    getattr(t, "dependencies", None)
                                    or getattr(t, "depends_on", None)
                                    or (existing_subtasks.get(str(t.id), {}).get("dependencies") or [])
                                ),
                            }
                            for t in state_update.get("subtasks", [])
                        ])
                        app_state.mark_dirty()
                        for t in state_update["subtasks"]:
                            if t.status == "running":
                                await emit(
                                    f"  ⧗ {t.id} 正在执行: {t.title} [{t.agent_type}]",
                                    "running"
                                )
                            elif t.status == "failed":
                                await emit(
                                    f"  ✗ {t.id} 失败: {(t.result or '')[:80]}",
                                    "error"
                                )

                    # 将 GraphState.discussions 同步到 discussion_manager
                    # GraphState key: node_id; manager key: {task_id}_{node_id}
                    if "discussions" in state_update:
                        for node_id, node_disc in (state_update["discussions"] or {}).items():
                            manager_key = f"{task_id}_{node_id}"
                            existing = app_state.discussion_manager.get_discussion(manager_key)
                            if existing is None:
                                existing = app_state.discussion_manager.create_discussion(manager_key)
                            # 合并新消息（避免重复追加）
                            existing_ids = {m.id for m in existing.messages}
                            new_messages = []
                            for msg in (node_disc.messages if hasattr(node_disc, "messages") else []):
                                if msg.id not in existing_ids:
                                    # 复制消息并修正 node_id 为 manager key
                                    from src.discussion.types import DiscussionMessage as _DM
                                    synced = _DM(
                                        id=msg.id,
                                        node_id=node_id,
                                        from_agent=msg.from_agent,
                                        to_agents=msg.to_agents,
                                        content=msg.content,
                                        timestamp=msg.timestamp,
                                        message_type=msg.message_type,
                                        metadata=msg.metadata,
                                    )
                                    existing.add_message(synced)
                                    existing_ids.add(msg.id)
                                    new_messages.append(synced)
                            existing.status = getattr(node_disc, "status", "resolved")
                            existing.consensus_reached = getattr(node_disc, "consensus_reached", False)
                            existing.consensus_topic = getattr(node_disc, "consensus_topic", None)

                            for synced in new_messages:
                                await _emit_discussion_message(
                                    task_id=task_id,
                                    node_id=node_id,
                                    message=synced,
                                )

                    if state_update.get("final_output"):
                        now_iso = datetime.now().isoformat()
                        async with app_state.state_lock:
                            current = app_state.tasks.get(task_id)
                            if not current:
                                break
                            current["subtasks"] = task.get("subtasks", [])
                            snapshot = app_state._set_task_and_system_state_unlocked(
                                task_id,
                                task_status="completed",
                                result=state_update["final_output"],
                                finished_at=now_iso,
                                system_status="completed",
                                current_node="",
                                current_task_id=None,
                            )

                        await emit(f"✓ 任务完成", "success")
                        await app_state.broadcast("task_completed", {
                            "id": task_id,
                            "result": state_update["final_output"],
                            "subtasks": task.get("subtasks", []),
                            "state_rev": snapshot["state_rev"],
                            "finished_at": now_iso,
                            "ts": now_iso,
                            **_event_meta(
                                task_id=task_id,
                                node_id=final_node,
                                phase=state_update.get("phase", ""),
                                summary=(state_update.get("final_output") or "")[:240],
                                verification="completed",
                                report_path="reports/",
                                timestamp=now_iso,
                            ),
                        })
                        await app_state.broadcast("system_status_changed", {
                            **snapshot,
                            "task_id": task_id,
                            "ts": now_iso,
                        })

                # ── 节点间隙：消费干预队列，注入 GraphState messages ──
                pending = app_state.intervention_queues.pop(task_id, [])
                if pending and not is_cancelled():
                    injected = [f"[用户实时指令] {inst}" for inst in pending]
                    await graph.aupdate_state(
                        config,
                        {"messages": injected},
                    )
                    await emit(f"↳ 干预已应用（{len(pending)} 条）", "info")
                    await app_state.broadcast("task_intervention_applied", {
                        "task_id": task_id,
                        "instructions": pending,
                        "ts": datetime.now().isoformat(),
                    })

        except asyncio.CancelledError:
            now_iso = datetime.now().isoformat()
            async with app_state.state_lock:
                current = app_state.tasks.get(task_id)
                if current and current.get("status") != "cancelled":
                    app_state._set_task_and_system_state_unlocked(
                        task_id,
                        task_status="cancelled",
                        error="任务被取消",
                        finished_at=now_iso,
                        system_status="idle",
                        current_node="",
                        current_task_id=None,
                    )
                    cancel_snapshot = app_state._snapshot_state_unlocked()
                else:
                    cancel_snapshot = app_state._snapshot_state_unlocked()
            await emit("⊘ 执行协程已取消", "warn")
            await app_state.broadcast("system_status_changed", {
                **cancel_snapshot,
                "task_id": task_id,
                "source": "run_task_cancelled",
                "ts": now_iso,
            })
            raise
        except Exception as e:
            now_iso = datetime.now().isoformat()
            async with app_state.state_lock:
                current = app_state.tasks.get(task_id)
                if current:
                    app_state._set_task_and_system_state_unlocked(
                        task_id,
                        task_status="failed",
                        error=str(e),
                        finished_at=now_iso,
                        system_status="failed",
                        current_node="",
                        current_task_id=None,
                    )
                    failed_node = current.get("current_node") or app_state.current_node
                    fail_snapshot = app_state._snapshot_state_unlocked()
                else:
                    failed_node = app_state.current_node
                    fail_snapshot = app_state._snapshot_state_unlocked()
            await emit(f"✗ 崩溃: {str(e)[:200]}", "error")

            # 生成崩溃报告
            crash_report = {
                "task_id": task_id,
                "failed_node": failed_node,
                "error_message": str(e),
                "traceback": traceback.format_exc(),
                "task": task.get("task"),
                "time": now_iso,
            }

            # 保存崩溃报告到 reports/ 目录（规范化）
            _REPORTS_DIR.mkdir(exist_ok=True)
            crash_report_path = _REPORTS_DIR / "crash_report.json"
            with open(crash_report_path, "w", encoding="utf-8") as f:
                json.dump(crash_report, f, indent=2, ensure_ascii=False)

            await app_state.broadcast("task_failed", {
                "id": task_id,
                "error": str(e),
                "crash_report_saved": str(crash_report_path),
                "state_rev": fail_snapshot.get("state_rev"),
                "ts": now_iso,
                **_event_meta(
                    task_id=task_id,
                    node_id=failed_node or "",
                    summary=(str(e) or "")[:240],
                    verification="failed",
                    report_path=str(crash_report_path),
                    timestamp=now_iso,
                ),
            })
            await app_state.broadcast("system_status_changed", {
                **fail_snapshot,
                "task_id": task_id,
                "error": str(e),
                "ts": now_iso,
            })
        finally:
            app_state.running_task_handles.pop(task_id, None)

            now_iso = datetime.now().isoformat()
            async with app_state.state_lock:
                current = app_state.tasks.get(task_id)
                if current:
                    status = current.get("status")
                    if status in TERMINAL_STATUSES:
                        app_state._set_task_and_system_state_unlocked(
                            task_id,
                            current_node="",
                            current_task_id=None,
                        )
                final_snapshot = app_state._snapshot_state_unlocked()

            if current and current.get("status") in TERMINAL_STATUSES:
                await app_state.broadcast("system_status_changed", {
                    **final_snapshot,
                    "task_id": task_id,
                    "source": "run_task_finalized",
                    "ts": now_iso,
                })
                app_state.save_to_disk()
                _fire(_schedule_post_task_init(task_id))

    # ── Graph API ──

    @app.get("/api/graph")
    async def get_graph():
        """获取 Graph 结构"""
        return app_state.graph_builder.to_dict()

    def _build_task_mermaid() -> str:
        """根据当前任务的子任务动态生成 Mermaid 字符串。
        有子任务时显示子任务 DAG；无子任务时退回标准骨架图。"""
        task_id = app_state.current_task_id
        task = app_state.tasks.get(task_id) if task_id else None
        subtasks: list[dict] = (task or {}).get("subtasks") or []

        if not subtasks:
            for t in reversed(list(app_state.tasks.values())):
                if t.get("subtasks"):
                    subtasks = t["subtasks"]
                    task = t
                    break

        if not subtasks:
            return ""

        def to_list_of_str(value: Any) -> list[str]:
            if not value:
                return []
            if isinstance(value, (list, tuple, set)):
                return [str(v).strip() for v in value if str(v).strip()]
            text = str(value).strip()
            return [text] if text else []

        def short_label(text: str, max_len: int = 42) -> str:
            clean = (text or "").replace('"', "'").replace("\n", " ").strip()
            if len(clean) <= max_len:
                return clean
            return clean[: max_len - 1].rstrip() + "…"

        normalized: list[dict[str, Any]] = []
        by_id: dict[str, dict[str, Any]] = {}
        for idx, st in enumerate(subtasks, start=1):
            if not isinstance(st, dict):
                continue
            raw_id = (st.get("id") or "").strip()
            if not raw_id:
                continue
            deps = to_list_of_str(st.get("dependencies") or st.get("depends_on"))
            item = {
                "raw_id": raw_id,
                "node_id": f"n{idx}",
                "title": st.get("title") or raw_id,
                "status": (st.get("status") or "pending").strip().lower(),
                "dependencies": deps,
            }
            normalized.append(item)
            by_id[raw_id] = item

        if not normalized:
            return ""

        edge_pairs: list[tuple[str, str]] = []
        edge_seen: set[tuple[str, str]] = set()
        indegree = {item["raw_id"]: 0 for item in normalized}

        for item in normalized:
            sid_raw = item["raw_id"]
            valid_deps: list[str] = []
            for dep_raw in item.get("dependencies", []):
                if dep_raw == sid_raw:
                    continue
                if dep_raw not in by_id:
                    continue
                pair = (dep_raw, sid_raw)
                if pair in edge_seen:
                    continue
                edge_seen.add(pair)
                edge_pairs.append(pair)
                valid_deps.append(dep_raw)
                indegree[sid_raw] = indegree.get(sid_raw, 0) + 1
            item["dependencies"] = valid_deps

        node_count = len(normalized)
        edge_count = len(edge_pairs)
        graph_direction = "TD" if node_count >= 10 or edge_count >= 12 else "LR"

        status_fill = {
            "running": "fill:#4c1d95,color:#e9d5ff,stroke:#a855f7,stroke-width:2px",
            "done": "fill:#14532d,color:#bbf7d0,stroke:#22c55e,stroke-width:2px",
            "completed": "fill:#14532d,color:#bbf7d0,stroke:#22c55e,stroke-width:2px",
            "failed": "fill:#7f1d1d,color:#fca5a5,stroke:#ef4444,stroke-width:2px",
            "pending": "fill:#27272a,color:#a1a1aa,stroke:#52525b,stroke-width:1.5px",
            "skipped": "fill:#1f1f27,color:#71717a,stroke:#3f3f46,stroke-width:1px",
            "blocked": "fill:#1f2937,color:#93c5fd,stroke:#3b82f6,stroke-width:1.5px,stroke-dasharray:4 3",
        }
        status_class = {
            "running": "running",
            "done": "done",
            "completed": "done",
            "failed": "failed",
            "pending": "pending",
            "skipped": "skipped",
            "blocked": "blocked",
        }

        done_ids = {
            item["raw_id"]
            for item in normalized
            if item.get("status") in ("done", "completed", "skipped")
        }

        lines = [f"graph {graph_direction}"]
        deferred_styles: list[str] = []
        class_assignments: dict[str, str] = {}

        task_status = ((task or {}).get("status") or "").strip().lower()
        task_title = short_label((task or {}).get("task") or "任务", max_len=52)
        hid = "task_header"

        if task_status == "running":
            phase = short_label(app_state.current_node or "running", max_len=22)
            lines.append(f'    {hid}(["⚙ {phase.upper()}"])')
            deferred_styles.append(
                f"    style {hid} fill:#4c1d95,color:#e9d5ff,stroke:#a855f7,stroke-width:2.5px"
            )
        elif task_status in ("completed", "done"):
            lines.append(f'    {hid}(["✓ {task_title}"])')
            deferred_styles.append(
                f"    style {hid} fill:#14532d,color:#bbf7d0,stroke:#22c55e,stroke-width:2.5px"
            )
        elif task_status == "failed":
            lines.append(f'    {hid}(["✗ {task_title}"])')
            deferred_styles.append(
                f"    style {hid} fill:#7f1d1d,color:#fca5a5,stroke:#ef4444,stroke-width:2.5px"
            )
        else:
            lines.append(f'    {hid}(["{task_title}"])')
            deferred_styles.append(
                f"    style {hid} fill:#27272a,color:#a1a1aa,stroke:#52525b,stroke-width:1.5px"
            )

        for item in normalized:
            sid = item["node_id"]
            raw_id = item["raw_id"]
            deps = item.get("dependencies", [])
            status = item.get("status") or "pending"
            is_blocked = bool(status == "pending" and deps and not all(dep in done_ids for dep in deps))
            visual_status = "blocked" if is_blocked else status

            icon = {
                "running": "⟳",
                "done": "✓",
                "completed": "✓",
                "failed": "✗",
                "pending": "○",
                "skipped": "—",
                "blocked": "⏸",
            }.get(visual_status, "○")

            title = short_label(item.get("title") or raw_id)
            lines.append(f'    {sid}(["{icon} {title}"])')
            deferred_styles.append(f"    style {sid} {status_fill.get(visual_status, status_fill['pending'])}")
            class_assignments[sid] = status_class.get(visual_status, "pending")

        edge_styles: list[str] = []
        edge_index = 0

        roots = [item for item in normalized if indegree.get(item["raw_id"], 0) == 0]
        for item in roots:
            lines.append(f"    {hid} --> {item['node_id']}")
            edge_styles.append("stroke:#52525b,stroke-width:1.4px")
            edge_index += 1

        for dep_raw, sid_raw in edge_pairs:
            dep_sid = by_id[dep_raw]["node_id"]
            sid = by_id[sid_raw]["node_id"]
            lines.append(f"    {dep_sid} --> {sid}")

            dep_status = by_id[dep_raw].get("status")
            if dep_status == "running":
                edge_styles.append("stroke:#a855f7,stroke-width:2.2px")
            elif dep_status not in ("done", "completed", "skipped"):
                edge_styles.append("stroke:#475569,stroke-width:1.6px,stroke-dasharray:4 3")
            else:
                edge_styles.append("stroke:#64748b,stroke-width:1.6px")
            edge_index += 1

        lines.extend([
            "    classDef pending fill:#27272a,color:#a1a1aa,stroke:#52525b,stroke-width:1.5px;",
            "    classDef running fill:#4c1d95,color:#e9d5ff,stroke:#a855f7,stroke-width:2px;",
            "    classDef done fill:#14532d,color:#bbf7d0,stroke:#22c55e,stroke-width:2px;",
            "    classDef failed fill:#7f1d1d,color:#fca5a5,stroke:#ef4444,stroke-width:2px;",
            "    classDef skipped fill:#1f1f27,color:#71717a,stroke:#3f3f46,stroke-width:1px;",
            "    classDef blocked fill:#1f2937,color:#93c5fd,stroke:#3b82f6,stroke-width:1.5px,stroke-dasharray:4 3;",
        ])

        for sid, cls in class_assignments.items():
            lines.append(f"    class {sid} {cls};")

        for i, style in enumerate(edge_styles):
            lines.append(f"    linkStyle {i} {style};")

        lines.extend(deferred_styles)
        return "\n".join(lines)

    @app.get("/api/graph/mermaid")
    async def get_graph_mermaid(current_node: str = ""):
        """获取 Mermaid 图形语法。有子任务时显示动态 DAG，否则显示标准骨架。"""
        mermaid_code = _build_task_mermaid()

        # 仅当调用者显式传入 current_node 时追加静态高亮（兼容旧用法）
        if current_node:
            mermaid_code += f"\n    style {current_node} stroke:#ff0000,stroke-width:4px"

        return {"mermaid": mermaid_code, "current_node": app_state.current_node}

    # ── 系统状态 API ──

    @app.get("/api/system/status")
    async def get_system_status(request: Request):
        """获取系统状态（含终端日志，供刷新后恢复）"""
        include_terminal = request.query_params.get("include_terminal") == "1"
        async with app_state.state_lock:
            response = app_state._snapshot_state_unlocked()
            if include_terminal:
                response["terminal_log"] = app_state.terminal_log[-200:]
        return response

    # ── 讨论 API ──

    @app.get("/api/tasks/{task_id}/nodes/{node_id}/discussion")
    async def get_discussion(task_id: str, node_id: str):
        """获取节点讨论"""
        if task_id not in app_state.tasks:
            raise HTTPException(status_code=404, detail="Task not found")

        discussion_key = f"{task_id}_{node_id}"
        discussion = app_state.discussion_manager.get_discussion(discussion_key)
        if discussion:
            return discussion.to_dict()
        return {"node_id": node_id, "messages": [], "participants": []}

    @app.post("/api/tasks/{task_id}/nodes/{node_id}/discussion")
    async def post_message(task_id: str, node_id: str, req: MessagePost):
        """发送讨论消息"""
        if task_id not in app_state.tasks:
            raise HTTPException(status_code=404, detail="Task not found")

        manager_node_id = f"{task_id}_{node_id}"
        msg = await app_state.discussion_manager.post_message(
            node_id=manager_node_id,
            from_agent=req.from_agent,
            content=req.content,
            to_agents=req.to_agents,
            message_type=req.message_type,
        )

        await _emit_discussion_message(task_id=task_id, node_id=node_id, message=msg)

        async def _auto_reply():
            reply_text = "已收到消息，建议先确认目标与约束，然后我会继续给出下一步执行建议。"
            reply_msg = await app_state.discussion_manager.post_message(
                node_id=manager_node_id,
                from_agent="assistant",
                content=reply_text,
                to_agents=[req.from_agent] if req.from_agent else [],
                message_type="response",
            )
            await _emit_discussion_message(task_id=task_id, node_id=node_id, message=reply_msg)

        _fire(_auto_reply())
        return msg.to_dict()

    @app.get("/api/discussions/summaries")
    async def get_discussion_summaries():
        """获取所有讨论摘要"""
        return {
            "summaries": [
                s.model_dump()
                for s in app_state.discussion_manager.get_summaries()
            ]
        }

    # ── Reports ──

    @app.get("/api/reports")
    async def list_reports():
        """列出 reports/ 目录下所有报告文件"""
        if not _REPORTS_DIR.exists():
            return {"files": []}
        files = []
        for f in sorted(_REPORTS_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if f.suffix in (".md", ".json") and f.is_file():
                try:
                    stat = f.stat()
                    files.append({
                        "name": f.name,
                        "stem": f.stem,
                        "ext": f.suffix,
                        "size": stat.st_size,
                        "mtime": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                    })
                except Exception:
                    pass
        return {"files": files}

    @app.get("/api/reports/{filename:path}")
    async def get_report(filename: str):
        """读取 reports/ 下指定文件内容"""
        if any(sep in filename for sep in ("/", "\\", ":")):
            raise HTTPException(status_code=400, detail="Invalid filename")
        if ".." in filename:
            raise HTTPException(status_code=400, detail="Invalid filename")
        if not _SAFE_REPORT_NAME.fullmatch(filename):
            raise HTTPException(status_code=400, detail="Invalid filename")

        report_base = _REPORTS_DIR.resolve()
        report_path = (report_base / filename).resolve()

        try:
            report_path.relative_to(report_base)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid filename")

        if not report_path.exists() or not report_path.is_file():
            raise HTTPException(status_code=404, detail="File not found")

        content = report_path.read_text(encoding="utf-8", errors="replace")
        return {"name": filename, "content": content}

    @app.exception_handler(404)
    async def _reports_not_found_override(request: Request, exc):
        path = request.url.path
        if path.startswith("/api/reports/"):
            tail = path[len("/api/reports/"):]
            if "/" in tail or "\\" in tail or "%2f" in path.lower() or "%5c" in path.lower() or ".." in tail:
                return JSONResponse(status_code=400, content={"detail": "Invalid filename"})
        return JSONResponse(status_code=404, content={"detail": "Not Found"})

    # ── WebSocket ──

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        """WebSocket 实时通信"""
        await websocket.accept()
        app_state.active_websockets.append(websocket)

        try:
            while True:
                data = await websocket.receive_text()
                try:
                    message = json.loads(data)
                    if message.get("type") == "terminal_input":
                        task_id = message.get("task_id") or app_state.current_task_id
                        cmd = message.get("command", "").strip()
                        if not cmd:
                            entry = {
                                "task_id": task_id,
                                "line": "[SYSTEM] Empty terminal command ignored",
                                "level": "warn",
                                "ts": datetime.now().strftime("%H:%M:%S"),
                            }
                            app_state.append_terminal_log(entry)
                            await app_state.broadcast("terminal_output", entry)
                            continue

                        if not task_id:
                            entry = {
                                "task_id": "",
                                "line": "[SYSTEM] No active task context, command rejected",
                                "level": "warn",
                                "ts": datetime.now().strftime("%H:%M:%S"),
                            }
                            app_state.append_terminal_log(entry)
                            await app_state.broadcast("terminal_output", entry)
                            continue

                        task = app_state.tasks.get(task_id)
                        if not task:
                            entry = {
                                "task_id": task_id,
                                "line": f"[SYSTEM] Task {task_id} not found, command rejected",
                                "level": "error",
                                "ts": datetime.now().strftime("%H:%M:%S"),
                            }
                            app_state.append_terminal_log(entry)
                            await app_state.broadcast("terminal_output", entry)
                            continue

                        if task.get("status") != "running":
                            entry = {
                                "task_id": task_id,
                                "line": f"[SYSTEM] Task {task_id} status={task.get('status')} (not running), command queued denied",
                                "level": "warn",
                                "ts": datetime.now().strftime("%H:%M:%S"),
                            }
                            app_state.append_terminal_log(entry)
                            await app_state.broadcast("terminal_output", entry)
                            continue

                        app_state.intervention_queues.setdefault(task_id, []).append(cmd)
                        entry = {"content": cmd, "timestamp": datetime.now().isoformat()}
                        task.setdefault("interventions", []).append(entry)
                        app_state.mark_dirty()
                        await app_state.broadcast("task_intervened", {
                            "task_id": task_id,
                            "instruction": cmd,
                            "timestamp": entry["timestamp"],
                            "echoed_to_terminal": True,
                        })

                        terminal_entry = {
                            "task_id": task_id,
                            "line": f"[USER] $ {cmd}",
                            "level": "input",
                            "ts": datetime.now().strftime("%H:%M:%S"),
                        }
                        app_state.append_terminal_log(terminal_entry)
                        await app_state.broadcast("terminal_output", terminal_entry)
                except json.JSONDecodeError:
                    pass
        except WebSocketDisconnect:
            if websocket in app_state.active_websockets:
                app_state.active_websockets.remove(websocket)


# 创建应用实例
app = create_app()
