"""
Agent 模块

包含 Subagent 模板池管理、协作模式、写手、协调者、状态管理器、执行器等
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
from .subagent_manager import (
    SubagentState,
    SubagentInfo,
    SubagentManager,
    get_manager,
    reset_manager,
)
from .caller import (
    SubagentCaller,
    get_caller,
    call_subagent,
)
from .executor_bridge import (
    ExecutorBridge,
    get_bridge,
    reset_bridge,
)
from .sdk_executor import (
    SDKExecutor,
    FallbackExecutor,
    HybridExecutor,
    SubagentResult,
    get_executor,
    execute_subagent,
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
    # Subagent Manager
    "SubagentState",
    "SubagentInfo",
    "SubagentManager",
    "get_manager",
    "reset_manager",
    # Subagent Caller
    "SubagentCaller",
    "get_caller",
    "call_subagent",
    # Executor Bridge
    "ExecutorBridge",
    "get_bridge",
    "reset_bridge",
    # SDK Executor
    "SDKExecutor",
    "FallbackExecutor",
    "HybridExecutor",
    "SubagentResult",
    "get_executor",
    "execute_subagent",
]
