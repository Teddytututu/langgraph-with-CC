// Vue 3 App
const { createApp, ref, computed, onMounted, watch } = Vue;

mermaid.initialize({
    startOnLoad: false,
    theme: 'dark',
    themeVariables: {
        // Base palette
        primaryColor: '#2a2a35',
        primaryTextColor: '#e4e4e7',
        primaryBorderColor: '#52525b',
        secondaryColor: '#1f1f27',
        tertiaryColor: '#3a3a46',
        // Lines / edges
        lineColor: '#6b7280',
        edgeLabelBackground: '#18181b',
        // Background
        background: '#18181b',
        mainBkg: '#27272a',
        // Typography
        fontFamily: '"Inter", ui-sans-serif, system-ui, sans-serif',
        fontSize: '14px',
        // Note/label
        noteBkgColor: '#3f3f46',
        noteTextColor: '#d4d4d8',
        notesBorderColor: '#52525b',
    },
    flowchart: {
        htmlLabels: true,
        padding: 36,
        nodeSpacing: 140,
        rankSpacing: 160,
        curve: 'basis',
        diagramMarginX: 72,
        diagramMarginY: 72,
        useMaxWidth: false,
        arrowMarkerAbsolute: true,
    },
});

createApp({
    setup() {
        // State
        const wsConnected = ref(false);
        const systemStatus = ref('idle');
        const currentTaskId = ref('');
        const currentNode = ref('');
        const tasks = ref([]);
        const selectedTask = ref(null);
        const selectedSubtask = ref(null);
        const discussionMessages = ref([]);
        const discussionParticipants = ref([]);
        const discussionCacheByNode = ref({});
        const discussionStatus = ref({
            taskId: '',
            nodeId: '',
            nodeTitle: '',
            nodeStatus: 'idle',
            phase: '',
            lastUpdated: '',
            messagesCount: 0,
            participantCount: 0,
            latestSpeaker: '',
            interventionsQueued: 0,
            interventionsApplied: 0,
        });
        const statusTimeline = ref([]);
        const timelineScope = ref('selected');
        const STATUS_TIMELINE_LIMIT = 300;
        const timelineIdSet = new Set();
        const lastSystemStatusForTimeline = ref('');
        const mermaidSvg = ref('');
        const graphZoom = ref(1);
        const graphPanX = ref(0);
        const graphPanY = ref(0);
        const rawMermaid = ref('');  // 缓存后端基础图结构，避免重复请求
        const showNewTask = ref(false);
        const terminalLines = ref([]);
        const terminalInput = ref('');
        const chatMessages = ref([]);
        const chatInput = ref('');
        const chatThinking = ref(false);
        const lastStateRev = ref(0);

        const parseTimestampToMs = (value) => {
            if (value === null || value === undefined || value === '') return Date.now();
            if (typeof value === 'number' && Number.isFinite(value)) {
                return value < 1e12 ? value * 1000 : value;
            }
            const text = String(value).trim();
            if (!text) return Date.now();

            if (/^\d{1,2}:\d{2}(:\d{2})?$/.test(text)) {
                const [h, m, s = '0'] = text.split(':').map(n => Number(n));
                if ([h, m, s].every(n => Number.isFinite(n))) {
                    const now = new Date();
                    now.setHours(h, m, s, 0);
                    return now.getTime();
                }
            }

            const parsed = Date.parse(text);
            return Number.isNaN(parsed) ? Date.now() : parsed;
        };

        const getPayloadStateRev = (payload) => {
            const rev = Number(payload?.state_rev);
            return Number.isFinite(rev) ? rev : null;
        };

        const shouldApplyStatePayload = (payload) => {
            const incomingRev = getPayloadStateRev(payload);
            if (incomingRev === null) return true;
            if (incomingRev < lastStateRev.value) return false;
            if (incomingRev > lastStateRev.value) {
                lastStateRev.value = incomingRev;
            }
            return true;
        };

        const formatTimestampHHMMSS = (value) => {
            if (value === null || value === undefined || value === '') return '';
            const tsMs = parseTimestampToMs(value);
            return new Date(tsMs).toLocaleTimeString('zh-CN', {
                hour12: false,
                hour: '2-digit',
                minute: '2-digit',
                second: '2-digit',
            });
        };

        const buildTimelineId = ({ sourceEvent, taskId, node, kind, title, detail }) => {
            return [sourceEvent || '', taskId || '', node || '', kind || '', title || '', detail || ''].join('|');
        };

        const addTimelineItem = ({
            id,
            ts,
            taskId = '',
            node = '',
            kind = 'milestone',
            level = 'info',
            title = '',
            detail = '',
            sourceEvent = '',
        }) => {
            const itemId = id || buildTimelineId({ sourceEvent, taskId, node, kind, title, detail });
            if (timelineIdSet.has(itemId)) return;

            const tsMs = parseTimestampToMs(ts);
            const item = {
                id: itemId,
                tsMs,
                taskId,
                node,
                kind,
                level,
                title,
                detail,
                sourceEvent,
            };

            statusTimeline.value.unshift(item);
            timelineIdSet.add(itemId);

            if (statusTimeline.value.length > STATUS_TIMELINE_LIMIT) {
                const removed = statusTimeline.value.splice(STATUS_TIMELINE_LIMIT);
                removed.forEach(evt => timelineIdSet.delete(evt.id));
            }
        };

        const filteredTimeline = computed(() => {
            const mode = timelineScope.value;
            const selectedId = selectedTask.value?.id;
            const list = !selectedId || mode === 'all'
                ? statusTimeline.value
                : statusTimeline.value.filter(evt => evt.taskId === selectedId);

            return [...list].sort((a, b) => b.tsMs - a.tsMs);
        });

        const newTask = ref({ task: '', time_minutes: null });
        const newMessage = ref({ from_agent: 'user', content: '' });
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
            totalSubtasks: selectedTask.value?.subtasks?.length || 0
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

        const refreshDiscussionStatusFromSelection = () => {
            const sub = selectedSubtask.value;
            const task = selectedTask.value;
            const latestMessage = discussionMessages.value[discussionMessages.value.length - 1];

            discussionStatus.value = {
                ...discussionStatus.value,
                taskId: task?.id || '',
                nodeId: sub?.id || '',
                nodeTitle: sub?.title || '',
                nodeStatus: sub?.status || 'idle',
                messagesCount: discussionMessages.value.length,
                participantCount: discussionParticipants.value.length,
                latestSpeaker: latestMessage?.from_agent || '',
            };
        };

        const discussionCacheKey = (taskId, nodeId) => `${taskId || ''}::${nodeId || ''}`;

        const mergeDiscussionMessages = (baseMessages = [], extraMessages = []) => {
            const merged = [];
            const seen = new Set();
            [...baseMessages, ...extraMessages].forEach(msg => {
                const id = msg?.id;
                if (id && seen.has(id)) return;
                if (id) seen.add(id);
                merged.push(msg);
            });
            return merged;
        };

        const upsertDiscussionCache = (taskId, nodeId, incomingMessage = null, participants = null) => {
            const key = discussionCacheKey(taskId, nodeId);
            const current = discussionCacheByNode.value[key] || { messages: [], participants: [] };
            const next = {
                messages: current.messages.slice(),
                participants: current.participants.slice(),
            };

            if (incomingMessage) {
                if (!next.messages.find(m => m.id === incomingMessage.id)) {
                    next.messages.push(incomingMessage);
                }
                if (incomingMessage.from_agent && !next.participants.includes(incomingMessage.from_agent)) {
                    next.participants.push(incomingMessage.from_agent);
                }
                (incomingMessage.to_agents || []).forEach(agent => {
                    if (agent && !next.participants.includes(agent)) next.participants.push(agent);
                });
            }

            if (Array.isArray(participants)) {
                participants.forEach(agent => {
                    if (agent && !next.participants.includes(agent)) next.participants.push(agent);
                });
            }

            discussionCacheByNode.value[key] = next;
            return next;
        };
        let ws = null;
        let _wsEverConnected = false;

        const connectWebSocket = () => {
            const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
            ws = new WebSocket(`${protocol}//${window.location.host}/ws`);

            ws.onopen = async () => {
                wsConnected.value = true;
                // 重连后做一次全量同步，拉平断线期间的状态差异
                if (_wsEverConnected) {
                    await fetchSystemStatus();
                    await fetchTasks();
                    scheduleGraphRefresh(0);
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
            const event = data?.event;
            const payload = data?.data || {};

            switch (event) {
                case 'system_status_changed': {
                    if (!shouldApplyStatePayload(payload)) break;
                    systemStatus.value = payload.status;
                    if (payload.current_node !== undefined) {
                        currentNode.value = payload.current_node || '';
                    }
                    if (payload.current_task_id !== undefined) {
                        currentTaskId.value = payload.current_task_id || '';
                    }
                    const prev = lastSystemStatusForTimeline.value;
                    if (payload.status && payload.status !== prev) {
                        addTimelineItem({
                            id: `system|${payload.status}`,
                            ts: payload.ts,
                            taskId: payload.task_id || '',
                            node: payload.node || '',
                            kind: 'milestone',
                            level: payload.status === 'failed' ? 'error' : 'info',
                            title: `系统状态 ${payload.status}`,
                            detail: payload.task ? `任务: ${payload.task}` : '',
                            sourceEvent: 'system_status_changed',
                        });
                        lastSystemStatusForTimeline.value = payload.status;
                    }
                    termLog(`▶ System → ${payload.status}${payload.task ? ': ' + payload.task.slice(0, 60) : ''}`, 'start');
                    scheduleGraphRefresh();
                    break;
                }
                case 'node_changed': {
                    if (!shouldApplyStatePayload(payload)) break;
                    currentNode.value = payload.node;
                    scheduleGraphRefresh();
                    break;
                }
                case 'terminal_output':
                    termLog(payload.line, payload.level || 'info', payload.ts);
                    if (
                        (payload.level === 'warn' || payload.level === 'error') &&
                        selectedTask.value?.id &&
                        payload.task_id === selectedTask.value.id
                    ) {
                        addTimelineItem({
                            id: `terminal|${payload.task_id}|${payload.level}|${payload.ts}|${payload.line || ''}`,
                            ts: payload.ts,
                            taskId: payload.task_id,
                            node: payload.node || '',
                            kind: 'error',
                            level: payload.level,
                            title: payload.level === 'error' ? '终端错误' : '终端警告',
                            detail: (payload.line || '').slice(0, 160),
                            sourceEvent: 'terminal_output',
                        });
                    }
                    break;
                case 'task_created':
                    if (!shouldApplyStatePayload(payload)) break;
                    if (!tasks.value.find(t => t.id === payload.id)) tasks.value.unshift(payload);
                    reports.value = [];
                    activeReport.value = '';
                    activeReportContent.value = '';
                    termLog(`⊕ 任务创建: ${payload.id}`, 'info');
                    break;
                case 'task_started':
                    if (!shouldApplyStatePayload(payload)) break;
                    mergeTasks([{ id: payload.id, status: 'running' }]);
                    addTimelineItem({
                        id: `task_started|${payload.id}|${payload.ts || ''}`,
                        ts: payload.ts,
                        taskId: payload.id,
                        node: payload.node || '',
                        kind: 'milestone',
                        level: 'start',
                        title: '任务开始执行',
                        detail: payload.id,
                        sourceEvent: 'task_started',
                    });
                    termLog(`▶ 任务启动: ${payload.id}`, 'start');
                    refreshDiscussionStatusFromSelection();
                    break;
                case 'task_progress':
                    handleTaskProgress(payload);
                    break;
                case 'task_completed':
                    if (!shouldApplyStatePayload(payload)) break;
                    handleTaskCompleted(payload);
                    addTimelineItem({
                        id: `task_completed|${payload.id}|${payload.finished_at || payload.ts || ''}`,
                        ts: payload.finished_at || payload.ts,
                        taskId: payload.id,
                        node: payload.node || '',
                        kind: 'milestone',
                        level: 'success',
                        title: '任务已完成',
                        detail: payload.id,
                        sourceEvent: 'task_completed',
                    });
                    termLog(`✓ 任务完成: ${payload.id}`, 'success');
                    break;
                case 'task_failed':
                    if (!shouldApplyStatePayload(payload)) break;
                    mergeTasks([{ id: payload.id, status: 'failed', error: payload.error }]);
                    addTimelineItem({
                        id: `task_failed|${payload.id}|${payload.ts || ''}|${payload.error || ''}`,
                        ts: payload.ts,
                        taskId: payload.id,
                        node: payload.node || '',
                        kind: 'error',
                        level: 'error',
                        title: '任务执行失败',
                        detail: payload.error || '',
                        sourceEvent: 'task_failed',
                    });
                    termLog(`✗ 任务失败: ${payload.error}`, 'error');
                    refreshDiscussionStatusFromSelection();
                    break;
                case 'task_intervened': {
                    const t = tasks.value.find(t => t.id === payload.task_id);
                    if (t) {
                        if (!t.interventions) t.interventions = [];
                        t.interventions.push({ content: payload.instruction, timestamp: payload.timestamp });
                    }
                    if (selectedTask.value?.id === payload.task_id) {
                        discussionStatus.value = {
                            ...discussionStatus.value,
                            interventionsQueued: (discussionStatus.value.interventionsQueued || 0) + 1,
                            lastUpdated: payload.timestamp || new Date().toISOString(),
                        };
                    }
                    addTimelineItem({
                        id: `task_intervened|${payload.task_id}|${payload.timestamp || ''}|${payload.instruction || ''}`,
                        ts: payload.timestamp,
                        taskId: payload.task_id || '',
                        node: payload.node_id || '',
                        kind: 'intervention',
                        level: 'warn',
                        title: '干预已排队',
                        detail: (payload.instruction || '').slice(0, 120),
                        sourceEvent: 'task_intervened',
                    });
                    if (payload.echoed_to_terminal !== true) {
                        termLog(`⚡ [USER] $ ${payload.instruction}`, 'input');
                    }
                    break;
                }
                case 'task_intervention_applied':
                    if (selectedTask.value?.id === payload.task_id || !payload.task_id) {
                        const applied = payload.instructions?.length || 1;
                        discussionStatus.value = {
                            ...discussionStatus.value,
                            interventionsApplied: (discussionStatus.value.interventionsApplied || 0) + applied,
                            lastUpdated: payload.ts || new Date().toISOString(),
                        };
                    }
                    addTimelineItem({
                        id: `task_intervention_applied|${payload.task_id || ''}|${payload.ts || ''}|${(payload.instructions || []).join('|')}`,
                        ts: payload.ts,
                        taskId: payload.task_id || selectedTask.value?.id || '',
                        node: payload.node_id || '',
                        kind: 'intervention',
                        level: 'success',
                        title: '干预已应用',
                        detail: `已注入 ${payload.instructions?.length || 1} 条`,
                        sourceEvent: 'task_intervention_applied',
                    });
                    termLog(`⚡ 已注入 ${payload.instructions?.length || 1} 条指令`, 'input');
                    break;
                case 'discussion_message': {
                    const cache = upsertDiscussionCache(
                        payload.task_id,
                        payload.node_id,
                        payload.message,
                    );

                    if (
                        selectedTask.value?.id === payload.task_id &&
                        selectedSubtask.value?.id === payload.node_id
                    ) {
                        const exists = discussionMessages.value.find(m => m.id === payload.message?.id);
                        if (!exists) discussionMessages.value.push(payload.message);
                        discussionParticipants.value = cache.participants.slice();
                        refreshDiscussionStatusFromSelection();
                    }
                    addTimelineItem({
                        id: `discussion_message|${payload.task_id || ''}|${payload.node_id || ''}|${payload.message?.id || ''}`,
                        ts: payload.message?.timestamp,
                        taskId: payload.task_id || '',
                        node: payload.node_id || '',
                        kind: 'milestone',
                        level: 'info',
                        title: `讨论消息 · ${payload.message?.from_agent || 'unknown'}`,
                        detail: (payload.message?.content || '').slice(0, 120),
                        sourceEvent: 'discussion_message',
                    });
                    termLog(`💬 [${payload.node_id}] ${payload.message?.content?.slice(0, 60)}`, 'info');
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
                case 'tasks_cleared':
                    if (!shouldApplyStatePayload(payload)) break;
                    tasks.value = [];
                    selectedTask.value = null;
                    selectedSubtask.value = null;
                    discussionMessages.value = [];
                    discussionParticipants.value = [];
                    discussionCacheByNode.value = {};
                    discussionStatus.value = {
                        taskId: '',
                        nodeId: '',
                        nodeTitle: '',
                        nodeStatus: 'idle',
                        phase: '',
                        lastUpdated: '',
                        messagesCount: 0,
                        participantCount: 0,
                        latestSpeaker: '',
                        interventionsQueued: 0,
                        interventionsApplied: 0,
                    };
                    statusTimeline.value = [];
                    timelineIdSet.clear();
                    lastSystemStatusForTimeline.value = '';
                    terminalLines.value = [];
                    reports.value = [];
                    activeReport.value = '';
                    activeReportContent.value = '';
                    scheduleGraphRefresh();
                    break;
            }
        };

        // Merge partial task updates in-place (preserves Vue reactivity / selectedTask ref)
        const mergeTasks = (updates) => {
            updates.forEach(update => {
                const task = tasks.value.find(t => t.id === update.id);
                if (task) Object.assign(task, update);
            });
        };

        // 防抖定时器（用于减少频繁重绘）
        let _graphDebounceTimer = null;
        let _graphRefreshTimer = null;
        const scheduleGraphRefresh = (delay = 180) => {
            if (_graphRefreshTimer) clearTimeout(_graphRefreshTimer);
            _graphRefreshTimer = setTimeout(() => {
                _graphRefreshTimer = null;
                fetchGraph();
            }, delay);
        };

        const handleTaskProgress = (payload) => {
            const task = tasks.value.find(t => t.id === payload.task_id);
            if (!task) return;

            let totalSubtasks = task.subtasks?.length || 0;
            let completedSubtasks = task.subtasks?.filter(s => s.status === 'done' || s.status === 'completed').length || 0;

            if (payload.subtasks) {
                // 合并子任务数据而不是直接覆盖，保留 description/result 等完整字段
                const incomingMap = new Map(payload.subtasks.map(s => [s.id, s]));
                if (!task.subtasks) task.subtasks = [];

                // 检测是否有实质性变化（新增子任务或状态变化）
                let hasRealChange = false;
                const existingIds = new Set(task.subtasks.map(s => s.id));

                task.subtasks = task.subtasks.map(existing => {
                    const update = incomingMap.get(existing.id);
                    if (update) {
                        // 只检测 status 变化（这是用户可见的关键变化）
                        if (update.status && update.status !== existing.status) {
                            hasRealChange = true;
                        }
                        return { ...existing, ...update };
                    }
                    return existing;
                });

                // 检测新增子任务
                incomingMap.forEach((incoming, id) => {
                    if (!existingIds.has(id)) {
                        hasRealChange = true;
                        task.subtasks.push(incoming);
                    }
                });

                totalSubtasks = task.subtasks.length;
                completedSubtasks = task.subtasks.filter(s => s.status === 'done' || s.status === 'completed').length;

                // 只在状态变化时才重绘图（防抖 500ms，避免频繁刷新导致页面跳动）
                if (hasRealChange) {
                    if (_graphDebounceTimer) clearTimeout(_graphDebounceTimer);
                    _graphDebounceTimer = setTimeout(() => scheduleGraphRefresh(0), 500);
                }
            }

            if (Object.prototype.hasOwnProperty.call(payload, 'result')) {
                task.result = payload.result;
            }
            // Force selectedTask reactivity refresh when subtasks change
            if (payload.subtasks && selectedTask.value?.id === payload.task_id) {
                selectedTask.value = task;
            }

            const phase = payload.phase || payload.current_phase || payload.stage || '';
            if (phase) {
                addTimelineItem({
                    id: `task_progress_phase|${payload.task_id}|${phase}|${payload.updated_at || payload.ts || ''}`,
                    ts: payload.updated_at || payload.ts,
                    taskId: payload.task_id,
                    node: payload.node_id || selectedSubtask.value?.id || '',
                    kind: 'progress',
                    level: 'running',
                    title: `Phase -> ${phase}`,
                    detail: totalSubtasks ? `子任务完成 ${completedSubtasks}/${totalSubtasks}` : '',
                    sourceEvent: 'task_progress',
                });
            }

            if (totalSubtasks > 0) {
                addTimelineItem({
                    id: `task_progress_ratio|${payload.task_id}|${completedSubtasks}|${totalSubtasks}`,
                    ts: payload.updated_at || payload.ts,
                    taskId: payload.task_id,
                    node: payload.node_id || selectedSubtask.value?.id || '',
                    kind: 'progress',
                    level: 'info',
                    title: `子任务完成 ${completedSubtasks}/${totalSubtasks}`,
                    detail: phase ? `Phase -> ${phase}` : '',
                    sourceEvent: 'task_progress',
                });
            }

            const selectedNode = selectedSubtask.value
                ? task.subtasks?.find(s => s.id === selectedSubtask.value.id)
                : null;
            if (selectedNode) {
                selectedSubtask.value = selectedNode;
                discussionStatus.value = {
                    ...discussionStatus.value,
                    taskId: task.id,
                    nodeId: selectedNode.id,
                    nodeTitle: selectedNode.title || discussionStatus.value.nodeTitle,
                    nodeStatus: selectedNode.status || discussionStatus.value.nodeStatus,
                    phase: phase || discussionStatus.value.phase,
                    lastUpdated: payload.updated_at || payload.ts || new Date().toISOString(),
                };

                refreshDiscussionStatusFromSelection();
            }
        };

        const handleTaskCompleted = (payload) => {
            const task = tasks.value.find(t => t.id === payload.id);
            if (!task) return;
            task.status = 'completed';
            if (payload.result !== undefined) task.result = payload.result;
            if (payload.subtasks) task.subtasks = payload.subtasks;
            // If this is the currently selected task, refresh the ref to ensure UI updates
            if (selectedTask.value?.id === payload.id) {
                selectedTask.value = task;
            }
            if (selectedTask.value?.id === payload.id && selectedSubtask.value) {
                const selectedNode = task.subtasks?.find(s => s.id === selectedSubtask.value.id);
                if (selectedNode) {
                    selectedSubtask.value = selectedNode;
                    discussionStatus.value = {
                        ...discussionStatus.value,
                        nodeStatus: selectedNode.status || 'completed',
                        phase: 'completed',
                        lastUpdated: payload.finished_at || payload.ts || new Date().toISOString(),
                    };
                    refreshDiscussionStatusFromSelection();
                }
            }
            scheduleGraphRefresh();
            fetchReports();
        };

        // Fetch all tasks from API, merge in-place to keep object references stable
        const fetchTasks = async () => {
            try {
                const res = await fetch('/api/tasks');
                if (!res.ok) {
                    throw new Error(`HTTP ${res.status}`);
                }
                const data = await res.json();
                const incoming = data.tasks || [];
                const incomingRev = Number(data.state_rev);
                if (Number.isFinite(incomingRev) && incomingRev < lastStateRev.value) {
                    return;
                }
                if (Number.isFinite(incomingRev) && incomingRev > lastStateRev.value) {
                    lastStateRev.value = incomingRev;
                }

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
        const graphFeatureFlags = {
            incremental: true,
        };
        let _graphStructureHash = '';
        let _graphStatusHash = '';

        const clampGraphZoom = (value) => Math.max(0.4, Math.min(2.5, value));

        const applyGraphViewportTransform = () => {
            Vue.nextTick(() => {
                const wrap = document.getElementById('graph-wrap');
                if (!wrap) return;
                wrap.style.transform = `translate(${graphPanX.value}px, ${graphPanY.value}px) scale(${graphZoom.value})`;
                wrap.style.transformOrigin = 'top left';
            });
        };

        const zoomGraphIn = () => {
            graphZoom.value = clampGraphZoom(graphZoom.value + 0.15);
            applyGraphViewportTransform();
        };

        const zoomGraphOut = () => {
            graphZoom.value = clampGraphZoom(graphZoom.value - 0.15);
            applyGraphViewportTransform();
        };

        const resetGraphView = () => {
            graphZoom.value = 1;
            graphPanX.value = 0;
            graphPanY.value = 0;
            applyGraphViewportTransform();
        };

        const fitGraphView = () => {
            Vue.nextTick(() => {
                const host = document.getElementById('graph-body');
                const wrap = document.getElementById('graph-wrap');
                if (!host || !wrap) return;

                const svg = wrap.querySelector('svg');
                if (!svg) {
                    resetGraphView();
                    return;
                }

                const hostRect = host.getBoundingClientRect();
                const svgRect = svg.getBoundingClientRect();
                if (!hostRect.width || !hostRect.height || !svgRect.width || !svgRect.height) {
                    resetGraphView();
                    return;
                }

                const scaleX = (hostRect.width * 0.94) / svgRect.width;
                const scaleY = (hostRect.height * 0.9) / svgRect.height;
                graphZoom.value = clampGraphZoom(Math.min(scaleX, scaleY, 1));
                graphPanX.value = Math.max(0, (hostRect.width - svgRect.width * graphZoom.value) / 2);
                graphPanY.value = Math.max(0, (hostRect.height - svgRect.height * graphZoom.value) / 2);
                applyGraphViewportTransform();
            });
        };

        const parseMermaidStateSignature = (mermaidText) => {
            const lines = (mermaidText || '').split('\n');
            const classes = lines
                .map(line => line.trim())
                .filter(line => line.startsWith('class '));
            const links = lines
                .map(line => line.trim())
                .filter(line => line.startsWith('linkStyle '));
            return {
                classes,
                links,
                statusHash: JSON.stringify({ classes, links }),
            };
        };

        const parseMermaidStructureSignature = (mermaidText) => {
            const lines = (mermaidText || '').split('\n');
            const structure = lines
                .map(line => line.trim())
                .filter(line => !line.startsWith('class ') && !line.startsWith('linkStyle '));
            return JSON.stringify(structure);
        };

        const applyGraphStateIncremental = (stateSig) => {
            const wrap = document.getElementById('graph-wrap');
            if (!wrap) return false;

            // 1) 清理旧 class 赋值
            wrap.querySelectorAll('.node').forEach(node => {
                node.classList.remove('pending', 'running', 'done', 'failed', 'skipped', 'blocked');
            });

            // 2) 应用节点状态 class（来自 Mermaid `class <id> <state>;`）
            for (const line of stateSig.classes) {
                const match = line.match(/^class\s+([^\s]+)\s+([^;\s]+);?$/);
                if (!match) continue;
                const nodeId = match[1];
                const cls = match[2];
                const nodeEl = wrap.querySelector(`#flowchart-${nodeId}`) || wrap.querySelector(`#${nodeId}`);
                if (nodeEl) nodeEl.classList.add(cls);
            }

            // 3) 依次应用 linkStyle（映射到 path 序号）
            const edgePaths = wrap.querySelectorAll('.edgePath path.path');
            edgePaths.forEach(path => {
                path.classList.remove('depedge', 'depedge-running', 'depedge-blocked');
                path.style.stroke = '';
                path.style.strokeWidth = '';
                path.style.strokeDasharray = '';
            });

            for (const line of stateSig.links) {
                const m = line.match(/^linkStyle\s+(\d+)\s+(.+);?$/);
                if (!m) continue;
                const idx = Number(m[1]);
                const stylePart = m[2];
                const path = edgePaths[idx];
                if (!path) continue;

                if (stylePart.includes('stroke:#a855f7')) path.classList.add('depedge-running');
                else if (stylePart.includes('stroke:#475569')) path.classList.add('depedge-blocked');
                else path.classList.add('depedge');

                const stroke = stylePart.match(/stroke:\s*([^,;]+)/)?.[1];
                const width = stylePart.match(/stroke-width:\s*([^,;]+)/)?.[1];
                const dash = stylePart.match(/stroke-dasharray:\s*([^,;]+)/)?.[1];
                if (stroke) path.style.stroke = stroke;
                if (width) path.style.strokeWidth = width;
                if (dash) path.style.strokeDasharray = dash;
            }

            return true;
        };

        // 根据当前活跃节点向原始图注入 classDef 高亮并渲染
        const updateGraphRender = async ({ force = false, statusOnly = false } = {}) => {
            // 静态骨架图由前端 SVG 直接渲染，无需 Mermaid
            if (!isDynamicGraph.value || !rawMermaid.value) {
                mermaidSvg.value = '';
                return;
            }

            try {
                const currentStructureHash = parseMermaidStructureSignature(rawMermaid.value);
                const stateSig = parseMermaidStateSignature(rawMermaid.value);
                const structureChanged = currentStructureHash !== _graphStructureHash;
                const statusChanged = stateSig.statusHash !== _graphStatusHash;

                if (!force && statusOnly && !statusChanged) {
                    return;
                }

                if (
                    graphFeatureFlags.incremental &&
                    !force &&
                    !structureChanged &&
                    statusChanged &&
                    mermaidSvg.value
                ) {
                    const ok = applyGraphStateIncremental(stateSig);
                    if (ok) {
                        _graphStatusHash = stateSig.statusHash;
                        return;
                    }
                }

                const id = 'graph-render-' + (++_renderSeq);
                const { svg } = await mermaid.render(id, rawMermaid.value);
                mermaidSvg.value = svg;
                _graphStructureHash = currentStructureHash;
                _graphStatusHash = stateSig.statusHash;

                if (graphFeatureFlags.incremental && statusChanged) {
                    applyGraphStateIncremental(stateSig);
                }
                applyGraphViewportTransform();
            } catch (e) {
                console.error('Mermaid render error:', e);
            }
        };

        // 是否显示动态子任务 DAG（否则显示静态 SVG Pipeline）
        const isDynamicGraph = computed(() => rawMermaid.value.includes('task_header'));

        // 拉取图结构（只在结构真正变化时请求网络）
        let _lastRawMermaid = '';
        const fetchGraph = async () => {
            try {
                const res = await fetch('/api/graph/mermaid');
                if (!res.ok) return;
                const data = await res.json();

                const incoming = data?.mermaid || '';
                const currentStructure = parseMermaidStructureSignature(incoming);
                const previousStructure = parseMermaidStructureSignature(_lastRawMermaid);
                const structureChanged = currentStructure !== previousStructure;

                _lastRawMermaid = incoming;
                rawMermaid.value = incoming;

                if (structureChanged) {
                    await updateGraphRender({ force: true });
                } else {
                    await updateGraphRender({ statusOnly: true });
                }
            } catch (e) {
                console.error('fetchGraph error', e);
            }
        };

        // 监听节点变化，实时闪烁高亮
        watch(currentNode, (newNode, oldNode) => {
            if (newNode !== oldNode) updateGraphRender({ statusOnly: true });
        });

        let _terminalRestored = false;
        const fetchSystemStatus = async (restoreTerminal = false) => {
            try {
                const url = restoreTerminal ? '/api/system/status?include_terminal=1' : '/api/system/status';
                const res = await fetch(url);
                if (!res.ok) {
                    throw new Error(`HTTP ${res.status}`);
                }
                const data = await res.json();
                const incomingRev = Number(data.state_rev);
                if (Number.isFinite(incomingRev) && incomingRev < lastStateRev.value) {
                    return;
                }
                if (Number.isFinite(incomingRev) && incomingRev > lastStateRev.value) {
                    lastStateRev.value = incomingRev;
                }
                systemStatus.value = data.status;
                currentNode.value = data.current_node || '';
                currentTaskId.value = data.current_task_id || '';
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
            if (!res.ok) {
                const msg = `创建任务失败: HTTP ${res.status}`;
                termLog(`✗ ${msg}`, 'error');
                alert(msg);
                return;
            }
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
            const task_id = selectedTask.value?.id || currentTaskId.value || '';
            if (!task_id) {
                termLog('⚠ 无可用任务上下文，请先选择任务或等待任务启动', 'warn');
                return;
            }
            if (ws && ws.readyState === WebSocket.OPEN) {
                ws.send(JSON.stringify({
                    type: 'terminal_input',
                    task_id,
                    command: terminalInput.value.trim(),
                }));
            } else {
                termLog('✗ WebSocket 未连接，无法发送指令', 'error');
            }
            terminalInput.value = '';
        };

        const clearTerminal = () => { terminalLines.value = []; };

        const clearAllTasks = async () => {
            if (!confirm('确定清空所有任务记录？此操作不可撤销。')) return;
            try {
                const res = await fetch('/api/tasks', { method: 'DELETE' });
                if (!res.ok) {
                    const err = await res.json();
                    alert(err.detail || '清空失败');
                    return;
                }
                tasks.value = [];
                selectedTask.value = null;
                terminalLines.value = [];
                scheduleGraphRefresh();
            } catch (e) {
                alert('请求失败: ' + e.message);
            }
        };

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
                const res = await fetch('/api/chat', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ message: msg, history }),
                });
                if (!res.ok) {
                    throw new Error(`HTTP ${res.status}`);
                }
            } catch (e) {
                chatThinking.value = false;
                chatMessages.value.push({ role: 'assistant', content: `请求失败: ${e.message}`, time: new Date().toLocaleTimeString() });
                termLog(`✗ Chat 请求失败: ${e.message}`, 'error');
            }
            // chatThinking 由 WS chat_reply 事件处理器关闭
        };

        // ── Reports ──
        const reports = Vue.ref([]);
        const activeReport = Vue.ref('');
        const activeReportContent = Vue.ref('');

        const fetchReports = async () => {
            try {
                const res = await fetch('/api/reports');
                if (!res.ok) return;
                const data = await res.json();
                reports.value = (data.files || []).filter(
                    r => typeof r?.name === 'string' && r.name.toLowerCase().endsWith('.md')
                );
                // 自动加载最新的 md 文件：当前无选中，或选中的文件已不在列表中时自动切换
                const stillExists = reports.value.some(r => r.name === activeReport.value);
                if (reports.value.length > 0 && !stillExists) {
                    const first = reports.value[0];
                    if (first) loadReport(first.name);
                }
            } catch (e) {
                console.error('fetchReports error', e);
            }
        };

        const loadReport = async (name) => {
            activeReport.value = name;
            activeReportContent.value = '';
            try {
                const res = await fetch(`/api/reports/${encodeURIComponent(name)}`);
                if (!res.ok) return;
                const data = await res.json();
                activeReportContent.value = data.content || '';
            } catch (e) {
                console.error('loadReport error', e);
            }
        };

        const selectTask = async (task) => {
            selectedTask.value = task;
            selectedSubtask.value = null;
            discussionMessages.value = [];
            discussionParticipants.value = [];
            discussionCacheByNode.value = {};
            discussionStatus.value = {
                taskId: task?.id || '',
                nodeId: '',
                nodeTitle: '',
                nodeStatus: 'idle',
                phase: '',
                lastUpdated: '',
                messagesCount: 0,
                participantCount: 0,
                latestSpeaker: '',
                interventionsQueued: 0,
                interventionsApplied: 0,
            };
            // Refresh from API to ensure result/subtasks are up to date
            try {
                const res = await fetch(`/api/tasks/${task.id}`);
                if (!res.ok) throw new Error(`HTTP ${res.status}`);
                const fresh = await res.json();
                Object.assign(task, fresh);
                // Re-assign selectedTask to trigger Vue reactivity for nested updates
                selectedTask.value = task;
            } catch (e) {
                console.error('selectTask: failed to refresh task', task.id, e);
            }
            refreshDiscussionStatusFromSelection();
            // 加载报告
            await fetchReports();
        };

        const selectSubtask = async (subtask) => {
            selectedSubtask.value = subtask;
            // 默认发言者：优先保留当前已选且仍可用；否则 assigned_agents 首项；再兜底 agent_type/user
            const currentFromAgent = newMessage.value.from_agent;
            const discussionAgentValues = discussionAgents.value.map(a => a.value);
            if (discussionAgentValues.includes(currentFromAgent)) {
                newMessage.value.from_agent = currentFromAgent;
            } else {
                newMessage.value.from_agent =
                    subtask.assigned_agents?.[0] || subtask.agent_type || 'user';
            }
            discussionParticipants.value = [];
            discussionStatus.value = {
                ...discussionStatus.value,
                taskId: selectedTask.value?.id || '',
                nodeId: subtask?.id || '',
                nodeTitle: subtask?.title || '',
                nodeStatus: subtask?.status || 'idle',
                phase: subtask?.phase || subtask?.current_phase || discussionStatus.value.phase || '',
                lastUpdated: subtask?.updated_at || subtask?.finished_at || subtask?.started_at || '',
            };
            if (selectedTask.value) {
                try {
                    const res = await fetch(`/api/tasks/${selectedTask.value.id}/nodes/${subtask.id}/discussion`);
                    if (!res.ok) {
                        throw new Error(`HTTP ${res.status}`);
                    }
                    const data = await res.json();
                    const key = discussionCacheKey(selectedTask.value.id, subtask.id);
                    const cached = discussionCacheByNode.value[key] || { messages: [], participants: [] };
                    const mergedMessages = mergeDiscussionMessages(data.messages || [], cached.messages || []);
                    discussionMessages.value = mergedMessages;
                    const mergedParticipants = Array.from(new Set([
                        ...(data.participants || []),
                        ...(cached.participants || []),
                    ]));
                    discussionParticipants.value = mergedParticipants;
                    discussionCacheByNode.value[key] = {
                        messages: mergedMessages,
                        participants: mergedParticipants,
                    };
                    // 讨论参与者加载后再校验一次默认发言者，避免切换节点时出现无效默认值
                    const updatedAgentValues = discussionAgents.value.map(a => a.value);
                    if (!updatedAgentValues.includes(newMessage.value.from_agent)) {
                        newMessage.value.from_agent =
                            subtask.assigned_agents?.[0] || subtask.agent_type || 'user';
                    }
                } catch (e) {
                    console.error('selectSubtask: failed to load discussion', subtask.id, e);
                    const key = discussionCacheKey(selectedTask.value.id, subtask.id);
                    const cached = discussionCacheByNode.value[key] || { messages: [], participants: [] };
                    discussionMessages.value = cached.messages || [];
                    discussionParticipants.value = cached.participants || [];
                }
            }
            refreshDiscussionStatusFromSelection();
        };

        const sendMessage = async () => {
            if (!newMessage.value.content.trim() || !selectedSubtask.value) return;
            try {
                const toAgents = Array.isArray(selectedSubtask.value.assigned_agents)
                    ? selectedSubtask.value.assigned_agents.filter(Boolean)
                    : [];
                const payload = {
                    ...newMessage.value,
                    to_agents: toAgents,
                };
                const res = await fetch(`/api/tasks/${selectedTask.value.id}/nodes/${selectedSubtask.value.id}/discussion`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                });
                if (!res.ok) {
                    throw new Error(`HTTP ${res.status}`);
                }
                const saved = await res.json();
                // Optimistically add to local list (WS event may also arrive)
                if (saved?.id && !discussionMessages.value.find(m => m.id === saved.id)) {
                    discussionMessages.value.push(saved);
                }
                upsertDiscussionCache(
                    selectedTask.value?.id,
                    selectedSubtask.value?.id,
                    saved,
                );
                refreshDiscussionStatusFromSelection();
                addTimelineItem({
                    id: `discussion_local|${selectedTask.value?.id || ''}|${selectedSubtask.value?.id || ''}|${saved?.id || saved?.timestamp || newMessage.value.content}`,
                    ts: saved?.timestamp || new Date().toISOString(),
                    taskId: selectedTask.value?.id || '',
                    node: selectedSubtask.value?.id || '',
                    kind: 'milestone',
                    level: 'info',
                    title: `讨论消息 · ${saved?.from_agent || newMessage.value.from_agent}`,
                    detail: (saved?.content || newMessage.value.content || '').slice(0, 120),
                    sourceEvent: 'discussion_message',
                });
                newMessage.value.content = '';
            } catch (e) {
                termLog(`✗ 发送讨论消息失败: ${e.message}`, 'error');
                alert(`发送失败: ${e.message}`);
            }
        };

        const openEditSubtask = (subtask) => {
            editingSubtask.value = subtask;
            editForm.value = {
                title: subtask.title || '',
                description: subtask.description || '',
                agent_type: subtask.agent_type || 'coder',
                priority: subtask.priority || 1,
                estimated_minutes: subtask.estimated_minutes || 10,
                knowledge_domains_str: (subtask.knowledge_domains || []).join(' '),
            };
        };

        const saveSubtask = async () => {
            if (!editingSubtask.value || !selectedTask.value) return;
            const { knowledge_domains_str, ...rest } = editForm.value;
            const payload = {
                ...rest,
                knowledge_domains: knowledge_domains_str
                    ? knowledge_domains_str.trim().split(/\s+/).filter(Boolean)
                    : undefined,
            };
            const res = await fetch(
                `/api/tasks/${selectedTask.value.id}/subtasks/${editingSubtask.value.id}`,
                {
                    method: 'PATCH',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload),
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
            const instruction = interveneText.value.trim();
            const taskId = selectedTask.value.id;
            const res = await fetch(`/api/tasks/${taskId}/intervene`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ instruction })
            });
            if (!res.ok) {
                let msg = `Intervene failed (${res.status})`;
                try {
                    const err = await res.json();
                    msg = err.detail || msg;
                } catch (_) {}

                addTimelineItem({
                    id: `intervene_rejected|${taskId}|${res.status}|${msg}`,
                    ts: new Date().toISOString(),
                    taskId,
                    node: selectedSubtask.value?.id || '',
                    kind: 'intervention',
                    level: 'error',
                    title: '干预被拒绝',
                    detail: msg,
                    sourceEvent: 'intervene_rejected',
                });

                alert(msg);
                return;
            }
            interveneText.value = '';
        };

        // Utils
        const getStatusText = (s) => ({ idle: 'Idle', running: 'Running', completed: 'Done', failed: 'Failed' }[s] || s);
        const formatTime = (t) => formatTimestampHHMMSS(t);

        const normalizeTaskText = (text) => String(text)
            .replace(/\r\n/g, '\n')
            .replace(/\r/g, '\n')
            .replace(/\\r\\n/g, '\n')
            .replace(/\\n/g, '\n');

        const escapeHtml = (text) => String(text)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');

        const renderTaskMd = (text) => {
            if (!text) return '';
            try {
                const normalized = normalizeTaskText(text);
                return marked.parse(escapeHtml(normalized), { breaks: true, gfm: true });
            } catch (e) {
                return escapeHtml(text);
            }
        };

        const renderTaskInline = (text) => {
            if (!text) return '';
            try {
                const normalized = normalizeTaskText(text);
                const safe = escapeHtml(normalized);
                return safe.replace(/\n+/g, '<br>');
            } catch (e) {
                return escapeHtml(text);
            }
        };

        const renderMd = (text) => {
            if (!text) return '';
            try { return marked.parse(text, { breaks: true, gfm: true }); }
            catch (e) { return text; }
        };

        onMounted(async () => {
            connectWebSocket();
            await fetchTasks();
            await fetchSystemStatus(true);  // true = 恢复终端日志
            scheduleGraphRefresh(0);
            fetchReports();
            window.addEventListener('resize', fitGraphView);

            // 刷新后自动选中正在运行的任务，否则选最新任务
            if (!selectedTask.value && tasks.value.length) {
                const running = tasks.value.find(t => t.status === 'running');
                selectedTask.value = running || tasks.value[0];
                discussionStatus.value = {
                    ...discussionStatus.value,
                    taskId: selectedTask.value?.id || '',
                };
            }

            if (!_terminalRestored) termLog('System ready. Waiting for tasks…', 'info');

            // 轮询仅作为 WebSocket 断线时的降级方案
            // WS 连接正常时由事件驱动，不产生冗余请求
            setInterval(async () => {
                if (wsConnected.value) return;   // WS 正常 → 跳过
                console.warn('[Polling] WS disconnected, falling back to HTTP poll');
                await fetchSystemStatus();
                await fetchTasks();
                scheduleGraphRefresh(0);
                refreshDiscussionStatusFromSelection();
            }, 5000);
        });

        return {
            wsConnected, systemStatus, currentNode, tasks, selectedTask, selectedSubtask,
            discussionMessages, discussionParticipants, discussionStatus, statusTimeline, filteredTimeline, timelineScope, mermaidSvg, isDynamicGraph, showNewTask, newTask, newMessage,
            terminalLines, terminalInput, editingSubtask, editForm, interveneText,
            chatMessages, chatInput, chatThinking,
            graphZoom,
            stats, getCompletedSubtasks, discussionAgents,
            reports, activeReport, activeReportContent,
            createTask, selectTask, selectSubtask, sendMessage, intervene, getStatusText, formatTime, renderMd, renderTaskMd, renderTaskInline,
            fetchGraph, fetchReports, loadReport, openEditSubtask, saveSubtask, sendTerminalCmd, clearTerminal, sendChat, clearAllTasks,
            zoomGraphIn, zoomGraphOut, resetGraphView, fitGraphView,
        };
    }
}).mount('#app');
