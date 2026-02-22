"""
Subagent 模板池管理

管理 .claude/agents/ 目录下的 subagent 模板
"""

import logging
import os
import re
from pathlib import Path
from typing import Optional
from pydantic import BaseModel

logger = logging.getLogger(__name__)


class SubagentTemplate(BaseModel):
    """Subagent 模板"""
    file_path: str
    name: str = ""
    description: str = ""
    tools: list[str] = []
    model: str = "inherit"
    content: str = ""

    def is_filled(self) -> bool:
        """检查模板是否已被填充"""
        return bool(self.name and self.description)


class SubagentPool:
    """Subagent 模板池管理"""

    def __init__(self, pool_dir: str = ".claude/agents"):
        self.pool_dir = Path(pool_dir)
        self._templates: dict[str, SubagentTemplate] = {}
        self._load_templates()

    def _strip_code_fence(self, content: str) -> str:
        """剥除 ```chatagent ... ``` 包装，仅保留内部内容"""
        # 支持 ```chatagent 或 ```agent 等 code fence 包装
        stripped = content.strip()
        match = re.match(r'^```[\w]*\n(.*?)\n?```\s*$', stripped, re.DOTALL)
        if match:
            return match.group(1)
        return content

    def _parse_frontmatter(self, content: str) -> dict:
        """解析 YAML frontmatter"""
        content = self._strip_code_fence(content)
        frontmatter = {}

        # 匹配 --- 之间的内容
        match = re.match(r'^---\s*\n(.*?)\n---\s*\n', content, re.DOTALL)
        if match:
            fm_text = match.group(1)
            for line in fm_text.split('\n'):
                if ':' in line:
                    key, value = line.split(':', 1)
                    key = key.strip()
                    value = value.strip().strip('"').strip("'")

                    # 解析列表字段
                    if key in ['tools']:
                        if value.startswith('[') and value.endswith(']'):
                            items = value[1:-1].split(',')
                            frontmatter[key] = [i.strip().strip('"').strip("'") for i in items if i.strip()]
                        else:
                            frontmatter[key] = []
                    else:
                        frontmatter[key] = value

        return frontmatter

    def _get_body(self, content: str) -> str:
        """获取 frontmatter 之后的正文"""
        content = self._strip_code_fence(content)
        match = re.match(r'^---\s*\n.*?\n---\s*\n(.*)$', content, re.DOTALL)
        if match:
            return match.group(1).strip()
        return content

    def _load_templates(self):
        """加载所有模板文件"""
        if not self.pool_dir.exists():
            self.pool_dir.mkdir(parents=True, exist_ok=True)
            return

        for file_path in self.pool_dir.glob("*.md"):
            try:
                content = file_path.read_text(encoding='utf-8')
                frontmatter = self._parse_frontmatter(content)
                body = self._get_body(content)

                template = SubagentTemplate(
                    file_path=str(file_path),
                    name=frontmatter.get('name', ''),
                    description=frontmatter.get('description', ''),
                    tools=frontmatter.get('tools', []),
                    model=frontmatter.get('model', 'inherit'),
                    content=body
                )

                # 使用文件名（不含扩展名）作为 ID
                agent_id = file_path.stem
                self._templates[agent_id] = template

            except Exception as e:
                logger.error(f"加载模板 {file_path} 失败: {e}")

    def get_template(self, agent_id: str) -> Optional[SubagentTemplate]:
        """获取指定模板"""
        return self._templates.get(agent_id)

    def get_all_templates(self) -> dict[str, SubagentTemplate]:
        """获取所有模板"""
        return self._templates

    def get_available_slots(self) -> list[str]:
        """获取所有空槽位（未被填充的模板）"""
        return [id_ for id_, t in self._templates.items() if not t.is_filled()]

    def get_filled_agents(self) -> list[str]:
        """获取所有已填充的 agent"""
        return [id_ for id_, t in self._templates.items() if t.is_filled()]

    def fill_agent(self, agent_id: str, name: str, description: str,
                   content: str = "", tools: list[str] = None) -> bool:
        """
        填充 agent 模板（由写手调用）

        Args:
            agent_id: 模板 ID（如 agent_01）
            name: agent 名称
            description: agent 描述
            content: 系统提示内容
            tools: 可用工具列表

        Returns:
            是否成功
        """
        if agent_id not in self._templates:
            # 如果模板不存在，创建新的
            file_path = self.pool_dir / f"{agent_id}.md"
        else:
            file_path = Path(self._templates[agent_id].file_path)

        tools = tools or []

        # 构建 frontmatter
        tools_str = ", ".join(f'"{t}"' for t in tools)
        fm = f"""---
name: "{name}"
description: "{description}"
tools: [{tools_str}]
---

{content}
"""

        try:
            file_path.write_text(fm, encoding='utf-8')

            # 更新内存中的模板
            self._templates[agent_id] = SubagentTemplate(
                file_path=str(file_path),
                name=name,
                description=description,
                tools=tools,
                content=content
            )
            return True

        except Exception as e:
            logger.error(f"填充模板失败: {e}")
            return False

    def reload(self):
        """重新加载所有模板"""
        self._templates.clear()
        self._load_templates()

    def find_by_name(self, name: str) -> Optional[str]:
        """根据名称查找 agent ID"""
        for id_, t in self._templates.items():
            if t.name == name:
                return id_
        return None

    def find_by_description_keyword(self, keyword: str) -> list[str]:
        """根据描述关键词查找 agent"""
        results = []
        keyword_lower = keyword.lower()
        for id_, t in self._templates.items():
            if keyword_lower in t.description.lower():
                results.append(id_)
        return results

    def create_agent_file(self, name: str, description: str,
                          content: str = "", tools: list[str] = None) -> str:
        """
        创建新的 agent 文件（使用下一个可用槽位）

        Returns:
            新创建的 agent ID
        """
        # 找到下一个可用的编号
        existing_ids = set(self._templates.keys())
        for i in range(1, 100):
            agent_id = f"agent_{i:02d}"
            if agent_id not in existing_ids:
                break

        if self.fill_agent(agent_id, name, description, content, tools):
            return agent_id
        return ""


# 全局单例
_pool_instance: Optional[SubagentPool] = None


def get_pool() -> SubagentPool:
    """获取全局 SubagentPool 实例"""
    global _pool_instance
    if _pool_instance is None:
        _pool_instance = SubagentPool()
    return _pool_instance


def reload_pool():
    """重新加载模板池"""
    global _pool_instance
    if _pool_instance:
        _pool_instance.reload()
