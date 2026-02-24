import pytest
from datetime import datetime

from src.graph.state import ExecutionPolicy, SubTask
from src.graph.nodes.executor import executor_node


class _DummyCaller:
    async def get_or_create_specialist(self, skills, task_description):
        return "agent_stub"

    async def call_specialist(self, agent_id, subtask, previous_results, time_budget):
        return {"success": True, "result": "ok"}

    def complete_subtask(self, specialist_id):
        return None


def _make_running_state(*, min_rounds=3, min_agents=3):
    task = SubTask(
        id="task-001",
        title="Strict fallback test task",
        description="Validate strict fallback behavior",
        agent_type="coder",
        knowledge_domains=["backend", "testing", "architecture"],
        completion_criteria=["done"],
        status="running",
        started_at=datetime.now(),
        assigned_agents=["a1", "a2", "a3"],
        estimated_minutes=5,
    )
    policy = ExecutionPolicy(
        force_complex_graph=True,
        min_agents_per_node=min_agents,
        min_discussion_rounds=min_rounds,
        strict_enforcement=True,
    )
    return {
        "subtasks": [task],
        "current_subtask_id": task.id,
        "execution_policy": policy,
        "artifacts": {},
    }


@pytest.mark.asyncio
async def test_executor_strict_partial_rounds_timeout_fallback_done(monkeypatch):
    async def _stub_discussion(*args, **kwargs):
        return (
            {
                "success": False,
                "error": "discussion_total_timeout>200s",
                "result": None,
                "specialist_id": "agent_synth",
                "assigned_agents": ["a1", "a2", "a3"],
                "actual_agents_used": 3,
                "actual_discussion_rounds": 1,
            },
            [],
        )

    monkeypatch.setattr("src.graph.nodes.executor.get_caller", lambda: _DummyCaller())
    monkeypatch.setattr("src.graph.nodes.executor._execute_multi_agent_discussion", _stub_discussion)

    out = await executor_node(_make_running_state(min_rounds=3, min_agents=3))

    assert out["subtasks"][0].status == "done"
    log = out["execution_log"][0]
    assert log["event"] == "task_executed"
    assert log["fallback_applied"] is True
    assert log["fallback_reason"] == "strict_partial_rounds_timeout"
    assert "discussion_total_timeout" in (log["original_error"] or "")


@pytest.mark.asyncio
async def test_executor_strict_insufficient_agents_policy_violation_failed(monkeypatch):
    async def _stub_discussion(*args, **kwargs):
        return (
            {
                "success": False,
                "error": "[POLICY_VIOLATION] available_agents<3",
                "result": None,
                "specialist_id": None,
                "assigned_agents": ["a1", "a2"],
                "actual_agents_used": 2,
                "actual_discussion_rounds": 1,
            },
            [],
        )

    monkeypatch.setattr("src.graph.nodes.executor.get_caller", lambda: _DummyCaller())
    monkeypatch.setattr("src.graph.nodes.executor._execute_multi_agent_discussion", _stub_discussion)

    out = await executor_node(_make_running_state(min_rounds=3, min_agents=3))

    assert out["subtasks"][0].status == "failed"
    log = out["execution_log"][0]
    assert log["event"] == "task_failed"
    assert log["fallback_applied"] is False
    assert "POLICY_VIOLATION" in (log["error"] or "")


@pytest.mark.asyncio
async def test_executor_strict_zero_rounds_failed(monkeypatch):
    async def _stub_discussion(*args, **kwargs):
        return (
            {
                "success": False,
                "error": "discussion_total_timeout>200s",
                "result": None,
                "specialist_id": "agent_synth",
                "assigned_agents": ["a1", "a2", "a3"],
                "actual_agents_used": 3,
                "actual_discussion_rounds": 0,
            },
            [],
        )

    monkeypatch.setattr("src.graph.nodes.executor.get_caller", lambda: _DummyCaller())
    monkeypatch.setattr("src.graph.nodes.executor._execute_multi_agent_discussion", _stub_discussion)

    out = await executor_node(_make_running_state(min_rounds=3, min_agents=3))

    assert out["subtasks"][0].status == "failed"
    log = out["execution_log"][0]
    assert log["event"] == "task_failed"
    assert log["actual_rounds"] == 0


@pytest.mark.asyncio
async def test_executor_strict_synthesis_empty_with_valid_rounds_fallback_done(monkeypatch):
    async def _stub_discussion(*args, **kwargs):
        return (
            {
                "success": False,
                "error": "synthesis_empty",
                "result": None,
                "specialist_id": "agent_synth",
                "assigned_agents": ["a1", "a2", "a3"],
                "actual_agents_used": 3,
                "actual_discussion_rounds": 3,
            },
            [],
        )

    monkeypatch.setattr("src.graph.nodes.executor.get_caller", lambda: _DummyCaller())
    monkeypatch.setattr("src.graph.nodes.executor._execute_multi_agent_discussion", _stub_discussion)

    out = await executor_node(_make_running_state(min_rounds=3, min_agents=3))

    assert out["subtasks"][0].status == "done"
    log = out["execution_log"][0]
    assert log["event"] == "task_executed"
    assert log["fallback_applied"] is True
    assert log["fallback_reason"] == "strict_synthesis_empty_or_timeout"
    assert log["actual_rounds"] == 3
