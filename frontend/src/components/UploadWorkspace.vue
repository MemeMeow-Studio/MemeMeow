<script setup lang="ts">
/** 上传工作区：管理文件选择、选项确认与逐文件结果。 */
import { computed, onMounted, onUnmounted, shallowRef } from 'vue'
import type { ImageProcessingOptions, ServiceConfig } from '../types'
import { errorMessage, uploadErrorMessage } from '../utils/presentation'
import ImageProcessingOptionsDialog from './ImageProcessingOptionsDialog.vue'
import UploadPendingItem from './UploadPendingItem.vue'
import UploadTaskHistory from './UploadTaskHistory.vue'
import { useUploadBatch } from '../composables/useUploadBatch'
import { defaultUploadMode } from '../utils/uploadMode'
import { isAndroidApp, pickAlbumImages } from '../utils/nativeMedia'

const props = defineProps<{
  config: ServiceConfig | null
  storageScope?: string
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
const taskHistory = shallowRef<InstanceType<typeof UploadTaskHistory> | null>(null)
const batchItems = batch.items
const batchSummary = batch.summary
const selectedItems = computed(() => {
  const pending = batchItems.value.filter((item) => item.status === 'pending' && item.attempts === 0)
  return pending.length ? pending : batch.submittableItems.value
})
const submitFiles = computed(() => selectedItems.value.map((item) => item.file))
const pendingItems = computed(() => batchItems.value.filter((item) => item.status === 'pending' && item.attempts === 0))
const busy = batch.busy
const paused = batch.paused
const isDragActive = shallowRef(false)
const pickingImages = shallowRef(false)
const queuedItemCount = computed(() => batchItems.value.filter((item) => (
  item.status === 'pending'
  || item.status === 'uploading'
  || (item.status === 'failed' && item.retryable)
)).length)

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

/** 文件选择、拖放和粘贴共用追加入口，确认提交时确定本次文件集合。 */
function onFiles(event: Event): void {
  if (busy.value) return
  const input = event.target as HTMLInputElement
  appendLocalFiles([...(input.files || [])])
  // 文件已复制到批次状态；清空原生值后，删除后再次选择同一文件仍会触发 change。
  input.value = ''
}

/** 安卓应用的文件入口打开系统照片选择器，选择结果进入现有队列。 */
async function onSelectImages(event: MouseEvent): Promise<void> {
  if (!isAndroidApp) return
  event.preventDefault()
  if (busy.value || pickingImages.value) return
  emit('clearError')
  pickingImages.value = true
  try {
    appendLocalFiles(await pickAlbumImages())
  } catch (reason) {
    emit('error', `相册图片选择失败：${errorMessage(reason)}`)
  } finally {
    pickingImages.value = false
  }
}

/** 将一组本地文件追加到批次，供剪贴板和后续拖放入口共用。 */
function appendLocalFiles(files: File[]): void {
  if (busy.value || !files.length) return
  if (pendingItems.value.length) batch.appendFiles(files)
  else {
    batch.setFiles(files)
    preserveRetryOptions.value = false
    retryOptions.value = { reverse_image_policy: 'forbid', auto_name: false }
  }
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
  if (!preserveRetryOptions.value) retryOptions.value = { reverse_image_policy: 'forbid', auto_name: false, upload_mode: defaultUploadMode(submitFiles.value.length) }
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
  taskHistory.value!.begin(selectedItems.value)
  dialogOpen.value = false
  const outcome = await batch.start(selectedFiles, options, props.config)
  await taskHistory.value?.refresh()
  if (outcome.transportError) {
    // 传输异常的 message 可能来自后端 detail；这里只信任稳定错误码并使用固定文案。
    emit('error', uploadErrorMessage(outcome.transportError))
    return
  }
  dialogOpen.value = false
  preserveRetryOptions.value = false
  retryOptions.value = { reverse_image_policy: 'forbid', auto_name: false }
}

/** 单项传输重试沿用原请求和选项，结果继续属于当前上传组。 */
async function retryTransfer(requestId: string): Promise<void> {
  const item = batchItems.value.find((candidate) => candidate.requestId === requestId)
  if (busy.value || !item || !item.retryable || !item.options) return
  taskHistory.value!.begin([item])
  await batch.start([item.file], item.options, props.config)
  await taskHistory.value!.refresh()
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
        @click="onSelectImages"
        @dragenter="onDragEnter"
        @dragover="onDragOver"
        @dragleave="onDragLeave"
        @drop="onDrop"
      >
        <input type="file" multiple accept=".png,.jpg,.jpeg,.gif" :aria-label="isAndroidApp ? '选择相册图片' : '选择图片文件'" :disabled="busy || pickingImages" @change="onFiles" />
        <span class="drop-title">{{ pickingImages ? '正在读取相册图片…' : isAndroidApp ? '选择相册图片' : '选择图片文件' }}</span>
        <span class="drop-sub">{{ queuedItemCount ? `已选择 ${queuedItemCount} 个文件，可继续添加` : isAndroidApp ? '点击打开相册，可选择多张图片' : '点击选择或拖入文件' }}</span>
        <span v-if="!isAndroidApp" class="drop-hint">支持 Ctrl+V 连续添加图片，最后统一上传</span>
        <span class="drop-hint">GIF 最大 100MB，超过 10MB 自动有损压缩，保留完整动画时长和循环设置</span>
      </label>
      <button class="primary wide" type="button" :disabled="busy || pickingImages || !submitFiles.length" @click="openOptions">
        {{ busy ? '上传中...' : '上传所选图片' }}
      </button>
      <div v-if="busy" class="upload-summary" aria-live="polite">
        <span v-if="batchSummary.pending" class="summary-muted">等待 {{ batchSummary.pending }}</span>
        <button v-if="busy && !paused" class="quiet" type="button" @click="batch.pause">暂停</button>
        <button v-if="busy && paused" class="quiet" type="button" @click="batch.resume">继续</button>
        <button v-if="busy" class="quiet" type="button" aria-label="取消未发送图片" @click="batch.cancel">取消未发送</button>
      </div>
    </div>
    <div v-if="!busy && pendingItems.length" class="upload-results" aria-live="polite">
      <template v-for="item in pendingItems" :key="item.id">
        <UploadPendingItem
          v-if="item.status === 'pending'"
          :item="item"
          :removable="!busy"
          @remove="batch.removePending"
        />
      </template>
    </div>
    <UploadTaskHistory ref="taskHistory" :key="props.storageScope || 'local'" :items="batchItems" :busy="busy" :storage-scope="props.storageScope" @retry-transfer="retryTransfer" />
  </section>

  <ImageProcessingOptionsDialog
    v-if="dialogOpen"
    :reverse-image-available="props.config?.reverse_image_available === true"
    :reverse-image-reason="props.config ? '反向图片服务不可用' : '服务状态未知'"
    :busy="busy"
    :return-focus="dialogTrigger"
    :initial-options="retryOptions"
    upload
    @cancel="cancelOptions"
    @confirm="confirmOptions"
  />
</template>
