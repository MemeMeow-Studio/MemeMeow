<script setup lang="ts">
/** 图片库工作区：管理筛选、选择、语境重试、缓存任务和加入合集流程。 */
import { computed, onMounted, shallowRef, watch } from 'vue'
import { api } from '../api'
import { showTaskDiagnostics } from '../config/debug'
import type { CollectionSummary, CoreImageProcessingStage, ImageProcessingOptions, ImageProcessingStage, MemeImage, SelectedImageRetryMode, ServiceConfig, TaskItem, UnreadyProcessingResponse } from '../types'
import {
  embeddingLabel,
  errorMessage,
  imageDisplayUrl,
  imageKey,
  metadataLabel,
  visualEmbeddingLabel,
} from '../utils/presentation'
import CollectionDialog from './CollectionDialog.vue'
import ImagePreviewDialog from './ImagePreviewDialog.vue'
import ImageProcessingOptionsDialog from './ImageProcessingOptionsDialog.vue'
import ImageRetryDialog from './ImageRetryDialog.vue'
import ResilientImage from './ResilientImage.vue'

const props = defineProps<{
  config: ServiceConfig | null
  cacheTask: TaskItem | null
  cacheBusy: boolean
  refreshToken: number
}>()

const emit = defineEmits<{
  error: [message: string]
  clearError: []
  generateCache: []
}>()

/** 返回每次新建处理确认时使用的安全选项，避免复用上一次的高风险状态。 */
function defaultProcessingOptions(): ImageProcessingOptions {
  return { reverse_image_policy: 'forbid', auto_name: false }
}

type SelectedRetrySubmission = {
  mode: SelectedImageRetryMode
  stages: CoreImageProcessingStage[]
  items: Array<{ meme_id: string }>
}

type ProcessingOptionsTarget = 'unready' | 'selected'

const images = shallowRef<MemeImage[]>([])
const filter = shallowRef('')
const page = shallowRef(1)
const pageSize = 50
const total = shallowRef(0)
const busy = shallowRef(false)
const selectedImages = shallowRef(new Set<string>())
const retryBusy = shallowRef(false)
const retryNotice = shallowRef('')
const collections = shallowRef<CollectionSummary[]>([])
const collectionBusy = shallowRef(false)
const collectionNotice = shallowRef('')
const collectionDialogOpen = shallowRef(false)
const collectionTarget = shallowRef('')
const dialogCollectionName = shallowRef('')
const collectionTrigger = shallowRef<HTMLElement | null>(null)
const previewImage = shallowRef<MemeImage | null>(null)
const previewTrigger = shallowRef<HTMLElement | null>(null)
const stageBusy = shallowRef('')
const processingOptionsOpen = shallowRef(false)
const processingOptionsTarget = shallowRef<ProcessingOptionsTarget | null>(null)
const processingOptionsTrigger = shallowRef<HTMLElement | null>(null)
const retryDetails = shallowRef<UnreadyProcessingResponse['results']>([])
const retryOptions = shallowRef<ImageProcessingOptions>(defaultProcessingOptions())
const preserveRetryOptions = shallowRef(false)
const retrySelectedDialogOpen = shallowRef(false)
const retrySelectedDialogTrigger = shallowRef<HTMLElement | null>(null)
const selectedRetrySubmission = shallowRef<SelectedRetrySubmission | null>(null)
const selectedRetryOptions = shallowRef<ImageProcessingOptions>(defaultProcessingOptions())
const preserveSelectedRetryOptions = shallowRef(false)
let libraryRequestId = 0

const selectedIds = computed(() => [...selectedImages.value])
const selectedCount = computed(() => selectedImages.value.size)
const cacheGenerating = computed(() => props.cacheBusy || ['queued', 'running'].includes(props.cacheTask?.status || ''))
const cacheButtonLabel = computed(() => {
  if (!props.config) return '等待服务连接...'
  if (cacheGenerating.value) return props.cacheTask?.status === 'queued' ? '排队中...' : '生成中...'
  if (props.cacheTask?.status === 'failed') return '重新生成检索缓存'
  return '生成检索缓存'
})
const cacheButtonTitle = computed(() => {
  if (!props.config) return '等待服务配置加载完成'
  if (cacheGenerating.value) return '检索缓存正在生成'
  if (props.cacheTask?.status === 'failed') return '上次生成失败，点击重试'
  return '扫描图片库并生成检索缓存'
})
const cacheTaskStatusLabel = computed(() => {
  if (props.cacheBusy && !props.cacheTask) return '正在提交缓存任务'
  if (props.cacheTask?.status === 'queued') return '等待生成'
  if (props.cacheTask?.status === 'running') return props.cacheTask.message || '正在生成缓存'
  if (props.cacheTask?.status === 'succeeded') return '缓存已更新'
  if (props.cacheTask?.status === 'failed') return props.cacheTask.message || '缓存生成失败'
  return ''
})
const pageCount = computed(() => Math.max(1, Math.ceil(total.value / pageSize)))

/** 加载当前筛选下的图片，并清理不再存在的选中项。 */
async function loadLibrary(): Promise<void> {
  const requestId = ++libraryRequestId
  emit('clearError')
  busy.value = true
  try {
    const data = await api.images({ search: filter.value, page: page.value, page_size: pageSize })
    if (requestId !== libraryRequestId) return
    images.value = data.items
    total.value = Number.isInteger(data.total) ? data.total : data.items.length
    if (previewImage.value) {
      previewImage.value = data.items.find((item: MemeImage) => item.meme_id === previewImage.value?.meme_id) || null
    }
    const keys = new Set(images.value.map(imageKey))
    selectedImages.value = new Set([...selectedImages.value].filter((key) => keys.has(key)))
  } catch (reason) {
    if (requestId === libraryRequestId) emit('error', errorMessage(reason))
  } finally {
    if (requestId === libraryRequestId) busy.value = false
  }
}

/** 将筛选结果切回第一页，避免旧页码超出新结果范围。 */
function applyFilter(): void {
  page.value = 1
  void loadLibrary()
}

/** 翻页时清理当前页之外的选择，避免跨页误操作。 */
function changePage(next: number): void {
  const target = Math.max(1, Math.min(pageCount.value, next))
  if (target === page.value) return
  page.value = target
  selectedImages.value = new Set()
  void loadLibrary()
}

/** 切换一张图片的选择状态，并用新 Set 触发浅层响应式更新。 */
function toggleImageSelection(item: MemeImage): void {
  const next = new Set(selectedImages.value)
  const key = imageKey(item)
  if (next.has(key)) next.delete(key)
  else next.add(key)
  selectedImages.value = next
}

/** 加载当前 scope 的合集列表。 */
async function loadCollections(): Promise<void> {
  collectionBusy.value = true
  try {
    collections.value = (await api.collections()).items
  } catch (reason) {
    emit('error', errorMessage(reason))
  } finally {
    collectionBusy.value = false
  }
}

/** 打开加入合集对话框，并记住焦点返回位置。 */
function openCollectionDialog(event: MouseEvent): void {
  if (!selectedImages.value.size || collectionBusy.value) return
  collectionTrigger.value = event.currentTarget instanceof HTMLElement ? event.currentTarget : null
  collectionDialogOpen.value = true
  collectionTarget.value = ''
  dialogCollectionName.value = ''
  collectionNotice.value = ''
  void loadCollections()
}

/** 关闭加入合集对话框；焦点恢复由对话框 composable 完成。 */
function closeCollectionDialog(): void {
  if (!collectionBusy.value) collectionDialogOpen.value = false
}

/** 将当前选择加入既有合集，并清空已提交的图片选择。 */
async function addSelectedToCollection(): Promise<void> {
  if (!collectionTarget.value || !selectedIds.value.length || collectionBusy.value) return
  collectionBusy.value = true
  try {
    const result = await api.addCollectionItems(collectionTarget.value, selectedIds.value)
    collectionNotice.value = `已加入 ${result.added_count} 张图片`
    collectionDialogOpen.value = false
    selectedImages.value = new Set()
  } catch (reason) {
    emit('error', errorMessage(reason))
  } finally {
    collectionBusy.value = false
  }
}

/** 在加入流程内创建合集并立即写入当前选择。 */
async function createCollectionAndAdd(): Promise<void> {
  if (!dialogCollectionName.value.trim() || !selectedIds.value.length || collectionBusy.value) return
  collectionBusy.value = true
  try {
    const created = await api.createCollection({ name: dialogCollectionName.value })
    await api.addCollectionItems(created.collection_id, selectedIds.value)
    collectionNotice.value = '合集已创建并加入图片'
    collectionDialogOpen.value = false
    selectedImages.value = new Set()
  } catch (reason) {
    emit('error', errorMessage(reason))
  } finally {
    collectionBusy.value = false
  }
}

/** 为当前图片提交一个不带父 Job 的独立阶段任务。 */
async function retryStage(item: MemeImage, stage: 'visual' | 'agent' | 'auto_rename' | 'text_embedding'): Promise<void> {
  if (stageBusy.value || typeof api.submitImageStage !== 'function' || (stage === 'auto_rename' && !canRetryStandaloneAutoRename(item))) return
  stageBusy.value = `${item.meme_id}:${stage}`
  emit('clearError')
  try {
    const request: Record<string, unknown> = { meme_id: item.meme_id, stage }
    if (stage !== 'visual') request.reverse_image_policy = 'forbid'
    await api.submitImageStage(request)
    retryNotice.value = `${item.filename}：已提交${stage === 'visual' ? '视觉向量生成' : stage === 'agent' ? '图片语境分析' : stage === 'auto_rename' ? '自动重命名' : '文本语义检索'}独立任务`
    await loadLibrary()
  } catch (reason) {
    emit('error', errorMessage(reason))
  } finally {
    stageBusy.value = ''
  }
}

/** 读取当前图片的持久化阶段事实，缺失时不猜测为可恢复。 */
function processingStage(item: MemeImage, stage: ImageProcessingStageName): ImageProcessingStage | undefined {
  return item.processing_stages?.find((candidate) => candidate.stage === stage)
}

type ImageProcessingStageName = 'visual' | 'agent' | 'auto_rename' | 'text_embedding'

type RetryResult = UnreadyProcessingResponse['results'][number]

/** 将服务端逐图结果兼容为稳定的提交分类，供摘要和明细共用。 */
function retryResultCategory(result: RetryResult): 'submitted' | 'reused' | 'conflict' | 'failed' {
  if (result.category === 'conflict' || result.status === 'conflict') return 'conflict'
  if (result.category === 'failed' || result.status === 'failed' || result.error) return 'failed'
  if (result.reused === true || result.category === 'reused' || result.status === 'reused') return 'reused'
  return 'submitted'
}

/** 将逐图分类和稳定错误收束为用户可扫描的一行反馈。 */
function retryResultMessage(result: RetryResult): string {
  const category = retryResultCategory(result)
  if (category === 'conflict') return `选项冲突${result.error ? `：${result.error}` : ''}`
  if (category === 'failed') return `提交失败${result.error ? `：${result.error}` : ''}`
  if (category === 'reused') return '已复用处理任务'
  return `已提交处理任务${showTaskDiagnostics && result.processing_job_id ? `（${result.processing_job_id}）` : ''}`
}

/** 只有父 Job 已将自动命名收束为 warning 时才显示独立恢复入口。 */
function canRetryStandaloneAutoRename(item: MemeImage): boolean {
  return processingStage(item, 'auto_rename')?.status === 'warning'
}

/** 打开选中图片重试对话框，并保留关闭后的键盘焦点。 */
function openRetrySelectedDialog(event: MouseEvent): void {
  if (retryBusy.value || !selectedCount.value) return
  retrySelectedDialogTrigger.value = event.currentTarget instanceof HTMLElement ? event.currentTarget : null
  retrySelectedDialogOpen.value = true
}

/** 关闭选中图片重试对话框并复位临时表单状态。 */
function cancelRetrySelected(): void {
  if (!retryBusy.value) retrySelectedDialogOpen.value = false
}

/** 提交选中图片的完整流水线或指定阶段，并在请求成功后清理选择。 */
async function submitSelectedRetry(submission: SelectedRetrySubmission, options?: ImageProcessingOptions): Promise<void> {
  if (retryBusy.value) return
  emit('clearError')
  retryNotice.value = ''
  retryBusy.value = true
  try {
    if (submission.mode === 'full') {
      if (!options) return
      const response = await api.contextBatch({
        items: submission.items,
        include_unready: true,
        reverse_image_policy: options.reverse_image_policy,
        auto_name: options.auto_name,
      })
      const results = Array.isArray(response.results) ? response.results : []
      const queued = results.filter((item: { task_id?: string }) => item.task_id).length
      const failed = results.filter((item: { error?: unknown }) => item.error).length
      retryNotice.value = `重试选中：已提交 ${queued} 个完整任务${failed ? `，${failed} 项未提交` : ''}`
    } else {
      const payload: Record<string, unknown> = { items: submission.items, stages: submission.stages }
      if (options) {
        if (!(submission.stages.length === 1 && submission.stages[0] === 'visual')) payload.reverse_image_policy = options.reverse_image_policy
        payload.auto_name = options.auto_name
      }
      const response = await api.retryImageStagesBatch(payload)
      retryNotice.value = `重试选中：已提交 ${response.submitted_count ?? 0} 个阶段任务${response.failed_count ? `，${response.failed_count} 项未提交` : ''}`
    }
    retrySelectedDialogOpen.value = false
    processingOptionsOpen.value = false
    processingOptionsTarget.value = null
    selectedRetrySubmission.value = null
    preserveSelectedRetryOptions.value = false
    selectedRetryOptions.value = defaultProcessingOptions()
    selectedImages.value = new Set()
    await loadLibrary()
  } catch (reason) {
    emit('error', errorMessage(reason))
  } finally {
    retryBusy.value = false
  }
}

/** 处理第一层重试范围确认；需要 Agent 时先切换到共享选项对话框。 */
async function confirmRetrySelected(payload: { mode: SelectedImageRetryMode; stages: CoreImageProcessingStage[] }): Promise<void> {
  if (retryBusy.value || !selectedCount.value) return
  const items = selectedIds.value.map((meme_id) => ({ meme_id }))
  if (!items.length) return
  const submission: SelectedRetrySubmission = { mode: payload.mode, stages: [...payload.stages], items }
  if (payload.mode === 'full' || payload.stages.includes('agent')) {
    selectedRetrySubmission.value = submission
    if (!preserveSelectedRetryOptions.value) selectedRetryOptions.value = defaultProcessingOptions()
    retrySelectedDialogOpen.value = false
    processingOptionsTarget.value = 'selected'
    processingOptionsTrigger.value = retrySelectedDialogTrigger.value
    processingOptionsOpen.value = true
    return
  }
  await submitSelectedRetry(submission)
}

/** 取消选中重试的共享选项确认，保留图片选择但丢弃本次未提交范围。 */
function cancelSelectedRetryOptions(): void {
  if (!retryBusy.value) {
    processingOptionsOpen.value = false
    processingOptionsTarget.value = null
    selectedRetrySubmission.value = null
    preserveSelectedRetryOptions.value = false
    selectedRetryOptions.value = defaultProcessingOptions()
  }
}

/** 使用共享选项提交已快照的选中重试范围；失败时保留选项和图片选择。 */
async function confirmSelectedRetryOptions(options: ImageProcessingOptions): Promise<void> {
  const submission = selectedRetrySubmission.value
  if (retryBusy.value || !submission) return
  selectedRetryOptions.value = options
  preserveSelectedRetryOptions.value = true
  await submitSelectedRetry(submission, options)
}

/** 打开 scope 级完整重试的共享选项对话框。 */
function openUnreadyOptions(event: MouseEvent): void {
  if (retryBusy.value) return
  if (!preserveRetryOptions.value) retryOptions.value = defaultProcessingOptions()
  retryDetails.value = []
  processingOptionsTrigger.value = event.currentTarget instanceof HTMLElement ? event.currentTarget : null
  processingOptionsTarget.value = 'unready'
  processingOptionsOpen.value = true
}

/** 取消完整重试确认，不产生请求。 */
function cancelUnreadyOptions(): void {
  if (!retryBusy.value) {
    processingOptionsOpen.value = false
    processingOptionsTarget.value = null
    preserveRetryOptions.value = false
    retryOptions.value = defaultProcessingOptions()
  }
}

/** 让服务端枚举当前 scope 全部核心未就绪图片，并显示分类摘要。 */
async function confirmUnreadyOptions(options: ImageProcessingOptions): Promise<void> {
  if (retryBusy.value) return
  emit('clearError')
  retryNotice.value = ''
  retryOptions.value = options
  preserveRetryOptions.value = true
  const submitUnreadyProcessing = api.unreadyProcessing
  if (typeof submitUnreadyProcessing !== 'function') {
    // API 能力尚未注入时也先保留选项弹层，避免点击主按钮变成无反馈操作。
    emit('error', '完整重试服务不可用')
    return
  }
  retryBusy.value = true
  try {
    const response = await submitUnreadyProcessing(options) as UnreadyProcessingResponse
    retryDetails.value = response.results || []
    retryNotice.value = `完整重试：目标 ${response.target_count ?? 0}，提交 ${response.submitted_count ?? 0}，复用 ${response.reused_count ?? 0}，冲突 ${response.conflict_count ?? 0}，失败 ${response.failed_count ?? 0}`
    processingOptionsOpen.value = false
    processingOptionsTarget.value = null
    // 请求已经返回后关闭本次确认；下一次打开不得继承可能更高风险的选择。
    // 请求异常不会进入这里，因此仍会在当前对话框内保留选择供安全重试。
    preserveRetryOptions.value = false
    retryOptions.value = defaultProcessingOptions()
    await loadLibrary()
  } catch (reason) {
    emit('error', errorMessage(reason))
    // 网络或服务错误时保持对话框和本次选项，避免安全重试丢失用户选择。
  } finally {
    retryBusy.value = false
  }
}

/** 将共享选项对话框的取消事件路由到当前重试场景。 */
function cancelProcessingOptions(): void {
  if (processingOptionsTarget.value === 'selected') cancelSelectedRetryOptions()
  else cancelUnreadyOptions()
}

/** 将共享选项对话框的确认事件路由到当前重试场景。 */
async function confirmProcessingOptions(options: ImageProcessingOptions): Promise<void> {
  if (processingOptionsTarget.value === 'selected') await confirmSelectedRetryOptions(options)
  else await confirmUnreadyOptions(options)
}

/** 打开图片预览并保留触发按钮，供关闭后恢复键盘焦点。 */
function openImagePreview(item: MemeImage, event: MouseEvent): void {
  previewTrigger.value = event.currentTarget instanceof HTMLElement ? event.currentTarget : null
  previewImage.value = item
}

/** 将详情中的阶段恢复动作交回工作区，保持稳定 meme_id 和现有提交契约。 */
function retryPreviewStage(stage: ImageProcessingStageName): void {
  if (previewImage.value) void retryStage(previewImage.value, stage)
}

/** 调用稳定 meme_id 完成重命名并刷新图库。 */
async function rename(item: MemeImage): Promise<void> {
  const name = window.prompt('新文件名', item.filename)
  if (!name) return
  try {
    await api.rename({ meme_id: item.meme_id, new_name: name })
    await loadLibrary()
  } catch (reason) {
    emit('error', errorMessage(reason))
  }
}

onMounted(loadLibrary)
watch(() => props.refreshToken, () => { void loadLibrary() })
</script>

<template>
  <section class="workspace" :aria-busy="busy">
    <div class="section-head">
      <div><h1>图片库</h1></div>
    </div>
    <div class="toolbar" aria-label="图片库工具">
      <input v-model="filter" aria-label="筛选文件名" placeholder="筛选文件名" @keyup.enter="applyFilter" />
      <button type="button" @click="loadLibrary">刷新</button>
      <span class="toolbar-spacer"></span>
      <div class="toolbar-group library-operations">
        <button class="quiet" type="button" :disabled="retryBusy || !selectedCount" @click="openCollectionDialog">
          加入合集<span v-if="selectedCount">（{{ selectedCount }}）</span>
        </button>
        <button class="quiet" type="button" :disabled="retryBusy || !selectedCount" @click="openRetrySelectedDialog">
          重试选中<span v-if="selectedCount">（{{ selectedCount }}）</span>
        </button>
        <button class="primary toolbar-primary" type="button" :disabled="retryBusy" @click="openUnreadyOptions">
          {{ retryBusy ? '提交中...' : '完整重试所有未就绪' }}
        </button>
        <button
          class="primary toolbar-primary cache-action"
          type="button"
          :disabled="cacheGenerating || !config"
          :aria-busy="cacheGenerating"
          :title="cacheButtonTitle"
          @click="emit('generateCache')"
        >
          {{ cacheButtonLabel }}
        </button>
        <span v-if="cacheTask || cacheBusy" class="cache-status" :class="cacheTask?.status || 'running'" role="status" aria-live="polite" aria-atomic="true">
          <span class="cache-status-dot" aria-hidden="true"></span>
          <span>{{ cacheTaskStatusLabel }}</span>
          <b v-if="cacheTask?.progress != null">{{ Math.round(cacheTask.progress * 100) }}%</b>
        </span>
      </div>
    </div>
    <div v-if="retryNotice" class="inline-notice" role="status">{{ retryNotice }}</div>
    <ul v-if="retryDetails.length" class="processing-result-details" aria-label="完整重试逐图结果">
      <li v-for="result in retryDetails" :key="result.meme_id" :class="retryResultCategory(result)">
        <strong v-if="showTaskDiagnostics">{{ result.meme_id }}</strong>
        <span>{{ retryResultMessage(result) }}</span>
      </li>
    </ul>
    <div class="library-list" role="list">
      <article v-for="item in images" :key="imageKey(item)" class="library-row" role="listitem">
        <label class="image-check">
          <input
            type="checkbox"
            :checked="selectedImages.has(imageKey(item))"
            :disabled="retryBusy"
            :aria-label="`选择 ${item.filename}`"
            @change="toggleImageSelection(item)"
          />
          <span aria-hidden="true"></span>
        </label>
        <div class="library-row-main">
          <button class="library-preview-trigger" type="button" :aria-label="`查看 ${item.filename} 图片与详情`" @click="openImagePreview(item, $event)">
            <ResilientImage :src="imageDisplayUrl(item)" :fallback-src="item.media_url" :alt="`预览 ${item.filename}`" />
          </button>
          <div class="file-meta">
            <strong :title="item.filename">{{ item.filename }}</strong>
            <small>{{ Math.ceil((item.size || 0) / 1024) }} KB · {{ item.extension }}</small>
          </div>
        </div>
        <div class="image-status-summary" aria-label="图片状态摘要">
          <span class="metadata-state" :class="item.metadata?.status || 'unknown'">{{ metadataLabel(item.metadata?.status) }}</span>
          <span class="embedding-state" :class="item.embedding_status || 'unknown'">{{ embeddingLabel(item.embedding_status) }}</span>
          <span class="visual-embedding-state" :class="item.visual_embedding_status || 'unknown'">{{ visualEmbeddingLabel(item.visual_embedding_status) }}</span>
        </div>
        <div class="image-row-actions" aria-label="图片操作">
          <button class="quiet metadata-button" type="button" @click="openImagePreview(item, $event)">查看详情</button>
          <button class="quiet" type="button" @click="rename(item)">重命名</button>
        </div>
      </article>
      <div v-if="!images.length" class="empty-state compact"><h2>图片库还没有图片</h2></div>
    </div>
    <p v-if="collectionNotice" class="inline-notice" role="status">{{ collectionNotice }}</p>
    <nav class="library-pagination" aria-label="图片库分页">
      <button type="button" :disabled="busy || page <= 1" @click="changePage(page - 1)">上一页</button>
      <span>第 {{ page }} / {{ pageCount }} 页，共 {{ total }} 张</span>
      <button type="button" :disabled="busy || page >= pageCount" @click="changePage(page + 1)">下一页</button>
    </nav>
  </section>

  <ImagePreviewDialog
    v-if="previewImage"
    :image="previewImage"
    :return-focus="previewTrigger"
    :retry-busy="retryBusy"
    :stage-busy="stageBusy"
    :stage-recovery-enabled="true"
    @close="previewImage = null"
    @retry-stage="retryPreviewStage"
  />
  <CollectionDialog
    v-if="collectionDialogOpen"
    v-model:target="collectionTarget"
    v-model:name="dialogCollectionName"
    :collections="collections"
    :selected-count="selectedCount"
    :busy="collectionBusy"
    :return-focus="collectionTrigger"
    @close="closeCollectionDialog"
    @add="addSelectedToCollection"
    @create-and-add="createCollectionAndAdd"
  />
  <ImageRetryDialog
    v-if="retrySelectedDialogOpen"
    :selected-count="selectedCount"
    :busy="retryBusy"
    :return-focus="retrySelectedDialogTrigger"
    @cancel="cancelRetrySelected"
    @confirm="confirmRetrySelected"
  />
  <ImageProcessingOptionsDialog
    v-if="processingOptionsOpen"
    :reverse-image-available="props.config?.reverse_image_available === true"
    :reverse-image-reason="props.config ? '反向图片服务不可用' : '服务状态未知'"
    :busy="retryBusy"
    :return-focus="processingOptionsTrigger"
    :initial-options="processingOptionsTarget === 'selected'
      ? (preserveSelectedRetryOptions ? selectedRetryOptions : undefined)
      : (preserveRetryOptions ? retryOptions : undefined)"
    @cancel="cancelProcessingOptions"
    @confirm="confirmProcessingOptions"
  />
</template>
