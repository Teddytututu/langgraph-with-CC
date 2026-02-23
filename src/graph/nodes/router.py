"""src/graph/nodes/router.py — 全局路由节点"""
from datetime import datetime
from pathlib import Path
import json
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
            "final_output": _build_final_output(state, budget=budget),
            "time_budget": budget,
        }

    # 超时 → 交付已完成部分
    if budget and budget.is_overtime:
        return {
            "phase": "timeout",
            "final_output": _build_final_output(state, timeout=True, budget=budget),
            "time_budget": budget,
        }

    # 迭代上限防护：超过 200 次循环强制超时交付
    current_iteration = state.get("iteration", 0)
    if current_iteration > 200:
        import logging as _log
        _log.getLogger(__name__).error("[router] 迭代已达 %d 次，强制超时交付", current_iteration)
        return {
            "phase": "timeout",
            "final_output": _build_final_output(state, timeout=True, budget=budget),
            "time_budget": budget,
        }

    return {
        "phase": state.get("phase", "init") if subtasks else "init",
        "time_budget": budget,
        "iteration": current_iteration + 1,
    }


def _build_final_output(state: GraphState, timeout: bool = False, budget=None) -> str:
    """汇总所有子任务结果

    Args:
        state: 当前图状态
        timeout: 是否超时交付
        budget: 已更新过 elapsed_minutes 的 TimeBudget 对象（不传则从 state 读取）
    """
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

    # 使用传入的已更新 budget，如果没有则从 state 读取
    eff_budget = budget or state.get("time_budget")
    if eff_budget:
        elapsed = eff_budget.elapsed_minutes
        # 如果 elapsed_minutes 仍为 0（budget 没有排过 router）, 尝试实时计算
        if elapsed == 0 and eff_budget.started_at:
            from datetime import datetime as _dt
            elapsed = (_dt.now() - eff_budget.started_at).total_seconds() / 60
        lines.append(
            f"\n---\n总耗时 {elapsed:.1f} 分钟 "
            f"/ 预算 {eff_budget.total_minutes:.0f} 分钟"
        )

    report_sections = []
    artifacts = dict(state.get("artifacts") or {})

    for t in subtasks:
        candidate_paths = []
        for key in (t.id, f"{t.id}:md", f"{t.id}:json"):
            path = artifacts.get(key)
            if path and path not in candidate_paths:
                candidate_paths.append(path)

        for p in candidate_paths:
            report_path = Path(p)
            if not report_path.exists() or not report_path.is_file():
                continue

            suffix = report_path.suffix.lower()
            try:
                if suffix == ".md":
                    content = report_path.read_text(encoding="utf-8", errors="replace")
                    report_sections.append(f"### {report_path.stem}\n")
                    report_sections.append(content)
                    report_sections.append("\n")
                    break
                if suffix == ".json":
                    data = json.loads(report_path.read_text(encoding="utf-8", errors="replace"))
                    report_sections.append(f"### {report_path.stem}\n")
                    report_sections.append(f"```json\n{json.dumps(data, ensure_ascii=False, indent=2)}\n```\n")
                    break
            except Exception:
                continue

    # reports 目录兜底扫描：仅在索引为空/失效时启用
    if _REPORTS_DIR.exists() and not report_sections:
        md_files = sorted(
            _REPORTS_DIR.glob("*.md"),
            key=lambda p: p.stat().st_mtime,
        )
        if md_files:
            for f in md_files:
                try:
                    content = f.read_text(encoding="utf-8", errors="replace")
                    report_sections.append(f"### {f.stem}\n")
                    report_sections.append(content)
                    report_sections.append("\n")
                except Exception:
                    pass

        json_files = sorted(
            _REPORTS_DIR.glob("*.json"),
            key=lambda p: p.stat().st_mtime,
        )
        if json_files:
            for f in json_files:
                try:
                    data = json.loads(f.read_text(encoding="utf-8", errors="replace"))
                    report_sections.append(f"### {f.stem}\n")
                    report_sections.append(f"```json\n{json.dumps(data, ensure_ascii=False, indent=2)}\n```\n")
                except Exception:
                    pass

    if report_sections:
        lines.append("\n---\n## 📁 详细分析报告\n")
        lines.extend(report_sections)

    return "\n".join(lines)
