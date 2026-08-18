<script setup lang="ts">
/** MemeMeow 根组件：只负责编排工作区、服务状态与跨工作区导航。 */
import { computed, onMounted, shallowRef } from 'vue'
import { api, pollTask } from './api'
import AppHeader from './components/AppHeader.vue'
import CollectionsWorkspace from './components/CollectionsWorkspace.vue'
import LibraryWorkspace from './components/LibraryWorkspace.vue'
import SearchWorkspace from './components/SearchWorkspace.vue'
import TasksWorkspace from './components/TasksWorkspace.vue'
import UploadWorkspace from './components/UploadWorkspace.vue'
import WorkspaceNav from './components/WorkspaceNav.vue'
import type { NavigationItem, PageId, ServiceConfig, TaskItem } from './types'
import { errorMessage } from './utils/presentation'

const pages: NavigationItem[] = [
  { id: 'search', label: '检索' },
  { id: 'library', label: '图片库' },
  { id: 'collections', label: '合集' },
  { id: 'upload', label: '上传' },
  { id: 'tasks', label: '处理任务' },
]

const page = shallowRef<PageId>('search')
const error = shallowRef('')
const config = shallowRef<ServiceConfig | null>(null)
const cacheTask = shallowRef<TaskItem | null>(null)
const cacheBusy = shallowRef(false)
const libraryRefreshToken = shallowRef(0)
const pendingTaskId = shallowRef<string | null>(null)

const embeddingState = computed(() => {
  if (cacheTask.value?.status === 'queued' || cacheTask.value?.status === 'running') return 'running'
  if (cacheTask.value?.status === 'failed') return 'failed'
  if (config.value?.embedding_cache_ready) return 'ready'
  return config.value ? 'missing' : 'unknown'
})

const embeddingStateLabel = computed(() => ({
  ready: 'Embedding 已就绪',
  running: 'Embedding 生成中',
  failed: 'Embedding 生成失败',
  missing: 'Embedding 未生成',
  unknown: 'Embedding 状态未知',
}[embeddingState.value]))

/** 清空全局错误提示，供工作区在发起新请求前调用。 */
function clearError(): void {
  error.value = ''
}

/** 展示子工作区上报的可读错误消息。 */
function showError(message: string): void {
  error.value = message || '请求失败'
}

/** 切换工作区并清理上一工作区的错误和待打开任务。 */
function changePage(next: PageId): void {
  page.value = next
  error.value = ''
  if (next !== 'tasks') pendingTaskId.value = null
}

/** 从上传结果进入任务页，并把目标任务作为显式路由意图传递。 */
function openUploadTask(taskId: string): void {
  pendingTaskId.value = taskId
  page.value = 'tasks'
  error.value = ''
}

/** 提交并轮询缓存任务；状态由不会随工作区切换卸载的根组件持有。 */
async function generateCache(): Promise<void> {
  if (cacheBusy.value || ['queued', 'running'].includes(cacheTask.value?.status || '') || !config.value) return
  clearError()
  cacheBusy.value = true
  try {
    const submitted = await api.generateCache() as TaskItem
    cacheTask.value = submitted
    cacheTask.value = await pollTask(submitted.task_id, (next: TaskItem) => { cacheTask.value = next }) as TaskItem
    config.value = await api.config()
    libraryRefreshToken.value += 1
    if (cacheTask.value.status === 'failed') {
      showError(cacheTask.value.error?.message || cacheTask.value.message || '检索缓存生成失败')
    }
  } catch (reason) {
    showError(errorMessage(reason))
  } finally {
    cacheBusy.value = false
  }
}

onMounted(async () => {
  try {
    config.value = await api.config()
  } catch (reason) {
    showError(errorMessage(reason))
  }
})
</script>

<template>
  <div class="shell">
    <AppHeader :config="config" :embedding-state="embeddingState" :embedding-state-label="embeddingStateLabel" />
    <div class="body">
      <WorkspaceNav :pages="pages" :active-page="page" @navigate="changePage" />
      <main class="content">
        <div v-if="error" class="error-banner" role="alert">
          <span>{{ error }}</span>
          <button type="button" aria-label="关闭错误" @click="clearError">关闭</button>
        </div>
        <SearchWorkspace v-if="page === 'search'" :config="config" @error="showError" @clear-error="clearError" />
        <LibraryWorkspace
          v-else-if="page === 'library'"
          :config="config"
          :cache-task="cacheTask"
          :cache-busy="cacheBusy"
          :refresh-token="libraryRefreshToken"
          @error="showError"
          @clear-error="clearError"
          @generate-cache="generateCache"
        />
        <CollectionsWorkspace v-else-if="page === 'collections'" @error="showError" />
        <UploadWorkspace
          v-else-if="page === 'upload'"
          :config="config"
          @error="showError"
          @clear-error="clearError"
          @open-task="openUploadTask"
        />
        <TasksWorkspace v-else :initial-task-id="pendingTaskId" @error="showError" />
      </main>
    </div>
  </div>
</template>
