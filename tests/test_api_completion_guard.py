import asyncio
from datetime import datetime

import pytest
from httpx import ASGITransport, AsyncClient

import src.web.api as api_module
from src.graph.state import SubTask


class _DummyGraph:
    async def astream(self, initial_state, config):
        yield {
            "router": {
                "phase": "executing",
                "final_output": "premature-output",
                "subtasks": [
                    SubTask(
                        id="task-001",
                        title="step 1",
                        description="running step",
                        agent_type="executor",
                        status="running",
                    )
                ],
                "execution_log": [],
            }
        }

        yield {
            "router": {
                "phase": "complete",
                "final_output": "final-output",
                "subtasks": [
                    SubTask(
                        id="task-001",
                        title="step 1",
                        description="done step",
                        agent_type="executor",
                        status="done",
                        result="ok",
                    )
                ],
                "execution_log": [],
            }
        }


@pytest.mark.asyncio
async def test_completion_guard_prevents_early_completed(monkeypatch):
    monkeypatch.setenv("WEB_TASK_NORMALIZE_ENABLED", "0")

    fired_tasks = []

    def _fire_capture(coro):
        t = asyncio.create_task(coro)
        fired_tasks.append(t)
        return t

    events = []

    async def _capture_broadcast(event, payload):
        events.append((event, payload))

    monkeypatch.setattr(api_module, "_fire", _fire_capture)
    monkeypatch.setattr(api_module.app_state.graph_builder, "compile", lambda: _DummyGraph())
    monkeypatch.setattr(api_module.app_state, "broadcast", _capture_broadcast)

    task_id = "guardtest"
    now = datetime.now().isoformat()
    api_module.app_state.tasks.clear()
    api_module.app_state.running_task_handles.clear()
    api_module.app_state.intervention_queues.clear()
    api_module.app_state.current_task_id = None
    api_module.app_state.current_node = ""
    api_module.app_state.system_status = "idle"
    api_module.app_state.terminal_log.clear()
    api_module.app_state.tasks[task_id] = {
        "id": task_id,
        "task": "guard completion test",
        "status": "created",
        "created_at": now,
        "updated_at": now,
        "subtasks": [],
        "discussions": {},
    }

    transport = ASGITransport(app=api_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(f"/api/tasks/{task_id}/start")
        assert resp.status_code == 200
        assert resp.json()["status"] == "running"

    assert fired_tasks
    await fired_tasks[0]

    task_completed_events = [payload for name, payload in events if name == "task_completed"]
    assert len(task_completed_events) == 1

    progress_events = [(name, payload) for name, payload in events if name == "task_progress"]
    assert any(p.get("phase") == "executing" for _, p in progress_events)
    assert any(p.get("phase") == "complete" for _, p in progress_events)

    warn_lines = [
        payload.get("line", "")
        for name, payload in events
        if name == "terminal_output"
    ]
    assert any("terminal guard not met" in line for line in warn_lines)

    completed_index = next(i for i, (name, _) in enumerate(events) if name == "task_completed")
    complete_progress_index = next(
        i for i, (name, payload) in enumerate(events)
        if name == "task_progress" and payload.get("phase") == "complete"
    )
    assert completed_index > complete_progress_index

@pytest.mark.asyncio
async def test_auto_queue_not_blocked_by_stale_running_without_handle(monkeypatch):
    monkeypatch.setenv("WEB_TASK_NORMALIZE_ENABLED", "0")

    fired_tasks = []

    def _fire_selective(coro):
        coro_name = getattr(getattr(coro, "cr_code", None), "co_name", "")
        if coro_name == "_start_next_queued_task":
            t = asyncio.create_task(coro)
            fired_tasks.append(t)
            return t
        coro.close()
        return None

    async def _noop_broadcast(event, payload):
        return None

    monkeypatch.setattr(api_module, "_fire", _fire_selective)
    monkeypatch.setattr(api_module.app_state, "broadcast", _noop_broadcast)

    stale_task_id = "stale-running"
    cancelled_task_id = "to-cancel"
    queued_task_id = "next-queued"
    now = datetime.now().isoformat()

    api_module.app_state.tasks.clear()
    api_module.app_state.running_task_handles.clear()
    api_module.app_state.intervention_queues.clear()
    api_module.app_state.current_task_id = stale_task_id
    api_module.app_state.current_node = "executor"
    api_module.app_state.system_status = "running"
    api_module.app_state.terminal_log.clear()

    api_module.app_state.tasks[stale_task_id] = {
        "id": stale_task_id,
        "task": "stale running task",
        "status": "running",
        "created_at": now,
        "updated_at": now,
        "subtasks": [],
        "discussions": {},
    }
    api_module.app_state.tasks[cancelled_task_id] = {
        "id": cancelled_task_id,
        "task": "cancel this task",
        "status": "running",
        "created_at": now,
        "updated_at": now,
        "subtasks": [],
        "discussions": {},
    }
    api_module.app_state.tasks[queued_task_id] = {
        "id": queued_task_id,
        "task": "queued follow-up",
        "status": "queued",
        "created_at": now,
        "updated_at": now,
        "subtasks": [],
        "discussions": {},
    }

    # 关键前提：stale-running 没有活动 handle（模拟“系统显示 running 但实际没在跑”）
    assert stale_task_id not in api_module.app_state.running_task_handles

    transport = ASGITransport(app=api_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.delete(f"/api/tasks/{cancelled_task_id}")
        assert resp.status_code == 200
        assert resp.json()["status"] == "cancelled"

    assert fired_tasks, "expected auto-queue coroutine to be scheduled"
    await fired_tasks[0]

    # 修复前：这里会错误地保持 queued；修复后：应被推进到 running
    assert api_module.app_state.tasks[queued_task_id]["status"] == "running"

