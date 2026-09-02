<script setup lang="ts">
/** 处理任务工作区：管理筛选、分页、轮询和详情打开状态。 */
import { computed, onBeforeUnmount, onMounted, shallowRef } from 'vue'
import { api } from '../api'
import { showTaskDiagnostics } from '../config/debug'
import type { ImageProcessingJob, ImageProcessingTerminalEvent, TaskItem } from '../types'
import {
  errorMessage,
  formatTaskTime,
  taskActivity,
  taskRowAriaLabel,
  taskStatusLabel,
  taskTypeLabel,
  imageStageLabel,
  imageStageStatusLabel,
  submissionModeLabel,
} from '../utils/presentation'
import TaskDrawer from './TaskDrawer.vue'

const props = defineProps<{
  initialTaskId?: string | null
}>()

const emit = defineEmits<{
  error: [message: string]
  imageProcessingTerminal: [event: ImageProcessingTerminalEvent]
}>()

const taskItems = shallowRef<TaskItem[]>([])
const processingJobs = shallowRef<ImageProcessingJob[]>([])
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
let processingRequestId = 0
let detailRequestId = 0
const latestProcessingByMeme = new Map<string, ImageProcessingJob>()
const fallbackCreatedAtByKey = new Map<string, string>()

const terminalJobStatuses = new Set(['succeeded', 'failed', 'blocked', 'unknown_execution', 'warning', 'skipped'])

/** 终态具有最高优先级；活动阶段只允许从排队推进到运行中。 */
function processingStatusRank(status: string): number {
  if (terminalJobStatuses.has(status)) return 2
  if (status === 'running') return 1
  return 0
}

/** 按 revision、Job 标识和状态进度判断候选是否比已知事实更新。 */
function isNewerProcessingJob(candidate: ImageProcessingJob, current: ImageProcessingJob | undefined): boolean {
  if (!current) return true
  if (candidate.revision !== current.revision) return candidate.revision > current.revision
  if (candidate.job_id !== current.job_id) {
    const candidateUpdated = candidate.updated_at ? Date.parse(candidate.updated_at) : Number.NaN
    const currentUpdated = current.updated_at ? Date.parse(current.updated_at) : Number.NaN
    return Number.isFinite(candidateUpdated) && Number.isFinite(currentUpdated) && candidateUpdated > currentUpdated
  }
  if (terminalJobStatuses.has(current.status)) return false
  const candidateUpdated = candidate.updated_at ? Date.parse(candidate.updated_at) : Number.NaN
  const currentUpdated = current.updated_at ? Date.parse(current.updated_at) : Number.NaN
  if (Number.isFinite(candidateUpdated) && Number.isFinite(currentUpdated) && candidateUpdated < currentUpdated) return false
  const candidateRank = processingStatusRank(candidate.status)
  const currentRank = processingStatusRank(current.status)
  if (candidateRank < currentRank) return false
  return candidateRank > currentRank || !Number.isFinite(currentUpdated) || (Number.isFinite(candidateUpdated) && candidateUpdated > currentUpdated)
}

/** 只在最新图片处理 Job 从活动状态进入终态时通知图片库刷新。 */
function applyProcessingJobs(nextJobs: ImageProcessingJob[]): void {
  const latestInResponse = new Map<string, ImageProcessingJob>()
  for (const job of nextJobs) {
    if (!job.meme_id || !job.job_id || !Number.isInteger(job.revision) || job.revision < 1) continue
    const current = latestInResponse.get(job.meme_id)
    if (isNewerProcessingJob(job, current)) latestInResponse.set(job.meme_id, job)
  }
  for (const job of latestInResponse.values()) {
    const previous = latestProcessingByMeme.get(job.meme_id)
    if (isNewerProcessingJob(job, previous)) {
      latestProcessingByMeme.set(job.meme_id, job)
      if (previous && processingStatusRank(previous.status) < 2 && terminalJobStatuses.has(job.status)) {
        emit('imageProcessingTerminal', { meme_id: job.meme_id, job_id: job.job_id, revision: job.revision })
      }
    }
  }
}

const visibleTaskItems = computed(() => taskItems.value.filter((item) => !(item.submission_mode === 'pipeline' && item.processing_job_id)))
const hasActiveTasks = computed(() => taskItems.value.some((item) => item.status === 'queued' || item.status === 'running') || processingJobs.value.some((item) => ['queued', 'running'].includes(item.status)))
const activityById = computed(() => new Map(taskItems.value.map((item) => [item.task_id, taskActivity(item)])))

type TaskListEntry =
  | { kind: 'job'; key: string; identifier: string; timestamp: number; job: ImageProcessingJob }
  | { kind: 'task'; key: string; identifier: string; timestamp: number; task: TaskItem }

/** 读取列表条目的创建时间；旧摘要缺少创建时间时仅首次回退更新时间。 */
function taskCreatedValue(item: { created_at?: string; updated_at?: string }, key: string): string | undefined {
  if (item.created_at && Number.isFinite(Date.parse(item.created_at))) return item.created_at
  const fallback = fallbackCreatedAtByKey.get(key)
  if (fallback) return fallback
  if (item.updated_at && Number.isFinite(Date.parse(item.updated_at))) {
    fallbackCreatedAtByKey.set(key, item.updated_at)
    return item.updated_at
  }
  return undefined
}

/** 将列表条目的创建时间转换为排序键，确保排序与展示使用同一回退语义。 */
function taskCreatedTimestamp(item: { created_at?: string; updated_at?: string }, key: string): number {
  const value = taskCreatedValue(item, key)
  return value ? Date.parse(value) : Number.NaN
}

/** 合并父 Job 与可见任务，按创建时间倒序并以标识符稳定打破并列。 */
const taskEntries = computed<TaskListEntry[]>(() => {
  const entries: TaskListEntry[] = [
    ...processingJobs.value.map((job) => {
      const key = `job:${job.job_id}`
      return { kind: 'job' as const, key, identifier: job.job_id, timestamp: taskCreatedTimestamp(job, key), job }
    }),
    ...visibleTaskItems.value.map((task) => {
      const key = `task:${task.task_id}`
      return { kind: 'task' as const, key, identifier: task.task_id, timestamp: taskCreatedTimestamp(task, key), task }
    }),
  ]
  return entries.sort((left, right) => {
    const leftValid = Number.isFinite(left.timestamp)
    const rightValid = Number.isFinite(right.timestamp)
    if (leftValid !== rightValid) return leftValid ? -1 : 1
    if (leftValid && left.timestamp !== right.timestamp) return right.timestamp - left.timestamp
    if (left.identifier === right.identifier) return 0
    return left.identifier > right.identifier ? -1 : 1
  })
})

/** 判断自动重命名叶子 Task 是否属于可降级 warning，兼容新旧响应字段。 */
function canRetryAutoRename(task: TaskItem): boolean {
  const stage = task.image_stage || (task.task_type === 'image_auto_rename' ? 'auto_rename' : null)
  if (stage !== 'auto_rename' || task.status !== 'failed' || task.read_only) return false
  if (task.image_stage_recoverable === false) return false
  return task.image_stage_recoverable === true || task.image_stage_status === 'warning'
}

/** 根据 Job 是否公开自动重命名阶段，保留历史三阶段 Job 的准确文案。 */
function processingPipelineLabel(job: ImageProcessingJob): string {
  const hasAutoRenameStage = job.auto_name === true || job.stages?.some((stage) => stage.stage === 'auto_rename')
  return hasAutoRenameStage ? '四阶段流水线' : '三阶段流水线'
}

/** 加载完整图片处理 Job；旧测试夹具没有该 API 时保持任务列表兼容。 */
async function loadProcessingJobs(): Promise<void> {
  if (typeof api.processingJobs !== 'function') return
  const requestId = ++processingRequestId
  try {
    const response = await api.processingJobs({ limit: 100 })
    if (!disposed && requestId === processingRequestId) {
      const nextJobs = Array.isArray(response.items) ? response.items : []
      applyProcessingJobs(nextJobs)
      processingJobs.value = nextJobs
      // 父 Job 可能没有可见叶子 Task；列表首次加载后仍要依据最新 Job
      // 状态启动轮询，否则用户只能手动刷新才能看到阶段推进。
      syncPolling()
    }
  } catch (reason) {
    if (!disposed) emit('error', errorMessage(reason))
  }
}

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
    void loadProcessingJobs()
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
  const selected = selectedTask.value
  const image = selected?.image
  if (!image?.meme_id || retrying.value) return
  if (selected?.task_type === 'image_auto_rename' && !canRetryAutoRename(selected)) return
  retrying.value = true
  try {
    let response
    if (selected && canRetryAutoRename(selected) && typeof api.submitImageStage === 'function') {
      response = await api.submitImageStage({ meme_id: image.meme_id, stage: 'auto_rename', reverse_image_policy: 'forbid' })
    } else if (selected?.task_type !== 'image_auto_rename' && selected?.submission_mode === 'pipeline' && selected.image_stage !== 'auto_rename' && selected.processing_job_id && typeof api.retryProcessingJob === 'function') {
      response = await api.retryProcessingJob(selected.processing_job_id)
    } else if (selected?.task_type !== 'image_auto_rename' && selected?.submission_mode === 'standalone' && selected.image_stage && typeof api.submitImageStage === 'function') {
      response = await api.submitImageStage({ meme_id: image.meme_id, stage: selected.image_stage, reverse_image_policy: 'forbid' })
    } else if (selected?.task_type === 'meme_context_generation' && selected.submission_mode == null) {
      // 未归类历史只保留旧诊断兼容，不把新的图片任务送入通用 retry。
      response = await api.context({ meme_id: image.meme_id })
    } else {
      return
    }
    await loadTasks()
    if (response?.task_id) await openTask(response.task_id)
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
  processingRequestId += 1
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
      <label>状态<select v-model="status" aria-label="按状态筛选" @change="loadTasks()"><option value="">全部</option><option value="queued">排队中</option><option value="running">处理中</option><option value="succeeded">已完成</option><option value="failed">失败</option><option value="blocked">已阻止</option><option value="unknown_execution">执行状态未知</option></select></label>
      <label>类型<select v-model="type" aria-label="按类型筛选" @change="loadTasks()"><option value="">全部</option><option value="meme_context_generation">语境生成</option><option value="visual_embedding_generation">图片向量</option><option value="image_auto_rename">自动重命名</option><option value="text_embedding_generation">文本 embedding</option><option value="cache_generation">检索缓存</option><option value="metadata_repair">元数据修复</option></select></label>
    </div>
    <div class="task-table" :class="{ loading }" role="table" aria-label="处理任务列表">
      <div class="task-head" role="row"><span role="columnheader">状态</span><span role="columnheader">类型</span><span role="columnheader">来源</span><span role="columnheader">关联图片</span><span role="columnheader">进度</span><span role="columnheader">创建时间</span></div>
      <template v-for="entry in taskEntries" :key="entry.key">
        <details v-if="entry.kind === 'job'" class="processing-job">
          <summary class="processing-job-parent">
            <span class="task-status-cell"><i :class="`status-dot ${entry.job.status}`" aria-hidden="true"></i>{{ taskStatusLabel(entry.job.status) }}</span>
            <span>
              <strong>完整图片处理</strong>
              <small>
                <template v-if="showTaskDiagnostics">Job #{{ entry.job.revision }} · 图片 ID {{ entry.job.meme_id }} · Job ID {{ entry.job.job_id }}</template>
                <template v-else>第 {{ entry.job.revision }} 次处理 · {{ processingPipelineLabel(entry.job) }}</template>
              </small>
            </span>
            <span class="task-source">{{ submissionModeLabel('pipeline') }}</span>
            <span class="task-image">{{ entry.job.image?.filename || '—' }}</span>
            <span>{{ entry.job.progress == null ? '—' : `${Math.round(entry.job.progress * 100)}%` }}</span>
            <time>{{ formatTaskTime(taskCreatedValue(entry.job, entry.key)) }}</time>
          </summary>
          <p v-if="entry.job.has_warnings" class="inline-notice warning" role="status">处理已完成，自动重命名未完成</p>
          <div class="processing-job-stages">
            <button v-for="stage in entry.job.stages" :key="`${entry.job.job_id}:${stage.stage}`" class="task-stage-row" type="button" :disabled="!stage.task_id" @click="stage.task_id && openTask(stage.task_id, $event)">
              <span><i :class="`status-dot ${stage.status}`" aria-hidden="true"></i>{{ imageStageLabel(stage.stage) }}</span>
              <span>{{ imageStageStatusLabel(stage.status) }}</span>
              <span v-if="showTaskDiagnostics">{{ stage.task_id || (stage.status === 'skipped' ? '未启用' : '等待创建叶子任务') }}</span>
              <span v-else>{{ stage.attempt != null ? `第 ${stage.attempt} 次尝试` : stage.status === 'skipped' ? '未启用' : '—' }}</span>
              <span v-if="stage.error" class="task-error">{{ stage.error.error || '阶段失败' }}</span>
              <span v-if="stage.status === 'warning'" class="task-error warning">处理完成，自动重命名未完成</span>
            </button>
          </div>
        </details>
        <button
          v-else
          class="task-row"
          :class="{ selected: selectedTask?.task_id === entry.task.task_id }"
          type="button"
          role="row"
          :aria-label="taskRowAriaLabel(entry.task)"
          @click="openTask(entry.task.task_id, $event)"
        >
          <span class="task-status-cell" role="cell"><i :class="`status-dot ${entry.task.status}`" aria-hidden="true"></i>{{ taskStatusLabel(entry.task.status) }}</span>
          <span class="task-type-cell" role="cell" data-label="类型">
            <span>{{ taskTypeLabel(entry.task.task_type) }}</span>
            <span v-if="activityById.get(entry.task.task_id)" class="task-activity" :aria-label="activityById.get(entry.task.task_id)?.ariaLabel">
              <b>{{ activityById.get(entry.task.task_id)?.turns }}</b><time>{{ activityById.get(entry.task.task_id)?.lastActivity }}</time>
            </span>
          </span>
          <span class="task-source" role="cell" data-label="来源">{{ submissionModeLabel(entry.task.historical_unclassified ? 'unclassified' : entry.task.submission_mode) }}<small v-if="entry.task.image_stage || entry.task.task_type === 'image_auto_rename'"> · {{ imageStageLabel(entry.task.image_stage || 'auto_rename') }}</small></span>
          <span class="task-image" role="cell" data-label="图片">{{ entry.task.image?.filename || '—' }}</span>
          <span class="task-progress" role="cell" data-label="进度">{{ entry.task.progress == null ? '—' : `${Math.round(entry.task.progress * 100)}%` }}</span>
          <time role="cell">{{ formatTaskTime(taskCreatedValue(entry.task, entry.key)) }}</time>
        </button>
      </template>
      <template v-if="loading && !taskEntries.length">
        <div v-for="n in 5" :key="n" class="task-skeleton"></div>
      </template>
      <div v-if="!loading && !taskEntries.length" class="empty-state compact"><h2>没有匹配的任务</h2></div>
    </div>
    <button v-if="cursor" class="quiet load-more" type="button" @click="loadTasks({ append: true })">加载更多</button>
    <TaskDrawer
      v-if="selectedTask"
      :task="selectedTask"
      :retrying="retrying"
      :return-focus="taskTrigger"
      @close="closeTask"
      @retry="retryTask"
      @retry-stage="retryTask"
      @retry-full="retryTask"
    />
  </section>
</template>
