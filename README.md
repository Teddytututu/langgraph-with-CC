# langgraph-with-CC

> **LangGraph + Claude Agent SDK 驱动的多 Subagent 自动任务执行器**  
> 用自然语言描述任务，系统自动规划、拆解、调度、审查，最终输出完整结果。  
> 支持命令行直接使用，以及浏览器 Web 界面实时监控。

---

## 环境要求

| 依赖 | 版本 | 说明 |
|------|------|------|
| Python | 3.10+ | 推荐 3.13 |
| claude-agent-sdk | 0.1.39+ | `pip install claude-agent-sdk` |
| Claude Code CLI | 2.1.50+ | [安装说明](https://claude.ai/code) |
| ANTHROPIC_API_KEY | — | 或 ANTHROPIC_AUTH_TOKEN，需在环境变量中设置 |

---

## 安装

```bash
# 1. 克隆仓库
git clone <repo-url>
cd excuter

# 2. 创建虚拟环境
python -m venv .venv

# Windows
.venv\Scripts\activate

# macOS / Linux
source .venv/bin/activate

# 3. 安装依赖
pip install -r requirements.txt   # 或手动安装下列包
pip install langgraph langchain fastapi uvicorn pydantic httpx
pip install claude-agent-sdk==0.1.39

# 4. 设置 API Key
# Windows PowerShell
$env:ANTHROPIC_API_KEY = "sk-ant-..."

# macOS / Linux
export ANTHROPIC_API_KEY="sk-ant-..."
```

> **提示**：如果你使用 GLM 或其他自定义路由模型，只需确保 `~/.claude/settings.json` 中已配置好路由规则，SDK 会自动加载。

---

## 方式一：命令行使用

直接运行，输入任务描述，程序自动完成所有步骤：

```bash
python -m src.main "写一个冒泡排序的 Python 函数，并附带单元测试"
```

### 带时间预算

```bash
python -m src.main "用 pygame 写一个贪吃蛇游戏，保存为 snake.py" --time 20
```

### 运行效果

```
[router]   分析任务类型...
[planner]  拆解子任务：["设计游戏逻辑", "实现渲染", "处理输入"]
[budget]   时间预算：20 分钟
[executor] 执行子任务 1/3: 设计游戏逻辑 ✓
[executor] 执行子任务 2/3: 实现渲染 ✓
[executor] 执行子任务 3/3: 处理输入 ✓
[reviewer] 代码审查通过
✅ 完成！结果已保存到工作目录
```

---

## 方式二：Web 界面使用

### 启动服务器

```bash
# ⚠️ 不要加 --reload（会破坏 SDK 的 anyio 取消域，导致任务失败）
uvicorn src.web.api:app --port 8000
```

浏览器打开 **http://localhost:8000**

### Web 界面功能

#### 1. 创建任务

点击右上角 **`+`** 按钮，填写：
- **Description**：任务描述（自然语言，越详细越好）
- **Time Budget**：时间预算（分钟，可留空表示不限）

点击 **Create** 后任务立即开始执行。

#### 2. 实时监控

左侧任务列表显示所有任务及状态：

| 状态 | 含义 |
|------|------|
| `running` | 正在执行 |
| `completed` | 执行成功 |
| `failed` | 执行失败 |

点击任意任务可查看：
- **Workflow 图**：Mermaid 实时渲染当前 LangGraph 执行流程
- **Progress**：子任务列表及每个子任务的执行状态
- **Output**：任务完成后的最终输出结果

#### 3. 查看子任务讨论

在 Progress 面板中点击某个子任务，右侧 Discussion 面板会显示该子任务的 Agent 间通信记录，也可以手动发送消息介入。

#### 4. 活动日志

页面下方 Activity 面板实时滚动显示系统事件（节点切换、任务完成等）。

---

## 示例任务

以下任务均已验证可以跑通：

```
写一个 Hello World 程序
```

```
用 Python 实现斐波那契数列，要求：递归版 + 动态规划版，并写测试
```

```
写一个鱼群模拟 pygame 游戏（Boids 算法），包含分离/对齐/聚合三大规则，
鼠标点击添加食物，深蓝色背景，保存为 fish_swarm.py
```

```
分析当前目录的 Python 代码结构，生成一份架构文档
```

---

## 项目结构

```
excuter/
├── src/
│   ├── main.py              # CLI 入口
│   ├── graph/               # LangGraph 编排层
│   │   ├── state.py         # GraphState 全局状态定义
│   │   ├── builder.py       # 静态 6 节点 Graph
│   │   ├── edges.py         # 条件路由逻辑
│   │   └── nodes/           # 6 个节点实现
│   │       ├── router.py    # 分析任务，决定执行路径
│   │       ├── planner.py   # 拆解子任务列表
│   │       ├── budget.py    # 时间预算管理
│   │       ├── executor.py  # 并发执行子任务
│   │       ├── reviewer.py  # 质量检查
│   │       └── reflector.py # 失败反思 + 改进建议
│   ├── agents/              # Subagent 执行层
│   │   ├── caller.py        # 统一调用接口
│   │   ├── sdk_executor.py  # Claude Agent SDK 封装
│   │   └── pool_registry.py # .claude/agents/*.md 模板管理
│   ├── discussion/          # Agent 间讨论系统
│   └── web/                 # Web 界面
│       ├── api.py           # FastAPI 路由 + WebSocket
│       └── static/          # Vue 3 前端
│           ├── index.html
│           ├── js/app.js
│           └── css/style.css
├── .claude/agents/          # Subagent 身份模板（48 个槽位）
│   ├── planner.md
│   ├── executor.md
│   ├── reviewer.md
│   ├── agent_01.md ~ agent_40.md
│   └── ...
├── report/                  # 设计文档
│   ├── DESIGN_PHILOSOPHY.md # 设计理念报告（主文档）
│   └── ...
└── scripts/
    └── poll_task.py         # 任务状态轮询工具
```

---

## API 参考

服务启动后，访问 **http://localhost:8000/docs** 查看完整 Swagger 文档。

常用接口：

```
POST   /api/tasks              创建任务
POST   /api/tasks/{id}/start   启动任务
GET    /api/tasks              列出所有任务
GET    /api/tasks/{id}         查询任务详情（含 result 字段）
GET    /api/system/status      系统状态
GET    /api/graph/mermaid      当前 Graph 的 Mermaid 语法
WS     /ws                     WebSocket 实时事件推送
```

---

## 已知限制

- **串行执行**：当前一次只能运行一个任务（第二个任务会排队等待）
- **无持久化**：服务重启后任务历史清空（任务结果文件仍保留在磁盘）
- **`--reload` 禁用**：uvicorn 不能使用 `--reload`，否则 anyio 取消域跨进程失效导致 SDK 启动失败

---

## 设计文档

详见 [report/DESIGN_PHILOSOPHY.md](report/DESIGN_PHILOSOPHY.md)，涵盖：
- 六大设计哲学
- 关键技术决策及理由
- 从"能跑"到"能用"的 18 个 Bug 修复历程
- 系统演化路径