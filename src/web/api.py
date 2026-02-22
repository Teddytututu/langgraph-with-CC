"""src/web/api.py — FastAPI 路由"""
from __future__ import annotations
import asyncio
import json
from datetime import datetime
from typing import Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from src.graph.state import GraphState, SubTask, TimeBudget
from src.graph.builder import build_graph
from src.graph.dynamic_builder import DynamicGraphBuilder
from src.discussion.manager import DiscussionManager, discussion_manager


# 全局状态
class AppState:
    def __init__(self):
        self.tasks: dict[str, dict] = {}
        self.graph_builder = DynamicGraphBuilder()
        self.discussion_manager = discussion_manager
        self.active_websockets: list[WebSocket] = []

    async def broadcast(self, event: str, data: dict):
        """广播事件到所有连接的 WebSocket"""
        message = json.dumps({"event": event, "data": data}, ensure_ascii=False)
        for ws in self.active_websockets[:]:  # 复制列表避免迭代时修改
            try:
                await ws.send_text(message)
            except:
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

    class TaskCreate(BaseModel):
        """创建任务请求"""
        task: str
        time_minutes: Optional[float] = None

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

    @app.post("/api/tasks/{task_id}/start")
    async def start_task(task_id: str):
        """启动任务执行"""
        if task_id not in app_state.tasks:
            raise HTTPException(status_code=404, detail="Task not found")

        task = app_state.tasks[task_id]
        task["status"] = "running"

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
                "time_budget": TimeBudget(total_minutes=task["time_minutes"]) if task["time_minutes"] else None,
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
                            }
                            for t in state_update.get("subtasks", [])
                        ],
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
                        await app_state.broadcast("task_completed", {
                            "id": task_id,
                            "result": state_update["final_output"],
                        })

        except Exception as e:
            task["status"] = "failed"
            task["error"] = str(e)
            await app_state.broadcast("task_failed", {
                "id": task_id,
                "error": str(e),
            })

    # ── Graph API ──

    @app.get("/api/graph")
    async def get_graph():
        """获取 Graph 结构"""
        return app_state.graph_builder.to_dict()

    @app.get("/api/graph/mermaid")
    async def get_graph_mermaid():
        """获取 Mermaid 图形语法"""
        return {"mermaid": app_state.graph_builder.to_mermaid()}

    # ── 讨论 API ──

    @app.get("/api/tasks/{task_id}/nodes/{node_id}/discussion")
    async def get_discussion(task_id: str, node_id: str):
        """获取节点讨论"""
        if task_id not in app_state.tasks:
            raise HTTPException(status_code=404, detail="Task not found")

        discussion = app_state.discussion_manager.get_discussion(node_id)
        if discussion:
            return discussion.to_dict()
        return {"node_id": node_id, "messages": [], "participants": []}

    class MessagePost(BaseModel):
        """发送消息请求"""
        from_agent: str
        content: str
        to_agents: list[str] = []
        message_type: str = "info"

    @app.post("/api/tasks/{task_id}/nodes/{node_id}/discussion")
    async def post_message(task_id: str, node_id: str, req: MessagePost):
        """发送讨论消息"""
        if task_id not in app_state.tasks:
            raise HTTPException(status_code=404, detail="Task not found")

        msg = await app_state.discussion_manager.post_message(
            node_id=node_id,
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
            app_state.active_websockets.remove(websocket)


# 创建应用实例
app = create_app()
