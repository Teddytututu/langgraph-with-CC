"""
Subagent 状态管理器

管理 subagent 的生命周期和状态：
- empty: 空模板（未填充）
- filling: 写手填充中
- ready: 已填充，可用
- in_use: 执行中
- completed: 子任务完成
"""

import json
import logging
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional

from pydantic import BaseModel

logger = logging.getLogger(__name__)


class SubagentState(str, Enum):
    """Subagent 状态枚举"""
    EMPTY = "empty"         # 空模板（未填充）
    FILLING = "filling"     # 写手填充中
    READY = "ready"         # 已填充，可用
    IN_USE = "in_use"       # 执行中
    COMPLETED = "completed" # 子任务完成


class SubagentInfo(BaseModel):
    """Subagent 信息"""
    agent_id: str
    state: SubagentState = SubagentState.EMPTY
    name: str = ""
    description: str = ""
    skills: list[str] = []
    filled_at: Optional[datetime] = None
    last_used_at: Optional[datetime] = None
    usage_count: int = 0


class SubagentManager:
    """管理 subagent 生命周期和状态"""

    # 独立 subagent（不占用编号池）
    INDEPENDENT_AGENTS = [
        "planner", "executor", "reviewer", "reflector",
        "coordinator", "writer_1", "writer_2", "writer_3"
    ]

    def __init__(self, pool_dir: str = ".claude/agents"):
        self.pool_dir = Path(pool_dir)
        self.states: dict[str, SubagentInfo] = {}
        self.usage_order: list[str] = []  # 记录使用顺序，用于循环清空
        self._init_states()

    def _init_states(self):
        """初始化所有 subagent 状态"""
        # 初始化独立 subagent
        for agent_id in self.INDEPENDENT_AGENTS:
            self.states[agent_id] = SubagentInfo(
                agent_id=agent_id,
                state=SubagentState.READY  # 独立 subagent 始终为 ready
            )

        # 初始化编号池（agent_01 ~ agent_40）
        for i in range(1, 41):
            agent_id = f"agent_{i:02d}"
            self.states[agent_id] = SubagentInfo(agent_id=agent_id)

    def get_next_empty(self) -> Optional[str]:
        """获取下一个空槽位（编号池）"""
        for i in range(1, 41):
            agent_id = f"agent_{i:02d}"
            if self.states[agent_id].state == SubagentState.EMPTY:
                return agent_id
        return None

    def get_next_ready(self, skills: list[str] = None) -> Optional[str]:
        """
        获取下一个可用的 subagent

        Args:
            skills: 需要的技能列表（可选，用于匹配）

        Returns:
            可用的 agent_id 或 None
        """
        # 首先查找编号池中 ready 状态的
        for i in range(1, 41):
            agent_id = f"agent_{i:02d}"
            info = self.states[agent_id]
            if info.state == SubagentState.READY:
                # 如果指定了技能，检查是否匹配
                if skills:
                    if any(s in info.skills for s in skills):
                        return agent_id
                else:
                    return agent_id
        return None

    def get_by_skills(self, skills: list[str]) -> Optional[str]:
        """
        根据技能查找最匹配的 subagent

        Args:
            skills: 需要的技能列表

        Returns:
            匹配度最高的 agent_id 或 None
        """
        best_match = None
        best_score = 0

        for agent_id, info in self.states.items():
            if info.state == SubagentState.READY:
                # 计算技能匹配度
                match_score = sum(1 for s in skills if s in info.skills)
                if match_score > best_score:
                    best_score = match_score
                    best_match = agent_id

        return best_match

    def mark_filling(self, agent_id: str) -> bool:
        """标记为填充中"""
        if agent_id not in self.states:
            return False
        self.states[agent_id].state = SubagentState.FILLING
        return True

    def mark_ready(self, agent_id: str, name: str = "",
                   description: str = "", skills: list[str] = None) -> bool:
        """
        标记为可用（写手填充完成）

        Args:
            agent_id: Agent ID
            name: Agent 名称
            description: Agent 描述
            skills: 技能列表
        """
        if agent_id not in self.states:
            return False

        info = self.states[agent_id]
        info.state = SubagentState.READY
        info.name = name or info.name
        info.description = description or info.description
        info.skills = skills or info.skills
        info.filled_at = datetime.now()
        return True

    def mark_in_use(self, agent_id: str) -> bool:
        """标记为使用中"""
        if agent_id not in self.states:
            return False

        self.states[agent_id].state = SubagentState.IN_USE
        self.states[agent_id].last_used_at = datetime.now()
        self.states[agent_id].usage_count += 1

        # 记录使用顺序（用于循环清空）
        if agent_id in self.usage_order:
            self.usage_order.remove(agent_id)
        self.usage_order.append(agent_id)

        return True

    def mark_completed(self, agent_id: str) -> bool:
        """标记子任务完成"""
        if agent_id not in self.states:
            return False
        self.states[agent_id].state = SubagentState.COMPLETED
        return True

    def mark_subtask_completed(self, agent_id: str) -> bool:
        """
        子任务完成 → 重置到 ready（保留专业知识）
        """
        if agent_id not in self.states:
            return False

        info = self.states[agent_id]
        # 独立 subagent 始终保持 ready
        if agent_id in self.INDEPENDENT_AGENTS:
            info.state = SubagentState.READY
        else:
            # 编号池中的 subagent 保留专业知识，重置为 ready
            info.state = SubagentState.READY
        return True

    def mark_task_completed(self, agent_ids: list[str]) -> bool:
        """
        总任务完成 → 重置到 empty（清空所有配置）
        """
        for agent_id in agent_ids:
            if agent_id in self.states:
                self._reset_to_empty(agent_id)
        return True

    def _reset_to_empty(self, agent_id: str):
        """重置 subagent 到空模板状态"""
        # 不重置独立 subagent
        if agent_id in self.INDEPENDENT_AGENTS:
            return

        # 清空文件内容
        file_path = self.pool_dir / f"{agent_id}.md"
        content = """---
name: ""
description: ""
tools: []
---

（预留 - 由写手填充）
"""
        try:
            file_path.write_text(content, encoding='utf-8')
        except Exception as e:
            logger.error(f"重置 subagent 文件失败: {e}")

        # 重置状态
        self.states[agent_id] = SubagentInfo(agent_id=agent_id)

        # 从使用顺序中移除
        if agent_id in self.usage_order:
            self.usage_order.remove(agent_id)

    def cycle_clear(self, count: int = 1) -> list[str]:
        """
        循环清空最早使用的 subagent

        Args:
            count: 要清空的数量

        Returns:
            被清空的 agent_id 列表
        """
        cleared = []
        for agent_id in self.usage_order[:count]:
            # 跳过独立 subagent
            if agent_id in self.INDEPENDENT_AGENTS:
                continue
            self._reset_to_empty(agent_id)
            cleared.append(agent_id)

        # 更新使用顺序
        self.usage_order = self.usage_order[len(cleared):]
        return cleared

    def get_all_states(self) -> dict[str, SubagentInfo]:
        """获取所有 subagent 状态"""
        return self.states

    def get_state(self, agent_id: str) -> Optional[SubagentState]:
        """获取指定 subagent 的状态"""
        if agent_id in self.states:
            return self.states[agent_id].state
        return None

    def get_info(self, agent_id: str) -> Optional[SubagentInfo]:
        """获取指定 subagent 的详细信息"""
        return self.states.get(agent_id)

    def get_available_count(self) -> int:
        """获取可用（ready）的 subagent 数量"""
        return sum(1 for info in self.states.values()
                   if info.state == SubagentState.READY)

    def get_empty_count(self) -> int:
        """获取空槽位数量"""
        return sum(1 for agent_id, info in self.states.items()
                   if info.state == SubagentState.EMPTY
                   and agent_id not in self.INDEPENDENT_AGENTS)

    def get_used_agents(self) -> list[str]:
        """获取已使用的 agent 列表"""
        return [aid for aid in self.usage_order
                if aid not in self.INDEPENDENT_AGENTS]

    def persist(self, file_path: str = ".claude/subagent_states.json"):
        """持久化状态到文件"""
        data = {
            "states": {
                agent_id: {
                    "state": info.state.value,
                    "name": info.name,
                    "description": info.description,
                    "skills": info.skills,
                    "filled_at": info.filled_at.isoformat() if info.filled_at else None,
                    "last_used_at": info.last_used_at.isoformat() if info.last_used_at else None,
                    "usage_count": info.usage_count,
                }
                for agent_id, info in self.states.items()
            },
            "usage_order": self.usage_order,
            "updated_at": datetime.now().isoformat(),
        }

        path = Path(file_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')

    def load(self, file_path: str = ".claude/subagent_states.json"):
        """从文件加载状态"""
        path = Path(file_path)
        if not path.exists():
            return False

        try:
            data = json.loads(path.read_text(encoding='utf-8'))

            # 恢复状态
            for agent_id, state_data in data.get("states", {}).items():
                if agent_id in self.states:
                    info = self.states[agent_id]
                    info.state = SubagentState(state_data["state"])
                    info.name = state_data.get("name", "")
                    info.description = state_data.get("description", "")
                    info.skills = state_data.get("skills", [])
                    info.usage_count = state_data.get("usage_count", 0)

                    if state_data.get("filled_at"):
                        info.filled_at = datetime.fromisoformat(state_data["filled_at"])
                    if state_data.get("last_used_at"):
                        info.last_used_at = datetime.fromisoformat(state_data["last_used_at"])

            # 恢复使用顺序
            self.usage_order = data.get("usage_order", [])

            return True

        except Exception as e:
            logger.error(f"加载 subagent 状态失败: {e}")
            return False


# 全局单例
_manager_instance: Optional[SubagentManager] = None


def get_manager() -> SubagentManager:
    """获取全局 SubagentManager 实例"""
    global _manager_instance
    if _manager_instance is None:
        _manager_instance = SubagentManager()
    return _manager_instance


def reset_manager():
    """重置全局 SubagentManager 实例"""
    global _manager_instance
    _manager_instance = None
