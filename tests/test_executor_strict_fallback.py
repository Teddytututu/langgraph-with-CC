import pytest
from datetime import datetime

from src.graph.state import ExecutionPolicy, SubTask
from src.graph.nodes.executor import executor_node, _execute_multi_agent_discussion


class _DummyCaller:
    async def get_or_create_specialist(self, skills, task_description):
        return "agent_stub"

    async def call_specialist(self, agent_id, subtask, previous_results, time_budget):
        return {"success": True, "result": "ok"}

    def complete_subtask(self, specialist_id):
        return None


class _DummyDiscussionCaller:
    def __init__(self, responses_by_agent):
        self._responses_by_agent = responses_by_agent
        self._create_idx = 0

    async def get_or_create_specialist(self, skills, task_description):
        sid = f"agent_{self._create_idx}"
        self._create_idx += 1
        return sid

    async def call_specialist(self, agent_id, subtask, previous_results, time_budget):
        queue = self._responses_by_agent.get(agent_id, [])
        if queue:
            return queue.pop(0)
        return {"success": True, "result": f"default-{agent_id}"}


def _make_running_state(*, min_rounds=10, min_agents=3):
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
async def test_executor_strict_partial_rounds_timeout_fail_closed(monkeypatch):
    async def _stub_discussion(*args, **kwargs):
        return (
            {
                "success": False,
                "error": "discussion_total_timeout>200s",
                "result": None,
                "specialist_id": "agent_synth",
                "assigned_agents": ["a1", "a2", "a3"],
                "actual_agents_used": 3,
                "actual_discussion_rounds": 5,
                "policy_violation": {
                    "violation_type": "rounds_insufficient",
                },
            },
            [],
        )

    monkeypatch.setattr("src.graph.nodes.executor.get_caller", lambda: _DummyCaller())
    monkeypatch.setattr("src.graph.nodes.executor._execute_multi_agent_discussion", _stub_discussion)

    out = await executor_node(_make_running_state(min_rounds=10, min_agents=3))

    assert out["subtasks"][0].status == "failed"
    log = out["execution_log"][0]
    assert log["event"] == "task_failed"
    assert log["fallback_applied"] is False
    assert log["violation_type"] == "rounds_insufficient"
    assert log["required_rounds"] == 10
    assert log["actual_rounds"] == 5
    assert log["required_agents"] == 3
    assert log["actual_agents_used"] == 3
    assert "POLICY_VIOLATION" in (log["error"] or "")


@pytest.mark.asyncio
async def test_executor_strict_partial_rounds_timeout_deferred_without_policy_tag(monkeypatch):
    async def _stub_discussion(*args, **kwargs):
        return (
            {
                "success": False,
                "error": "[POLICY_VIOLATION] violation_type=rounds_insufficient actual_rounds=10 required_rounds=10 actual_agents=3 required_agents=3 detail=post_discussion_policy_check_failed",
                "result": None,
                "specialist_id": "agent_synth",
                "assigned_agents": ["a1", "a2", "a3"],
                "actual_agents_used": 3,
                "actual_discussion_rounds": 10,
                "original_error": "discussion_total_timeout>200s",
                "policy_violation": {
                    "violation_type": "rounds_insufficient",
                    "actual_rounds": 10,
                    "required_rounds": 10,
                    "actual_agents": 3,
                    "required_agents": 3,
                },
            },
            [],
        )

    monkeypatch.setattr("src.graph.nodes.executor.get_caller", lambda: _DummyCaller())
    monkeypatch.setattr("src.graph.nodes.executor._execute_multi_agent_discussion", _stub_discussion)

    out = await executor_node(_make_running_state(min_rounds=10, min_agents=3))

    assert out["subtasks"][0].status == "pending"
    assert out["phase"] == "executing"
    log = out["execution_log"][0]
    assert log["event"] == "task_deferred"
    assert log["terminal"] is False
    assert log["error"] == "discussion_total_timeout>200s"
    assert "POLICY_VIOLATION" not in (log["error"] or "")


@pytest.mark.asyncio
async def test_executor_strict_insufficient_agents_policy_violation_failed(monkeypatch):
    async def _stub_discussion(*args, **kwargs):
        return (
            {
                "success": False,
                "error": "[POLICY_VIOLATION] violation_type=agents_insufficient actual_rounds=1 required_rounds=10 actual_agents=2 required_agents=3",
                "result": None,
                "specialist_id": None,
                "assigned_agents": ["a1", "a2"],
                "actual_agents_used": 2,
                "actual_discussion_rounds": 1,
                "policy_violation": {
                    "violation_type": "agents_insufficient",
                    "actual_agents": 2,
                    "required_agents": 3,
                },
            },
            [],
        )

    monkeypatch.setattr("src.graph.nodes.executor.get_caller", lambda: _DummyCaller())
    monkeypatch.setattr("src.graph.nodes.executor._execute_multi_agent_discussion", _stub_discussion)

    out = await executor_node(_make_running_state(min_rounds=10, min_agents=3))

    assert out["subtasks"][0].status == "failed"
    log = out["execution_log"][0]
    assert log["event"] == "task_failed"
    assert log["fallback_applied"] is False
    assert log["violation_type"] == "agents_insufficient"
    assert "POLICY_VIOLATION" in (log["error"] or "")
    assert "actual_agents=2<3" in (log["error"] or "")
    assert "actual_rounds=1<10" not in (log["error"] or "")


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

    out = await executor_node(_make_running_state(min_rounds=10, min_agents=3))

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
                "actual_discussion_rounds": 10,
            },
            [],
        )

    monkeypatch.setattr("src.graph.nodes.executor.get_caller", lambda: _DummyCaller())
    monkeypatch.setattr("src.graph.nodes.executor._execute_multi_agent_discussion", _stub_discussion)

    out = await executor_node(_make_running_state(min_rounds=10, min_agents=3))

    assert out["subtasks"][0].status == "done"
    log = out["execution_log"][0]
    assert log["event"] == "task_executed"
    assert log["fallback_applied"] is True
    assert log["fallback_reason"] == "strict_synthesis_empty_or_timeout"
    assert log["actual_rounds"] == 10


@pytest.mark.asyncio
async def test_executor_strict_shortfall_detail_only_reports_unmet_dims(monkeypatch):
    async def _stub_discussion(*args, **kwargs):
        return (
            {
                "success": False,
                "error": "discussion_total_timeout>200s",
                "result": None,
                "specialist_id": "agent_synth",
                "assigned_agents": ["a1", "a2", "a3"],
                "actual_agents_used": 3,
                "actual_discussion_rounds": 4,
            },
            [],
        )

    monkeypatch.setattr("src.graph.nodes.executor.get_caller", lambda: _DummyCaller())
    monkeypatch.setattr("src.graph.nodes.executor._execute_multi_agent_discussion", _stub_discussion)

    out = await executor_node(_make_running_state(min_rounds=10, min_agents=3))

    assert out["subtasks"][0].status == "failed"
    log = out["execution_log"][0]
    assert log["event"] == "task_failed"
    assert log["violation_type"] == "rounds_insufficient"
    assert "actual_rounds=4<10" in (log["error"] or "")
    assert "actual_agents=3<3" not in (log["error"] or "")


@pytest.mark.asyncio
async def test_executor_strict_round_level_agent_shortage_does_not_fail_immediately():
    task = SubTask(
        id="task-raw-discussion",
        title="strict discussion continues after per-round shortage",
        description="validate strict round continuation",
        agent_type="coder",
        knowledge_domains=["backend", "testing", "architecture"],
        completion_criteria=["done"],
        status="running",
        started_at=datetime.now(),
        estimated_minutes=5,
    )

    # Round1: all succeed; Round2: only one succeeds even after retry/replacement; Round3: all succeed.
    responses = {
        "agent_0": [
            {"success": True, "result": "r1-a0"},
            {"success": True, "result": "r2-a0"},
            {"success": True, "result": "r3-a0"},
            {"success": True, "result": "synth"},
        ],
        "agent_1": [
            {"success": True, "result": "r1-a1"},
            {"success": False, "error": "r2-a1-timeout", "result": None},
            {"success": False, "error": "r2-a1-retry-timeout", "result": None},
            {"success": True, "result": "r3-a1"},
        ],
        "agent_2": [
            {"success": True, "result": "r1-a2"},
            {"success": False, "error": "r2-a2-timeout", "result": None},
            {"success": False, "error": "r2-a2-retry-timeout", "result": None},
            {"success": True, "result": "r3-a2"},
        ],
        "agent_3": [{"success": False, "error": "replacement-fail", "result": None}],
        "agent_4": [{"success": False, "error": "replacement-fail", "result": None}],
    }

    caller = _DummyDiscussionCaller(responses)
    result, discussion_log = await _execute_multi_agent_discussion(
        caller=caller,
        task=task,
        domains=["backend", "testing", "architecture"],
        previous_results=[],
        budget_ctx=None,
        min_rounds=3,
        min_agents=3,
        strict=True,
        deadline=None,
        discussion_timeout_sec=600.0,
    )

    assert result["success"] is True
    assert result["actual_discussion_rounds"] == 3
    assert result["actual_agents_used"] >= 3
    assert any(
        entry.get("type") == "policy_warning" and "round_agents_below_min_agents" in entry.get("content", "")
        for entry in discussion_log
    )


@pytest.mark.asyncio
async def test_executor_strict_policy_requires_min_rounds_10():
    with pytest.raises(ValueError, match="strict_enforcement=true requires min_discussion_rounds>=10"):
        ExecutionPolicy(
            force_complex_graph=True,
            min_agents_per_node=3,
            min_discussion_rounds=9,
            strict_enforcement=True,
        )

    policy = ExecutionPolicy(
        force_complex_graph=True,
        min_agents_per_node=3,
        min_discussion_rounds=10,
        strict_enforcement=True,
    )
    assert policy.min_discussion_rounds == 10

    from src.graph.nodes.executor import _compute_discussion_timeout_sec

    timeout = _compute_discussion_timeout_sec(
        estimated_minutes=0,
        required_rounds=10,
        required_agents=3,
        strict=True,
    )

    assert timeout >= 600.0


