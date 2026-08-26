<script setup lang="ts">
/** 图片预览对话框：加载完整元数据，并负责复制、焦点约束和关闭恢复。 */
import { computed, shallowRef } from 'vue'
import { api } from '../api'
import { useModalDialog } from '../composables/useModalDialog'
import type { ImageProcessingStage, MemeImage } from '../types'
import { errorMessage, imageStageLabel, imageStageStatusLabel, taskStatusLabel } from '../utils/presentation'

const props = defineProps<{
  image: MemeImage
  returnFocus?: HTMLElement | null
  retryBusy?: boolean
  stageBusy?: string
  stageRecoveryEnabled?: boolean
}>()

const emit = defineEmits<{
  close: []
  'retry-stage': [stage: ImageProcessingStage['stage']]
}>()

const dialog = shallowRef<HTMLElement | null>(null)
const closeButton = shallowRef<HTMLElement | null>(null)
const previewJson = shallowRef<unknown>(null)
const loading = shallowRef(true)
const previewError = shallowRef('')
const copyNotice = shallowRef('')
const previewJsonText = computed(() => previewJson.value ? JSON.stringify(previewJson.value, null, 2) : '')
const processingStages = computed(() => props.image.processing_stages || [])
const retryableStages = computed(() => processingStages.value.filter((stage) => {
  if (stage.stage === 'auto_rename') return stage.status === 'warning'
  return ['failed', 'blocked', 'unknown_execution'].includes(stage.status)
}))
const processingWarning = computed(() => {
  if (!props.image.processing_has_warnings) return ''
  return props.image.processing_status === 'succeeded' ? '核心处理已完成，自动命名未完成' : '自动命名未完成'
})

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

/** 判断当前详情中的阶段恢复按钮是否应可用，沿用图库既有的安全条件。 */
function canRetryStage(stage: ImageProcessingStage): boolean {
  return retryableStages.value.some((candidate) => candidate.stage === stage.stage)
}

/** 将阶段错误压缩为优先级明确的可读原因。 */
function stageErrorMessage(stage: ImageProcessingStage): string {
  return stage.error?.error || stage.error?.message || '阶段失败'
}

/** 给恢复按钮返回稳定中文文案，避免把后端枚举暴露给用户。 */
function stageRetryLabel(stage: ImageProcessingStage): string {
  if (stage.stage === 'auto_rename') return '恢复自动命名'
  if (stage.stage === 'visual') return '仅视觉'
  if (stage.stage === 'agent') return '仅 Agent'
  return '仅文本'
}

/** 恢复动作进行中时锁定所有阶段按钮，避免重复提交。 */
function stageRetryDisabled(): boolean {
  return props.retryBusy === true || !!props.stageBusy
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
        </div>
        <button ref="closeButton" class="quiet" type="button" aria-label="关闭图片预览" @click="emit('close')">
          关闭
        </button>
      </header>
      <div class="image-dialog-content">
        <div class="image-dialog-preview">
          <img :src="image.media_url" :alt="`放大查看 ${image.filename}`" />
        </div>
        <div class="image-dialog-side">
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
          <section class="image-processing-details" aria-labelledby="image-processing-details-title">
            <div class="image-processing-details-head">
              <div>
                <h3 id="image-processing-details-title">处理阶段</h3>
                <p v-if="image.processing_job_id">完整处理 Job：{{ image.processing_job_id }}</p>
              </div>
              <span v-if="image.processing_status" class="processing-overall-status">{{ taskStatusLabel(image.processing_status) }}</span>
            </div>
            <p v-if="processingWarning" class="processing-warning" role="status">{{ processingWarning }}</p>
            <ul v-if="processingStages.length" class="image-processing-stage-list">
              <li v-for="stage in processingStages" :key="`${image.meme_id}:${stage.stage}`" class="image-processing-stage-detail" :class="stage.status">
                <div class="image-processing-stage-summary">
                  <span class="status-dot" :class="stage.status" aria-hidden="true"></span>
                  <strong>{{ imageStageLabel(stage.stage) }}</strong>
                  <span>{{ imageStageStatusLabel(stage.status) }}</span>
                </div>
                <p v-if="stage.error" class="image-processing-stage-error">原因：{{ stageErrorMessage(stage) }}</p>
                <button
                  v-if="stageRecoveryEnabled === true && canRetryStage(stage)"
                  class="quiet image-processing-stage-retry"
                  type="button"
                  :disabled="stageRetryDisabled()"
                  @click="emit('retry-stage', stage.stage)"
                >
                  {{ stageRetryLabel(stage) }}
                </button>
              </li>
            </ul>
            <p v-else class="processing-details-empty">暂无处理阶段记录</p>
          </section>
        </div>
      </div>
    </section>
  </div>
</template>
