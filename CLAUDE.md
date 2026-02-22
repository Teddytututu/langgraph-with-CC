# Claude Code 任务执行指南

本系统是一个基于 LangGraph 和 `claude-agent-sdk` 的多 Agent 执行器。

## 工作流程

### 1. 启动阶段
用户布置任务后，你需要：
1. 启动主程序：`python -m src.main` 或 `uvicorn src.web.api:app`
2. 程序会在后台运行，处理业务逻辑
3. 你可以休息，等待被唤醒

### 2. 执行阶段
Python 程序会自动：
- 使用 LangGraph 调度各个 Agent 节点
- 通过 claude-agent-sdk 调用 Claude 完成具体任务
- 跟踪状态、管理时间预算

### 3. 唤醒阶段
当以下情况发生时，你会被唤醒：

#### 崩溃唤醒（crash_report.json）
1. 读取 `crash_report.json` 查看错误信息
2. 分析 Traceback 和崩溃时的状态
3. 修复对应的 Python 代码
4. 重新启动程序

#### 决策唤醒（decision_request.json）
1. 读取 `decision_request.json` 查看决策问题
2. 分析选项和上下文
3. 做出决策，写入 `decision_result.json`：
   ```json
   {
     "decision": "选择的选项",
     "reason": "决策理由",
     "confidence": "high/medium/low"
   }
   ```

#### 卡壳唤醒（stuck_report.json）
1. 读取 `stuck_report.json` 查看卡壳状态
2. 分析当前状态和尝试记录
3. 调整策略或修改代码
4. 继续执行

## 系统架构

```
┌─────────────────────────────────────────────────────────────┐
│                    CLAUDE.md 工作流程                        │
│                                                             │
│   1. 用户布置任务 ─→ Claude Code 启动 ─→ 读取 CLAUDE.md    │
│   2. CLAUDE.md 启动 main 程序                               │
│   3. Python 程序在后台跑业务流                              │
│   4. CLAUDE.md 休息（等待唤醒）                             │
│                                                             │
└─────────────────────────────────────────────────────────────┘
                             │
           ┌─────────────────┼─────────────────┐
           │                 │                 │
           ▼                 ▼                 ▼
    ┌─────────────┐   ┌─────────────┐   ┌─────────────┐
    │   崩溃时    │   │ 重大决策时  │   │   卡壳时    │
    │             │   │             │   │             │
    │ crash_report│   │decision.json│   │ stuck.json  │
    └─────────────┘   └─────────────┘   └─────────────┘
           │                 │                 │
           └─────────────────┼─────────────────┘
                             ▼
┌─────────────────────────────────────────────────────────────┐
│                   CLAUDE.md 被唤醒                           │
│                                                             │
│   根据唤醒原因执行不同操作：                                │
│   - 崩溃：读取报告，修复代码，重启程序                      │
│   - 决策：读取问题，做出决策，写入 decision_result.json     │
│   - 卡壳：分析状态，调整策略，继续执行                      │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

## 关键文件

| 文件 | 说明 |
|------|------|
| `src/main.py` | 程序入口 |
| `src/web/api.py` | FastAPI Web 服务 |
| `src/utils/claude_communication.py` | 与 CLAUDE.md 通信工具 |
| `src/agents/sdk_executor.py` | SDK 执行器，负责调用 claude-agent-sdk |
| `src/agents/caller.py` | Subagent 调用接口，封装执行逻辑 |
| `src/agents/pool_registry.py` | Subagent 模板池管理 |
| `src/graph/state.py` | GraphState 状态定义 |
| `src/graph/builder.py` | LangGraph 构建器 |
| `src/graph/nodes/*.py` | 各节点实现 |
| `crash_report.json` | 崩溃报告（崩溃时生成） |
| `decision_request.json` | 决策请求（需要决策时生成） |
| `decision_result.json` | 决策结果（CLAUDE.md 填写） |
| `stuck_report.json` | 卡壳报告（卡壳时生成） |

## 启动命令

```bash
# 方式 1: 直接运行
python -m src.main

# 方式 2: 启动 Web 服务
uvicorn src.web.api:app --reload --port 8000
```

## 环境要求

- Python 3.10+
- 已安装 `claude-agent-sdk`
- 已设置 `ANTHROPIC_API_KEY` 或 `ANTHROPIC_AUTH_TOKEN`

## 常见问题

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
