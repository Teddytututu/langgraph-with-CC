import pytest
import asyncio
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

    assert "discussions" in out
    assert "task-001" in out["discussions"]
    discussion = out["discussions"]["task-001"]
    assert discussion.node_id == "task-001"
    assert discussion.consensus_reached is False
    assert discussion.status == "active"
    assert len(discussion.messages) >= 1

    assert out["subtasks"][0].status == "done"
    log = out["execution_log"][0]
    assert log["event"] == "task_degraded_continued"
    assert log["fallback_applied"] is False
    assert log["violation_type"] == "rounds_insufficient"
    assert log["required_rounds"] == 10
    assert log["actual_rounds"] == 5
    assert log["required_agents"] == 3
    assert log["actual_agents_used"] == 3


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

    assert out["subtasks"][0].status == "failed"
    assert out["phase"] == "executing"
    assert "discussions" in out
    assert "task-001" in out["discussions"]
    deferred_discussion = out["discussions"]["task-001"]
    assert deferred_discussion.node_id == "task-001"
    assert deferred_discussion.status == "active"
    assert deferred_discussion.consensus_reached is False
    log = out["execution_log"][0]
    assert log["event"] == "task_degraded_continued"
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
    assert "discussions" in out
    assert "task-001" in out["discussions"]
    failed_discussion = out["discussions"]["task-001"]
    assert failed_discussion.node_id == "task-001"
    assert failed_discussion.status == "active"
    assert failed_discussion.consensus_reached is False
    log = out["execution_log"][0]
    assert log["event"] == "task_degraded_continued"
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
    assert log["event"] == "task_degraded_continued"
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
    assert log["event"] == "task_degraded_continued"
    assert log["violation_type"] == "rounds_insufficient"
    assert "actual_rounds=4<10" in (log["error"] or "")
    assert "actual_agents=3<3" not in (log["error"] or "")


@pytest.mark.asyncio
async def test_executor_strict_failure_allows_followup_task_start(monkeypatch):
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
                "policy_violation": {
                    "violation_type": "rounds_insufficient",
                },
            },
            [],
        )

    monkeypatch.setattr("src.graph.nodes.executor.get_caller", lambda: _DummyCaller())
    monkeypatch.setattr("src.graph.nodes.executor._execute_multi_agent_discussion", _stub_discussion)

    task1 = SubTask(
        id="task-001",
        title="strict terminal failure",
        description="should fail under strict policy",
        agent_type="coder",
        knowledge_domains=["backend", "testing", "architecture"],
        completion_criteria=["done"],
        status="running",
        started_at=datetime.now(),
        estimated_minutes=5,
        priority=1,
    )
    task2 = SubTask(
        id="task-002",
        title="follow-up task",
        description="should still be scheduled",
        agent_type="coder",
        knowledge_domains=["backend", "testing", "architecture"],
        completion_criteria=["done"],
        status="pending",
        estimated_minutes=5,
        priority=2,
    )
    policy = ExecutionPolicy(
        force_complex_graph=True,
        min_agents_per_node=3,
        min_discussion_rounds=10,
        strict_enforcement=True,
    )

    first = await executor_node({
        "subtasks": [task1, task2],
        "current_subtask_id": task1.id,
        "execution_policy": policy,
        "artifacts": {},
    })

    assert first["phase"] == "executing"
    assert first["execution_log"][0]["event"] == "task_degraded_continued"
    assert next(t for t in first["subtasks"] if t.id == "task-001").status == "done"
    assert next(t for t in first["subtasks"] if t.id == "task-002").status == "pending"

    second = await executor_node({
        "subtasks": first["subtasks"],
        "current_subtask_id": None,
        "execution_policy": policy,
        "artifacts": first.get("artifacts", {}),
        "phase": "executing",
    })

    assert second["phase"] == "executing"
    assert second["current_subtask_id"] == "task-002"
    assert next(t for t in second["subtasks"] if t.id == "task-002").status == "running"


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
async def test_executor_strict_timeout_bounded_retry_becomes_terminal(monkeypatch):
    async def _stub_discussion(*args, **kwargs):
        return (
            {
                "success": False,
                "error": "discussion_total_timeout>200s",
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

    state = _make_running_state(min_rounds=10, min_agents=3)
    state["max_iterations"] = 3
    state["subtasks"][0] = state["subtasks"][0].model_copy(update={"retry_count": 1})

    out = await executor_node(state)

    assert out["subtasks"][0].status == "failed"
    log = out["execution_log"][0]
    assert log["event"] == "task_degraded_continued"
    assert log["terminal"] is False
    assert log["terminal_reason"] == "strict_execution_failed_nonterminal"


@pytest.mark.asyncio
async def test_executor_strict_all_specialists_timeout_marks_failed(monkeypatch):
    async def _stub_discussion(*args, **kwargs):
        return (
            {
                "success": False,
                "error": "specialist_call_failed",
                "result": "## Round 1\n\n[api_design|agent_29]\n[api_design failed: specialist_call_failed]",
                "specialist_id": "agent_29",
                "assigned_agents": ["agent_29", "agent_17", "agent_19", "agent_22"],
                "actual_agents_used": 0,
                "actual_discussion_rounds": 1,
            },
            [],
        )

    monkeypatch.setattr("src.graph.nodes.executor.get_caller", lambda: _DummyCaller())
    monkeypatch.setattr("src.graph.nodes.executor._execute_multi_agent_discussion", _stub_discussion)

    out = await executor_node(_make_running_state(min_rounds=10, min_agents=3))

    assert out["subtasks"][0].status == "done"
    log = out["execution_log"][0]
    assert log["event"] == "task_degraded_continued"
    assert log["terminal"] is False
    assert log["failure_stage"] == "unknown"
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



@pytest.mark.asyncio
async def test_discussion_no_per_call_timeout_uses_outer_budget_only():
    task = SubTask(
        id="task-no-per-call-timeout",
        title="disable per-call timeout",
        description="ensure specialist_call_timeout is not generated by per-call guard",
        agent_type="coder",
        knowledge_domains=["backend", "testing", "architecture"],
        completion_criteria=["done"],
        status="running",
        started_at=datetime.now(),
        estimated_minutes=5,
    )

    class _SlowCaller:
        def __init__(self):
            self._create_idx = 0

        async def get_or_create_specialist(self, skills, task_description):
            sid = f"agent_{self._create_idx}"
            self._create_idx += 1
            return sid

        async def call_specialist(self, agent_id, subtask, previous_results, time_budget):
            await asyncio.sleep(0.07)
            return {"success": True, "result": f"ok-{agent_id}"}

    caller = _SlowCaller()

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(
            _execute_multi_agent_discussion(
                caller=caller,
                task=task,
                domains=["backend", "testing", "architecture"],
                previous_results=[],
                budget_ctx=None,
                min_rounds=1,
                min_agents=3,
                strict=True,
                deadline=None,
                discussion_timeout_sec=60.0,
            ),
            timeout=0.03,
        )
