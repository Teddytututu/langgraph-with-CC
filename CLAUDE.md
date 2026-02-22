# Claude Code 操作手册

> **阅读本文件的是 Claude Code。**  
> 本文件是你的任务指令。按照下方流程依次执行，不要跳过任何步骤。

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
# Windows
netstat -ano | findstr ":8001" | findstr LISTENING
```

- **有输出** → 服务器已运行，跳到 Step 4。  
- **无输出** → 继续 Step 3。

### Step 3 — 启动 Web 服务器

```bash
# 必须用 venv Python，必须不加 --reload（anyio CancelScope 限制）
.venv\Scripts\python.exe -m uvicorn src.web.api:app --port 8001
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

1. `netstat -ano | findstr ":8001"` 再次确认。
2. 若确实挂了：重新执行 Step 3（启动服务器）。
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

### 2.5 `heartbeat` — 定期心跳（每 30 秒）

```json
{"event": "heartbeat", "server": "ok"|"down"}
```

- `server: "ok"` → 无需操作。  
- `server: "down"` → 等同于 `server_down`，执行 2.1。

---

## 三、服务器重启流程（Restart Procedure）

```bash
# 1. 找到占用 8001 端口的 PID
netstat -ano | findstr ":8001"

# 2. 杀掉进程（把 <PID> 替换为实际 PID）
taskkill /PID <PID> /F /T

# 3. 等待端口释放后重新启动（后台）
.venv\Scripts\python.exe -m uvicorn src.web.api:app --port 8001
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

---

## 七、Web UI 地址

| 地址 | 说明 |
|------|------|
| `http://localhost:8001` | 主界面 |
| `http://localhost:8001/docs` | FastAPI Swagger 文档 |
| `http://localhost:8001/api/system/status` | 系统状态 JSON |
| `http://localhost:8001/api/tasks` | 任务列表 JSON |

---

## 附：环境约束

- **Python venv**: `.venv\Scripts\python.exe`（始终用此路径，不用系统 python）
- **端口**: 8001（固定）
- **uvicorn**: 永不加 `--reload`（anyio CancelScope 跨进程限制）
- **模型路由**: 通过用户 glm-5 设置，`ANTHROPIC_API_KEY` 可为空但需存在
