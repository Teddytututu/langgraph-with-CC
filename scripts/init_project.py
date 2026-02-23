"""
scripts/init_project.py
=======================
项目初始化脚本 — 将项目重置为"可接受下一次任务"的原始状态。

用法：
    .venv/Scripts/python.exe scripts/init_project.py
    .venv/Scripts/python.exe scripts/init_project.py --dry-run   # 只预览，不修改

动作清单：
  1. 重置所有 .claude/agents/agent_XX.md 为空槽位模板
  2. 删除运行时产物：app_state.json, sdk_debug.log
  3. 删除信号文件：crash_report.json, decision_request.json, decision_result.json, stuck_report.json
  4. 保留不动：coordinator/planner/executor/reviewer/reflector/writer_*.md（系统核心 agent）
"""

import argparse
import shutil
import sys
from pathlib import Path

# ─── 配置 ────────────────────────────────────────────────────────────────────

ROOT = Path(__file__).parent.parent

# 动态槽位目录（仅重置 agent_NN.md，不碰系统 agent）
AGENTS_DIR = ROOT / ".claude" / "agents"

# 需要删除的运行时文件（相对 ROOT）
RUNTIME_FILES = [
    "app_state.json",
    "sdk_debug.log",
    "reports/crash_report.json",
    "decision_request.json",
    "decision_result.json",
    "stuck_report.json",
    "fix_request.json",
    "checkpoints.db",
]

# reports 目录下需要清空的文件
REPORTS_FILES = [
    "reports/task.md",
    "reports/task.json",
]

# reports 子目录需要清理
REPORTS_DIRS = [
    "reports/inspections",
]

# 根目录白名单：这些文件永远不删
ROOT_KEEP = {
    ".env", ".gitignore", "CLAUDE.md", "README.md",
    "LICENSE", "requirements.txt",
}

# 根目录永久保留的 .py 文件（非任务产物）
ROOT_PY_KEEP = {
    "test_agent_sdk.py",
}

# 空槽位模板内容
BLANK_TEMPLATE = """\
```chatagent
---
name: ""
description: ""
---

（预留 - 由写手填充）
```
"""

# ─── 工具函数 ─────────────────────────────────────────────────────────────────


def log(msg: str, dry: bool = False):
    prefix = "[DRY-RUN]" if dry else "[INIT]"
    print(f"{prefix} {msg}")


def reset_agent_slots(dry: bool = False) -> int:
    """重置所有 agent_XX.md 为空槽位，返回重置数量"""
    if not AGENTS_DIR.exists():
        log(f"agents 目录不存在，跳过: {AGENTS_DIR}", dry)
        return 0

    count = 0
    for md in sorted(AGENTS_DIR.glob("agent_*.md")):
        content = md.read_text(encoding="utf-8")
        already_blank = 'name: ""' in content and 'description: ""' in content
        if already_blank:
            log(f"  SKIP  {md.name}  (already blank)", dry)
            continue
        log(f"  RESET {md.name}", dry)
        if not dry:
            md.write_text(BLANK_TEMPLATE, encoding="utf-8")
        count += 1

    return count


def delete_runtime_files(dry: bool = False) -> int:
    """删除运行时产物，返回删除数量"""
    count = 0
    for name in RUNTIME_FILES:
        path = ROOT / name
        if path.exists():
            log(f"  DELETE {name}", dry)
            if not dry:
                try:
                    path.unlink()
                except PermissionError as e:
                    log(f"  WARN   {name}: {e} (跳过)", dry)
                    continue
            count += 1
        else:
            log(f"  SKIP   {name}  (not found)", dry)
    return count


def delete_task_outputs(dry: bool = False) -> int:
    """删除根目录中任务产出的散落文件

    规则：
      - *.py  且不在 ROOT_PY_KEEP 白名单中
      - *.json 且不在 ROOT_KEEP 白名单中
    """
    count = 0
    for f in sorted(ROOT.iterdir()):
        if not f.is_file():
            continue
        name = f.name
        if name in ROOT_KEEP or name.startswith("."):
            continue
        should_delete = False
        if f.suffix == ".py" and name not in ROOT_PY_KEEP:
            should_delete = True
        elif f.suffix == ".json":
            should_delete = True
        elif f.suffix in (".db", ".log") and name not in RUNTIME_FILES:
            should_delete = True
        if should_delete:
            log(f"  DELETE {name}  (任务产出)", dry)
            if not dry:
                try:
                    f.unlink()
                except (PermissionError, OSError) as e:
                    log(f"  WARN   {name}: {e} (跳过)", dry)
                    continue
            count += 1
    return count


def clear_reports(dry: bool = False) -> int:
    """清空 reports 目录下的任务产物

    1. 删除 REPORTS_FILES 中指定的文件
    2. 清空 REPORTS_DIRS 中的所有文件（保留 .gitkeep）
    """
    count = 0

    # 删除指定的 reports 文件
    for name in REPORTS_FILES:
        path = ROOT / name
        if path.exists():
            log(f"  DELETE {name}", dry)
            if not dry:
                try:
                    path.unlink()
                except PermissionError as e:
                    log(f"  WARN   {name}: {e} (跳过)", dry)
                    continue
            count += 1
        else:
            log(f"  SKIP   {name}  (not found)", dry)

    # 清空 inspections 等子目录
    for dir_name in REPORTS_DIRS:
        dir_path = ROOT / dir_name
        if dir_path.exists() and dir_path.is_dir():
            for f in sorted(dir_path.iterdir()):
                if f.is_file() and f.name != ".gitkeep":
                    rel_path = f.relative_to(ROOT)
                    log(f"  DELETE {rel_path}", dry)
                    if not dry:
                        try:
                            f.unlink()
                        except PermissionError as e:
                            log(f"  WARN   {f.name}: {e} (跳过)", dry)
                            continue
                    count += 1
        else:
            log(f"  SKIP   {dir_name}/  (not found)", dry)

    return count




def main():
    parser = argparse.ArgumentParser(description="重置项目到初始状态")
    parser.add_argument("--dry-run", action="store_true", help="只预览，不修改任何文件")
    args = parser.parse_args()
    dry = args.dry_run

    print("=" * 60)
    print("  Project Init — 将项目重置为原始状态")
    if dry:
        print("  *** DRY-RUN MODE：不会修改任何文件 ***")
    print("=" * 60)

    print("\n[1] 重置动态 Agent 槽位:")
    n_agents = reset_agent_slots(dry)

    print("\n[2] 清理运行时产物:")
    n_files = delete_runtime_files(dry)

    print("\n[3] 清理任务产出文件（根目录散落):")
    n_outputs = delete_task_outputs(dry)

    print("\n[4] 清理 reports 目录:")
    n_reports = clear_reports(dry)

    total = n_agents + n_files + n_outputs + n_reports
    print("\n" + "=" * 60)
    print(f"  完成：重置 {n_agents} 个 agent 槽位，删除 {n_files + n_outputs + n_reports} 个文件")
    if dry:
        print("  （DRY-RUN：以上均未实际执行）")
    print("=" * 60)

    if not dry and total == 0:
        print("  项目已是干净状态，无需操作。")


if __name__ == "__main__":
    main()
