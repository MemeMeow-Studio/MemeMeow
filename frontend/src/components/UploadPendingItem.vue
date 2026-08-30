<script setup lang="ts">
/** 待上传文件展示单元：只读取本地 File，管理本地预览资源并发送移除事件。 */
import { onUnmounted, shallowRef, watch } from 'vue'
import type { UploadBatchItem } from '../composables/useUploadBatch'

const props = defineProps<{
  item: UploadBatchItem
  removable: boolean
}>()

const emit = defineEmits<{
  remove: [id: string]
}>()

const previewUrl = shallowRef<string | null>(null)
const previewFailed = shallowRef(false)
let objectUrl: string | null = null

/** 回收当前文件的对象 URL，并清空预览引用，避免文件移除后继续占用解码内存。 */
function revokePreview(): void {
  if (objectUrl && typeof URL.revokeObjectURL === 'function') URL.revokeObjectURL(objectUrl)
  objectUrl = null
  previewUrl.value = null
}

/** 为待上传文件创建一次本地预览；浏览器不支持时保留文件名和移除能力。 */
function createPreview(file: File): void {
  revokePreview()
  previewFailed.value = false
  try {
    if (typeof URL.createObjectURL !== 'function') throw new Error('本地预览不可用')
    objectUrl = URL.createObjectURL(file)
    previewUrl.value = objectUrl
  } catch {
    previewFailed.value = true
  }
}

/** 响应浏览器图片解码失败，立即回收对象 URL，不回退到服务端媒体请求。 */
function handlePreviewError(): void {
  previewFailed.value = true
  revokePreview()
}

/** 文件身份变化时替换本地预览，并在行组件卸载时回收最后一个对象 URL。 */
watch(() => props.item.file, createPreview, { immediate: true })
onUnmounted(revokePreview)
</script>

<template>
  <article class="upload-result upload-pending-item">
    <div class="upload-pending-preview">
      <img
        v-if="previewUrl && !previewFailed"
        :src="previewUrl"
        :alt="`本地预览：${props.item.file.name}`"
        draggable="false"
        @error="handlePreviewError"
      />
      <span
        v-else
        class="upload-pending-preview-fallback"
        role="img"
        :aria-label="`无法预览 ${props.item.file.name}`"
      >
        预览不可用
      </span>
    </div>
    <div class="upload-pending-meta">
      <span class="upload-pending-status">等待中</span>
      <strong :title="props.item.file.name">{{ props.item.file.name }}</strong>
    </div>
    <button
      v-if="props.removable && props.item.status === 'pending'"
      class="quiet upload-pending-remove"
      type="button"
      :aria-label="`移除待上传图片 ${props.item.file.name}`"
      @click="emit('remove', props.item.id)"
    >
      移除
    </button>
  </article>
</template>
