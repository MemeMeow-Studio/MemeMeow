<script setup lang="ts">
/** 上传工作区：管理文件选择、选项确认与逐文件结果。 */
import { computed, onMounted, onUnmounted, shallowRef } from 'vue'
import type { ImageProcessingOptions, ServiceConfig } from '../types'
import { uploadErrorMessage } from '../utils/presentation'
import ImageProcessingOptionsDialog from './ImageProcessingOptionsDialog.vue'
import { useUploadBatch, type UploadBatchItem } from '../composables/useUploadBatch'

const props = defineProps<{
  config: ServiceConfig | null
}>()

const emit = defineEmits<{
  error: [message: string]
  clearError: []
  openTask: [taskId: string]
}>()

const files = shallowRef<File[]>([])
const dialogOpen = shallowRef(false)
const dialogTrigger = shallowRef<HTMLElement | null>(null)
const retryOptions = shallowRef<ImageProcessingOptions>({ reverse_image_policy: 'forbid', auto_name: false })
const preserveRetryOptions = shallowRef(false)
const batch = useUploadBatch()
const batchItems = batch.items
const batchSummary = batch.summary
const busy = batch.busy
const canRetryFailed = computed(() => !busy.value && batchItems.value.some((item) => item.status === 'failed' && item.retryable))

/** 剪贴板图片的受支持 MIME 与扩展名，必须与后端上传格式保持一致。 */
const clipboardImageExtensions: Readonly<Record<string, string>> = {
  'image/png': '.png',
  'image/jpeg': '.jpg',
  'image/gif': '.gif',
}
/** 文件名已有受支持扩展名时允许保留，避免无意义地改名。 */
const supportedImageExtensions = new Set(['.png', '.jpg', '.jpeg', '.gif'])
/** 组件内的粘贴文件序号，配合时间戳生成可读且不重复的临时文件名。 */
let pastedFileSequence = 0

/** 读取原生文件输入，并以不可变数组保存用户选择。 */
function onFiles(event: Event): void {
  if (busy.value) return
  const input = event.target as HTMLInputElement
  files.value = [...(input.files || [])]
  batch.setFiles(files.value)
  preserveRetryOptions.value = false
  retryOptions.value = { reverse_image_policy: 'forbid', auto_name: false }
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
  files.value = [...files.value, ...normalized]
  batch.appendFiles(normalized)
}

onMounted(() => window.addEventListener('paste', onPaste))
onUnmounted(() => window.removeEventListener('paste', onPaste))

/** 打开共享选项对话框；请求尚未发生时保留文件选择。 */
function openOptions(event?: MouseEvent): void {
  if (busy.value || !files.value.length) return
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
  if (!files.value.length || busy.value) return
  emit('clearError')
  retryOptions.value = options
  preserveRetryOptions.value = true
  const selectedFiles = files.value
  const outcome = await batch.start(selectedFiles, options, props.config)
  // 逐文件接口可能部分成功；仅保留仍可重试的失败项作为下一次输入。
  files.value = batchItems.value
    .filter((item) => item.status === 'failed' && item.retryable)
    .map((item) => item.file)
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
      <label class="drop-zone">
        <input type="file" multiple accept=".png,.jpg,.jpeg,.gif" aria-label="选择图片文件" :disabled="busy" @change="onFiles" />
        <span class="drop-title">选择图片文件</span>
        <span class="drop-sub">{{ files.length ? `已选择 ${files.length} 个文件，可继续添加` : '点击选择或拖入文件' }}</span>
        <span class="drop-hint">支持 Ctrl+V 连续添加图片，最后统一上传</span>
      </label>
      <button class="primary wide" type="button" :disabled="busy || !files.length" @click="openOptions">
        {{ busy ? '上传中...' : '上传所选图片' }}
      </button>
      <div v-if="batchSummary.total" class="upload-summary" aria-live="polite">
        <strong>已处理 {{ batchSummary.succeeded + batchSummary.failed + batchSummary.cancelled }} / {{ batchSummary.total }}</strong>
        <span>成功 {{ batchSummary.succeeded }}，失败 {{ batchSummary.failed }}，取消 {{ batchSummary.cancelled }}</span>
        <span v-if="batchSummary.pending" class="summary-muted">等待 {{ batchSummary.pending }}</span>
        <button v-if="busy && !batch.paused" class="quiet" type="button" @click="batch.pause">暂停</button>
        <button v-if="busy && batch.paused" class="quiet" type="button" @click="batch.resume">继续</button>
        <button v-if="busy" class="quiet" type="button" aria-label="取消未发送图片" @click="batch.cancel">取消未发送</button>
        <button v-if="canRetryFailed" class="quiet" type="button" @click="openOptions">重试失败项</button>
      </div>
    </div>
    <div v-if="batchItems.length" class="upload-results" aria-live="polite">
      <div v-for="item in batchItems" :key="item.id" v-memo="[item.status, item.error, item.result?.meme_id, item.result?.processing_status]" class="upload-result" :class="{ fail: item.status === 'failed' || item.status === 'cancelled' }">
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
