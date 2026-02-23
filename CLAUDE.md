# Claude Code 操作手册

> **阅读本文件的是 Claude Code。**  
> 本文件是你的任务指令。按照下方流程依次执行，不要跳过任何步骤。

> ⚠️ **SUBAGENT 守卫**：如果你的 system prompt 中包含"SUBAGENT 模式"或"subagent"字样，说明你是通过 claude-agent-sdk 启动的子进程。**立刻跳过下方全部 Boot Sequence / 监控协议步骤**，直接执行 system prompt 中指定的具体任务。系统维护、服务器启动、端口检测等操作均不适用于 subagent。

---

## 一、启动时立即执行（Boot Sequence）

每次被唤醒（用户打开 repo 或直接对话），**立刻**按顺序执行：

### Step 1 — 确认环境

```bash
# 确认 venv 可用
.venv\Scripts\python.exe --version

# 确认关键依赖存在
.venv\Scripts\python.exe -c "import uvicorn, fastapi, langgraph; print('OK')"
```

如果报 ImportError，运行：
```bash
.venv\Scripts\pip install -r requirements.txt
```

### Step 2 — 检查服务器是否已在运行

```bash
# 首选：直接探活 API（最可靠）
.venv\Scripts\python.exe -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8001/api/system/status', timeout=3); print('UP')"
```

- **输出 `UP`** → 服务器已运行，跳到 Step 4。
- **连接失败/超时** → 继续 Step 3。

> 如需查看端口占用（辅助排查）：
> ```bash
> netstat -ano | findstr ":8001"
> ```

### Step 3 — 启动 Web 服务器

```bash
# 必须用 venv Python，必须不加 --reload（anyio CancelScope 限制），-X utf8 避免中文乱码
.venv\Scripts\python.exe -X utf8 -m uvicorn src.web.api:app --port 8001
```

> 以后台任务启动。启动后等待 3 秒，再次运行 Step 2 确认端口可达。  
> 如果失败，读取 stderr，按「常见修复手册」处理。

### Step 4 — 启动监控守卫

```bash
.venv\Scripts\python.exe scripts/watch.py --port 8001
```

> **以后台任务运行，持续读取其 stdout。**  
> 每一行是一个 JSON 事件，根据 `event` 字段按「监控响应协议」处理。

---

## 二、监控响应协议（Monitor Loop）

`scripts/watch.py` 会向 stdout 输出 JSON 行事件。你读到每一行后：

### 2.1 `server_down` — 服务器挂了

```json
{"event": "server_down", "port": 8001}
```

**立即执行：**

1. `.venv\Scripts\python.exe -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8001/api/system/status', timeout=3); print('UP')"` 再次确认。
2. 若探活失败：重新执行 Step 3（启动服务器）。
3. 等待 `server_up` 事件确认恢复。

### 2.2 `signal` / `type: crash` — 程序崩溃

```json
{"event": "signal", "type": "crash", "path": "crash_report.json", "content": {...}}
```

**立即执行：**

1. 读 `content.traceback`，定位崩溃文件和行号。
2. 用 `read_file` 查看上下文（前后 20 行）。
3. 修复代码（`replace_string_in_file`）。
4. 删除信号文件：
   ```bash
   del crash_report.json
   ```
5. 重启服务器（按「三、服务器重启流程」）。
6. 在对话中说明：修复了什么 / 重启完成。

### 2.3 `signal` / `type: decision` — 需要决策

```json
{"event": "signal", "type": "decision", "path": "decision_request.json", "content": {...}}
```

**立即执行：**

1. 读 `content.question` 和 `content.options`。
2. 根据 `content.context` 做出最优选择。
3. 写入决策结果文件 `decision_result.json`：
   ```json
   {
     "decision": "<选择的选项>",
     "reason": "<一句话理由>",
     "confidence": "high|medium|low"
   }
   ```
4. 删除请求文件：
   ```bash
   del decision_request.json
   ```

### 2.4 `signal` / `type: stuck` — 执行卡壳

```json
{"event": "signal", "type": "stuck", "path": "stuck_report.json", "content": {...}}
```

**立即执行：**

1. 读 `content.node`（卡在哪个节点）、`content.reason`、`content.attempts`。
2. 分析卡壳原因（逻辑死循环 / 外部调用超时 / 状态机错误）。
3. 按「五、Graph 路由检查清单」修复。
4. 删除信号文件：
   ```bash
   del stuck_report.json
   ```
5. 重启服务器（按「三、服务器重启流程」）。

### 2.5 `signal` / `type: fix_request` — autorun 自我修复请求

```json
{"event": "signal", "type": "fix_request", "path": "fix_request.json", "content": {...}}
```

> 由 `scripts/autorun.py` 写入。表示「验证命令失败，请修复代码直到它通过」。

**立即执行：**

1. 读 `content.goal`（用户目标）和 `content.failure`（验证命令的失败输出）。
2. 根据 `failure` 中的错误信息定位文件和行号，用 `read_file` 查看上下文。
3. 用 `replace_string_in_file` 修复——**只修复导致验证失败的问题，不添加额外功能**。
4. 删除信号文件（这是「修复完成」的信号，autorun 会重新运行验证）：
   ```bash
   del fix_request.json
   ```
5. 等待 autorun 下一次验证结果；若再次出现 fix_request，重复以上步骤。

### 2.6 `heartbeat` — 定期心跳（每 30 秒）

```json
{"event": "heartbeat", "server": "ok"|"down"}
```

- `server: "ok"` → 无需操作。  
- `server: "down"` → 等同于 `server_down`，执行 2.1。

> `fix_request` 循环期间如果也收到 `server_down`，优先处理 `fix_request`（修好代码再重启服务器）。

---

## 三、服务器重启流程（Restart Procedure）

> ⚠️ **重启前必须检查是否有任务正在运行**：
>
> ```bash
> .venv\Scripts\python.exe -c "import urllib.request,json; d=json.loads(urllib.request.urlopen('http://127.0.0.1:8001/api/system/status',timeout=3).read()); print('status:', d['status'])"
> ```
>
> 若输出 `status: running` → **禁止重启**，等任务完成或用户明确指示放弃后再重启。  
> 若输出 `status: idle / completed / failed` → 可以安全重启。

```bash
# 1. 找到占用 8001 端口的 PID
netstat -ano | findstr ":8001"

# 2. 杀掉进程（把 <PID> 替换为实际 PID）
taskkill /PID <PID> /F /T

# 3. 等待端口释放后重新启动（后台）
.venv\Scripts\python.exe -X utf8 -m uvicorn src.web.api:app --port 8001 --host 127.0.0.1
```

> 启动后 **3 秒内**应在 `scripts/watch.py` stdout 看到 `server_up` 事件。  
> 超时未出现 → 检查 stderr，按「四、常见修复手册」诊断。

---

## 四、常见修复手册（Quick Fix Reference）

| 症状 | 原因 | 修复 |
|------|------|------|
| `IndentationError` on startup | 代码缩进被破坏 | `read_file` 对应行，修复缩进 |
| `Failed to start Claude Code` | `--reload` 导致 anyio 跨进程 | 确保启动命令不含 `--reload` |
| `422 Unprocessable Entity` on POST | Pydantic model 定义在函数内 | 移到模块顶层 |
| `Subagent xxx 模板不存在` | `.claude/agents/` 缺模板文件 | 创建对应 `.md` 模板文件 |
| `RuntimeError: Attempted to exit cancel scope in a different task` | 同 `--reload` 问题 | 去掉 `--reload` |
| Graph 死循环（`stuck` 反复出现） | 路由条件未覆盖某 phase | 检查 `src/graph/edges.py` 和 `src/graph/nodes/router.py` |
| `KeyError: 'final_output'` in node | state_update 结构变化 | 用 `.get()` 替代直接 `[]` 取值 |
| 端口 8001 已被占用 | 上次残留进程 | `netstat` 找 PID，`taskkill` 杀掉 |

---

## 五、Graph 路由检查清单

卡壳或死循环时，按顺序检查：

1. **`src/graph/edges.py`** — 条件边函数是否覆盖所有 `phase` 枚举值？
2. **`src/graph/nodes/router.py`** — `phase` 转换逻辑是否有遗漏分支？
3. **`src/graph/state.py`** — `GraphState` 是否有字段名拼写错误？
4. **`src/graph/builder.py`** — 节点注册和边连接是否一致？

---

## 六、关键文件速查

| 文件 | 用途 |
|------|------|
| `src/main.py` | CLI 入口 |
| `src/web/api.py` | FastAPI + WebSocket 服务 |
| `src/utils/claude_communication.py` | 写信号文件的工具函数 |
| `src/agents/sdk_executor.py` | 调用 claude-agent-sdk 执行 subagent |
| `src/agents/caller.py` | Subagent 调用封装 |
| `src/agents/pool_registry.py` | Subagent 模板池 |
| `src/graph/state.py` | GraphState 类型定义 |
| `src/graph/builder.py` | LangGraph 构建（含 MemorySaver） |
| `src/graph/nodes/*.py` | 各节点实现 |
| `scripts/watch.py` | 监控守卫（Claude Code 的「眼睛」） |
| `crash_report.json` | 崩溃信号（触发 2.2） |
| `decision_request.json` | 决策信号（触发 2.3） |
| `decision_result.json` | 决策结果（由 Claude Code 写入） |
| `stuck_report.json` | 卡壳信号（触发 2.4） |
| `fix_request.json` | autorun 修复请求（触发 2.5） |
| `scripts/autorun.py` | 目标驱动的自我修复执行循环 |

---

## 七、Web UI 地址

| 地址 | 说明 |
|------|------|
| `http://localhost:8001` | 主界面 |
| `http://localhost:8001/docs` | FastAPI Swagger 文档 |
| `http://localhost:8001/api/system/status` | 系统状态 JSON |
| `http://localhost:8001/api/tasks` | 任务列表 JSON |

---

## 八、项目初始化（Init / Reset）

### 何时需要初始化

每次**新任务开始前**，或发现项目处于"上次任务残留"状态时，执行初始化：

- `.claude/agents/agent_XX.md` 中仍有上次任务填充的 subagent 内容
- `app_state.json` 中保留旧任务状态
- 根目录存在 `sdk_debug.log`、信号文件等运行时产物

### 初始化命令

```bash
# 正式执行（重置所有动态槽位 + 删除运行时文件）
.venv\Scripts\python.exe scripts/init_project.py

# 仅预览，不修改
.venv\Scripts\python.exe scripts/init_project.py --dry-run
```

### 脚本做了什么

| 操作 | 目标文件 | 说明 |
|------|----------|------|
| 重置为空槽位 | `.claude/agents/agent_*.md` | `name: ""`、`description: ""`，正文替换为"预留-由写手填充" |
| 删除 | `app_state.json` | 任务队列 + 终端日志持久化数据 |
| 删除 | `sdk_debug.log` | SDK 调试日志 |
| 删除 | `crash/decision/stuck_*.json` | 进程间通信信号文件 |

### 不会被重置的文件

- `.claude/agents/coordinator.md` / `planner.md` / `executor.md` / `reviewer.md` / `reflector.md` — 系统核心 agent
- `.claude/agents/writer_*.md` — 写手 agent
- 全部源码 `src/`、配置 `.env` / `requirements.txt`

### Git 提交前检查清单

初始化后，`git status` 应只显示真正改动的源码文件。  
若仍看到 `agent_XX.md` 或 `app_state.json`，先运行初始化脚本再提交。

---

## 附：环境约束

- **Python venv**: `.venv\Scripts\python.exe`（始终用此路径，不用系统 python）
- **端口**: 8001（固定）
- **uvicorn**: 永不加 `--reload`（anyio CancelScope 跨进程限制）
- **模型路由**: 通过用户 glm-5 设置，`ANTHROPIC_API_KEY` 可为空但需存在
