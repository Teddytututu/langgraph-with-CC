"""src/graph/nodes/router.py — 全局路由节点"""
from datetime import datetime
from pathlib import Path
from src.graph.state import GraphState

_REPORTS_DIR = Path("reports")


async def router_node(state: GraphState) -> dict:
    """判断整体进度，决定下一步"""
    budget = state.get("time_budget")

    # 纯函数式更新时间
    if budget and budget.started_at:
        elapsed = (
            datetime.now() - budget.started_at
        ).total_seconds() / 60
        remaining = max(0, budget.total_minutes - elapsed)
        budget = budget.model_copy(update={
            "elapsed_minutes": elapsed,
            "remaining_minutes": remaining,
            "is_overtime": remaining <= 0,
        })

    # 全部完成 → 汇总输出
    subtasks = state.get("subtasks", [])
    if subtasks and all(
        t.status in ("done", "skipped", "failed") for t in subtasks
    ):
        return {
            "phase": "complete",
            "final_output": _build_final_output(state),
            "time_budget": budget,
        }

    # 超时 → 交付已完成部分
    if budget and budget.is_overtime:
        return {
            "phase": "timeout",
            "final_output": _build_final_output(state, timeout=True),
            "time_budget": budget,
        }

    return {
        "phase": state.get("phase", "init") if subtasks else "init",
        "time_budget": budget,
        "iteration": state.get("iteration", 0) + 1,
    }


def _build_final_output(state: GraphState, timeout: bool = False) -> str:
    """汇总所有子任务结果"""
    lines = []
    if timeout:
        lines.append("⚠️ **时间预算已用尽，以下为已完成部分：**\n")
    else:
        lines.append("✅ **所有任务已完成：**\n")

    subtasks = state.get("subtasks", [])
    for t in subtasks:
        icon = "✅" if t.status == "done" else "❌" if t.status == "failed" else "⏳"
        lines.append(f"### {icon} {t.title}")
        if t.result:
            lines.append(t.result)
        lines.append("")

    budget = state.get("time_budget")
    if budget:
        lines.append(
            f"\n---\n总耗时 {budget.elapsed_minutes:.1f} 分钟 "
            f"/ 预算 {budget.total_minutes:.0f} 分钟"
        )

    # 扫描 reports/ 目录，将所有 .md 文件追加到输出
    if _REPORTS_DIR.exists():
        md_files = sorted(
            _REPORTS_DIR.glob("*.md"),
            key=lambda p: p.stat().st_mtime,
        )
        if md_files:
            lines.append("\n---\n## 📁 详细分析报告\n")
            for f in md_files:
                try:
                    content = f.read_text(encoding="utf-8", errors="replace")
                    lines.append(f"### {f.stem}\n")
                    lines.append(content)
                    lines.append("\n")
                except Exception:
                    pass

        # 扫描 JSON 报告
        json_files = sorted(
            _REPORTS_DIR.glob("*.json"),
            key=lambda p: p.stat().st_mtime,
        )
        if json_files:
            lines.append("\n---\n## 📊 数据文件\n")
            import json as _json
            for f in json_files:
                try:
                    data = _json.loads(f.read_text(encoding="utf-8", errors="replace"))
                    lines.append(f"### {f.stem}\n")
                    lines.append(f"```json\n{_json.dumps(data, ensure_ascii=False, indent=2)}\n```\n")
                except Exception:
                    pass

    return "\n".join(lines)
