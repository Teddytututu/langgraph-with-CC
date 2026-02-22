// src/web/static/js/app.js - Vue 3 应用

const { createApp, ref, computed, onMounted, watch } = Vue;

// 初始化 Mermaid
mermaid.initialize({
    startOnLoad: false,
    theme: 'dark',
    themeVariables: {
        primaryColor: '#6366f1',
        primaryTextColor: '#f8fafc',
        primaryBorderColor: '#6366f1',
        lineColor: '#64748b',
        secondaryColor: '#1e293b',
        tertiaryColor: '#334155',
        background: '#0f172a',
        mainBkg: '#1e293b',
        nodeBorder: '#6366f1',
        clusterBkg: '#1e293b',
        titleColor: '#f8fafc',
        edgeLabelBackground: '#0f172a',
    },
    flowchart: {
        useMaxWidth: true,
        htmlLabels: true,
        curve: 'basis',
    }
});

createApp({
    setup() {
        // 状态
        const wsConnected = ref(false);
        const systemStatus = ref('idle');
        const currentNode = ref('');
        const currentTaskId = ref('');
        const tasks = ref([]);
        const selectedTask = ref(null);
        const selectedSubtask = ref(null);
        const discussionMessages = ref([]);
        const mermaidSvg = ref('');
        const showNewTask = ref(false);

        // 新任务表单
        const newTask = ref({
            task: '',
            time_minutes: null
        });

        // 新消息表单
        const newMessage = ref({
            from_agent: 'director',
            content: ''
        });

        // 计算属性：完成的子任务数量
        const getCompletedSubtasks = computed(() => {
            if (!selectedTask.value?.subtasks) return 0;
            return selectedTask.value.subtasks.filter(s => s.status === 'done' || s.status === 'completed').length;
        });

        // WebSocket 连接
        let ws = null;

        const connectWebSocket = () => {
            const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
            ws = new WebSocket(`${protocol}//${window.location.host}/ws`);

            ws.onopen = () => {
                wsConnected.value = true;
                console.log('WebSocket connected');
            };

            ws.onclose = () => {
                wsConnected.value = false;
                console.log('WebSocket disconnected');
                setTimeout(connectWebSocket, 5000);
            };

            ws.onmessage = (event) => {
                const data = JSON.parse(event.data);
                handleWebSocketMessage(data);
            };

            ws.onerror = (error) => {
                console.error('WebSocket error:', error);
            };
        };

        const handleWebSocketMessage = (data) => {
            const { event, data: payload } = data;

            switch (event) {
                case 'system_status_changed':
                    systemStatus.value = payload.status;
                    if (payload.task_id) {
                        currentTaskId.value = payload.task_id;
                    }
                    if (payload.status === 'running' || payload.status === 'completed') {
                        fetchGraph();
                    }
                    break;

                case 'node_changed':
                    currentNode.value = payload.node;
                    fetchGraph();
                    break;

                case 'task_created':
                    tasks.value.unshift(payload);
                    break;

                case 'task_started':
                    updateTaskStatus(payload.id, 'running');
                    break;

                case 'task_progress':
                    updateTaskProgress(payload);
                    break;

                case 'task_completed':
                    updateTaskStatus(payload.id, 'completed');
                    if (selectedTask.value?.id === payload.id) {
                        selectedTask.value.result = payload.result;
                    }
                    break;

                case 'task_failed':
                    updateTaskStatus(payload.id, 'failed');
                    break;

                case 'discussion_message':
                    if (selectedSubtask.value?.id === payload.node_id) {
                        discussionMessages.value.push(payload.message);
                        scrollToBottom();
                    }
                    break;
            }
        };

        const updateTaskStatus = (taskId, status) => {
            const task = tasks.value.find(t => t.id === taskId);
            if (task) {
                task.status = status;
            }
            if (selectedTask.value?.id === taskId) {
                selectedTask.value.status = status;
            }
        };

        const updateTaskProgress = (payload) => {
            const task = tasks.value.find(t => t.id === payload.task_id);
            if (task) {
                task.subtasks = payload.subtasks;
            }
            if (selectedTask.value?.id === payload.task_id) {
                selectedTask.value.subtasks = payload.subtasks;
            }
        };

        // API 调用
        const fetchTasks = async () => {
            try {
                const response = await fetch('/api/tasks');
                const data = await response.json();
                tasks.value = data.tasks;
            } catch (error) {
                console.error('Failed to fetch tasks:', error);
            }
        };

        const fetchGraph = async () => {
            try {
                const response = await fetch('/api/graph/mermaid');
                const data = await response.json();

                // 生成唯一 ID 避免 Mermaid 缓存问题
                const uniqueId = `graph-svg-${Date.now()}`;
                const { svg } = await mermaid.render(uniqueId, data.mermaid);
                mermaidSvg.value = svg;

                if (data.current_node) {
                    currentNode.value = data.current_node;
                }
            } catch (error) {
                console.error('Failed to fetch graph:', error);
                mermaidSvg.value = '<p style="color: var(--text-muted); text-align: center;">加载 Graph 失败</p>';
            }
        };

        const fetchSystemStatus = async () => {
            try {
                const response = await fetch('/api/system/status');
                const data = await response.json();
                systemStatus.value = data.status;
                currentNode.value = data.current_node;
                currentTaskId.value = data.current_task_id;
            } catch (error) {
                console.error('Failed to fetch system status:', error);
            }
        };

        const createTask = async () => {
            if (!newTask.value.task.trim()) return;

            try {
                const response = await fetch('/api/tasks', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(newTask.value)
                });

                const data = await response.json();
                showNewTask.value = false;
                newTask.value = { task: '', time_minutes: null };

                // 自动启动任务
                await fetch(`/api/tasks/${data.id}/start`, { method: 'POST' });
            } catch (error) {
                console.error('Failed to create task:', error);
            }
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
                    const response = await fetch(
                        `/api/tasks/${selectedTask.value.id}/nodes/${subtask.id}/discussion`
                    );
                    const data = await response.json();
                    discussionMessages.value = data.messages || [];
                    scrollToBottom();
                } catch (error) {
                    console.error('Failed to fetch discussion:', error);
                    discussionMessages.value = [];
                }
            }
        };

        const sendMessage = async () => {
            if (!newMessage.value.content.trim() || !selectedSubtask.value || !selectedTask.value) return;

            try {
                await fetch(
                    `/api/tasks/${selectedTask.value.id}/nodes/${selectedSubtask.value.id}/discussion`,
                    {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(newMessage.value)
                    }
                );

                newMessage.value.content = '';
            } catch (error) {
                console.error('Failed to send message:', error);
            }
        };

        const scrollToBottom = () => {
            setTimeout(() => {
                const container = document.querySelector('.discussion-container');
                if (container) {
                    container.scrollTop = container.scrollHeight;
                }
            }, 100);
        };

        // 工具函数
        const getStatusIcon = (status) => {
            const icons = {
                created: '📋',
                pending: '⏳',
                running: '🔄',
                done: '✅',
                completed: '✅',
                failed: '❌',
                skipped: '⏭️'
            };
            return icons[status] || '❓';
        };

        const getStatusText = (status) => {
            const texts = {
                idle: '待机中',
                running: '执行中',
                completed: '已完成',
                failed: '执行失败',
                created: '已创建',
                pending: '等待中',
                done: '已完成',
                skipped: '已跳过'
            };
            return texts[status] || status;
        };

        const formatTime = (timestamp) => {
            if (!timestamp) return '';
            const date = new Date(timestamp);
            return date.toLocaleTimeString('zh-CN', {
                hour: '2-digit',
                minute: '2-digit'
            });
        };

        // 生命周期
        onMounted(() => {
            connectWebSocket();
            fetchTasks();
            fetchSystemStatus();
        });

        return {
            // 状态
            wsConnected,
            systemStatus,
            currentNode,
            currentTaskId,
            tasks,
            selectedTask,
            selectedSubtask,
            discussionMessages,
            mermaidSvg,
            showNewTask,
            newTask,
            newMessage,

            // 计算属性
            getCompletedSubtasks,

            // 方法
            createTask,
            selectTask,
            selectSubtask,
            sendMessage,
            getStatusIcon,
            getStatusText,
            formatTime
        };
    }
}).mount('#app');
