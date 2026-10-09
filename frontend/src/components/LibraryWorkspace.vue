<script setup lang="ts">
/** 图片库工作区：管理筛选、选择、语境重试、缓存任务和加入合集流程。 */
import { computed, onMounted, shallowRef, watch } from 'vue'
import { api } from '../api'
import { useToast } from '../composables/useToast'
import { showTaskDiagnostics } from '../config/debug'
import type { CollectionSummary, CoreImageProcessingStage, ImageProcessingBatchResponse, ImageProcessingOptions, ImageProcessingStage, MemeImage, SelectedImageRetryMode, ServiceConfig, TaskItem } from '../types'
import { loadLibraryPageSize, saveLibraryPageSize, type ImageLibraryStatus } from '../utils/libraryPreferences'
import LibraryFilters from './library/LibraryFilters.vue'
import {
  embeddingLabel,
  errorMessage,
  imageDisplayUrl,
  imageKey,
  isRetryable,
  imageStageLabel,
  metadataLabel,
  visualEmbeddingLabel,
} from '../utils/presentation'
import CollectionDialog from './CollectionDialog.vue'
import ActionMenu from './ActionMenu.vue'
import ImagePreviewDialog from './ImagePreviewDialog.vue'
import ImageProcessingOptionsDialog from './ImageProcessingOptionsDialog.vue'
import ImageRetryDialog from './ImageRetryDialog.vue'
import ResilientImage from './ResilientImage.vue'
import LibraryImageGrid from './library/LibraryImageGrid.vue'
import LibraryViewToggle from './library/LibraryViewToggle.vue'

const props = defineProps<{
  config: ServiceConfig | null
  cacheTask: TaskItem | null
  cacheBusy: boolean
  refreshToken: number
}>()

const emit = defineEmits<{
  error: [message: string]
  loadError: [message: string]
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

type ProcessingOptionsTarget = 'selected'

const images = shallowRef<MemeImage[]>([])
const filter = shallowRef('')
const sort = shallowRef('name_asc')
const page = shallowRef(1)
const pageSize = shallowRef(loadLibraryPageSize())
const status = shallowRef<ImageLibraryStatus>('all')
const total = shallowRef(0)
const busy = shallowRef(false)
const selectedImages = shallowRef(new Set<string>())
const selectionMode = shallowRef(false)
const refreshTrigger = shallowRef<HTMLElement | null>(null)
const retryBusy = shallowRef(false)
const notifications = useToast()
const collections = shallowRef<CollectionSummary[]>([])
const collectionBusy = shallowRef(false)
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
const retryDetails = shallowRef<RetryResult[]>([])
const retrySelectedDialogOpen = shallowRef(false)
const retrySelectedDialogTrigger = shallowRef<HTMLElement | null>(null)
const selectedRetrySubmission = shallowRef<SelectedRetrySubmission | null>(null)
const selectedRetryOptions = shallowRef<ImageProcessingOptions>(defaultProcessingOptions())
const preserveSelectedRetryOptions = shallowRef(false)
let libraryRequestId = 0
const view = shallowRef<'grid' | 'list'>('grid')
const deletingImageId = shallowRef<string | null>(null)
const imageActionsDisabled = computed(() => busy.value || retryBusy.value || !!stageBusy.value || !!deletingImageId.value)

const selectedIds = computed(() => [...selectedImages.value])
const selectedCount = computed(() => selectedImages.value.size)
const selectionActive = computed(() => selectionMode.value || selectedCount.value > 0)
const unreadyImages = computed(() => images.value.filter(isRetryable))
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
const pageCount = computed(() => Math.max(1, Math.ceil(total.value / pageSize.value)))

/** 加载当前筛选下的图片，并清理不再存在的选中项。 */
async function loadLibrary(): Promise<void> {
  const requestId = ++libraryRequestId
  emit('clearError')
  busy.value = true
  try {
    const data = await api.images({ search: filter.value, page: page.value, page_size: pageSize.value, sort: sort.value, ...(status.value === "all" ? {} : { status: status.value }) })
    if (requestId !== libraryRequestId) return
    images.value = data.items
    total.value = Number.isInteger(data.total) ? data.total : data.items.length
    const nextPageCount = Math.max(1, Math.ceil(total.value / pageSize.value))
    if (page.value > nextPageCount) {
      // 删除或筛选可能让当前深页失效；先清空页内选择，再重新读取合法末页。
      page.value = nextPageCount
      selectedImages.value = new Set()
      await loadLibrary()
      return
    }
    if (previewImage.value) {
      previewImage.value = data.items.find((item: MemeImage) => item.meme_id === previewImage.value?.meme_id) || null
    }
    const keys = new Set(images.value.map(imageKey))
    selectedImages.value = new Set([...selectedImages.value].filter((key) => keys.has(key)))
  } catch (reason) {
    if (requestId === libraryRequestId) emit('loadError', errorMessage(reason))
  } finally {
    if (requestId === libraryRequestId) busy.value = false
  }
}

/** 筛选或排序变化时返回第一页，并清理当前选择。 */
function applyFilter(): void {
  page.value = 1
  selectedImages.value = new Set()
  notifications.show('开始读取筛选后的图片', 'info')
  void loadLibrary()
}

/** 手动刷新当前图片列表，开始提示仅由用户操作产生。 */
function refreshLibrary(): void {
  if (busy.value) return
  notifications.show('开始刷新图片库', 'info')
  void loadLibrary()
}

/** 更新状态条件，返回第一页并清空本次查询之前的选择。 */
function changeStatus(value: ImageLibraryStatus): void {
  status.value = value
  applyFilter()
}

/** 保存每页数量偏好，返回第一页并清空已有选择。 */
function changePageSize(value: number): void {
  saveLibraryPageSize(value)
  pageSize.value = value
  applyFilter()
}

/** 勾选当前页三项状态中尚有未就绪的图片，并保留当前页手动选择。 */
function selectCurrentPageUnready(): void {
  if (imageActionsDisabled.value || !unreadyImages.value.length) return
  selectionMode.value = true
  selectedImages.value = new Set([...selectedImages.value, ...unreadyImages.value.map(imageKey)])
}

/** 翻页时清理当前页之外的选择，避免跨页误操作。 */
function changePage(next: number): void {
  const target = Math.max(1, Math.min(pageCount.value, next))
  if (target === page.value) return
  page.value = target
  selectedImages.value = new Set()
  notifications.show('开始读取图片分页', 'info')
  void loadLibrary()
}

/** 切换一张图片的选择状态，并用新 Set 触发浅层响应式更新。 */
function toggleImageSelection(item: MemeImage): void {
  if (imageActionsDisabled.value) return
  const next = new Set(selectedImages.value)
  const key = imageKey(item)
  if (next.has(key)) next.delete(key)
  else next.add(key)
  selectedImages.value = next
}

/** 切换手机图片选择模式；结束选择时清理批量操作的目标。 */
function toggleSelectionMode(): void {
  selectionMode.value = !selectionActive.value
  if (!selectionMode.value) selectedImages.value = new Set()
}

/** 成功提交后批量按钮会隐藏，将对话框焦点交给仍可访问的刷新按钮。 */
function dialogReturnFocus(trigger: HTMLElement | null): HTMLElement | null {
  if (!selectedCount.value && trigger?.closest('.library-batch-actions')) return refreshTrigger.value
  return trigger
}

/** 加载当前 scope 的合集列表。 */
async function loadCollections(): Promise<void> {
  collectionBusy.value = true
  try {
    collections.value = (await api.collections()).items
  } catch (reason) {
    emit('loadError', errorMessage(reason))
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
    notifications.success(`已加入 ${result.added_count} 张图片`)
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
  notifications.show('开始创建合集并加入图片', 'info')
  try {
    const created = await api.createCollection({ name: dialogCollectionName.value })
    await api.addCollectionItems(created.collection_id, selectedIds.value)
    notifications.success('合集已创建并加入图片')
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
  notifications.show(`开始提交${imageStageLabel(stage)}任务`, 'info')
  try {
    const request: Record<string, unknown> = { meme_id: item.meme_id, stage }
    if (stage !== 'visual') request.reverse_image_policy = 'forbid'
    await api.submitImageStage(request)
    notifications.success(`${item.filename}：已提交${stage === 'visual' ? '视觉向量生成' : stage === 'agent' ? '图片语境分析' : stage === 'auto_rename' ? '自动重命名' : '文本向量生成'}独立任务`)
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

type RetryResult = ImageProcessingBatchResponse['results'][number] & { stage?: CoreImageProcessingStage }

/** 将服务端逐图结果收束为新的四种提交分类，兼容旧字段读取。 */
function retryResultCategory(result: RetryResult): 'submitted' | 'processing_active' | 'not_needed' | 'failed' {
  if (result.category === 'processing_active' || result.category === 'conflict' || result.status === 'conflict') return 'processing_active'
  if (result.category === 'not_needed') return 'not_needed'
  if (result.category === 'failed' || result.status === 'failed' || result.error) return 'failed'
  if (result.reused === true || result.category === 'reused' || result.status === 'reused') return 'not_needed'
  return 'submitted'
}

/** 将逐图分类和稳定错误收束为用户可扫描的一行反馈。 */
function retryResultMessage(result: RetryResult): string {
  const category = retryResultCategory(result)
  if (result.stage) return `${imageStageLabel(result.stage)}：${result.error ? `提交失败：${result.error}` : '已提交处理任务'}`
  if (category === 'processing_active') return `正在处理${result.reason || result.error ? `：${result.reason || result.error}` : ''}`
  if (category === 'not_needed') return `无需修复${result.reason || result.error ? `：${result.reason || result.error}` : ''}`
  if (category === 'failed') return `提交失败${result.error || result.reason ? `：${result.error || result.reason}` : ''}`
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
      const results = (Array.isArray(response.results) ? response.results : []) as RetryResult[]
      retryDetails.value = results
      const counts = results.reduce(
        (summary, item) => {
          summary[retryResultCategory(item)] += 1
          return summary
        },
        { submitted: 0, processing_active: 0, not_needed: 0, failed: 0 },
      )
      notifications.show(`重试选中：提交 ${counts.submitted} 个完整任务，处理中 ${counts.processing_active}，无需修复 ${counts.not_needed}，失败 ${counts.failed}`, counts.failed ? 'warning' : 'success')
    } else {
      const payload: Record<string, unknown> = { items: submission.items, stages: submission.stages }
      if (options) {
        if (!(submission.stages.length === 1 && submission.stages[0] === 'visual')) payload.reverse_image_policy = options.reverse_image_policy
        payload.auto_name = options.auto_name
      }
      const response = await api.retryImageStagesBatch(payload)
      if (!Array.isArray(response.results)) throw new Error('阶段批量提交响应缺少逐项结果')
      retryDetails.value = response.results
      notifications.show(`重试选中：已提交 ${response.submitted_count ?? 0} 个阶段任务${response.failed_count ? `，${response.failed_count} 项未提交` : ''}`, response.failed_count ? 'warning' : 'success')
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

/** 将共享选项对话框的取消事件路由到当前重试场景。 */
function cancelProcessingOptions(): void {
  cancelSelectedRetryOptions()
}

/** 将共享选项对话框的确认事件路由到当前重试场景。 */
async function confirmProcessingOptions(options: ImageProcessingOptions): Promise<void> {
  await confirmSelectedRetryOptions(options)
}

/** 打开图片预览并保留触发按钮，供关闭后恢复键盘焦点。 */
function openImagePreview(item: MemeImage, event: MouseEvent): void {
  previewTrigger.value = event.currentTarget instanceof HTMLElement ? event.currentTarget : null
  previewImage.value = item
}

/** 从方块图片打开详情，并保留图片上的按钮作为关闭后的焦点返回位置。 */
function openGridImagePreview(item: MemeImage, trigger: HTMLElement): void {
  previewTrigger.value = trigger
  previewImage.value = item
}

/** 确认后按稳定 ID 删除图片，刷新真实列表并保留服务端给出的错误原因。 */
async function deleteImage(item: MemeImage): Promise<void> {
  if (imageActionsDisabled.value || !window.confirm(`确定删除「${item.filename}」？图片及其语境和索引将被删除。`)) return
  deletingImageId.value = item.meme_id
  emit('clearError')
  try {
    await api.deleteImage({ meme_id: item.meme_id })
    await loadLibrary()
  } catch (reason) {
    await loadLibrary()
    emit('error', errorMessage(reason))
  } finally {
    deletingImageId.value = null
  }
}

/** 将详情中的阶段恢复动作交回工作区，保持稳定 meme_id 和现有提交契约。 */
function retryPreviewStage(stage: ImageProcessingStageName): void {
  if (previewImage.value) void retryStage(previewImage.value, stage)
}

/** 调用稳定 meme_id 完成重命名并刷新图库。 */
async function rename(item: MemeImage): Promise<void> {
  if (imageActionsDisabled.value) return
  const name = window.prompt('新文件名', item.filename)
  if (!name) return
  busy.value = true
  notifications.show('开始重命名图片', 'info')
  try {
    await api.rename({ meme_id: item.meme_id, new_name: name })
    notifications.success('图片已重命名')
    await loadLibrary()
  } catch (reason) {
    emit('error', errorMessage(reason))
  } finally {
    busy.value = false
  }
}

onMounted(loadLibrary)
watch(() => props.refreshToken, () => { void loadLibrary() })
</script>

<template>
  <section class="workspace" :aria-busy="busy">
    <div class="section-head">
      <div><h1>图片库</h1></div>
      <LibraryViewToggle v-model="view" />
    </div>
    <div class="toolbar library-toolbar" aria-label="图片库工具">
      <input v-model="filter" aria-label="筛选文件名" placeholder="筛选文件名" @keyup.enter="applyFilter" />
      <select v-model="sort" aria-label="图片排序方式" :disabled="retryBusy" @change="applyFilter">
        <option value="name_asc">名称 A–Z</option>
        <option value="updated_desc">最近更新</option>
      </select>
      <LibraryFilters :status="status" :page-size="pageSize" :disabled="imageActionsDisabled" @update:status="changeStatus" @update:page-size="changePageSize" />
      <button ref="refreshTrigger" type="button" :disabled="busy" @click="refreshLibrary">刷新</button>
      <button class="quiet library-select-toggle" type="button" :disabled="imageActionsDisabled" :aria-pressed="selectionActive" @click="toggleSelectionMode">{{ selectionActive ? '结束选择' : '选择图片' }}</button>
      <span class="toolbar-spacer"></span>
      <div class="toolbar-group library-operations">
        <div class="library-batch-actions" :class="{ 'selection-active': selectedCount > 0 }" aria-label="选中图片操作">
        <button class="quiet" type="button" :disabled="imageActionsDisabled || !selectedCount" @click="openCollectionDialog">
          加入合集<span v-if="selectedCount">（{{ selectedCount }}）</span>
        </button>
        <button class="quiet" type="button" :disabled="imageActionsDisabled || !selectedCount" @click="openRetrySelectedDialog">
          重试选中<span v-if="selectedCount">（{{ selectedCount }}）</span>
        </button>
        </div>
        <ActionMenu class="library-management" label="管理" :disabled="imageActionsDisabled">
        <button class="primary toolbar-primary" type="button" :disabled="imageActionsDisabled || !unreadyImages.length" @click="selectCurrentPageUnready">
          选中当前页所有未就绪
        </button>
        <button
          class="primary toolbar-primary cache-action"
          type="button"
          :disabled="imageActionsDisabled || cacheGenerating || !config"
          :aria-busy="cacheGenerating"
          :title="cacheButtonTitle"
          @click="emit('generateCache')"
        >
          {{ cacheButtonLabel }}
        </button>
        </ActionMenu>
        <span v-if="cacheTask || cacheBusy" class="cache-status" :class="cacheTask?.status || 'running'" role="status" aria-live="polite" aria-atomic="true">
          <span class="cache-status-dot" aria-hidden="true"></span>
          <span>{{ cacheTaskStatusLabel }}</span>
          <b v-if="cacheTask?.progress != null">{{ Math.round(cacheTask.progress * 100) }}%</b>
        </span>
      </div>
    </div>
    <ul v-if="retryDetails.length" class="processing-result-details" aria-label="图片处理逐图结果">
      <li v-for="result in retryDetails" :key="`${result.meme_id}:${result.stage || 'pipeline'}`" :class="retryResultCategory(result)">
        <strong v-if="showTaskDiagnostics">{{ result.meme_id }}</strong>
        <span>{{ retryResultMessage(result) }}</span>
      </li>
    </ul>
    <LibraryImageGrid
      v-if="view === 'grid'"
      :class="{ 'selection-active': selectionActive }"
      :images="images"
      :selected-ids="selectedImages"
      :disabled="imageActionsDisabled"
      :deleting-image-id="deletingImageId"
      @toggle-selection="toggleImageSelection"
      @preview="openGridImagePreview"
      @rename="rename"
      @delete="deleteImage"
    />
    <div v-else class="library-list" :class="{ 'selection-active': selectionActive }" role="list">
      <article v-for="item in images" :key="imageKey(item)" class="library-row" role="listitem">
        <label class="image-check">
          <input
            type="checkbox"
            :checked="selectedImages.has(imageKey(item))"
            :disabled="imageActionsDisabled"
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
          <span class="visual-embedding-state" :class="item.visual_embedding_status || 'unknown'">{{ visualEmbeddingLabel(item.visual_embedding_status) }}</span>
          <span class="metadata-state" :class="item.metadata?.status || 'unknown'">{{ metadataLabel(item.metadata?.status) }}</span>
          <span class="embedding-state" :class="item.embedding_status || 'unknown'">{{ embeddingLabel(item.embedding_status) }}</span>
        </div>
        <div class="image-row-actions" aria-label="图片操作">
          <button class="quiet metadata-button" type="button" @click="openImagePreview(item, $event)">查看详情</button>
          <ActionMenu label="更多" :disabled="imageActionsDisabled">
            <button class="quiet" type="button" :disabled="imageActionsDisabled" @click="rename(item)">重命名</button>
            <button class="quiet" type="button" :disabled="imageActionsDisabled" :aria-label="`删除图片 ${item.filename}`" @click="deleteImage(item)">{{ deletingImageId === item.meme_id ? '删除中...' : '删除' }}</button>
          </ActionMenu>
        </div>
      </article>
      <div v-if="!images.length" class="empty-state compact"><h2>图片库还没有图片</h2></div>
    </div>
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
    :return-focus="() => dialogReturnFocus(retrySelectedDialogTrigger)"
    @cancel="cancelRetrySelected"
    @confirm="confirmRetrySelected"
  />
  <ImageProcessingOptionsDialog
    v-if="processingOptionsOpen"
    :reverse-image-available="props.config?.reverse_image_available === true"
    :reverse-image-reason="props.config ? '反向图片服务不可用' : '服务状态未知'"
    :busy="retryBusy"
    :return-focus="() => dialogReturnFocus(processingOptionsTrigger)"
    processing-target="full_retry"
    :initial-options="preserveSelectedRetryOptions ? selectedRetryOptions : undefined"
    @cancel="cancelProcessingOptions"
    @confirm="confirmProcessingOptions"
  />
</template>
