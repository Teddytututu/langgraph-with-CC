// Vue 3 App
const { createApp, ref, computed, onMounted } = Vue;

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
        const mermaidSvg = ref('');
        const showNewTask = ref(false);
        const activityLogs = ref([]);

        const newTask = ref({ task: '', time_minutes: null });
        const newMessage = ref({ from_agent: 'director', content: '' });

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

        // WebSocket
        let ws = null;

        const connectWebSocket = () => {
            const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
            ws = new WebSocket(`${protocol}//${window.location.host}/ws`);

            ws.onopen = () => { wsConnected.value = true; };
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
                    addActivity(`System: ${payload.status}`);
                    fetchGraph();
                    break;
                case 'node_changed':
                    currentNode.value = payload.node;
                    addActivity(`Executing: ${payload.node}`);
                    fetchGraph();
                    break;
                case 'task_created':
                    // Avoid duplicate if fetchTasks already added it
                    if (!tasks.value.find(t => t.id === payload.id)) {
                        tasks.value.unshift(payload);
                    }
                    addActivity(`Task created: ${payload.id}`);
                    break;
                case 'task_started':
                    mergeTasks([{ id: payload.id, status: 'running' }]);
                    addActivity(`Task started: ${payload.id}`);
                    break;
                case 'task_progress':
                    handleTaskProgress(payload);
                    break;
                case 'task_completed':
                    handleTaskCompleted(payload);
                    addActivity(`Task completed: ${payload.id}`);
                    break;
                case 'task_failed':
                    mergeTasks([{ id: payload.id, status: 'failed', error: payload.error }]);
                    addActivity(`Task failed: ${payload.id}`);
                    break;
            }
        };

        const addActivity = (title) => {
            activityLogs.value.unshift({
                title,
                time: new Date().toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit' })
            });
            if (activityLogs.value.length > 20) activityLogs.value.pop();
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

        const fetchGraph = async () => {
            try {
                const res = await fetch('/api/graph/mermaid');
                const data = await res.json();
                const { svg } = await mermaid.render(`graph-${Date.now()}`, data.mermaid);
                mermaidSvg.value = svg;
            } catch (e) {
                // keep previous SVG or show nothing
            }
        };

        const fetchSystemStatus = async () => {
            try {
                const res = await fetch('/api/system/status');
                const data = await res.json();
                systemStatus.value = data.status;
                currentNode.value = data.current_node || '';
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
            await fetch(`/api/tasks/${data.id}/start`, { method: 'POST' });
        };

        const selectTask = async (task) => {
            selectedTask.value = task;
            selectedSubtask.value = null;
            discussionMessages.value = [];
            // Refresh from API to ensure result/subtasks are up to date
            try {
                const res = await fetch(`/api/tasks/${task.id}`);
                const fresh = await res.json();
                Object.assign(task, fresh);
            } catch (e) {}
        };

        const selectSubtask = async (subtask) => {
            selectedSubtask.value = subtask;
            if (selectedTask.value) {
                try {
                    const res = await fetch(`/api/tasks/${selectedTask.value.id}/nodes/${subtask.id}/discussion`);
                    const data = await res.json();
                    discussionMessages.value = data.messages || [];
                } catch (e) {
                    discussionMessages.value = [];
                }
            }
        };

        const sendMessage = async () => {
            if (!newMessage.value.content.trim() || !selectedSubtask.value) return;
            await fetch(`/api/tasks/${selectedTask.value.id}/nodes/${selectedSubtask.value.id}/discussion`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(newMessage.value)
            });
            newMessage.value.content = '';
        };

        // Utils
        const getStatusText = (s) => ({ idle: 'Idle', running: 'Running', completed: 'Done', failed: 'Failed' }[s] || s);
        const formatTime = (t) => t ? new Date(t).toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit' }) : '';

        onMounted(async () => {
            connectWebSocket();
            await fetchTasks();
            await fetchSystemStatus();
            fetchGraph();

            // Poll every 3s when running to keep subtasks + result fresh
            setInterval(async () => {
                await fetchSystemStatus();
                if (systemStatus.value === 'running') {
                    await fetchTasks();
                    fetchGraph();
                }
            }, 3000);
        });

        return {
            wsConnected, systemStatus, currentNode, tasks, selectedTask, selectedSubtask,
            discussionMessages, mermaidSvg, showNewTask, newTask, newMessage, activityLogs,
            stats, getCompletedSubtasks,
            createTask, selectTask, selectSubtask, sendMessage, getStatusText, formatTime,
            fetchGraph,
        };
    }
}).mount('#app');
