"""快速验收测试 — 覆盖本次实现的所有新功能"""
import asyncio
import sys
import traceback

PASS = []
FAIL = []


def ok(name):
    PASS.append(name)
    print(f"  PASS  {name}")


def fail(name, exc):
    FAIL.append(name)
    print(f"  FAIL  {name}: {exc}")


# ─────────────────────────────────────
# 1. budget 模块
# ─────────────────────────────────────
def test_budget():
    from src.budget import create_budget, update_elapsed, is_overtime, remaining_ratio
    b = create_budget(30)
    assert b.total_minutes == 30
    assert b.remaining_minutes == 30
    b2 = update_elapsed(b)
    assert 0 <= b2.elapsed_minutes < 1
    assert not is_overtime(b)
    assert 0.99 < remaining_ratio(b) <= 1.0
    ok("budget.create_budget / update_elapsed / is_overtime / remaining_ratio")


# ─────────────────────────────────────
# 2. WriterAgent.analyze_task_and_define_agents
# ─────────────────────────────────────
def test_writer_agent():
    from src.agents.writer_agent import WriterAgent, AgentDefinition
    from unittest.mock import MagicMock
    wa = WriterAgent.__new__(WriterAgent)
    wa.pool = MagicMock()

    # 代码任务 → coder
    defs = wa.analyze_task_and_define_agents("编写一个Python函数")
    names = [d.name for d in defs]
    assert "coder" in names, f"expected coder, got {names}"
    ok("writer_agent: coder detected")

    # 研究任务 → researcher
    defs = wa.analyze_task_and_define_agents("research the best libraries")
    names = [d.name for d in defs]
    assert "researcher" in names, f"expected researcher, got {names}"
    ok("writer_agent: researcher detected")

    # 报告任务 → writer
    defs = wa.analyze_task_and_define_agents("撰写一份分析报告")
    names = [d.name for d in defs]
    assert "writer" in names or "analyst" in names, f"expected writer/analyst, got {names}"
    ok("writer_agent: writer/analyst detected")

    # 空任务 → executor fallback
    defs = wa.analyze_task_and_define_agents("do this thing")
    assert len(defs) >= 1
    ok("writer_agent: fallback executor returned")

    # 每个 AgentDefinition 都有非空 system_prompt
    defs = wa.analyze_task_and_define_agents("编写代码并撰写文档")
    for d in defs:
        assert d.system_prompt, f"empty system_prompt for {d.name}"
    ok("writer_agent: all defs have system_prompt")


# ─────────────────────────────────────
# 3. CoordinatorAgent.choose_collaboration_mode
# ─────────────────────────────────────
def test_coordinator():
    from src.agents.coordinator import CoordinatorAgent
    from src.agents.collaboration import CollaborationMode
    from src.graph.state import SubTask

    coord = CoordinatorAgent()

    # 有依赖 → CHAIN
    from unittest.mock import MagicMock
    st = MagicMock()
    st.dependencies = ["task-001"]
    mode = coord.choose_collaboration_mode("some task", ["coder"], subtasks=[st])
    assert mode == CollaborationMode.CHAIN, f"expected CHAIN, got {mode}"
    ok("coordinator: has_dependencies → CHAIN")

    # 讨论关键词 → DISCUSSION
    mode = coord.choose_collaboration_mode("请大家讨论达成共识", ["coder", "analyst"])
    assert mode == CollaborationMode.DISCUSSION, f"expected DISCUSSION, got {mode}"
    ok("coordinator: consensus keywords → DISCUSSION")

    # 无依赖无共识 → PARALLEL
    mode = coord.choose_collaboration_mode("analyze logs", ["coder", "analyst"])
    assert mode == CollaborationMode.PARALLEL, f"expected PARALLEL, got {mode}"
    ok("coordinator: no deps → PARALLEL")


# ─────────────────────────────────────
# 4. Executor _compute_timeout
# ─────────────────────────────────────
def test_executor_timeout():
    from src.graph.nodes.executor import _compute_timeout
    from src.graph.state import SubTask

    # min clamp: 0.5 min * 120 = 60 < 120  → 120
    st = SubTask(id="t1", title="x", description="x", agent_type="coder", estimated_minutes=0.5)
    assert _compute_timeout(st) == 120.0, f"expected 120, got {_compute_timeout(st)}"
    ok("executor: timeout min clamp (0.5 min → 120s)")

    # normal: 4 min * 120 = 480
    st2 = SubTask(id="t2", title="x", description="x", agent_type="coder", estimated_minutes=4)
    assert _compute_timeout(st2) == 480.0, f"expected 480, got {_compute_timeout(st2)}"
    ok("executor: timeout 4min → 480s")

    # max clamp: 20 min * 120 = 2400 > 1800 → 1800
    st3 = SubTask(id="t3", title="x", description="x", agent_type="coder", estimated_minutes=20)
    assert _compute_timeout(st3) == 1800.0, f"expected 1800, got {_compute_timeout(st3)}"
    ok("executor: timeout max clamp (20 min → 1800s)")


# ─────────────────────────────────────
# 5. Builder / SqliteSaver
# ─────────────────────────────────────
def test_builder():
    from src.graph.builder import build_graph, _make_default_checkpointer
    cp = _make_default_checkpointer()
    assert cp is not None
    ok(f"builder: checkpointer created ({type(cp).__name__})")

    g = build_graph()
    assert g is not None
    ok("builder: build_graph() succeeds")


# ─────────────────────────────────────
# 6. Planner system prompt contains knowledge_domains
# ─────────────────────────────────────
def test_planner_prompt():
    from src.graph.nodes.planner import PLANNER_SYSTEM_PROMPT
    assert "knowledge_domains" in PLANNER_SYSTEM_PROMPT
    assert "completion_criteria" in PLANNER_SYSTEM_PROMPT
    ok("planner: system prompt contains knowledge_domains + completion_criteria")


# ─────────────────────────────────────
# 7. Reflector _update_specialist_prompts (pool patching)
# ─────────────────────────────────────
def test_reflector_update_prompts():
    from src.graph.nodes.reflector import _update_specialist_prompts
    from src.graph.state import SubTask
    from src.agents.pool_registry import SubagentTemplate
    from unittest.mock import MagicMock, patch

    task = SubTask(
        id="t1", title="fix bug", description="fix it",
        agent_type="coder", assigned_agents=["agent_01"],
        retry_count=1,
    )

    mock_template = SubagentTemplate(
        file_path="/fake/agent_01.md",
        name="TestAgent",
        description="A test agent",
        tools=["Read"],
        content="Original system prompt.",
    )

    mock_pool = MagicMock()
    mock_pool.get_template.return_value = mock_template

    with patch("src.graph.nodes.reflector.get_pool", return_value=mock_pool):
        _update_specialist_prompts(task, "Root cause: X. Fix: Y.")

    mock_pool.fill_agent.assert_called_once()
    call_kwargs = mock_pool.fill_agent.call_args
    new_content = call_kwargs[1]["content"] if call_kwargs[1] else call_kwargs[0][3]
    assert "经验补丁" in new_content
    assert "Root cause: X. Fix: Y." in new_content
    ok("reflector: _update_specialist_prompts patches pool content")


# ─────────────────────────────────────
# 8. Collaboration modes — basic execute
# ─────────────────────────────────────
async def _test_collaboration_async():
    from src.agents.collaboration import (
        ChainCollaboration, ParallelCollaboration,
        AgentExecutor, CollaborationMode,
    )

    results = []

    async def make_fn(label):
        async def fn(task, ctx):
            return f"{label}:{task}"
        return fn

    agents = [
        AgentExecutor(agent_id="a1", name="A1", execute_fn=await make_fn("A1")),
        AgentExecutor(agent_id="a2", name="A2", execute_fn=await make_fn("A2")),
    ]

    # Chain: A2 receives A1's output
    chain = ChainCollaboration(agents)
    r = await chain.execute("hello")
    assert r.success
    assert r.final_output == "A2:A1:hello"
    ok("collaboration: ChainCollaboration chain passes output")

    # Parallel: both get same input
    par = ParallelCollaboration(agents)
    r = await par.execute("hello")
    assert r.success
    assert "a1" in r.results and "a2" in r.results
    ok("collaboration: ParallelCollaboration both agents run")


def test_collaboration():
    asyncio.run(_test_collaboration_async())


# ─────────────────────────────────────
# Runner
# ─────────────────────────────────────
if __name__ == "__main__":
    tests = [
        test_budget,
        test_writer_agent,
        test_coordinator,
        test_executor_timeout,
        test_builder,
        test_planner_prompt,
        test_reflector_update_prompts,
        test_collaboration,
    ]
    for t in tests:
        name = t.__name__
        print(f"\n▶ {name}")
        try:
            t()
        except Exception as e:
            fail(name, e)
            traceback.print_exc()

    print(f"\n{'='*50}")
    print(f"  PASSED: {len(PASS)}/{len(PASS)+len(FAIL)}")
    if FAIL:
        print(f"  FAILED: {FAIL}")
        sys.exit(1)
    else:
        print("  All tests passed ✓")
