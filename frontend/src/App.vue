<script setup>
// MemeMeow 的主工作台：检索、图片库、异步处理与上传共享同一组任务状态。
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { api, pollTask } from './api'

const page = ref('search')
const pages = [
  { id: 'search', label: '检索' },
  { id: 'library', label: '图片库' },
  { id: 'upload', label: '上传' },
  { id: 'tasks', label: '处理任务' },
]
const busy = ref(false)
const error = ref('')
const config = ref(null)
const task = ref(null)

const query = ref('')
const resultCount = ref(8)
const llmEnhance = ref(false)
const results = ref([])

const directory = ref('')
const directories = ref([])
const images = ref([])
const filter = ref('')
const newDirectory = ref('')
const selectionMode = ref(false)
const selectedImages = ref(new Set())
const retryBusy = ref(false)
const retryNotice = ref('')

const files = ref([])
const autoName = ref(false)
const uploadResults = ref([])

const taskItems = ref([])
const taskCursor = ref(null)
const taskLoading = ref(false)
const taskStatus = ref('')
const taskType = ref('')
const selectedTask = ref(null)
const embeddingTask = ref(null)
let taskTimer = null

const hasActiveTasks = computed(() => taskItems.value.some((item) => item.status === 'queued' || item.status === 'running'))
const selectedCount = computed(() => selectedImages.value.size)
const embeddingState = computed(() => {
  if (embeddingTask.value?.status === 'queued' || embeddingTask.value?.status === 'running') return 'running'
  if (embeddingTask.value?.status === 'failed') return 'failed'
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

function clearError() { error.value = '' }
function showError(reason) { error.value = reason?.message || '请求失败' }
function isTerminal(item) { return item?.status === 'succeeded' || item?.status === 'failed' }

async function runSearch() {
  clearError(); busy.value = true
  try { results.value = (await api.search({ query: query.value, n_results: resultCount.value, llm_enhance: llmEnhance.value })).results } catch (reason) { showError(reason) } finally { busy.value = false }
}

async function loadLibrary() {
  clearError(); busy.value = true
  try {
    const data = await api.images({ directory: directory.value, search: filter.value })
    images.value = data.items; directories.value = data.directories || []
    const keys = new Set(images.value.map(imageKey))
    selectedImages.value = new Set([...selectedImages.value].filter((key) => keys.has(key)))
  } catch (reason) { showError(reason) } finally { busy.value = false }
}

function imageKey(item) { return `${item.directory || ''}/${item.filename}` }
function isRetryable(item) { return item.metadata?.status !== 'ready' }
function toggleImageSelection(item) {
  if (!isRetryable(item)) return
  const next = new Set(selectedImages.value)
  const key = imageKey(item)
  if (next.has(key)) next.delete(key); else next.add(key)
  selectedImages.value = next
}
function toggleSelectionMode() {
  selectionMode.value = !selectionMode.value
  if (!selectionMode.value) selectedImages.value = new Set()
  retryNotice.value = ''
}

async function retryImages(items, label) {
  if (!items.length || retryBusy.value) return
  clearError(); retryNotice.value = ''; retryBusy.value = true
  try {
    const response = await api.contextBatch({ items, include_unready: true })
    const queued = response.results.filter((item) => item.task_id).length
    const failed = response.results.filter((item) => item.error).length
    retryNotice.value = `${label}：已提交 ${queued} 个任务${failed ? `，${failed} 项未提交` : ''}`
    selectedImages.value = new Set()
    selectionMode.value = false
    await loadLibrary()
    await loadTasks()
  } catch (reason) { showError(reason) } finally { retryBusy.value = false }
}

function retrySelected() {
  const items = images.value.filter((item) => selectedImages.value.has(imageKey(item))).map((item) => ({ directory: item.directory || '', filename: item.filename }))
  return retryImages(items, '重试选中')
}

function retryAll() {
  return retryImages(images.value.filter(isRetryable).map((item) => ({ directory: item.directory || '', filename: item.filename })), '重试未就绪图片')
}

function embeddingLabel(status) {
  return { ready: '已索引', pending: '待生成', blocked: '缺少 JSON' }[status] || '未索引'
}

async function makeDirectory() {
  if (!newDirectory.value.trim()) return
  try { await api.createDirectory({ name: newDirectory.value.trim(), parent: directory.value }); newDirectory.value = ''; await loadLibrary() } catch (reason) { showError(reason) }
}

async function rename(item) {
  const name = window.prompt('新文件名', item.filename)
  if (!name) return
  try { await api.rename({ directory: directory.value, filename: item.filename, new_name: name }); await loadLibrary() } catch (reason) { showError(reason) }
}

function onFiles(event) { files.value = [...event.target.files] }
async function upload() {
  if (!files.value.length) return
  clearError(); busy.value = true
  try { uploadResults.value = (await api.upload(directory.value, files.value, autoName.value)).results; files.value = [] } catch (reason) { showError(reason) } finally { busy.value = false }
}

async function loadTasks({ append = false } = {}) {
  taskLoading.value = true
  try {
    const response = await api.tasks({ status: taskStatus.value || undefined, task_type: taskType.value || undefined, cursor: append ? taskCursor.value : undefined })
    taskItems.value = append ? [...taskItems.value, ...response.items] : response.items
    taskCursor.value = response.next_cursor
    if (selectedTask.value) {
      const refreshed = taskItems.value.find((item) => item.task_id === selectedTask.value.task_id)
      if (refreshed) selectedTask.value = refreshed
    }
  } catch (reason) { showError(reason) } finally { taskLoading.value = false }
}

async function openTask(id) {
  try { selectedTask.value = await api.task(id) } catch (reason) { showError(reason) }
}

async function retryTask() {
  const image = selectedTask.value?.image
  if (!image?.relative_path) return
  const relative = image.relative_path.split('/')
  const filename = relative.pop()
  try {
    const response = await api.context({ directory: relative.join('/'), filename })
    await loadTasks(); await openTask(response.task_id)
  } catch (reason) { showError(reason) }
}

function startTaskPolling() {
  stopTaskPolling()
  if (page.value !== 'tasks' || document.visibilityState !== 'visible' || !hasActiveTasks.value) return
  taskTimer = window.setInterval(() => loadTasks(), 2500)
}
function stopTaskPolling() { if (taskTimer) window.clearInterval(taskTimer); taskTimer = null }
function onVisibilityChange() { startTaskPolling() }

async function generateCache() {
  try {
    const submitted = await api.generateCache(); task.value = submitted; embeddingTask.value = submitted
    task.value = await pollTask(submitted.task_id, (next) => { task.value = next; embeddingTask.value = next })
    config.value = await api.config()
    if (page.value === 'library') await loadLibrary()
  } catch (reason) { showError(reason) }
}

function changePage(next) {
  page.value = next; clearError()
  if (next === 'library') loadLibrary()
  if (next === 'tasks') { loadTasks().then(startTaskPolling) } else stopTaskPolling()
}
function openUploadTask(item) { if (item.metadata_job_id) { page.value = 'tasks'; loadTasks().then(() => openTask(item.metadata_job_id)).then(startTaskPolling) } }

onMounted(async () => {
  try { config.value = await api.config() } catch (reason) { showError(reason) }
  document.addEventListener('visibilitychange', onVisibilityChange)
})
onBeforeUnmount(() => { stopTaskPolling(); document.removeEventListener('visibilitychange', onVisibilityChange) })
</script>

<template>
  <div class="shell">
    <header class="topbar">
      <div class="brand"><span class="brand-mark" aria-hidden="true">M</span><div><strong>MemeMeow</strong></div></div>
      <div class="service-state"><span class="pulse" :class="`pulse-${embeddingState}`"></span><span>{{ config?.embedding_model || '服务配置加载中' }}</span><span class="embedding-global" :class="embeddingState">{{ embeddingStateLabel }}</span></div>
    </header>
    <div class="body">
      <aside class="sidebar">
        <nav><button v-for="item in pages" :key="item.id" :class="{ active: page === item.id }" @click="changePage(item.id)">{{ item.label }}</button></nav>
        <div class="sidebar-foot"><button class="cache-button" title="生成检索缓存" @click="generateCache">生成检索缓存</button><div v-if="task" class="task-line"><span :class="`dot ${task.status}`"></span>{{ task.message || task.status }}<b v-if="task.progress != null">{{ Math.round(task.progress * 100) }}%</b></div></div>
      </aside>
      <main class="content">
        <div v-if="error" class="error-banner" role="alert">{{ error }}<button aria-label="关闭错误" @click="clearError">关闭</button></div>
        <section v-if="page === 'search'" class="workspace">
          <div class="section-head"><div><h1>找到合适的表达</h1><p>用一句自然语言描述你想要的情绪或场景。</p></div><span class="cache-pill" :class="{ ready: config }">{{ config ? 'API 已连接' : '等待连接' }}</span></div>
          <form class="search-form" @submit.prevent="runSearch"><input v-model="query" placeholder="例如：开会时发现自己忘记准备材料" autofocus /><button class="primary" :disabled="busy || !query.trim()">{{ busy ? '检索中...' : '开始检索' }}</button></form>
          <div class="controls"><label>结果数量 <input v-model.number="resultCount" type="number" min="1" max="30" /></label><label class="switch"><input v-model="llmEnhance" type="checkbox" /><span></span>使用 LLM 优化语义</label></div>
          <div v-if="results.length" class="result-grid"><a v-for="url in results" :key="url" class="result-item" :href="url" target="_blank"><img :src="url" alt="检索结果" loading="lazy" /></a></div>
          <div v-else class="empty-state"><h2>{{ busy ? '正在分析你的描述' : '还没有检索结果' }}</h2></div>
        </section>
        <section v-else-if="page === 'library'" class="workspace">
          <div class="section-head"><div><h1>图片库</h1><p>浏览、筛选和整理本地图片。</p></div></div>
          <div class="toolbar"><select v-model="directory" @change="loadLibrary"><option value="">根目录</option><option v-for="item in directories" :key="item" :value="directory ? `${directory}/${item}` : item">{{ item }}</option></select><input v-model="filter" placeholder="筛选文件名" @keyup.enter="loadLibrary" /><button @click="loadLibrary">刷新</button><input v-model="newDirectory" placeholder="新目录名" @keyup.enter="makeDirectory" /><button @click="makeDirectory">创建目录</button><span class="toolbar-spacer"></span><button class="quiet" :class="{ active: selectionMode }" @click="toggleSelectionMode">{{ selectionMode ? '完成选择' : '选择图片' }}</button><button class="quiet" :disabled="retryBusy || !selectedCount" @click="retrySelected">重试选中<span v-if="selectedCount">（{{ selectedCount }}）</span></button><button class="primary toolbar-primary" :disabled="retryBusy || !images.some(isRetryable)" @click="retryAll">{{ retryBusy ? '提交中...' : '重试所有未就绪' }}</button></div>
          <div v-if="retryNotice" class="inline-notice" role="status">{{ retryNotice }}</div>
          <div class="library-list"><article v-for="item in images" :key="imageKey(item)" class="library-row"><label v-if="selectionMode" class="image-check" :class="{ disabled: !isRetryable(item) }"><input type="checkbox" :checked="selectedImages.has(imageKey(item))" :disabled="!isRetryable(item) || retryBusy" @change="toggleImageSelection(item)" /><span></span></label><img :src="item.media_url" alt="" loading="lazy" /><div class="file-meta"><strong>{{ item.filename }}</strong><small>{{ Math.ceil(item.size / 1024) }} KB · {{ item.extension }}</small></div><span class="metadata-state" :class="item.metadata.status">{{ item.metadata.status }}</span><span class="embedding-state" :class="item.embedding_status">{{ embeddingLabel(item.embedding_status) }}</span><button class="quiet" @click="rename(item)">重命名</button></article><div v-if="!images.length" class="empty-state compact"><h2>这个目录还没有图片</h2></div></div>
        </section>
        <section v-else-if="page === 'upload'" class="workspace narrow">
          <div class="section-head"><div><h1>上传图片</h1><p>支持 PNG、JPG、JPEG 和 GIF。</p></div></div>
          <div class="upload-panel"><label class="field">目标目录<select v-model="directory"><option value="">根目录</option><option v-for="item in directories" :key="item" :value="item">{{ item }}</option></select></label><label class="drop-zone"><input type="file" multiple accept=".png,.jpg,.jpeg,.gif" @change="onFiles" /><span class="drop-title">选择图片文件</span><span class="drop-sub">{{ files.length ? `已选择 ${files.length} 个文件` : '点击选择或拖入文件' }}</span></label><label class="switch"><input v-model="autoName" type="checkbox" /><span></span>处理完成后按标题自动命名</label><button class="primary wide" :disabled="busy || !files.length" @click="upload">{{ busy ? '上传中...' : '上传所选图片' }}</button></div>
          <div v-if="uploadResults.length" class="upload-results"><div v-for="item in uploadResults" :key="item.filename" class="upload-result" :class="{ fail: !item.ok }"><span>{{ item.ok ? '完成' : '失败' }}</span><strong>{{ item.filename }}</strong><button v-if="item.metadata_job_id" class="quiet" @click="openUploadTask(item)">查看任务</button><small v-else>{{ item.ok ? item.saved_filename : item.error }}</small></div></div>
        </section>
        <section v-else class="workspace task-workspace" :class="{ detail: selectedTask }">
          <div class="section-head"><div><h1>处理任务</h1></div><button class="quiet" :disabled="taskLoading" @click="loadTasks">刷新</button></div>
          <div class="task-toolbar"><label>状态<select v-model="taskStatus" @change="loadTasks"><option value="">全部</option><option value="queued">排队中</option><option value="running">处理中</option><option value="succeeded">已完成</option><option value="failed">失败</option></select></label><label>类型<select v-model="taskType" @change="loadTasks"><option value="">全部</option><option value="meme_context_generation">语境生成</option><option value="cache_generation">检索缓存</option><option value="metadata_repair">元数据修复</option></select></label></div>
          <div class="task-table" :class="{ loading: taskLoading }"><div class="task-head"><span>状态</span><span>类型</span><span>关联图片</span><span>进度</span><span>最近更新</span></div><button v-for="item in taskItems" :key="item.task_id" class="task-row" :class="{ selected: selectedTask?.task_id === item.task_id }" @click="openTask(item.task_id)"><span><i :class="`status-dot ${item.status}`"></i>{{ item.status }}</span><span>{{ item.task_type }}</span><span class="task-image">{{ item.image?.filename || '—' }}</span><span>{{ item.progress == null ? '—' : `${Math.round(item.progress * 100)}%` }}</span><time>{{ new Date(item.updated_at).toLocaleString() }}</time></button><div v-if="taskLoading && !taskItems.length" class="task-skeleton" v-for="n in 5" :key="n"></div><div v-if="!taskLoading && !taskItems.length" class="empty-state compact"><h2>没有匹配的任务</h2></div></div>
          <button v-if="taskCursor" class="quiet load-more" @click="loadTasks({ append: true })">加载更多</button>
          <aside v-if="selectedTask" class="task-drawer" aria-label="任务详情"><div class="drawer-head"><h2>任务详情</h2><button class="quiet" aria-label="关闭任务详情" @click="selectedTask = null">关闭</button></div><dl><div><dt>状态</dt><dd>{{ selectedTask.status }}</dd></div><div><dt>类型</dt><dd>{{ selectedTask.task_type }}</dd></div><div><dt>阶段</dt><dd>{{ selectedTask.message || '—' }}</dd></div><div><dt>创建时间</dt><dd>{{ new Date(selectedTask.created_at).toLocaleString() }}</dd></div><div><dt>完成时间</dt><dd>{{ selectedTask.completed_at ? new Date(selectedTask.completed_at).toLocaleString() : '—' }}</dd></div><div v-if="selectedTask.error"><dt>错误</dt><dd>{{ selectedTask.error.error }}</dd></div><div v-if="selectedTask.result?.auto_named !== undefined"><dt>自动命名</dt><dd>{{ selectedTask.result.auto_named ? selectedTask.result.saved_filename : selectedTask.result.auto_name_error || '未执行' }}</dd></div></dl><button v-if="selectedTask.task_type === 'meme_context_generation' && selectedTask.status === 'failed'" class="primary" @click="retryTask">重试</button></aside>
        </section>
      </main>
    </div>
  </div>
</template>
