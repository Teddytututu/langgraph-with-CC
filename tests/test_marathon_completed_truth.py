import importlib.util
import json
import pathlib

import pytest


_MARATHON_PATH = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "marathon.py"
_SPEC = importlib.util.spec_from_file_location("marathon_under_test", _MARATHON_PATH)
marathon = importlib.util.module_from_spec(_SPEC)
assert _SPEC and _SPEC.loader
_SPEC.loader.exec_module(marathon)


def test_evaluate_completed_task_truth_clean_completed(monkeypatch):
    payload = {
        "id": "task-clean",
        "status": "completed",
        "subtasks": [
            {
                "id": "st-1",
                "status": "done",
                "result": "all checks passed",
                "result_summary": "clean",
            }
        ],
    }

    monkeypatch.setattr(marathon, "_api", lambda *args, **kwargs: payload)

    out = marathon._evaluate_completed_task_truth("task-clean")

    assert out["truthful"] is True
    assert out["reason"] == "clean_completed"
    assert out["source"] == "api"
    assert out["evidence"] == []


def test_evaluate_completed_task_truth_degraded_completed(monkeypatch):
    payload = {
        "id": "task-degraded",
        "status": "completed",
        "subtasks": [
            {
                "id": "st-1",
                "status": "done",
                "result": "[DEGRADED_CONTINUE] discussion_total_timeout>200s",
                "result_summary": "degraded",
            }
        ],
    }

    monkeypatch.setattr(marathon, "_api", lambda *args, **kwargs: payload)

    out = marathon._evaluate_completed_task_truth("task-degraded")

    assert out["truthful"] is False
    assert out["source"] == "api"
    assert "completed_with_degraded_evidence" in out["reason"]
    assert out["evidence"]
    assert out["evidence"][0]["subtask_id"] == "st-1"
    assert out["evidence"][0]["marker"] in {"[DEGRADED_CONTINUE]", "discussion_total_timeout"}


def test_evaluate_completed_task_truth_missing_details_and_export(monkeypatch, tmp_path):
    monkeypatch.setattr(marathon, "_api", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(marathon, "TASK_EXPORTS_DIR", tmp_path / "exports" / "tasks")

    out = marathon._evaluate_completed_task_truth("task-missing")

    assert out["truthful"] is False
    assert out["source"] == "none"
    assert out["evidence"][0]["marker"] == "task_detail_unavailable"


def test_run_completed_with_degraded_evidence_writes_fix_request(monkeypatch, tmp_path):
    events = []

    monkeypatch.setattr(marathon, "FIX_REQUEST", tmp_path / "fix_request.json")
    monkeypatch.setattr(marathon, "_submit_task", lambda task_text: "task-1")
    monkeypatch.setattr(marathon, "_poll_until_terminal", lambda *args, **kwargs: ("completed", ""))
    monkeypatch.setattr(
        marathon,
        "_evaluate_completed_task_truth",
        lambda task_id: {
            "truthful": False,
            "reason": "completed_with_degraded_evidence:[DEGRADED_CONTINUE]",
            "source": "api",
            "evidence": [
                {
                    "subtask_id": "st-1",
                    "marker": "[DEGRADED_CONTINUE]",
                    "status": "done",
                    "evidence": "[DEGRADED_CONTINUE] discussion_total_timeout>200s",
                }
            ],
        },
    )

    def _fake_emit(event, **fields):
        events.append((event, fields))

    monkeypatch.setattr(marathon, "_emit", _fake_emit)

    def _fake_wait(*args, **kwargs):
        if marathon.FIX_REQUEST.exists():
            marathon.FIX_REQUEST.unlink()

    monkeypatch.setattr(marathon, "_wait_for_fix", _fake_wait)

    class _StopLoop(Exception):
        pass

    def _stop_sleep(_seconds):
        raise _StopLoop()

    monkeypatch.setattr(marathon.time, "sleep", _stop_sleep)

    with pytest.raises(_StopLoop):
        marathon.run(
            task_text="demo",
            poll_interval=1,
            wait_poll=1,
            wait_timeout=0,
            cooldown=1,
            backoff_base=1,
            backoff_max=1,
            round_timeout=30,
        )

    assert any(name == "completed_rejected" for name, _ in events)
    assert any(name == "round_failed" and payload.get("reason") == "completed_untruthful" for name, payload in events)

    fix_events = [payload for name, payload in events if name == "fix_request_written"]
    assert fix_events and fix_events[0].get("reason") == "completed_untruthful"


def test_run_completed_clean_does_not_write_fix_request(monkeypatch, tmp_path):
    events = []

    monkeypatch.setattr(marathon, "FIX_REQUEST", tmp_path / "fix_request.json")
    monkeypatch.setattr(marathon, "_submit_task", lambda task_text: "task-2")
    monkeypatch.setattr(marathon, "_poll_until_terminal", lambda *args, **kwargs: ("completed", ""))
    monkeypatch.setattr(
        marathon,
        "_evaluate_completed_task_truth",
        lambda task_id: {"truthful": True, "reason": "clean_completed", "source": "api", "evidence": []},
    )

    def _fake_emit(event, **fields):
        events.append((event, fields))

    monkeypatch.setattr(marathon, "_emit", _fake_emit)

    class _StopLoop(Exception):
        pass

    def _stop_sleep(_seconds):
        raise _StopLoop()

    monkeypatch.setattr(marathon.time, "sleep", _stop_sleep)

    with pytest.raises(_StopLoop):
        marathon.run(
            task_text="demo",
            poll_interval=1,
            wait_poll=1,
            wait_timeout=0,
            cooldown=1,
            backoff_base=1,
            backoff_max=1,
            round_timeout=30,
        )

    assert any(name == "task_completed" for name, _ in events)
    assert not any(name == "round_failed" for name, _ in events)
    assert not marathon.FIX_REQUEST.exists()
