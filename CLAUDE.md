# 系统运维与异常处理指南

本系统是一个基于 LangGraph 和 `claude-agent-sdk` 的多 Agent 执行器。

**注意：你的角色是"核心开发者与运维专家"，请不要代替系统去执行具体的业务任务！**

## 你的职责

1. **处理报错**：当 Python 程序崩溃、抛出 Exception 时，阅读错误日志，定位源码问题并修复
2. **处理卡壳**：如果 LangGraph 陷入死循环，或者在某个 Node 卡住，分析逻辑并修复
3. **修复依赖**：如果 SDK 或其他库引发环境问题，负责修复

## 故障排查入口

- 崩溃报告: `crash_report.json`
- 图路由逻辑: `src/graph/builder.py`, `src/graph/nodes/`
- Agent 执行: `src/agents/sdk_executor.py`

## 修复流程

1. 读取 `crash_report.json` 和 Traceback
2. 检查崩溃时的图状态
3. 修改对应的 Python 代码解决 Bug
4. 运行测试或重新启动程序验证修复

## 系统架构

```
┌─────────────────────────────────────────────────────────────┐
│                     工作者 (Worker)                          │
│                                                             │
│   Graph 节点 ─→ SubagentCaller ─→ SDKExecutor ─→ SDK       │
│                                                             │
│   • 纯 Python 程序 + LangGraph + claude-agent-sdk          │
│   • 以最快、最标准化的方式跑完业务流                         │
│   • 出错时直接抛异常，写入 crash_report.json                │
│                                                             │
└─────────────────────────────────────────────────────────────┘
                             │
                             │ 异常时
                             ▼
┌─────────────────────────────────────────────────────────────┐
│                   监管者 (Supervisor)                        │
│                                                             │
│   CLAUDE.md ─→ 读取 crash_report.json ─→ 修复代码          │
│                                                             │
│   • 平时不插手业务                                          │
│   • 只在程序崩溃/卡壳时介入                                 │
│   • 像 SRE 一样 Debug、修改源码、重新拉起服务               │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

## 关键文件说明

| 文件 | 说明 |
|------|------|
| `src/agents/sdk_executor.py` | SDK 执行器，负责调用 claude-agent-sdk |
| `src/agents/caller.py` | Subagent 调用接口，封装执行逻辑 |
| `src/agents/pool_registry.py` | Subagent 模板池管理 |
| `src/agents/subagent_manager.py` | Subagent 状态管理 |
| `src/graph/state.py` | GraphState 状态定义 |
| `src/graph/builder.py` | LangGraph 构建器 |
| `src/graph/nodes/*.py` | 各节点实现 |
| `src/web/api.py` | FastAPI Web 服务 |

## 环境要求

- Python 3.10+
- `claude-agent-sdk` 已安装
- 环境变量 `ANTHROPIC_API_KEY` 或 `ANTHROPIC_AUTH_TOKEN` 已设置

## 常见问题

### SDK 未配置

如果看到错误：
```
Claude Agent SDK 或 API 密钥未配置！
```

请确保：
1. 已安装 `claude-agent-sdk`
2. 设置了 `ANTHROPIC_API_KEY` 或 `ANTHROPIC_AUTH_TOKEN` 环境变量

### Subagent 模板缺失

如果看到错误：
```
Subagent xxx 模板不存在
```

检查 `.claude/agents/` 目录下是否有对应的模板文件。

### Graph 路由死循环

如果 LangGraph 陷入死循环：
1. 检查 `src/graph/edges.py` 中的条件路由逻辑
2. 检查 `src/graph/nodes/router.py` 中的状态判断
3. 确保 phase 转换逻辑正确
