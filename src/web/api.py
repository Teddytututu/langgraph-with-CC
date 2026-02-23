"""src/web/api.py — FastAPI 路由"""
from __future__ import annotations
import asyncio
import json
import logging
import traceback

logger = logging.getLogger(__name__)

# 防止后台 Task 被 GC 回收 —— asyncio 不持有强引用
_background_tasks: set[asyncio.Task] = set()


def _fire(coro):
    """创建后台 Task 并保持强引用直到完成"""
    t = asyncio.create_task(coro)
    _background_tasks.add(t)
    t.add_done_callback(_background_tasks.discard)
    return t
from datetime import datetime
from typing import Optional
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from src.graph.state import GraphState, SubTask, TimeBudget
from src.graph.builder import build_graph
from src.graph.dynamic_builder import DynamicGraphBuilder
from src.discussion.manager import DiscussionManager, discussion_manager
from src.agents.sdk_executor import get_executor


# ── 请求体模型（必须在模块层定义，FastAPI 才能正确解析 body） ──

class TaskCreate(BaseModel):
    """创建任务请求"""
    task: str
    time_minutes: Optional[float] = None


class MessagePost(BaseModel):
    """发送消息请求"""
    from_agent: str
    content: str
    to_agents: list[str] = []
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
    history: list[dict] = []  # [{"role": "user"|"assistant", "content": str}]


# 持久化文件路径
_STATE_FILE = Path("app_state.json")


# 全局状态
class AppState:
    def __init__(self):
        self.tasks: dict[str, dict] = {}
        self.graph_builder = DynamicGraphBuilder()
        self.discussion_manager = discussion_manager
        self.active_websockets: list[WebSocket] = []
        self.system_status: str = "idle"
        self.current_node: str = ""
        self.current_task_id: str = ""
        self.intervention_queues: dict[str, list[str]] = {}
        self.terminal_log: list[dict] = []
        self._dirty: bool = False  # 标记是否有未保存的变更

    def append_terminal_log(self, entry: dict):
        self.terminal_log.append(entry)
        if len(self.terminal_log) > 500:
            self.terminal_log = self.terminal_log[-500:]
        self._dirty = True

    def mark_dirty(self):
        self._dirty = True

    def save_to_disk(self):
        """把核心状态序列化到磁盘"""
        try:
            data = {
                "tasks": self.tasks,
                "system_status": self.system_status,
                "current_node": self.current_node,
                "current_task_id": self.current_task_id,
                "terminal_log": self.terminal_log[-300:],
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
            # 重启后正在运行中的任务实际已停止，标为 failed
            for t in self.tasks.values():
                if t.get("status") == "running":
                    t["status"] = "failed"
                    t["error"] = "服务器重启，任务中断"
            if self.system_status == "running":
                self.system_status = "idle"
            self.current_node = ""
            self.current_task_id = data.get("current_task_id", "")
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
        allow_origins=["*"],
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

    # ── 页面路由 ──

    @app.get("/", response_class=HTMLResponse)
    async def index():
        """主页"""
        return FileResponse("src/web/static/index.html")

    # ── 任务 API ──

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

        task_data = {
            "id": task_id,
            "task": req.task,
            "time_minutes": req.time_minutes,
            "status": "created",
            "created_at": datetime.now().isoformat(),
            "subtasks": [],
            "discussions": {},
        }

        app_state.tasks[task_id] = task_data
        app_state.mark_dirty()

        await app_state.broadcast("task_created", task_data)

        # 自动启动：创建后立即触发执行（如有其他任务在跑则等待）
        async def _auto_start():
            # 等待系统空闲，最多 60 分钟
            waited = 0
            while app_state.system_status == "running" and waited < 3600:
                await asyncio.sleep(5)
                waited += 5
            task_data["status"] = "running"
            app_state.system_status = "running"
            app_state.current_task_id = task_id
            await app_state.broadcast("system_status_changed", {
                "status": "running",
                "task_id": task_id,
                "task": task_data["task"],
            })
            await app_state.broadcast("task_started", {"id": task_id})
            await run_task(task_id)

        _fire(_auto_start())

        return {"id": task_id, "status": "created"}

    @app.get("/api/tasks")
    async def list_tasks():
        """获取任务列表"""
        return {
            "tasks": list(app_state.tasks.values()),
            "count": len(app_state.tasks),
        }

    @app.delete("/api/tasks")
    async def clear_all_tasks():
        """清空所有任务及子任务，重置系统状态"""
        running = [t for t in app_state.tasks.values() if t.get("status") == "running"]
        if running:
            raise HTTPException(status_code=409, detail="任务正在运行，无法清空")
        app_state.tasks.clear()
        app_state.current_task_id = None
        app_state.current_node = ""
        app_state.system_status = "idle"
        app_state.terminal_log.clear()
        app_state.intervention_queues.clear()
        app_state.mark_dirty()
        await app_state.broadcast("tasks_cleared", {})
        return {"status": "cleared"}

    @app.get("/api/tasks/{task_id}")
    async def get_task(task_id: str):
        """获取任务详情"""
        if task_id not in app_state.tasks:
            raise HTTPException(status_code=404, detail="Task not found")
        return app_state.tasks[task_id]

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

        await app_state.broadcast("task_progress", {
            "task_id": task_id,
            "subtasks": subtasks,
        })
        return target

    @app.delete("/api/tasks/{task_id}")
    async def cancel_task(task_id: str):
        """取消/停止指定任务"""
        if task_id not in app_state.tasks:
            raise HTTPException(status_code=404, detail="Task not found")

        task = app_state.tasks[task_id]
        old_status = task.get("status")

        if old_status == "running":
            task["status"] = "cancelled"
            task["error"] = "用户取消"
            app_state.mark_dirty()

            # 如果这是当前任务，重置系统状态
            if app_state.current_task_id == task_id:
                app_state.system_status = "idle"
                app_state.current_node = ""

            await app_state.broadcast("task_cancelled", {
                "id": task_id,
                "previous_status": old_status,
            })

            # 检查是否还有其他运行中的任务
            still_running = [t for t in app_state.tasks.values() if t.get("status") == "running"]
            if not still_running:
                app_state.system_status = "idle"

        return {"status": "cancelled", "task_id": task_id}

    @app.post("/api/tasks/{task_id}/intervene")
    async def intervene_task(task_id: str, req: TaskIntervene):
        """向运行中的任务注入实时指令"""
        if task_id not in app_state.tasks:
            raise HTTPException(status_code=404, detail="Task not found")

        task = app_state.tasks[task_id]

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
        })

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
        if task_id not in app_state.tasks:
            raise HTTPException(status_code=404, detail="Task not found")

        task = app_state.tasks[task_id]
        task["status"] = "running"
        app_state.mark_dirty()

        # 更新系统状态
        app_state.system_status = "running"
        app_state.current_task_id = task_id
        await app_state.broadcast("system_status_changed", {
            "status": "running",
            "task_id": task_id,
            "task": task["task"],
        })

        # 在后台执行任务
        _fire(run_task(task_id))

        await app_state.broadcast("task_started", {"id": task_id})

        return {"status": "running"}

    async def run_task(task_id: str):
        """执行任务（后台）"""
        task = app_state.tasks[task_id]

        async def emit(line: str, level: str = "info"):
            """broadcast 一行终端输出并持久化到 terminal_log"""
            entry = {
                "task_id": task_id,
                "line": line,
                "level": level,
                "ts": datetime.now().strftime("%H:%M:%S"),
            }
            app_state.append_terminal_log(entry)
            await app_state.broadcast("terminal_output", entry)

        try:
            graph = app_state.graph_builder.compile()
            await emit(f"▶ 任务已启动: {task['task'][:60]}", "start")

            initial_state: GraphState = {
                "user_task": task["task"],
                "time_budget": TimeBudget(total_minutes=task["time_minutes"], started_at=datetime.now()) if task["time_minutes"] else None,
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
                for node_name, state_update in event.items():
                    # 更新当前节点
                    app_state.current_node = node_name
                    await app_state.broadcast("node_changed", {
                        "task_id": task_id,
                        "node": node_name,
                    })
                    await emit(f"\u25b6 [{node_name.upper()}] phase={state_update.get('phase','')}", "node")

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

                    # 广播状态更新
                    await app_state.broadcast("task_progress", {
                        "task_id": task_id,
                        "node": node_name,
                        "phase": state_update.get("phase", ""),
                        "subtasks": [
                            {
                                "id": t.id,
                                "title": t.title,
                                "status": t.status,
                                "agent_type": t.agent_type,
                            }
                            for t in state_update.get("subtasks", [])
                        ],
                        "result": state_update.get("final_output"),
                    })

                    # 子任务状态变化时推送名单
                    if "subtasks" in state_update:
                        task["subtasks"] = [
                            {
                                "id": t.id,
                                "title": t.title,
                                "description": t.description,
                                "agent_type": t.agent_type,
                                "status": t.status,
                                "result": t.result,
                            }
                            for t in state_update.get("subtasks", [])
                        ]
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
                            existing.status = getattr(node_disc, "status", "resolved")
                            existing.consensus_reached = getattr(node_disc, "consensus_reached", False)
                            existing.consensus_topic = getattr(node_disc, "consensus_topic", None)

                    if state_update.get("final_output"):
                        task["result"] = state_update["final_output"]
                        task["status"] = "completed"
                        app_state.system_status = "completed"
                        app_state.current_node = ""
                        app_state.mark_dirty()
                        await emit(f"✓ 任务完成", "success")
                        await app_state.broadcast("task_completed", {
                            "id": task_id,
                            "result": state_update["final_output"],
                            "subtasks": task["subtasks"],
                        })
                        await app_state.broadcast("system_status_changed", {
                            "status": "completed",
                            "task_id": task_id,
                        })

                # ── 节点间隙：消费干预队列，注入 GraphState messages ──
                pending = app_state.intervention_queues.pop(task_id, [])
                if pending:
                    injected = [f"[用户实时指令] {inst}" for inst in pending]
                    await graph.aupdate_state(
                        config,
                        {"messages": injected},
                    )
                    await app_state.broadcast("task_intervention_applied", {
                        "task_id": task_id,
                        "instructions": pending,
                    })

        except Exception as e:
            task["status"] = "failed"
            task["error"] = str(e)
            app_state.mark_dirty()
            await emit(f"✗ 崩溃: {str(e)[:200]}", "error")

            # 生成崩溃报告
            crash_report = {
                "task_id": task_id,
                "failed_node": app_state.current_node,
                "error_message": str(e),
                "traceback": traceback.format_exc(),
                "task": task["task"],
                "time": datetime.now().isoformat()
            }

            # 保存崩溃报告到 reports/ 目录（规范化）
            Path("reports").mkdir(exist_ok=True)
            crash_report_path = Path("reports/crash_report.json")
            with open(crash_report_path, "w", encoding="utf-8") as f:
                json.dump(crash_report, f, indent=2, ensure_ascii=False)

            app_state.system_status = "failed"
            app_state.current_node = ""
            await app_state.broadcast("task_failed", {
                "id": task_id,
                "error": str(e),
                "crash_report_saved": str(crash_report_path),
            })
            await app_state.broadcast("system_status_changed", {
                "status": "failed",
                "task_id": task_id,
                "error": str(e),
            })

    # ── Graph API ──

    @app.get("/api/graph")
    async def get_graph():
        """获取 Graph 结构"""
        return app_state.graph_builder.to_dict()

    def _build_task_mermaid() -> str:
        """根据当前任务的子任务动态生成 Mermaid 字符串。
        有子任务时显示子任务 DAG；无子任务时退回标准骨架图。"""
        # 优先用 current_task_id；若无子任务则找最近一个有子任务的任务
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

        # ── 动态子任务 DAG ────────────────────────────────────────────────────
        def safe_id(raw: str) -> str:
            """Mermaid 节点 ID 不能含连字符/空格，task-001 → task001"""
            return raw.replace("-", "").replace("_", "").replace(" ", "")

        status_icon = {"running": "⟳", "done": "✓", "failed": "✗",
                       "pending": "○", "skipped": "—"}
        status_fill = {
            "running": "fill:#4c1d95,color:#e9d5ff,stroke:#a855f7,stroke-width:2px",
            "done":    "fill:#14532d,color:#bbf7d0,stroke:#22c55e,stroke-width:2px",
            "failed":  "fill:#7f1d1d,color:#fca5a5,stroke:#ef4444,stroke-width:2px",
            "pending": "fill:#27272a,color:#a1a1aa,stroke:#52525b,stroke-width:1.5px",
            "skipped": "fill:#1f1f27,color:#71717a,stroke:#3f3f46,stroke-width:1px",
        }

        lines = ["graph LR"]
        deferred_styles = []

        # ── 顶部标题节点 ──────────────────────────────────────────────────────
        task_status = (task or {}).get("status", "")
        task_title = ((task or {}).get("task") or "任务")[:32].replace('"', "'")
        hid = "task_header"

        if task_status == "running":
            phase = app_state.current_node or "running"
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

        # ── 子任务节点 ────────────────────────────────────────────────────────
        for st in subtasks:
            sid = safe_id(st["id"])
            icon = status_icon.get(st.get("status", "pending"), "○")
            title = st["title"].replace('"', "'")[:24]
            lines.append(f'    {sid}(["{icon} {title}"])')
            fill = status_fill.get(st.get("status", "pending"), status_fill["pending"])
            deferred_styles.append(f"    style {sid} {fill}")

        # ── 边：header → 根节点，依赖节点间连线 ──────────────────────────────
        dep_ids = {dep for st in subtasks for dep in (st.get("dependencies") or [])}
        roots = [st for st in subtasks if st["id"] not in dep_ids]
        for st in roots:
            lines.append(f"    {hid} --> {safe_id(st['id'])}")

        for st in subtasks:
            for dep_id in (st.get("dependencies") or []):
                if any(s["id"] == dep_id for s in subtasks):
                    lines.append(f"    {safe_id(dep_id)} --> {safe_id(st['id'])}")

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
    async def get_system_status():
        """获取系统状态（含终端日志，供刷新后恢复）"""
        return {
            "status": app_state.system_status,
            "current_node": app_state.current_node,
            "current_task_id": app_state.current_task_id,
            "tasks_count": len(app_state.tasks),
            "running_tasks": len([t for t in app_state.tasks.values() if t["status"] == "running"]),
            "terminal_log": app_state.terminal_log[-300:],
        }

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

        msg = await app_state.discussion_manager.post_message(
            node_id=f"{task_id}_{node_id}",
            from_agent=req.from_agent,
            content=req.content,
            to_agents=req.to_agents,
            message_type=req.message_type,
        )

        await app_state.broadcast("discussion_message", {
            "task_id": task_id,
            "node_id": node_id,
            "message": msg.to_dict(),
        })

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
        import os
        reports_dir = Path("reports")
        if not reports_dir.exists():
            return {"files": []}
        files = []
        for f in sorted(reports_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
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

    @app.get("/api/reports/{filename}")
    async def get_report(filename: str):
        """读取 reports/ 下指定文件内容"""
        # 防目录穿越
        if "/" in filename or ".." in filename:
            raise HTTPException(status_code=400, detail="Invalid filename")
        path = Path("reports") / filename
        if not path.exists() or not path.is_file():
            raise HTTPException(status_code=404, detail="File not found")
        content = path.read_text(encoding="utf-8", errors="replace")
        return {"name": filename, "content": content}

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
                        if task_id and cmd and task_id in app_state.tasks:
                            app_state.intervention_queues.setdefault(task_id, []).append(cmd)
                            entry = {"content": cmd, "timestamp": datetime.now().isoformat()}
                            app_state.tasks[task_id].setdefault("interventions", []).append(entry)
                            await app_state.broadcast("task_intervened", {
                                "task_id": task_id,
                                "instruction": cmd,
                                "timestamp": entry["timestamp"],
                            })
                            await app_state.broadcast("terminal_output", {
                                "task_id": task_id,
                                "line": f"[USER] $ {cmd}",
                                "level": "input",
                                "ts": datetime.now().strftime("%H:%M:%S"),
                            })
                except json.JSONDecodeError:
                    pass
        except WebSocketDisconnect:
            if websocket in app_state.active_websockets:
                app_state.active_websockets.remove(websocket)


# 创建应用实例
app = create_app()
