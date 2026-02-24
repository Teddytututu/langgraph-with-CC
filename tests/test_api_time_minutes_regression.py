import pytest
from fastapi.testclient import TestClient

import src.web.api as api_module


def _drop_background(coro):
    coro.close()
    return None


@pytest.mark.parametrize("payload", [{"task": "no minutes"}, {"task": "null minutes", "time_minutes": None}])
def test_create_and_start_task_without_time_minutes(monkeypatch, payload):
    monkeypatch.setenv("WEB_TASK_NORMALIZE_ENABLED", "0")
    monkeypatch.setattr(api_module, "_fire", _drop_background)

    api_module.app_state.tasks.clear()
    api_module.app_state.running_task_handles.clear()
    api_module.app_state.current_task_id = None
    api_module.app_state.current_node = ""
    api_module.app_state.system_status = "idle"

    client = TestClient(api_module.app)

    create_resp = client.post("/api/tasks", json=payload)
    assert create_resp.status_code == 200
    task_id = create_resp.json()["id"]

    task_resp = client.get(f"/api/tasks/{task_id}")
    assert task_resp.status_code == 200
    assert task_resp.json().get("time_minutes") is None

    start_resp = client.post(f"/api/tasks/{task_id}/start")
    assert start_resp.status_code == 200
    assert start_resp.json()["status"] == "running"
