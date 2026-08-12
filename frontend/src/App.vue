<script setup>
// MemeMeow 的主工作台：检索、图片库、异步处理与上传共享同一组任务状态。
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { api, pollTask } from './api'

const page = ref('search')
const pages = [
  { id: 'search', label: '检索' },
  { id: 'library', label: '图片库' },
  { id: 'upload', label: '上传' },
  { id: 'tasks', label: '处理任务' },
  { id: 'settings', label: '后端设置' },
]
const busy = ref(false)
const error = ref('')
const config = ref(null)

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
const cacheTask = ref(null)
const cacheBusy = ref(false)
const embeddingTask = ref(null)
const backendSettings = ref(null)
const settingsLoading = ref(false)
const settingsSaving = ref(false)
const settingsNotice = ref('')
const settingsConcurrency = ref(1)
// 设置凭据只在当前页面内存中短暂保存，绝不写入 localStorage、任务记录或日志。
const settingsAdminToken = ref('')

// 预览状态同时承载图片放大和经接口读取的 sidecar JSON，避免批量选择状态互相污染。
const previewImage = ref(null)
const previewJson = ref(null)
const previewLoading = ref(false)
const previewError = ref('')
const previewCopyNotice = ref('')
const copyNotice = ref('')
const previewDialog = ref(null)
const previewCloseButton = ref(null)
let previewTriggerElement = null
let taskTimer = null
let previewRequestId = 0
let copyNoticeTimer = null
let copyRequestId = 0
const clipboardImageMime = 'image/png'

const hasActiveTasks = computed(() => taskItems.value.some((item) => item.status === 'queued' || item.status === 'running'))
const selectedCount = computed(() => selectedImages.value.size)
// 以规范化媒体路径作为图片身份，避免后端或代理附加查询参数时重复展示同一张图。
const uniqueResults = computed(() => {
  const seen = new Set()
  return results.value.filter((url) => {
    const identity = resultIdentity(url)
    if (!identity || seen.has(identity)) return false
    seen.add(identity)
    return true
  })
})
// 缓存状态同时驱动图片库操作和顶部 Embedding 反馈，避免任务执行期间重复提交。
const cacheGenerating = computed(() => cacheBusy.value || ['queued', 'running'].includes(cacheTask.value?.status))
const cacheButtonLabel = computed(() => {
  if (!config.value) return '等待服务连接...'
  if (cacheGenerating.value) return cacheTask.value?.status === 'queued' ? '排队中...' : '生成中...'
  if (cacheTask.value?.status === 'failed') return '重新生成检索缓存'
  return '生成检索缓存'
})
const cacheButtonTitle = computed(() => {
  if (!config.value) return '等待服务配置加载完成'
  if (cacheGenerating.value) return '检索缓存正在生成'
  if (cacheTask.value?.status === 'failed') return '上次生成失败，点击重试'
  return '扫描图片库并生成检索缓存'
})
const cacheTaskStatusLabel = computed(() => {
  if (cacheBusy.value && !cacheTask.value) return '正在提交缓存任务'
  if (cacheTask.value?.status === 'queued') return '等待生成'
  if (cacheTask.value?.status === 'running') return cacheTask.value.message || '正在生成缓存'
  if (cacheTask.value?.status === 'succeeded') return '缓存已更新'
  if (cacheTask.value?.status === 'failed') return cacheTask.value.message || '缓存生成失败'
  return ''
})
const embeddingState = computed(() => {
  if (cacheGenerating.value || embeddingTask.value?.status === 'queued' || embeddingTask.value?.status === 'running') return 'running'
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
const previewJsonText = computed(() => previewJson.value ? JSON.stringify(previewJson.value, null, 2) : '')

function clearError() { error.value = '' }
function showError(reason) { error.value = reason?.message || '请求失败' }
function isTerminal(item) { return item?.status === 'succeeded' || item?.status === 'failed' }

/**
 * 计算检索结果的稳定图片身份，去掉仅影响缓存的查询参数和片段。
 * @param {string} url 后端返回的媒体地址。
 * @returns {string} 用于去重和 Vue key 的规范化地址。
 */
function resultIdentity(url) {
  if (typeof url !== 'string' || !url.trim()) return ''
  try {
    const parsed = new URL(url, window.location.origin)
    return `${parsed.origin}${parsed.pathname}`
  } catch {
    return url.split(/[?#]/, 1)[0]
  }
}

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

/**
 * 将后端任务状态转换为用户可读的中文标签，保留原始状态作为 CSS 状态钩子。
 * @param {string} status 后端返回的任务状态。
 * @returns {string} 用于界面展示的状态文本。
 */
function taskStatusLabel(status) {
  return { queued: '排队中', running: '处理中', succeeded: '已完成', failed: '失败' }[status] || '未知状态'
}

/**
 * 将任务类型转换为可扫描的中文名称，避免把内部枚举直接暴露给用户。
 * @param {string} type 后端返回的任务类型。
 * @returns {string} 用于列表和详情面板的任务名称。
 */
function taskTypeLabel(type) {
  return { meme_context_generation: '语境生成', cache_generation: '检索缓存', metadata_repair: '元数据修复' }[type] || type || '未知任务'
}

/**
 * 将任务时间压缩到桌面和移动端都能稳定容纳的本地化格式。
 * @param {string|number|Date} value 后端时间戳。
 * @returns {string} 无效时间返回占位符，否则返回中文本地时间。
 */
function formatTaskTime(value) {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '—'
  return date.toLocaleString('zh-CN', { year: 'numeric', month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}

/**
 * 将图片语境元数据状态转换为用户可读标签。
 * @param {string} status 图片元数据状态。
 * @returns {string} 对应的中文状态。
 */
function metadataLabel(status) {
  return { ready: '语境就绪', pending: '待生成', repair_required: '需修复' }[status] || '状态未知'
}

/** 打开图片预览并按需读取该图片的完整 sidecar JSON。 */
async function openImagePreview(item, event) {
  previewTriggerElement = event?.currentTarget instanceof HTMLElement ? event.currentTarget : null
  previewImage.value = item
  previewJson.value = null
  previewError.value = ''
  previewCopyNotice.value = ''
  previewLoading.value = true
  const requestId = ++previewRequestId
  await nextTick()
  previewCloseButton.value?.focus()
  try {
    const data = await api.imageMetadata({ directory: item.directory || '', filename: item.filename })
    if (requestId === previewRequestId) previewJson.value = data
  } catch (reason) {
    if (requestId === previewRequestId) previewError.value = reason?.message || '图片 JSON 读取失败'
  } finally {
    if (requestId === previewRequestId) previewLoading.value = false
  }
}

/** 关闭图片预览并取消过期请求对当前弹层的影响。 */
function closeImagePreview() {
  previewRequestId += 1
  previewImage.value = null
  previewJson.value = null
  previewLoading.value = false
  previewError.value = ''
  previewCopyNotice.value = ''
  const trigger = previewTriggerElement
  previewTriggerElement = null
  nextTick(() => {
    if (trigger?.isConnected) trigger.focus()
  })
}

/** 将文本写入剪贴板，供预览弹层复制 JSON 使用。 */
async function writeTextToClipboard(value) {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(value)
    return
  }
  const textarea = document.createElement('textarea')
  textarea.value = value
  textarea.setAttribute('readonly', '')
  textarea.style.position = 'fixed'
  textarea.style.opacity = '0'
  document.body.appendChild(textarea)
  textarea.select()
  try {
    if (!document.execCommand('copy')) throw new Error('clipboard_unavailable')
  } finally {
    textarea.remove()
  }
}

/** 复制预览弹层中的完整 JSON。 */
async function copyPreviewJson() {
  if (!previewJsonText.value) return
  try {
    await writeTextToClipboard(previewJsonText.value)
    previewCopyNotice.value = 'JSON 已复制'
  } catch (reason) {
    previewCopyNotice.value = reason?.message || 'JSON 复制失败，请检查浏览器权限'
  }
}

/**
 * 检查当前浏览器是否暴露可写入 PNG 的异步图片剪贴板能力。
 * @returns {boolean} 浏览器具备图片剪贴板写入能力时返回 true。
 * @remarks 只检查图片类型；检索结果不能退化为文本或 URL 剪贴板。
 */
function supportsImageClipboard() {
  if (typeof navigator.clipboard?.write !== 'function' || typeof ClipboardItem !== 'function') return false
  if (typeof ClipboardItem.supports !== 'function') return true
  try {
    return ClipboardItem.supports(clipboardImageMime)
  } catch {
    return false
  }
}

/**
 * 将浏览器的剪贴板写入异常转换为可操作的错误码。
 * @param {unknown} reason 浏览器 Clipboard API 抛出的异常。
 * @returns {string} 供复制结果提示使用的稳定错误码。
 * @remarks 保留权限与安全上下文的区别，避免把所有 Chrome 拒绝都误报为浏览器不支持。
 */
function imageClipboardWriteFailureCode(reason) {
  if (reason?.name === 'NotAllowedError') return window.isSecureContext ? 'image_clipboard_permission_denied' : 'image_clipboard_insecure_context'
  if (reason?.name === 'NotSupportedError') return 'image_clipboard_unsupported'
  return 'image_clipboard_write_failed'
}

/**
 * 将可解码的图片 Blob 转成剪贴板兼容的 PNG Blob。
 * @param {Blob} blob 后端返回的原始图片二进制。
 * @returns {Promise<Blob>} 可供 ClipboardItem 写入的 PNG 二进制。
 * @remarks Chromium 当前只接受 image/png；JPEG/GIF 在写入前在浏览器内转换，仍然只写图片二进制。
 */
async function convertImageBlobToPng(blob) {
  let bitmap
  let objectUrl
  try {
    if (typeof createImageBitmap === 'function') {
      bitmap = await createImageBitmap(blob)
    } else {
      objectUrl = URL.createObjectURL(blob)
      const image = new Image()
      image.decoding = 'async'
      image.src = objectUrl
      await image.decode()
      bitmap = image
    }
    const width = bitmap.width
    const height = bitmap.height
    if (!width || !height) throw new Error('image_decode_failed')
    const canvas = document.createElement('canvas')
    canvas.width = width
    canvas.height = height
    const context = canvas.getContext('2d')
    if (!context) throw new Error('image_decode_failed')
    context.drawImage(bitmap, 0, 0)
    const png = await new Promise((resolve, reject) => {
      canvas.toBlob((result) => {
        if (result) resolve(result)
        else reject(new Error('image_encode_failed'))
      }, clipboardImageMime)
    })
    return png
  } catch {
    throw new Error('image_decode_failed')
  } finally {
    bitmap?.close?.()
    if (objectUrl) URL.revokeObjectURL(objectUrl)
  }
}

/**
 * 请求并准备检索结果图片，供 ClipboardItem 的延迟数据使用。
 * @param {string} url 后端返回的同源图片地址。
 * @returns {Promise<Blob>} 原始 PNG 或转换后的 PNG 图片二进制。
 * @remarks credentials 保留同源会话，HTTP 错误和非图片响应都不会进入剪贴板。
 */
async function fetchImageForClipboard(url) {
  let response
  try {
    response = await fetch(url, { credentials: 'same-origin' })
    if (!response?.ok) throw new Error('image_fetch_failed')
  } catch {
    throw new Error('image_fetch_failed')
  }
  let blob
  try {
    blob = await response.blob()
  } catch {
    throw new Error('image_fetch_failed')
  }
  const mime = typeof blob?.type === 'string' ? blob.type.trim().toLowerCase() : ''
  if (!mime.startsWith('image/')) throw new Error('image_mime_unavailable')
  if (mime === clipboardImageMime) return blob
  return convertImageBlobToPng(blob)
}

/**
 * 将检索结果的二进制图片写入系统剪贴板。
 * @param {string} url 后端返回的图片地址，可为相对路径。
 * @returns {Promise<void>} 写入完成或把明确失败原因展示到页面后结束。
 * @remarks 检索图片禁止降级为文本或 URL，所有失败都必须保留在图片复制流程内。
 */
async function copySearchImage(url) {
  const requestId = ++copyRequestId
  window.clearTimeout(copyNoticeTimer)
  let notice = ''
  try {
    if (window.isSecureContext === false) throw new Error('image_clipboard_insecure_context')
    if (!supportsImageClipboard()) throw new Error('image_clipboard_unsupported')
    if (typeof fetch !== 'function') throw new Error('image_fetch_unavailable')

    // ClipboardItem 先接收 Promise，再异步读取图片；write 本身在点击手势的同步调用栈内执行，避免网络延迟耗尽用户激活窗口。
    let imageDataFailure = null
    const imageData = fetchImageForClipboard(url).catch((reason) => {
      imageDataFailure = reason
      throw reason
    })
    // ClipboardItem 会消费原 Promise；额外挂载拒绝处理，避免浏览器在写入失败后报告未处理异常。
    imageData.catch(() => {})
    let item
    try {
      item = new ClipboardItem({ [clipboardImageMime]: imageData })
    } catch {
      throw new Error('image_mime_unavailable')
    }
    try {
      await navigator.clipboard.write([item])
    } catch (reason) {
      if (imageDataFailure) throw imageDataFailure
      throw new Error(imageClipboardWriteFailureCode(reason))
    }
    notice = '图片已复制'
  } catch (reason) {
    notice = {
      image_clipboard_unsupported: '图片复制失败：当前浏览器不支持复制图片',
      image_fetch_unavailable: '图片复制失败：图片加载功能不可用，无法复制',
      image_fetch_failed: '图片复制失败：图片加载失败，无法复制',
      image_mime_unavailable: '图片复制失败：图片 MIME 类型不可用，无法复制',
      image_decode_failed: '图片复制失败：图片格式无法转换，图片未复制',
      image_clipboard_insecure_context: '图片复制失败：当前页面不是安全上下文，请使用 http://localhost:5275 或 HTTPS',
      image_clipboard_permission_denied: '图片复制失败：Chrome 拒绝剪贴板写入，请在网站设置中允许剪贴板后重试',
      image_clipboard_write_failed: '图片复制失败：剪贴板写入被拒绝，图片未复制',
    }[reason?.message] || '图片复制失败，图片未复制'
  }
  if (requestId !== copyRequestId) return
  copyNotice.value = notice
  copyNoticeTimer = window.setTimeout(() => { copyNotice.value = '' }, 2600)
}

/** 统一处理预览弹层的 Escape 关闭快捷键。 */
function onGlobalKeydown(event) {
  if (!previewImage.value) return
  if (event.key === 'Escape') {
    event.preventDefault()
    closeImagePreview()
    return
  }
  if (event.key !== 'Tab') return
  const focusable = [...(previewDialog.value?.querySelectorAll('button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])') || [])]
  if (!focusable.length) return
  const first = focusable[0]
  const last = focusable[focusable.length - 1]
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault()
    last.focus()
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault()
    first.focus()
  }
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

/** 加载后端设置页数据；不缓存密钥或部署路径。 */
async function loadBackendSettings() {
  if (typeof api.backendSettings !== 'function') return
  settingsLoading.value = true; settingsNotice.value = ''
  try {
    backendSettings.value = await api.backendSettings()
    settingsConcurrency.value = backendSettings.value.editable?.opencode_concurrency?.value ?? 1
  } catch (reason) { showError(reason) } finally { settingsLoading.value = false }
}

/** 保存并发待生效值，并明确告知操作者需要重启服务。 */
async function saveBackendSettings() {
  if (typeof api.updateBackendSettings !== 'function') return
  settingsSaving.value = true; settingsNotice.value = ''
  try {
    backendSettings.value = await api.updateBackendSettings(
      { opencode_concurrency: Number(settingsConcurrency.value) },
      settingsAdminToken.value.trim() || undefined,
    )
    // 成功后立即丢弃凭据，避免在页面生命周期内无必要地保留敏感值。
    settingsAdminToken.value = ''
    settingsNotice.value = backendSettings.value.restart_required ? '配置已保存，重启服务后生效' : '配置已保存'
  } catch (reason) { showError(reason) } finally { settingsSaving.value = false }
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

/** 提交并轮询整库检索缓存任务，供图片库工具栏的维护操作调用。 */
async function generateCache() {
  if (cacheGenerating.value || !config.value) return
  clearError(); cacheBusy.value = true
  try {
    const submitted = await api.generateCache()
    cacheTask.value = submitted
    embeddingTask.value = submitted
    const completed = await pollTask(submitted.task_id, (next) => { cacheTask.value = next; embeddingTask.value = next })
    cacheTask.value = completed
    config.value = await api.config()
    if (page.value === 'library') await loadLibrary()
    if (completed.status === 'failed') showError(completed.error?.message || completed.message || '检索缓存生成失败')
  } catch (reason) { showError(reason) } finally { cacheBusy.value = false }
}

function changePage(next) {
  if (next !== 'settings') settingsAdminToken.value = ''
  page.value = next; clearError()
  if (next === 'library') loadLibrary()
  if (next === 'settings') loadBackendSettings()
  if (next === 'tasks') { loadTasks().then(startTaskPolling) } else stopTaskPolling()
}
function openUploadTask(item) { if (item.metadata_job_id) { page.value = 'tasks'; loadTasks().then(() => openTask(item.metadata_job_id)).then(startTaskPolling) } }

onMounted(async () => {
  try { config.value = await api.config() } catch (reason) { showError(reason) }
  document.addEventListener('visibilitychange', onVisibilityChange)
  document.addEventListener('keydown', onGlobalKeydown)
})
onBeforeUnmount(() => {
  settingsAdminToken.value = ''
  stopTaskPolling()
  window.clearTimeout(copyNoticeTimer)
  document.removeEventListener('visibilitychange', onVisibilityChange)
  document.removeEventListener('keydown', onGlobalKeydown)
})
</script>

<template>
  <div class="shell">
    <!-- THESIS: 让真实图片和清晰状态成为工作台的第一视觉，拒绝营销式 hero。 OWN-WORLD: #F8F7FB 工作面、#2A2638 深墨、#5157D9 紫蓝操作色与倾斜 M 标记。 STORY: 用户描述意图、扫描结果并直接复制图片。 FIRST VIEWPORT: 顶栏固定服务状态，左侧工作区导航，内容区先显示检索标题与输入。 FORM: 操作型四区工作台，seed 83e1ae7b。 FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, and DESIGN.md -->
    <header class="topbar">
      <div class="brand" aria-label="MemeMeow"><span class="brand-mark" aria-hidden="true">M</span><div><strong>MemeMeow</strong></div></div>
      <div class="service-state" role="status" aria-live="polite"><span class="pulse" :class="`pulse-${embeddingState}`" aria-hidden="true"></span><span class="service-model" :title="config?.embedding_model || '服务配置加载中'">{{ config?.embedding_model || '服务配置加载中' }}</span><span class="service-divider" aria-hidden="true"></span><span class="embedding-global" :class="embeddingState">{{ embeddingStateLabel }}</span></div>
    </header>
    <div class="body">
      <aside class="sidebar">
        <nav aria-label="工作区"><button v-for="item in pages" :key="item.id" type="button" :class="{ active: page === item.id }" :aria-current="page === item.id ? 'page' : undefined" @click="changePage(item.id)">{{ item.label }}</button></nav>
      </aside>
      <main class="content" :aria-busy="busy">
        <div v-if="error" class="error-banner" role="alert"><span>{{ error }}</span><button type="button" aria-label="关闭错误" @click="clearError">关闭</button></div>
        <section v-if="page === 'search'" class="workspace search-workspace" :aria-busy="busy">
          <div class="section-head"><div><h1>找到合适的表达</h1><p>用一句自然语言描述你想要的情绪或场景。</p></div><span class="cache-pill" :class="{ ready: config }">{{ config ? 'API 已连接' : '等待连接' }}</span></div>
          <form class="search-form" aria-label="自然语言检索" @submit.prevent="runSearch"><label class="sr-only" for="search-query">描述想找的表情包</label><input id="search-query" v-model="query" placeholder="例如：开会时发现自己忘记准备材料" autocomplete="off" autofocus /><button class="primary" type="submit" :disabled="busy || !query.trim()">{{ busy ? '分析中...' : '开始检索' }}</button></form>
          <div class="controls"><label class="number-control"><span>结果数量</span><input v-model.number="resultCount" type="number" min="1" max="30" aria-label="结果数量" /></label><label class="switch"><input v-model="llmEnhance" type="checkbox" /><span class="switch-track" aria-hidden="true"></span><span class="switch-text">使用 LLM 优化语义</span></label></div>
          <template v-if="uniqueResults.length"><div class="result-grid"><button v-for="(url, index) in uniqueResults" :key="resultIdentity(url)" class="result-item" type="button" :aria-label="`复制检索结果 ${index + 1}`" title="复制图片" @click="copySearchImage(url)"><img :src="url" alt="检索结果" loading="lazy" /></button></div><p v-if="copyNotice" class="copy-notice" role="status" aria-live="polite">{{ copyNotice }}</p></template>
          <div v-else class="empty-state" :class="{ loading: busy }" role="status" aria-live="polite"><h2>{{ busy ? '正在分析你的描述' : '还没有检索结果' }}</h2><p v-if="!busy">输入一句情绪或场景描述后开始。</p></div>
        </section>
        <section v-else-if="page === 'library'" class="workspace">
          <div class="section-head"><div><h1>图片库</h1><p>浏览、筛选和整理本地图片。</p></div></div>
          <div class="toolbar" aria-label="图片库工具"><select v-model="directory" aria-label="选择目录" @change="loadLibrary"><option value="">根目录</option><option v-for="item in directories" :key="item" :value="directory ? `${directory}/${item}` : item">{{ item }}</option></select><input v-model="filter" aria-label="筛选文件名" placeholder="筛选文件名" @keyup.enter="loadLibrary" /><button type="button" @click="loadLibrary">刷新</button><input v-model="newDirectory" aria-label="新目录名" placeholder="新目录名" @keyup.enter="makeDirectory" /><button type="button" @click="makeDirectory">创建目录</button><span class="toolbar-spacer"></span><div class="toolbar-group library-operations"><button class="quiet" type="button" :class="{ active: selectionMode }" :aria-pressed="selectionMode" @click="toggleSelectionMode">{{ selectionMode ? '完成选择' : '选择图片' }}</button><button class="quiet" type="button" :disabled="retryBusy || !selectedCount" @click="retrySelected">重试选中<span v-if="selectedCount">（{{ selectedCount }}）</span></button><button class="primary toolbar-primary" type="button" :disabled="retryBusy || !images.some(isRetryable)" @click="retryAll">{{ retryBusy ? '提交中...' : '重试所有未就绪' }}</button><button class="primary toolbar-primary cache-action" type="button" :disabled="cacheGenerating || !config" :aria-busy="cacheGenerating" :title="cacheButtonTitle" @click="generateCache">{{ cacheButtonLabel }}</button><span v-if="cacheTask || cacheBusy" class="cache-status" :class="cacheTask?.status || 'running'" role="status" aria-live="polite" aria-atomic="true"><span class="cache-status-dot" aria-hidden="true"></span><span>{{ cacheTaskStatusLabel }}</span><b v-if="cacheTask?.progress != null">{{ Math.round(cacheTask.progress * 100) }}%</b></span></div></div>
          <div v-if="retryNotice" class="inline-notice" role="status">{{ retryNotice }}</div>
          <div class="library-list" role="list"><article v-for="item in images" :key="imageKey(item)" class="library-row" role="listitem"><label v-if="selectionMode" class="image-check" :class="{ disabled: !isRetryable(item) }"><input type="checkbox" :checked="selectedImages.has(imageKey(item))" :disabled="!isRetryable(item) || retryBusy" :aria-label="`选择 ${item.filename}`" @change="toggleImageSelection(item)" /><span aria-hidden="true"></span></label><button class="library-preview-trigger" type="button" :aria-label="`查看 ${item.filename} 图片与 JSON`" @click="openImagePreview(item, $event)"><img :src="item.media_url" :alt="`预览 ${item.filename}`" loading="lazy" /></button><div class="file-meta"><strong :title="item.filename">{{ item.filename }}</strong><small>{{ Math.ceil(item.size / 1024) }} KB · {{ item.extension }}</small></div><span class="metadata-state" :class="item.metadata?.status || 'unknown'">{{ metadataLabel(item.metadata?.status) }}</span><span class="embedding-state" :class="item.embedding_status || 'unknown'">{{ embeddingLabel(item.embedding_status) }}</span><button class="quiet metadata-button" type="button" @click="openImagePreview(item, $event)">查看 JSON</button><button class="quiet" type="button" @click="rename(item)">重命名</button></article><div v-if="!images.length" class="empty-state compact"><h2>这个目录还没有图片</h2><p>上传图片后，它们会出现在这里。</p></div></div>
        </section>
        <section v-else-if="page === 'upload'" class="workspace narrow">
          <div class="section-head"><div><h1>上传图片</h1><p>支持 PNG、JPG、JPEG 和 GIF。</p></div></div>
          <div class="upload-panel"><label class="field"><span>目标目录</span><select v-model="directory" aria-label="目标目录"><option value="">根目录</option><option v-for="item in directories" :key="item" :value="item">{{ item }}</option></select></label><label class="drop-zone"><input type="file" multiple accept=".png,.jpg,.jpeg,.gif" aria-label="选择图片文件" @change="onFiles" /><span class="drop-title">选择图片文件</span><span class="drop-sub">{{ files.length ? `已选择 ${files.length} 个文件` : '点击选择或拖入文件' }}</span></label><label class="switch"><input v-model="autoName" type="checkbox" /><span class="switch-track" aria-hidden="true"></span><span class="switch-text">处理完成后按标题自动命名</span></label><button class="primary wide" type="button" :disabled="busy || !files.length" @click="upload">{{ busy ? '上传中...' : '上传所选图片' }}</button></div>
          <div v-if="uploadResults.length" class="upload-results" aria-live="polite"><div v-for="item in uploadResults" :key="item.filename" class="upload-result" :class="{ fail: !item.ok }"><span>{{ item.ok ? '完成' : '失败' }}</span><strong :title="item.filename">{{ item.filename }}</strong><button v-if="item.metadata_job_id" class="quiet" type="button" @click="openUploadTask(item)">查看任务</button><small v-else>{{ item.ok ? item.saved_filename : item.error }}</small></div></div>
        </section>
        <section v-else-if="page === 'settings'" class="workspace settings-workspace">
          <div class="section-head"><div><h1>后端设置</h1><p>查看服务状态并调整安全范围内的 Agent 并发数量。</p></div><button class="quiet" :disabled="settingsLoading" @click="loadBackendSettings">刷新</button></div>
          <div v-if="settingsLoading && !backendSettings" class="settings-loading">正在加载后端设置</div>
          <template v-else-if="backendSettings">
            <section class="settings-section"><div class="settings-section-head"><h2>后端状态</h2><span class="settings-readonly">只读</span></div><div class="settings-grid"><div><dt>语义模型</dt><dd>{{ backendSettings.readonly?.embedding_model || '未配置' }}</dd></div><div><dt>OpenCode 模型</dt><dd>{{ backendSettings.readonly?.opencode_model || '未配置' }}</dd></div><div><dt>运行时</dt><dd>{{ backendSettings.readonly?.runtime_ready ? '已就绪' : '待检查' }}</dd></div><div><dt>Embedding 缓存</dt><dd>{{ backendSettings.readonly?.embedding_cache_ready ? '已就绪' : '未生成' }}</dd></div><div><dt>设置管理</dt><dd>{{ backendSettings.readonly?.settings_admin_enabled ? '已启用' : '未启用' }}</dd></div><div><dt>配置版本</dt><dd>{{ backendSettings.settings_version || '—' }}</dd></div></div></section>
            <section class="settings-section"><div class="settings-section-head"><h2>Agent 并发数量</h2><span class="settings-editable">可调整</span></div><div class="settings-edit-row"><label class="field"><span>设置管理凭据</span><input id="settings-admin-token" v-model="settingsAdminToken" type="password" autocomplete="off" :disabled="!backendSettings.readonly?.settings_admin_enabled" placeholder="输入服务端管理凭据" /></label><label class="field"><span>并发上限（{{ backendSettings.editable?.opencode_concurrency?.minimum || 1 }} - {{ backendSettings.editable?.opencode_concurrency?.maximum || 8 }}）</span><input v-model.number="settingsConcurrency" type="number" min="1" max="8" step="1" /></label><button class="primary" :disabled="settingsSaving || !backendSettings.readonly?.settings_admin_enabled || backendSettings.editable?.opencode_concurrency?.environment_overridden || !settingsAdminToken.trim()" @click="saveBackendSettings">{{ settingsSaving ? '保存中...' : '保存并发设置' }}</button></div><p v-if="!backendSettings.readonly?.settings_admin_enabled" class="settings-warning">设置管理未启用，当前页面为只读。</p><p v-if="backendSettings.readonly?.settings_admin_enabled && !settingsAdminToken.trim()" class="settings-hint">请输入设置管理凭据后保存。</p><p v-if="backendSettings.editable?.opencode_concurrency?.environment_overridden" class="settings-warning">当前值由环境变量覆盖，请在部署环境修改。</p><p v-if="backendSettings.restart_required || settingsNotice" class="settings-notice" role="status">{{ settingsNotice || '已保存的值将在服务重启后生效。' }}</p><p v-if="backendSettings.pending?.opencode_concurrency != null" class="settings-pending">待生效值：{{ backendSettings.pending.opencode_concurrency }} · 当前有效值：{{ backendSettings.effective.opencode_concurrency }}</p></section>
            <section class="settings-section"><div class="settings-section-head"><h2>部署环境管理</h2><span class="settings-readonly">仅部署环境</span></div><div class="settings-grid deployment-grid"><div><dt>OpenCode 可执行文件</dt><dd>由服务端环境管理</dd></div><div><dt>Runtime / 数据目录</dt><dd>路径已隐藏</dd></div><div><dt>Provider 地址</dt><dd>{{ backendSettings.deployment?.provider_url?.configured ? '已配置（不展示地址）' : '未配置' }}</dd></div><div><dt>API Key</dt><dd>{{ backendSettings.deployment?.api_key?.configured ? '已配置（不展示密钥）' : '未配置' }}</dd></div></div></section>
          </template>
          <div v-else class="empty-state compact"><h2>后端设置暂不可用</h2></div>
        </section>
        <section v-else class="workspace task-workspace" :class="{ detail: selectedTask }">
          <div class="section-head"><div><h1>处理任务</h1></div><button class="quiet" type="button" :disabled="taskLoading" @click="loadTasks">刷新</button></div>
          <div class="task-toolbar"><label>状态<select v-model="taskStatus" aria-label="按状态筛选" @change="loadTasks"><option value="">全部</option><option value="queued">排队中</option><option value="running">处理中</option><option value="succeeded">已完成</option><option value="failed">失败</option></select></label><label>类型<select v-model="taskType" aria-label="按类型筛选" @change="loadTasks"><option value="">全部</option><option value="meme_context_generation">语境生成</option><option value="cache_generation">检索缓存</option><option value="metadata_repair">元数据修复</option></select></label></div>
          <div class="task-table" :class="{ loading: taskLoading }" role="table" aria-label="处理任务列表"><div class="task-head" role="row"><span role="columnheader">状态</span><span role="columnheader">类型</span><span role="columnheader">关联图片</span><span role="columnheader">进度</span><span role="columnheader">最近更新</span></div><button v-for="item in taskItems" :key="item.task_id" class="task-row" :class="{ selected: selectedTask?.task_id === item.task_id }" type="button" role="row" :aria-label="`${taskStatusLabel(item.status)}，${taskTypeLabel(item.task_type)}，${item.image?.filename || '无关联图片'}`" @click="openTask(item.task_id)"><span class="task-status-cell" role="cell"><i :class="`status-dot ${item.status}`" aria-hidden="true"></i>{{ taskStatusLabel(item.status) }}</span><span class="task-type-cell" role="cell" data-label="类型">{{ taskTypeLabel(item.task_type) }}</span><span class="task-image" role="cell" data-label="图片">{{ item.image?.filename || '—' }}</span><span class="task-progress" role="cell" data-label="进度">{{ item.progress == null ? '—' : `${Math.round(item.progress * 100)}%` }}</span><time role="cell">{{ formatTaskTime(item.updated_at) }}</time></button><div v-if="taskLoading && !taskItems.length" class="task-skeleton" v-for="n in 5" :key="n"></div><div v-if="!taskLoading && !taskItems.length" class="empty-state compact"><h2>没有匹配的任务</h2><p>调整筛选条件后再试。</p></div></div>
          <button v-if="taskCursor" class="quiet load-more" type="button" @click="loadTasks({ append: true })">加载更多</button>
          <aside v-if="selectedTask" class="task-drawer" role="dialog" aria-modal="true" aria-label="任务详情"><div class="drawer-head"><h2>任务详情</h2><button class="quiet" type="button" aria-label="关闭任务详情" @click="selectedTask = null">关闭</button></div><dl><div><dt>状态</dt><dd>{{ taskStatusLabel(selectedTask.status) }}</dd></div><div><dt>类型</dt><dd>{{ taskTypeLabel(selectedTask.task_type) }}</dd></div><div><dt>阶段</dt><dd>{{ selectedTask.message || '—' }}</dd></div><div><dt>创建时间</dt><dd>{{ formatTaskTime(selectedTask.created_at) }}</dd></div><div><dt>完成时间</dt><dd>{{ formatTaskTime(selectedTask.completed_at) }}</dd></div><div v-if="selectedTask.error"><dt>错误</dt><dd>{{ selectedTask.error.error }}</dd></div><div v-if="selectedTask.result?.auto_named !== undefined"><dt>自动命名</dt><dd>{{ selectedTask.result.auto_named ? selectedTask.result.saved_filename : selectedTask.result.auto_name_error || '未执行' }}</dd></div></dl><button v-if="selectedTask.task_type === 'meme_context_generation' && selectedTask.status === 'failed'" class="primary" type="button" @click="retryTask">重试</button></aside>
        </section>
      </main>
    </div>
    <div v-if="previewImage" class="image-dialog-backdrop" role="presentation" @click.self="closeImagePreview">
      <section ref="previewDialog" class="image-dialog" role="dialog" aria-modal="true" aria-labelledby="image-dialog-title" tabindex="-1">
        <header class="image-dialog-head"><div><h2 id="image-dialog-title">{{ previewImage.filename }}</h2><p>图片预览与 JSON</p></div><button ref="previewCloseButton" class="quiet" type="button" aria-label="关闭图片预览" @click="closeImagePreview">关闭</button></header>
        <div class="image-dialog-content">
          <div class="image-dialog-preview"><img :src="previewImage.media_url" :alt="`放大查看 ${previewImage.filename}`" /></div>
          <section class="metadata-panel" aria-labelledby="metadata-panel-title"><div class="metadata-panel-head"><h3 id="metadata-panel-title">图片 JSON</h3><button class="quiet" type="button" :disabled="previewLoading || !previewJsonText" @click="copyPreviewJson">复制 JSON</button></div><p v-if="previewLoading" class="metadata-loading" role="status">正在读取 JSON...</p><p v-else-if="previewError" class="metadata-error" role="alert">{{ previewError }}</p><pre v-else class="metadata-json">{{ previewJsonText }}</pre><p v-if="previewCopyNotice" class="copy-notice" role="status" aria-live="polite">{{ previewCopyNotice }}</p></section>
        </div>
      </section>
    </div>
  </div>
</template>
