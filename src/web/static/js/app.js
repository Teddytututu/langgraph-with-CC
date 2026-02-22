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
                    if (payload.status === 'running' || payload.status === 'completed') fetchGraph();
                    break;
                case 'node_changed':
                    currentNode.value = payload.node;
                    addActivity(`Executing: ${payload.node}`);
                    fetchGraph();
                    break;
                case 'task_created':
                    tasks.value.unshift(payload);
                    addActivity(`Task created: ${payload.id}`);
                    break;
                case 'task_started':
                    updateTaskStatus(payload.id, 'running');
                    addActivity(`Task started: ${payload.id}`);
                    break;
                case 'task_progress':
                    updateTaskProgress(payload);
                    break;
                case 'task_completed':
                    updateTaskStatus(payload.id, 'completed');
                    addActivity(`Task completed: ${payload.id}`);
                    break;
                case 'task_failed':
                    updateTaskStatus(payload.id, 'failed');
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

        const updateTaskStatus = (id, status) => {
            const task = tasks.value.find(t => t.id === id);
            if (task) task.status = status;
            if (selectedTask.value?.id === id) selectedTask.value.status = status;
        };

        const updateTaskProgress = (payload) => {
            const task = tasks.value.find(t => t.id === payload.task_id);
            if (task) task.subtasks = payload.subtasks;
            if (selectedTask.value?.id === payload.task_id) selectedTask.value.subtasks = payload.subtasks;
        };

        // API
        const fetchTasks = async () => {
            const res = await fetch('/api/tasks');
            const data = await res.json();
            tasks.value = data.tasks;
        };

        const fetchGraph = async () => {
            try {
                const res = await fetch('/api/graph/mermaid');
                const data = await res.json();
                const { svg } = await mermaid.render(`graph-${Date.now()}`, data.mermaid);
                mermaidSvg.value = svg;
            } catch (e) {
                mermaidSvg.value = '<p style="color:#52525B">Failed to load graph</p>';
            }
        };

        const fetchSystemStatus = async () => {
            const res = await fetch('/api/system/status');
            const data = await res.json();
            systemStatus.value = data.status;
            currentNode.value = data.current_node;
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

        const selectTask = (task) => {
            selectedTask.value = task;
            selectedSubtask.value = null;
            discussionMessages.value = [];
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

        onMounted(() => {
            connectWebSocket();
            fetchTasks();
            fetchSystemStatus();
        });

        return {
            wsConnected, systemStatus, currentNode, tasks, selectedTask, selectedSubtask,
            discussionMessages, mermaidSvg, showNewTask, newTask, newMessage, activityLogs,
            stats, getCompletedSubtasks,
            createTask, selectTask, selectSubtask, sendMessage, getStatusText, formatTime
        };
    }
}).mount('#app');
