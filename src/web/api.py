"""src/web/api.py — FastAPI 路由"""
from __future__ import annotations
import asyncio
import json
import traceback
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


# 全局状态
class AppState:
    def __init__(self):
        self.tasks: dict[str, dict] = {}
        self.graph_builder = DynamicGraphBuilder()
        self.discussion_manager = discussion_manager
        self.active_websockets: list[WebSocket] = []
        self.system_status: str = "idle"  # idle, running, completed, failed
        self.current_node: str = ""  # 当前执行的节点
        self.current_task_id: str = ""  # 当前执行的任务 ID
        # 实时干预：task_id -> 待注入指令列表
        self.intervention_queues: dict[str, list[str]] = {}

    async def broadcast(self, event: str, data: dict):
        """广播事件到所有连接的 WebSocket"""
        message = json.dumps({"event": event, "data": data}, ensure_ascii=False)
        for ws in self.active_websockets[:]:  # 复制列表避免迭代时修改
            try:
                await ws.send_text(message)
            except:
                if ws in self.active_websockets:
                    self.active_websockets.remove(ws)


app_state = AppState()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    # 启动时初始化标准工作流
    app_state.graph_builder.create_standard_workflow()
    yield
    # 关闭时清理
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
        task_id = str(uuid.uuid4())[:8]

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

        await app_state.broadcast("task_created", task_data)

        return {"id": task_id, "status": "created"}

    @app.get("/api/tasks")
    async def list_tasks():
        """获取任务列表"""
        return {
            "tasks": list(app_state.tasks.values()),
            "count": len(app_state.tasks),
        }

    @app.get("/api/tasks/{task_id}")
    async def get_task(task_id: str):
        """获取任务详情"""
        if task_id not in app_state.tasks:
            raise HTTPException(status_code=404, detail="Task not found")
        return app_state.tasks[task_id]

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

    @app.post("/api/tasks/{task_id}/start")
    async def start_task(task_id: str):
        """启动任务执行"""
        if task_id not in app_state.tasks:
            raise HTTPException(status_code=404, detail="Task not found")

        task = app_state.tasks[task_id]
        task["status"] = "running"

        # 更新系统状态
        app_state.system_status = "running"
        app_state.current_task_id = task_id
        await app_state.broadcast("system_status_changed", {
            "status": "running",
            "task_id": task_id,
            "task": task["task"],
        })

        # 在后台执行任务
        asyncio.create_task(run_task(task_id))

        await app_state.broadcast("task_started", {"id": task_id})

        return {"status": "running"}

    async def run_task(task_id: str):
        """执行任务（后台）"""
        task = app_state.tasks[task_id]

        try:
            graph = app_state.graph_builder.compile()

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

                    # 更新任务数据
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

                    if state_update.get("final_output"):
                        task["result"] = state_update["final_output"]
                        task["status"] = "completed"
                        app_state.system_status = "completed"
                        app_state.current_node = ""
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

            # 生成崩溃报告
            crash_report = {
                "task_id": task_id,
                "failed_node": app_state.current_node,
                "error_message": str(e),
                "traceback": traceback.format_exc(),
                "task": task["task"],
                "time": datetime.now().isoformat()
            }

            # 保存崩溃报告到文件
            crash_report_path = Path("crash_report.json")
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

    @app.get("/api/graph/mermaid")
    async def get_graph_mermaid(current_node: str = ""):
        """获取 Mermaid 图形语法，支持高亮当前节点"""
        mermaid_code = app_state.graph_builder.to_mermaid()

        # 如果有当前执行节点，添加高亮样式
        if current_node or app_state.current_node:
            node = current_node or app_state.current_node
            # 在 mermaid 代码中添加高亮样式
            highlight_line = f"    style {node} stroke:#ff0000,stroke-width:4px"
            mermaid_code = mermaid_code + "\n" + highlight_line

        return {"mermaid": mermaid_code, "current_node": app_state.current_node}

    # ── 系统状态 API ──

    @app.get("/api/system/status")
    async def get_system_status():
        """获取系统状态"""
        return {
            "status": app_state.system_status,
            "current_node": app_state.current_node,
            "current_task_id": app_state.current_task_id,
            "tasks_count": len(app_state.tasks),
            "running_tasks": len([t for t in app_state.tasks.values() if t["status"] == "running"]),
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

    # ── WebSocket ──

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        """WebSocket 实时通信"""
        await websocket.accept()
        app_state.active_websockets.append(websocket)

        try:
            while True:
                data = await websocket.receive_text()
                # 处理客户端消息（如需要）
                try:
                    message = json.loads(data)
                    # 可以在这里处理客户端发来的命令
                except json.JSONDecodeError:
                    pass
        except WebSocketDisconnect:
            if websocket in app_state.active_websockets:
                app_state.active_websockets.remove(websocket)


# 创建应用实例
app = create_app()
