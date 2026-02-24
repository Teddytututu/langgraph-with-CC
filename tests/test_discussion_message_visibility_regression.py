from pathlib import Path


def test_discussion_message_matches_selected_canonical_and_context_paths():
    """Regression guard: discussion_message visibility must support canonical/subtask/context mapping."""
    app_js = Path("src/web/static/js/app.js")
    text = app_js.read_text(encoding="utf-8")

    required_fragments = [
        "const eventSubtaskId = payload.subtask_id || ''",
        "const eventNodeId = payload.node_id || eventSubtaskId",
        "const contextSubtask = resolveSubtaskByDiscussionContext({",
        "const matchesSelected = (",
        "activeSelectedId === eventNodeId",
        "activeSelectedCanonicalId && activeSelectedCanonicalId === eventNodeId",
        "activeSelectedCanonicalId && eventSubtaskId && activeSelectedCanonicalId === eventSubtaskId",
        "contextSubtask?.id && contextSubtask.id === activeSelectedId",
        "if (taskMatched && matchesSelected)",
    ]

    missing = [frag for frag in required_fragments if frag not in text]
    assert not missing, f"Missing discussion visibility regression fragments: {missing}"
