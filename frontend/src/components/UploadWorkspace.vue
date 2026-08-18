<script setup lang="ts">
/** 上传工作区：管理文件选择、选项确认与逐文件结果。 */
import { shallowRef } from 'vue'
import { api } from '../api'
import type { ImageProcessingOptions, ServiceConfig, UploadResult } from '../types'
import { errorMessage } from '../utils/presentation'
import ImageProcessingOptionsDialog from './ImageProcessingOptionsDialog.vue'

const props = defineProps<{
  config: ServiceConfig | null
}>()

const emit = defineEmits<{
  error: [message: string]
  clearError: []
  openTask: [taskId: string]
}>()

const files = shallowRef<File[]>([])
const uploadResults = shallowRef<UploadResult[]>([])
const busy = shallowRef(false)
const dialogOpen = shallowRef(false)
const dialogTrigger = shallowRef<HTMLElement | null>(null)
const retryOptions = shallowRef<ImageProcessingOptions>({ reverse_image_policy: 'forbid', auto_name: false })
const preserveRetryOptions = shallowRef(false)

/** 读取原生文件输入，并以不可变数组保存用户选择。 */
function onFiles(event: Event): void {
  const input = event.target as HTMLInputElement
  files.value = [...(input.files || [])]
  preserveRetryOptions.value = false
  retryOptions.value = { reverse_image_policy: 'forbid', auto_name: false }
}

/** 打开共享选项对话框；请求尚未发生时保留文件选择。 */
function openOptions(event: MouseEvent): void {
  if (busy.value || !files.value.length) return
  if (!preserveRetryOptions.value) retryOptions.value = { reverse_image_policy: 'forbid', auto_name: false }
  dialogTrigger.value = event.currentTarget instanceof HTMLElement ? event.currentTarget : null
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
  busy.value = true
  try {
    const selectedFiles = files.value
    const response = await api.upload(selectedFiles, options)
    uploadResults.value = response.results
    // 逐文件接口可能部分成功；只移除已成功的输入，失败项仍可安全重试。
    files.value = selectedFiles.filter((_file, index) => response.results[index]?.ok !== true)
    dialogOpen.value = false
    // 本次请求已经收束；下一次重新打开必须重新使用安全默认值。网络异常
    // 则不会进入这里，对话框仍保持打开并保留当前选择供用户重试。
    preserveRetryOptions.value = false
    retryOptions.value = { reverse_image_policy: 'forbid', auto_name: false }
  } catch (reason) {
    emit('error', errorMessage(reason))
    // 网络或服务错误时保持对话框和选择，用户可以直接再次确认。
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <section class="workspace narrow" :aria-busy="busy">
    <div class="section-head">
      <div><h1>上传图片</h1><p>支持 PNG、JPG、JPEG 和 GIF。</p></div>
    </div>
    <div class="upload-panel">
      <label class="drop-zone">
        <input type="file" multiple accept=".png,.jpg,.jpeg,.gif" aria-label="选择图片文件" @change="onFiles" />
        <span class="drop-title">选择图片文件</span>
        <span class="drop-sub">{{ files.length ? `已选择 ${files.length} 个文件` : '点击选择或拖入文件' }}</span>
      </label>
      <button class="primary wide" type="button" :disabled="busy || !files.length" @click="openOptions">
        {{ busy ? '上传中...' : '上传所选图片' }}
      </button>
    </div>
    <div v-if="uploadResults.length" class="upload-results" aria-live="polite">
      <div v-for="(item, index) in uploadResults" :key="item.meme_id || `${item.filename}-${index}`" class="upload-result" :class="{ fail: !item.ok }">
        <span>{{ item.ok ? '完成' : '失败' }}</span>
        <strong :title="item.filename">{{ item.filename }}</strong>
        <button v-if="item.processing_job_id || item.metadata_job_id" class="quiet" type="button" @click="emit('openTask', item.processing_job_id || item.metadata_job_id || '')">查看任务</button>
        <small v-else>{{ item.ok ? item.saved_filename : item.error }}</small>
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
