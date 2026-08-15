<script setup lang="ts">
/** 处理任务工作区：管理筛选、分页、轮询和详情打开状态。 */
import { computed, onBeforeUnmount, onMounted, shallowRef } from 'vue'
import { api } from '../api'
import type { TaskItem } from '../types'
import {
  errorMessage,
  formatTaskTime,
  taskActivity,
  taskRowAriaLabel,
  taskStatusLabel,
  taskTypeLabel,
} from '../utils/presentation'
import TaskDrawer from './TaskDrawer.vue'

const props = defineProps<{
  initialTaskId?: string | null
}>()

const emit = defineEmits<{
  error: [message: string]
}>()

const taskItems = shallowRef<TaskItem[]>([])
const cursor = shallowRef<string | null>(null)
const loading = shallowRef(false)
const status = shallowRef('')
const type = shallowRef('')
const selectedTask = shallowRef<TaskItem | null>(null)
const taskTrigger = shallowRef<HTMLElement | null>(null)
const retrying = shallowRef(false)
let taskTimer: number | undefined
let disposed = false
let listRequestId = 0
let detailRequestId = 0

const hasActiveTasks = computed(() => taskItems.value.some((item) => item.status === 'queued' || item.status === 'running'))
const activityById = computed(() => new Map(taskItems.value.map((item) => [item.task_id, taskActivity(item)])))

/** 加载任务列表，可选追加分页，并同步已打开任务的最新摘要。 */
async function loadTasks({ append = false }: { append?: boolean } = {}): Promise<void> {
  const requestId = ++listRequestId
  loading.value = true
  try {
    const response = await api.tasks({
      status: status.value || undefined,
      task_type: type.value || undefined,
      cursor: append ? cursor.value : undefined,
    })
    if (disposed || requestId !== listRequestId) return
    taskItems.value = append ? [...taskItems.value, ...response.items] : response.items
    cursor.value = response.next_cursor
    if (selectedTask.value) {
      const refreshed = taskItems.value.find((item) => item.task_id === selectedTask.value?.task_id)
      if (refreshed) selectedTask.value = refreshed
    }
  } catch (reason) {
    if (!disposed && requestId === listRequestId) emit('error', errorMessage(reason))
  } finally {
    if (!disposed && requestId === listRequestId) {
      loading.value = false
      syncPolling()
    }
  }
}

/** 打开任务详情，并记录列表触发按钮供关闭后恢复焦点。 */
async function openTask(id: string, event?: MouseEvent): Promise<void> {
  const requestId = ++detailRequestId
  if (event?.currentTarget instanceof HTMLElement) taskTrigger.value = event.currentTarget
  try {
    const response = await api.task(id)
    if (!disposed && requestId === detailRequestId) selectedTask.value = response
  } catch (reason) {
    if (!disposed && requestId === detailRequestId) emit('error', errorMessage(reason))
  }
}

/** 关闭当前详情；TaskDrawer 在卸载时负责恢复焦点。 */
function closeTask(): void {
  selectedTask.value = null
}

/** 为失败的语境任务重新创建任务，并打开新任务详情。 */
async function retryTask(): Promise<void> {
  const image = selectedTask.value?.image
  if (!image?.meme_id || retrying.value) return
  retrying.value = true
  try {
    const response = await api.context({ meme_id: image.meme_id })
    await loadTasks()
    await openTask(response.task_id)
  } catch (reason) {
    if (!disposed) emit('error', errorMessage(reason))
  } finally {
    if (!disposed) retrying.value = false
  }
}

/** 根据页面可见性和活跃任务数量同步轮询状态。 */
function syncPolling(): void {
  if (disposed || document.visibilityState !== 'visible' || !hasActiveTasks.value) {
    stopPolling()
    return
  }
  if (!taskTimer) taskTimer = window.setInterval(() => { void loadTasks() }, 2500)
}

/** 停止当前任务轮询计时器。 */
function stopPolling(): void {
  if (taskTimer) window.clearInterval(taskTimer)
  taskTimer = undefined
}

/** 页面可见性变化时重新评估轮询条件。 */
function onVisibilityChange(): void {
  syncPolling()
}

onMounted(async () => {
  document.addEventListener('visibilitychange', onVisibilityChange)
  await loadTasks()
  if (disposed) return
  if (props.initialTaskId) await openTask(props.initialTaskId)
  syncPolling()
})

onBeforeUnmount(() => {
  disposed = true
  listRequestId += 1
  detailRequestId += 1
  stopPolling()
  document.removeEventListener('visibilitychange', onVisibilityChange)
})
</script>

<template>
  <section class="workspace task-workspace" :class="{ detail: selectedTask }">
    <div class="section-head">
      <div><h1>处理任务</h1></div>
      <button class="quiet" type="button" :disabled="loading" @click="loadTasks()">刷新</button>
    </div>
    <div class="task-toolbar">
      <label>状态<select v-model="status" aria-label="按状态筛选" @change="loadTasks()"><option value="">全部</option><option value="queued">排队中</option><option value="running">处理中</option><option value="succeeded">已完成</option><option value="failed">失败</option></select></label>
      <label>类型<select v-model="type" aria-label="按类型筛选" @change="loadTasks()"><option value="">全部</option><option value="meme_context_generation">语境生成</option><option value="cache_generation">检索缓存</option><option value="metadata_repair">元数据修复</option></select></label>
    </div>
    <div class="task-table" :class="{ loading }" role="table" aria-label="处理任务列表">
      <div class="task-head" role="row"><span role="columnheader">状态</span><span role="columnheader">类型</span><span role="columnheader">关联图片</span><span role="columnheader">进度</span><span role="columnheader">最近更新</span></div>
      <button
        v-for="item in taskItems"
        :key="item.task_id"
        class="task-row"
        :class="{ selected: selectedTask?.task_id === item.task_id }"
        type="button"
        role="row"
        :aria-label="taskRowAriaLabel(item)"
        @click="openTask(item.task_id, $event)"
      >
        <span class="task-status-cell" role="cell"><i :class="`status-dot ${item.status}`" aria-hidden="true"></i>{{ taskStatusLabel(item.status) }}</span>
        <span class="task-type-cell" role="cell" data-label="类型">
          <span>{{ taskTypeLabel(item.task_type) }}</span>
          <span v-if="activityById.get(item.task_id)" class="task-activity" :aria-label="activityById.get(item.task_id)?.ariaLabel">
            <b>{{ activityById.get(item.task_id)?.turns }}</b><time>{{ activityById.get(item.task_id)?.lastActivity }}</time>
          </span>
        </span>
        <span class="task-image" role="cell" data-label="图片">{{ item.image?.filename || '—' }}</span>
        <span class="task-progress" role="cell" data-label="进度">{{ item.progress == null ? '—' : `${Math.round(item.progress * 100)}%` }}</span>
        <time role="cell">{{ formatTaskTime(item.updated_at) }}</time>
      </button>
      <div v-for="n in 5" v-if="loading && !taskItems.length" :key="n" class="task-skeleton"></div>
      <div v-if="!loading && !taskItems.length" class="empty-state compact"><h2>没有匹配的任务</h2><p>调整筛选条件后再试。</p></div>
    </div>
    <button v-if="cursor" class="quiet load-more" type="button" @click="loadTasks({ append: true })">加载更多</button>
    <TaskDrawer
      v-if="selectedTask"
      :task="selectedTask"
      :retrying="retrying"
      :return-focus="taskTrigger"
      @close="closeTask"
      @retry="retryTask"
    />
  </section>
</template>
