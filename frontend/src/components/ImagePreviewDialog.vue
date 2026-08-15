<script setup lang="ts">
/** 图片预览对话框：加载完整元数据，并负责复制、焦点约束和关闭恢复。 */
import { computed, shallowRef } from 'vue'
import { api } from '../api'
import { useModalDialog } from '../composables/useModalDialog'
import type { MemeImage } from '../types'
import { errorMessage } from '../utils/presentation'

const props = defineProps<{
  image: MemeImage
  returnFocus?: HTMLElement | null
}>()

const emit = defineEmits<{
  close: []
}>()

const dialog = shallowRef<HTMLElement | null>(null)
const closeButton = shallowRef<HTMLElement | null>(null)
const previewJson = shallowRef<unknown>(null)
const loading = shallowRef(true)
const previewError = shallowRef('')
const copyNotice = shallowRef('')
const previewJsonText = computed(() => previewJson.value ? JSON.stringify(previewJson.value, null, 2) : '')

useModalDialog({
  dialog,
  initialFocus: closeButton,
  returnFocus: props.returnFocus,
  close: () => emit('close'),
})

/** 读取当前图片的完整数据库元数据，组件卸载后不再更新视图。 */
async function loadMetadata(): Promise<void> {
  try {
    previewJson.value = await api.imageMetadata(props.image.meme_id)
  } catch (reason) {
    previewError.value = errorMessage(reason, '图片元数据读取失败')
  } finally {
    loading.value = false
  }
}

/** 将文本写入剪贴板，在旧浏览器中使用隐藏 textarea 作为兼容路径。 */
async function writeTextToClipboard(value: string): Promise<void> {
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

/** 复制完整元数据 JSON，并在对话框内反馈结果。 */
async function copyMetadata(): Promise<void> {
  if (!previewJsonText.value) return
  try {
    await writeTextToClipboard(previewJsonText.value)
    copyNotice.value = '元数据已复制'
  } catch (reason) {
    copyNotice.value = errorMessage(reason, '元数据复制失败，请检查浏览器权限')
  }
}

void loadMetadata()
</script>

<template>
  <div class="image-dialog-backdrop" role="presentation" @click.self="emit('close')">
    <section
      ref="dialog"
      class="image-dialog"
      role="dialog"
      aria-modal="true"
      aria-labelledby="image-dialog-title"
      tabindex="-1"
    >
      <header class="image-dialog-head">
        <div>
          <h2 id="image-dialog-title">{{ image.filename }}</h2>
          <p>图片预览与元数据</p>
        </div>
        <button ref="closeButton" class="quiet" type="button" aria-label="关闭图片预览" @click="emit('close')">
          关闭
        </button>
      </header>
      <div class="image-dialog-content">
        <div class="image-dialog-preview">
          <img :src="image.media_url" :alt="`放大查看 ${image.filename}`" />
        </div>
        <section class="metadata-panel" aria-labelledby="metadata-panel-title">
          <div class="metadata-panel-head">
            <h3 id="metadata-panel-title">图片元数据</h3>
            <button class="quiet" type="button" :disabled="loading || !previewJsonText" @click="copyMetadata">
              复制元数据
            </button>
          </div>
          <p v-if="loading" class="metadata-loading" role="status">正在读取元数据...</p>
          <p v-else-if="previewError" class="metadata-error" role="alert">{{ previewError }}</p>
          <pre v-else class="metadata-json">{{ previewJsonText }}</pre>
          <p v-if="copyNotice" class="copy-notice" role="status" aria-live="polite">{{ copyNotice }}</p>
        </section>
      </div>
    </section>
  </div>
</template>
