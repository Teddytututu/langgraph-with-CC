// Vue 3 App
const { createApp, ref, computed, onMounted, watch } = Vue;

mermaid.initialize({
    startOnLoad: false,
    theme: 'dark',
    themeVariables: {
        primaryColor: '#8B5CF6',
        primaryTextColor: '#FAFAFA',
        primaryBorderColor: '#8B5CF6',
        lineColor: '#52525B',
        secondaryColor: '#27272A',
        background: '#18181B',
        mainBkg: '#27272A',
        nodeBorder: '#8B5CF6',
    }
});

createApp({
    setup() {
        // State
        const wsConnected = ref(false);
        const systemStatus = ref('idle');
        const currentNode = ref('');
        const tasks = ref([]);
        const selectedTask = ref(null);
        const selectedSubtask = ref(null);
        const discussionMessages = ref([]);
        const discussionParticipants = ref([]);
        const mermaidSvg = ref('');
        const rawMermaid = ref('');  // 缓存后端基础图结构，避免重复请求
        const showNewTask = ref(false);
        const terminalLines = ref([]);
        const terminalInput = ref('');
        const chatMessages = ref([]);
        const chatInput = ref('');
        const chatThinking = ref(false);

        const newTask = ref({ task: '', time_minutes: null });
        const newMessage = ref({ from_agent: 'director', content: '' });
        const interveneText = ref('');

        // Subtask edit state
        const editingSubtask = ref(null);
        const editForm = ref({ title: '', description: '', agent_type: 'coder', priority: 1, estimated_minutes: 10 });

        // Terminal helper
        const termLog = (text, level = 'info', ts = null) => {
            const time = ts || new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
            terminalLines.value.push({ time, text, level });
            if (terminalLines.value.length > 800) terminalLines.value.shift();
            // Auto-scroll
            Vue.nextTick(() => {
                const el = document.getElementById('terminal-output');
                if (el) el.scrollTop = el.scrollHeight;
            });
        };

        // Stats
        const stats = computed(() => ({
            totalTasks: tasks.value.length,
            runningTasks: tasks.value.filter(t => t.status === 'running').length,
            completedTasks: tasks.value.filter(t => t.status === 'completed').length,
            totalSubtasks: tasks.value.reduce((acc, t) => acc + (t.subtasks?.length || 0), 0)
        }));

        const getCompletedSubtasks = computed(() => {
            if (!selectedTask.value?.subtasks) return 0;
            return selectedTask.value.subtasks.filter(s => s.status === 'done' || s.status === 'completed').length;
        });

        // Discussion 面板：当前选中节点的 subagent 列表
        // 来源优先级： assigned_agents → participants → agent_type 兑底
        const discussionAgents = computed(() => {
            const sub = selectedSubtask.value;
            if (!sub) return [{ value: 'user', label: 'User' }];

            const seen = new Set();
            const agents = [];
            const add = (val) => {
                if (val && !seen.has(val)) {
                    seen.add(val);
                    agents.push({ value: val, label: val });
                }
            };

            // 1、当前节点明确分配的 subagent
            (sub.assigned_agents || []).forEach(add);
            // 2、讨论库中已参与的 agent
            discussionParticipants.value.forEach(add);
            // 3、如果不为空就屏蔽默认，否则先添加 agent_type 作为兼容屏蔽
            if (agents.length === 0 && sub.agent_type) add(sub.agent_type);

            // 始终包含 User 选项（供人工介入）
            return [{ value: 'user', label: 'User' }, ...agents];
        });

        // WebSocket
        let ws = null;
        let _wsEverConnected = false;

        const connectWebSocket = () => {
            const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
            ws = new WebSocket(`${protocol}//${window.location.host}/ws`);

            ws.onopen = async () => {
                wsConnected.value = true;
                // 重连后做一次全量同步，拉平断线期间的状态差异
                if (_wsEverConnected) {
                    await fetchTasks();
                    await fetchSystemStatus();
                    await fetchGraph();
                }
                _wsEverConnected = true;
            };
            ws.onclose = () => { wsConnected.value = false; setTimeout(connectWebSocket, 5000); };
            ws.onmessage = (event) => {
                const data = JSON.parse(event.data);
                handleWSMessage(data);
            };
        };

        const handleWSMessage = (data) => {
            const { event, data: payload } = data;

            switch (event) {
                case 'system_status_changed':
                    systemStatus.value = payload.status;
                    termLog(`▶ System → ${payload.status}${ payload.task ? ': '+payload.task.slice(0,60) : '' }`, 'start');
                    fetchGraph();
                    break;
                case 'node_changed':
                    currentNode.value = payload.node;
                    // 不重新请求网络，直接用缓存的原始图重渲染
                    updateGraphRender();
                    break;
                case 'terminal_output':
                    termLog(payload.line, payload.level || 'info', payload.ts);
                    break;
                case 'task_created':
                    if (!tasks.value.find(t => t.id === payload.id)) tasks.value.unshift(payload);
                    termLog(`⊕ 任务创建: ${payload.id}`, 'info');
                    break;
                case 'task_started':
                    mergeTasks([{ id: payload.id, status: 'running' }]);
                    termLog(`▶ 任务启动: ${payload.id}`, 'start');
                    break;
                case 'task_progress':
                    handleTaskProgress(payload);
                    break;
                case 'task_completed':
                    handleTaskCompleted(payload);
                    termLog(`✓ 任务完成: ${payload.id}`, 'success');
                    break;
                case 'task_failed':
                    mergeTasks([{ id: payload.id, status: 'failed', error: payload.error }]);
                    termLog(`✗ 任务失败: ${payload.error}`, 'error');
                    break;
                case 'task_intervened': {
                    const t = tasks.value.find(t => t.id === payload.task_id);
                    if (t) {
                        if (!t.interventions) t.interventions = [];
                        t.interventions.push({ content: payload.instruction, timestamp: payload.timestamp });
                    }
                    termLog(`⚡ [USER] $ ${payload.instruction}`, 'input');
                    break;
                }
                case 'task_intervention_applied':
                    termLog(`⚡ 已注入 ${payload.instructions?.length || 1} 条指令`, 'input');
                    break;
                case 'discussion_message': {
                    if (
                        selectedTask.value?.id === payload.task_id &&
                        selectedSubtask.value?.id === payload.node_id
                    ) {
                        const exists = discussionMessages.value.find(m => m.id === payload.message?.id);
                        if (!exists) discussionMessages.value.push(payload.message);
                    }
                    termLog(`💬 [${payload.node_id}] ${payload.message?.content?.slice(0,60)}`, 'info');
                    break;
                }
                case 'chat_reply': {
                    const ts = payload.ts ? new Date(payload.ts).toLocaleTimeString() : new Date().toLocaleTimeString();
                    chatMessages.value.push({ role: 'assistant', content: payload.content, time: ts });
                    chatThinking.value = false;
                    Vue.nextTick(() => {
                        const el = document.getElementById('chat-messages');
                        if (el) el.scrollTop = el.scrollHeight;
                    });
                    break;
                }
            }
        };

        // Merge partial task updates in-place (preserves Vue reactivity / selectedTask ref)
        const mergeTasks = (updates) => {
            updates.forEach(update => {
                const task = tasks.value.find(t => t.id === update.id);
                if (task) Object.assign(task, update);
            });
        };

        const handleTaskProgress = (payload) => {
            const task = tasks.value.find(t => t.id === payload.task_id);
            if (!task) return;
            if (payload.subtasks) task.subtasks = payload.subtasks;
            if (payload.result) task.result = payload.result;
        };

        const handleTaskCompleted = (payload) => {
            const task = tasks.value.find(t => t.id === payload.id);
            if (!task) return;
            task.status = 'completed';
            if (payload.result !== undefined) task.result = payload.result;
            if (payload.subtasks) task.subtasks = payload.subtasks;
            fetchGraph();
        };

        // Fetch all tasks from API, merge in-place to keep object references stable
        const fetchTasks = async () => {
            try {
                const res = await fetch('/api/tasks');
                const data = await res.json();
                const incoming = data.tasks || [];

                // Add new tasks, update existing ones in-place
                incoming.forEach(newT => {
                    const existing = tasks.value.find(t => t.id === newT.id);
                    if (existing) {
                        Object.assign(existing, newT);
                    } else {
                        tasks.value.push(newT);
                    }
                });

                // Remove tasks that no longer exist on server
                const incomingIds = new Set(incoming.map(t => t.id));
                tasks.value = tasks.value.filter(t => incomingIds.has(t.id));
            } catch (e) {
                console.warn('fetchTasks error', e);
            }
        };

        // 渲染计数器，每次渲染用唯一 ID 防止 Mermaid 内部缓存污染
        let _renderSeq = 0;

        // 根据当前活跃节点向原始图注入 classDef 高亮并渲染
        const updateGraphRender = async () => {
            if (!rawMermaid.value) return;
            let mStr = rawMermaid.value;

            // 基础样式：统一节点外观
            mStr += '\nclassDef default fill:#252526,stroke:#444,stroke-width:2px,color:#ddd;';
            // 活跃节点：紫色发光
            mStr += '\nclassDef active fill:#6c63ff,stroke:#fff,stroke-width:4px,color:#fff,filter:drop-shadow(0 0 10px rgba(108,99,255,0.8));';

            if (currentNode.value) {
                mStr += `\nclass ${currentNode.value} active;`;
            }

            try {
                const id = 'graph-render-' + (++_renderSeq);
                const { svg } = await mermaid.render(id, mStr);
                mermaidSvg.value = svg;
            } catch (e) {
                console.error('Mermaid render error:', e);
            }
        };

        // 拉取图结构（只在结构真正变化时请求网络）
        let _lastRawMermaid = '';
        const fetchGraph = async () => {
            try {
                const res = await fetch('/api/graph/mermaid');
                if (!res.ok) return;
                const data = await res.json();
                // 结构未变则只重渲染高亮，不替换 rawMermaid
                if (data.mermaid === _lastRawMermaid) {
                    await updateGraphRender();
                    return;
                }
                _lastRawMermaid = data.mermaid;
                rawMermaid.value = data.mermaid;
                await updateGraphRender();
            } catch (e) {
                console.error('fetchGraph error', e);
            }
        };

        // 监听节点变化，实时闪烁高亮
        watch(currentNode, (newNode, oldNode) => {
            if (newNode !== oldNode) updateGraphRender();
        });

        let _terminalRestored = false;
        const fetchSystemStatus = async (restoreTerminal = false) => {
            try {
                const res = await fetch('/api/system/status');
                const data = await res.json();
                systemStatus.value = data.status;
                currentNode.value = data.current_node || '';
                // 刷新后一次性恢复终端日志
                if (restoreTerminal && !_terminalRestored && data.terminal_log?.length) {
                    _terminalRestored = true;
                    terminalLines.value = [];
                    data.terminal_log.forEach(e => termLog(e.line, e.level || 'info', e.ts));
                }
            } catch (e) {
                console.warn('fetchSystemStatus error', e);
            }
        };

        const createTask = async () => {
            if (!newTask.value.task.trim()) return;
            const res = await fetch('/api/tasks', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(newTask.value)
            });
            const data = await res.json();
            showNewTask.value = false;
            newTask.value = { task: '', time_minutes: null };
            // API now auto-starts; select the task immediately
            if (!tasks.value.find(t => t.id === data.id)) tasks.value.unshift(data);
            selectedTask.value = tasks.value.find(t => t.id === data.id) || data;
            termLog(`⊕ 提交任务 ${data.id} 并自动启动`, 'start');
        };

        const sendTerminalCmd = () => {
            if (!terminalInput.value.trim()) return;
            const task_id = selectedTask.value?.id || app_state_task_id;
            if (ws && ws.readyState === WebSocket.OPEN) {
                ws.send(JSON.stringify({
                    type: 'terminal_input',
                    task_id: task_id || '',
                    command: terminalInput.value.trim(),
                }));
            }
            terminalInput.value = '';
        };

        const clearTerminal = () => { terminalLines.value = []; };

        const sendChat = async () => {
            const msg = chatInput.value.trim();
            if (!msg || chatThinking.value) return;
            chatInput.value = '';

            const now = new Date().toLocaleTimeString();
            chatMessages.value.push({ role: 'user', content: msg, time: now });
            chatThinking.value = true;

            Vue.nextTick(() => {
                const el = document.getElementById('chat-messages');
                if (el) el.scrollTop = el.scrollHeight;
            });

            const history = chatMessages.value.slice(-9, -1).map(m => ({ role: m.role, content: m.content }));

            // 立即发送，不等待回复（回复通过 WebSocket chat_reply 事件推送）
            try {
                await fetch('/api/chat', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ message: msg, history }),
                });
            } catch (e) {
                chatThinking.value = false;
                chatMessages.value.push({ role: 'assistant', content: `请求失败: ${e.message}`, time: new Date().toLocaleTimeString() });
            }
            // chatThinking 由 WS chat_reply 事件处理器关闭
        };

        const selectTask = async (task) => {
            selectedTask.value = task;
            selectedSubtask.value = null;
            discussionMessages.value = [];
            // Refresh from API to ensure result/subtasks are up to date
            try {
                const res = await fetch(`/api/tasks/${task.id}`);
                if (!res.ok) throw new Error(`HTTP ${res.status}`);
                const fresh = await res.json();
                Object.assign(task, fresh);
            } catch (e) {
                console.error('selectTask: failed to refresh task', task.id, e);
            }
        };

        const selectSubtask = async (subtask) => {
            selectedSubtask.value = subtask;
            // 自动将发言者切换为当前节点第一个已分配的 agent（没有则留在 user）
            newMessage.value.from_agent =
                subtask.assigned_agents?.[0] || subtask.agent_type || 'user';
            discussionParticipants.value = [];
            if (selectedTask.value) {
                try {
                    const res = await fetch(`/api/tasks/${selectedTask.value.id}/nodes/${subtask.id}/discussion`);
                    const data = await res.json();
                    discussionMessages.value = data.messages || [];
                    discussionParticipants.value = data.participants || [];
                    // 如果记录到了更多参与者，刷新默选
                    if (discussionParticipants.value.length > 0 &&
                        !subtask.assigned_agents?.length) {
                        newMessage.value.from_agent = discussionParticipants.value[0];
                    }
                } catch (e) {
                    console.error('selectSubtask: failed to load discussion', subtask.id, e);
                    discussionMessages.value = [];
                }
            }
        };

        const sendMessage = async () => {
            if (!newMessage.value.content.trim() || !selectedSubtask.value) return;
            const res = await fetch(`/api/tasks/${selectedTask.value.id}/nodes/${selectedSubtask.value.id}/discussion`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(newMessage.value)
            });
            const saved = await res.json();
            // Optimistically add to local list (WS event may also arrive)
            if (saved?.id && !discussionMessages.value.find(m => m.id === saved.id)) {
                discussionMessages.value.push(saved);
            }
            newMessage.value.content = '';
        };

        const openEditSubtask = (subtask) => {
            editingSubtask.value = subtask;
            editForm.value = {
                title: subtask.title || '',
                description: subtask.description || '',
                agent_type: subtask.agent_type || 'coder',
                priority: subtask.priority || 1,
                estimated_minutes: subtask.estimated_minutes || 10,
            };
        };

        const saveSubtask = async () => {
            if (!editingSubtask.value || !selectedTask.value) return;
            const res = await fetch(
                `/api/tasks/${selectedTask.value.id}/subtasks/${editingSubtask.value.id}`,
                {
                    method: 'PATCH',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(editForm.value),
                }
            );
            if (res.ok) {
                const updated = await res.json();
                Object.assign(editingSubtask.value, updated);
            }
            editingSubtask.value = null;
        };

        const intervene = async () => {
            if (!interveneText.value.trim() || !selectedTask.value) return;
            await fetch(`/api/tasks/${selectedTask.value.id}/intervene`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ instruction: interveneText.value.trim() })
            });
            interveneText.value = '';
        };

        // Utils
        const getStatusText = (s) => ({ idle: 'Idle', running: 'Running', completed: 'Done', failed: 'Failed' }[s] || s);
        const formatTime = (t) => t ? new Date(t).toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit' }) : '';
        const renderMd = (text) => {
            if (!text) return '';
            try { return marked.parse(text, { breaks: true, gfm: true }); }
            catch (e) { return text; }
        };

        onMounted(async () => {
            connectWebSocket();
            await fetchTasks();
            await fetchSystemStatus(true);  // true = 恢复终端日志
            fetchGraph();

            // 刷新后自动选中正在运行的任务，否则选最新任务
            if (!selectedTask.value && tasks.value.length) {
                const running = tasks.value.find(t => t.status === 'running');
                selectedTask.value = running || tasks.value[0];
            }

            if (!_terminalRestored) termLog('System ready. Waiting for tasks…', 'info');

            // 轮询仅作为 WebSocket 断线时的降级方案
            // WS 连接正常时由事件驱动，不产生冗余请求
            setInterval(async () => {
                if (wsConnected.value) return;   // WS 正常 → 跳过
                console.warn('[Polling] WS disconnected, falling back to HTTP poll');
                await fetchSystemStatus();
                await fetchTasks();
                await fetchGraph();
            }, 5000);
        });

        return {
            wsConnected, systemStatus, currentNode, tasks, selectedTask, selectedSubtask,
            discussionMessages, discussionParticipants, mermaidSvg, showNewTask, newTask, newMessage,
            terminalLines, terminalInput, editingSubtask, editForm, interveneText,
            chatMessages, chatInput, chatThinking,
            stats, getCompletedSubtasks, discussionAgents,
            createTask, selectTask, selectSubtask, sendMessage, intervene, getStatusText, formatTime, renderMd,
            fetchGraph, openEditSubtask, saveSubtask, sendTerminalCmd, clearTerminal, sendChat,
        };
    }
}).mount('#app');
