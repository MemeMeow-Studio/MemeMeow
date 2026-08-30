<script setup lang="ts">
/** 上传工作区：管理文件选择、选项确认与逐文件结果。 */
import { computed, onMounted, onUnmounted, shallowRef } from 'vue'
import type { ImageProcessingOptions, ServiceConfig } from '../types'
import { uploadErrorMessage } from '../utils/presentation'
import ImageProcessingOptionsDialog from './ImageProcessingOptionsDialog.vue'
import UploadPendingItem from './UploadPendingItem.vue'
import { useUploadBatch, type UploadBatchItem } from '../composables/useUploadBatch'

const props = defineProps<{
  config: ServiceConfig | null
}>()

const emit = defineEmits<{
  error: [message: string]
  clearError: []
  openTask: [taskId: string]
}>()

const dialogOpen = shallowRef(false)
const dialogTrigger = shallowRef<HTMLElement | null>(null)
const retryOptions = shallowRef<ImageProcessingOptions>({ reverse_image_policy: 'forbid', auto_name: false })
const preserveRetryOptions = shallowRef(false)
const batch = useUploadBatch()
const batchItems = batch.items
const batchSummary = batch.summary
const submitFiles = batch.submittableFiles
const busy = batch.busy
const paused = batch.paused
const isDragActive = shallowRef(false)
const pendingItems = computed(() => batchItems.value.filter((item) => item.status === 'pending'))
const resultItems = computed(() => batchItems.value.filter((item) => item.status !== 'pending'))
const queuedItemCount = computed(() => batchItems.value.filter((item) => (
  item.status === 'pending'
  || item.status === 'uploading'
  || (item.status === 'failed' && item.retryable)
)).length)
const canRetryFailed = computed(() => !busy.value && batchItems.value.some((item) => item.status === 'failed' && item.retryable))

/** 剪贴板图片的受支持 MIME 与扩展名，必须与后端上传格式保持一致。 */
const clipboardImageExtensions: Readonly<Record<string, string>> = {
  'image/png': '.png',
  'image/jpeg': '.jpg',
  'image/gif': '.gif',
}
/** 文件名已有受支持扩展名时允许保留，避免无意义地改名。 */
const supportedImageExtensions = new Set(['.png', '.jpg', '.jpeg', '.gif'])
/** 拖放沿用文件选择器的图片 MIME 范围；类型缺失时由扩展名判断，不提前替代服务端校验。 */
const supportedImageMimeTypes = new Set(Object.keys(clipboardImageExtensions))
/** 组件内的粘贴文件序号，配合时间戳生成可读且不重复的临时文件名。 */
let pastedFileSequence = 0
let dragDepth = 0

/** 读取原生文件输入，以替换当前批次中的本地待上传项。 */
function onFiles(event: Event): void {
  if (busy.value) return
  const input = event.target as HTMLInputElement
  batch.setFiles([...(input.files || [])])
  preserveRetryOptions.value = false
  retryOptions.value = { reverse_image_policy: 'forbid', auto_name: false }
}

/** 将一组本地文件追加到批次，供剪贴板和后续拖放入口共用。 */
function appendLocalFiles(files: File[]): void {
  if (busy.value) return
  batch.appendFiles(files)
}

/** 判断拖放文件是否落在既有受控图片范围，类型缺失时仍接受受控扩展名。 */
function isSupportedDropFile(value: unknown): value is File {
  if (!isClipboardFile(value)) return false
  const file = value as File
  const fileType = file.type.trim().toLowerCase()
  const extension = file.name.trim().match(/\.[^.]+$/)?.[0]?.toLowerCase() || ''
  return supportedImageMimeTypes.has(fileType) || supportedImageExtensions.has(extension)
}

/** 清除拖放计数和激活样式，覆盖放下、离开及组件卸载场景。 */
function resetDragState(): void {
  dragDepth = 0
  isDragActive.value = false
}

/** 记录嵌套拖入事件并抑制浏览器默认处理，避免文件被浏览器直接打开。 */
function onDragEnter(event: DragEvent): void {
  event.preventDefault()
  if (busy.value) return
  dragDepth += 1
  isDragActive.value = true
}

/** 保持上传区域成为有效放置目标，并在上传中继续抑制浏览器默认行为。 */
function onDragOver(event: DragEvent): void {
  event.preventDefault()
  if (!busy.value) isDragActive.value = true
}

/** 以嵌套深度收束拖入激活态，避免经过区域内部文字时闪烁。 */
function onDragLeave(event: DragEvent): void {
  event.preventDefault()
  dragDepth = Math.max(0, dragDepth - 1)
  if (dragDepth === 0) isDragActive.value = false
}

/** 按 DataTransfer 文件顺序追加本地文件；上传中只抑制默认行为而不改写活动批次。 */
function onDrop(event: DragEvent): void {
  event.preventDefault()
  resetDragState()
  if (busy.value) return
  const files = Array.from(event.dataTransfer?.files || []).filter(isSupportedDropFile)
  appendLocalFiles(files)
}

/**
 * 判断剪贴板返回值是否具备 File 的跨 realm 数据形状。
 * 输入来自 DataTransferItem.getAsFile；输出供上传队列消费的 File，不能依赖当前 window 的 instanceof。
 */
function isClipboardFile(value: unknown): value is File {
  if (typeof value !== 'object' || value === null) return false
  const candidate = value as Partial<File>
  return typeof candidate.name === 'string'
    && typeof candidate.type === 'string'
    && typeof candidate.size === 'number'
    && Number.isFinite(candidate.size)
    && typeof candidate.slice === 'function'
}

/**
 * 为剪贴板文件补齐后端接受的扩展名，并避免同一队列中的文件名冲突。
 * 输入是剪贴板 File 与当前队列文件名；输出保留原对象或返回同内容的规范化 File。
 */
function normalizePastedFile(file: File, existingNames: Set<string>): File {
  const fileType = file.type.trim().toLowerCase()
  const originalName = file.name.trim()
  const originalExtension = originalName.match(/\.[^.]+$/)?.[0]?.toLowerCase() || ''
  const mimeExtension = clipboardImageExtensions[fileType]
  const compatibleNameExtension = fileType === 'image/jpeg'
    ? ['.jpg', '.jpeg'].includes(originalExtension)
    : mimeExtension === originalExtension
  const extension = compatibleNameExtension
    ? originalExtension
    : mimeExtension || (supportedImageExtensions.has(originalExtension) ? originalExtension : '.png')
  const originalBase = (originalName.replace(/\.[^.]+$/, '') || 'pasted-image')
    .replace(/[\\/\x00-\x1f\x7f:*?"<>|]/g, '_')
    .trim()
    .replace(/^[. ]+|[. ]+$/g, '')
  const baseName = !originalName || !originalBase || originalBase.toLowerCase() === 'blob'
    ? `pasted-${Date.now()}-${++pastedFileSequence}`
    : originalBase
  const existingNameKeys = new Set(Array.from(existingNames, (name) => name.trim().toLowerCase()))
  let candidate = `${baseName}${extension}`
  let suffix = 2
  while (existingNameKeys.has(candidate.toLowerCase())) candidate = `${baseName}-${suffix++}${extension}`
  existingNameKeys.add(candidate.toLowerCase())
  existingNames.add(candidate)
  const normalizedType = fileType || (extension === '.jpg' || extension === '.jpeg' ? 'image/jpeg' : `image/${extension.slice(1)}`)
  return candidate === originalName && normalizedType === file.type ? file : new File([file], candidate, { type: normalizedType })
}

/**
 * 读取当前剪贴板中的图片并追加到待上传队列。
 * 输入是浏览器 paste 事件；上传过程中直接忽略，避免改写活动批次。
 */
function onPaste(event: ClipboardEvent): void {
  if (busy.value || !event.clipboardData) return
  const clipboardItems = event.clipboardData.items
  if (!clipboardItems) return
  const pasted = Array.from(clipboardItems)
    .filter((item) => item.kind === 'file' && typeof item.type === 'string' && clipboardImageExtensions[item.type.trim().toLowerCase()])
    .map((item) => {
      try {
        return item.getAsFile()
      } catch {
        return null
      }
    })
    .filter(isClipboardFile)
  if (!pasted.length) return
  event.preventDefault()
  const existingNames = new Set(batchItems.value.map((item) => item.file.name))
  const normalized = pasted.map((file) => normalizePastedFile(file, existingNames))
  appendLocalFiles(normalized)
}

onMounted(() => window.addEventListener('paste', onPaste))
onUnmounted(() => {
  window.removeEventListener('paste', onPaste)
  resetDragState()
})

/** 打开共享选项对话框；请求尚未发生时保留文件选择。 */
function openOptions(event?: MouseEvent): void {
  if (busy.value || !submitFiles.value.length) return
  if (!preserveRetryOptions.value) retryOptions.value = { reverse_image_policy: 'forbid', auto_name: false }
  dialogTrigger.value = event?.currentTarget instanceof HTMLElement ? event.currentTarget : null
  dialogOpen.value = true
}

/** 取消选项确认，不产生上传请求。 */
function cancelOptions(): void {
  if (!busy.value) {
    dialogOpen.value = false
    preserveRetryOptions.value = false
    retryOptions.value = { reverse_image_policy: 'forbid', auto_name: false }
  }
}

/** 使用一组选项上传全部文件，失败时保留文件和选项以便安全重试。 */
async function confirmOptions(options: ImageProcessingOptions): Promise<void> {
  if (!submitFiles.value.length || busy.value) return
  emit('clearError')
  retryOptions.value = options
  preserveRetryOptions.value = true
  const selectedFiles = submitFiles.value
  const outcome = await batch.start(selectedFiles, options, props.config)
  if (outcome.transportError) {
    // 传输异常的 message 可能来自后端 detail；这里只信任稳定错误码并使用固定文案。
    emit('error', uploadErrorMessage(outcome.transportError))
    // 网络或服务错误时保持对话框和选择，用户可以直接再次确认。
    return
  }
  dialogOpen.value = false
  preserveRetryOptions.value = false
  retryOptions.value = { reverse_image_policy: 'forbid', auto_name: false }
}

/** 返回不依赖后端枚举原文的逐项状态文案。 */
function statusLabel(item: UploadBatchItem): string {
  if (item.status === 'succeeded') return '完成'
  if (item.status === 'uploading') return '上传中'
  if (item.status === 'pending') return '等待中'
  if (item.status === 'cancelled') return '已取消'
  return item.error === 'rate_limited' ? '等待重试' : '失败'
}

/** 展示服务端成功摘要或稳定错误原因，不把内部错误码直接暴露给用户。 */
function itemDetail(item: UploadBatchItem): string {
  if (item.error) return uploadErrorMessage(item.error)
  return item.result?.saved_filename || item.result?.processing_status || ''
}
</script>

<template>
  <section class="workspace narrow" :aria-busy="busy">
    <div class="section-head">
      <div><h1>上传图片</h1><p>支持 PNG、JPG、JPEG 和 GIF。</p></div>
    </div>
    <div class="upload-panel">
      <label
        class="drop-zone"
        :class="{ 'is-dragging': isDragActive }"
        @dragenter="onDragEnter"
        @dragover="onDragOver"
        @dragleave="onDragLeave"
        @drop="onDrop"
      >
        <input type="file" multiple accept=".png,.jpg,.jpeg,.gif" aria-label="选择图片文件" :disabled="busy" @change="onFiles" />
        <span class="drop-title">选择图片文件</span>
        <span class="drop-sub">{{ queuedItemCount ? `已选择 ${queuedItemCount} 个文件，可继续添加` : '点击选择或拖入文件' }}</span>
        <span class="drop-hint">支持 Ctrl+V 连续添加图片，最后统一上传</span>
      </label>
      <button class="primary wide" type="button" :disabled="busy || !submitFiles.length" @click="openOptions">
        {{ busy ? '上传中...' : '上传所选图片' }}
      </button>
      <div v-if="batchSummary.total" class="upload-summary" aria-live="polite">
        <strong>已处理 {{ batchSummary.succeeded + batchSummary.failed + batchSummary.cancelled }} / {{ batchSummary.total }}</strong>
        <span>成功 {{ batchSummary.succeeded }}，失败 {{ batchSummary.failed }}，取消 {{ batchSummary.cancelled }}</span>
        <span v-if="batchSummary.pending" class="summary-muted">等待 {{ batchSummary.pending }}</span>
        <button v-if="busy && !paused" class="quiet" type="button" @click="batch.pause">暂停</button>
        <button v-if="busy && paused" class="quiet" type="button" @click="batch.resume">继续</button>
        <button v-if="busy" class="quiet" type="button" aria-label="取消未发送图片" @click="batch.cancel">取消未发送</button>
        <button v-if="canRetryFailed" class="quiet" type="button" @click="openOptions">重试失败项</button>
      </div>
    </div>
    <div v-if="batchItems.length" class="upload-results" aria-live="polite">
      <UploadPendingItem
        v-for="item in pendingItems"
        :key="item.id"
        :item="item"
        :removable="!busy"
        @remove="batch.removePending"
      />
      <div v-for="item in resultItems" :key="item.id" v-memo="[item.status, item.error, item.result?.meme_id, item.result?.processing_status]" class="upload-result" :class="{ fail: item.status === 'failed' || item.status === 'cancelled' }">
        <span>{{ statusLabel(item) }}</span>
        <strong :title="item.file.name">{{ item.file.name }}</strong>
        <button v-if="item.result?.processing_job_id || item.result?.metadata_job_id" class="quiet" type="button" @click="emit('openTask', item.result?.processing_job_id || item.result?.metadata_job_id || '')">查看任务</button>
        <small>{{ itemDetail(item) }}</small>
      </div>
    </div>
  </section>

  <ImageProcessingOptionsDialog
    v-if="dialogOpen"
    :reverse-image-available="props.config?.reverse_image_available === true"
    :reverse-image-reason="props.config ? '反向图片服务不可用' : '服务状态未知'"
    :busy="busy"
    :return-focus="dialogTrigger"
    :initial-options="preserveRetryOptions ? retryOptions : undefined"
    @cancel="cancelOptions"
    @confirm="confirmOptions"
  />
</template>
