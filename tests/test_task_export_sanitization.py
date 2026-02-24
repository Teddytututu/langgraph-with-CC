import asyncio
import json
from datetime import datetime

import pytest
from httpx import ASGITransport, AsyncClient

import src.web.api as api_module
from src.graph.state import SubTask


class _NoisyExportGraph:
    async def astream(self, initial_state, config):
        noisy_output = "\n".join([
            "## Round 1",
            "[executor failed: specialist_call_timeout] retrying",
            "## Round 2",
            "[executor failed: specialist_call_timeout] retrying again",
        ])
        yield {
            "router": {
                "phase": "complete",
                "final_output": noisy_output,
                "subtasks": [
                    SubTask(
                        id="s1",
                        title="step noisy",
                        description="desc",
                        agent_type="executor",
                        status="failed",
                        result="[executor failed: specialist_call_timeout] degraded path",
                    )
                ],
                "execution_log": [],
            }
        }


class _CleanExportGraph:
    async def astream(self, initial_state, config):
        yield {
            "router": {
                "phase": "complete",
                "final_output": "All done.\n- fixed issue\n- verified behavior",
                "subtasks": [
                    SubTask(
                        id="s1",
                        title="step clean",
                        description="desc",
                        agent_type="executor",
                        status="done",
                        result="Implemented and verified.",
                    )
                ],
                "execution_log": [],
            }
        }


async def _await_all_fired(fired_tasks):
    cursor = 0
    while True:
        pending = [t for t in fired_tasks[cursor:] if not t.done()]
        cursor = len(fired_tasks)
        if not pending:
            break
        await asyncio.gather(*pending)


async def _run_and_collect_export(monkeypatch, tmp_path, graph, task_id: str):
    monkeypatch.setenv("WEB_TASK_NORMALIZE_ENABLED", "0")

    fired_tasks = []

    def _fire_capture(coro):
        t = asyncio.create_task(coro)
        fired_tasks.append(t)
        return t

    async def _noop_broadcast(event, payload):
        return None

    monkeypatch.setattr(api_module, "_fire", _fire_capture)
    monkeypatch.setattr(api_module.app_state.graph_builder, "compile", lambda: graph)
    monkeypatch.setattr(api_module.app_state, "broadcast", _noop_broadcast)
    monkeypatch.setattr(api_module.init_project, "run_full_init", lambda dry=False: {"status": "ok"})

    export_dir = tmp_path / "exports" / "tasks"
    monkeypatch.setattr(api_module, "_EXPORTS_DIR", export_dir)

    now = datetime.now().isoformat()
    api_module.app_state.tasks.clear()
    api_module.app_state.running_task_handles.clear()
    api_module.app_state.intervention_queues.clear()
    api_module.app_state.post_init_done_task_ids.clear()
    api_module.app_state.current_task_id = None
    api_module.app_state.current_node = ""
    api_module.app_state.system_status = "idle"
    api_module.app_state.terminal_log.clear()
    api_module.app_state.tasks[task_id] = {
        "id": task_id,
        "task": "export sanitization test",
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

    await _await_all_fired(fired_tasks)

    json_path = export_dir / f"{task_id}.json"
    md_path = export_dir / f"{task_id}.md"
    assert json_path.exists()
    assert md_path.exists()

    payload = json.loads(json_path.read_text(encoding="utf-8"))
    md_text = md_path.read_text(encoding="utf-8")
    return payload, md_text


@pytest.mark.asyncio
async def test_export_sanitizes_noisy_transcript_result(monkeypatch, tmp_path):
    payload, md_text = await _run_and_collect_export(
        monkeypatch,
        tmp_path,
        _NoisyExportGraph(),
        task_id="export-noisy",
    )

    assert payload["outcome_summary"]["result_sanitized"] is True
    assert payload["outcome_summary"]["result_source"] == "summary_fallback"
    assert "downgraded to a structured summary" in payload["result"]
    assert "## Round" not in payload["result"]

    assert payload["subtasks"][0]["has_timeout_marker"] is True
    assert payload["subtasks"][0]["has_degraded_marker"] is True
    assert payload["subtasks"][0]["marker_hits"]

    assert "## Outcome Summary" in md_text
    assert "## Offending Evidence (Top N)" in md_text
    assert "specialist_call_timeout" in md_text


@pytest.mark.asyncio
async def test_export_keeps_clean_result_with_summary_sections(monkeypatch, tmp_path):
    payload, md_text = await _run_and_collect_export(
        monkeypatch,
        tmp_path,
        _CleanExportGraph(),
        task_id="export-clean",
    )

    assert payload["outcome_summary"]["result_sanitized"] is False
    assert payload["outcome_summary"]["result_source"] == "result"
    assert payload["result"].startswith("All done.")
    assert "downgraded to a structured summary" not in payload["result"]

    assert "## Result" in md_text
    assert "## Outcome Summary" in md_text
    assert "## Offending Evidence (Top N)" in md_text
