"""
Agent 模块

包含 Subagent 模板池管理、协作模式、写手、协调者等
"""

from .pool_registry import (
    SubagentPool,
    SubagentTemplate,
    get_pool,
    reload_pool,
)
from .collaboration import (
    CollaborationMode,
    AgentExecutor,
    ChainCollaboration,
    ParallelCollaboration,
    DiscussionCollaboration,
    execute_collaboration,
)
from .writer_agent import (
    WriterAgent,
    AgentDefinition,
    WRITER_SYSTEM_PROMPT,
)
from .coordinator import (
    CoordinatorAgent,
    TaskAnalysis,
    COORDINATOR_SYSTEM_PROMPT,
)

__all__ = [
    # Pool Registry
    "SubagentPool",
    "SubagentTemplate",
    "get_pool",
    "reload_pool",
    # Collaboration
    "CollaborationMode",
    "AgentExecutor",
    "ChainCollaboration",
    "ParallelCollaboration",
    "DiscussionCollaboration",
    "execute_collaboration",
    # Writer Agent
    "WriterAgent",
    "AgentDefinition",
    "WRITER_SYSTEM_PROMPT",
    # Coordinator
    "CoordinatorAgent",
    "TaskAnalysis",
    "COORDINATOR_SYSTEM_PROMPT",
]
