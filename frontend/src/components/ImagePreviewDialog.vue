<script setup lang="ts">
/** 图片预览对话框：分层展示图片语境、文件详情和原始响应，并负责焦点约束与关闭恢复。 */
import { computed, shallowRef } from 'vue'
import { api } from '../api'
import { useModalDialog } from '../composables/useModalDialog'
import { showTaskDiagnostics } from '../config/debug'
import type { ImageProcessingStage, MemeImage } from '../types'
import { formatTaskTime, imageStageLabel, imageStageStatusLabel, taskStatusLabel } from '../utils/presentation'

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
const previewJsonText = computed(() => {
  if (previewJson.value === null || previewJson.value === undefined) return ''
  const serialized = JSON.stringify(previewJson.value, null, 2)
  return typeof serialized === 'string' ? serialized : ''
})
const processingStages = computed(() => props.image.processing_stages || [])
const retryableStages = computed(() => processingStages.value.filter((stage) => {
  if (stage.stage === 'auto_rename') return stage.status === 'warning'
  return ['failed', 'blocked', 'unknown_execution'].includes(stage.status)
}))
const processingWarning = computed(() => {
  if (!props.image.processing_has_warnings) return ''
  return props.image.processing_status === 'succeeded' ? '核心处理已完成，自动命名未完成' : '自动命名未完成'
})

type MetadataRecord = Record<string, unknown>

interface SummaryField {
  key: string
  label: string
  value: string
}

interface FileDetail {
  label: string
  value: string
}

interface SourceLink {
  label: string
  url: string
}

interface MetadataStatusView {
  label: string
  description: string
}

const SUMMARY_FIELD_DEFINITIONS = [
  { key: 'title', label: '标题' },
  { key: 'summary', label: '摘要' },
  { key: 'subjects', label: '主体' },
  { key: 'visible_text', label: '图片文字' },
  { key: 'meaning', label: '含义' },
  { key: 'keywords', label: '关键词' },
] as const

const METADATA_STATUS_VIEWS: Readonly<Record<string, MetadataStatusView>> = Object.freeze({
  pending: { label: '待生成', description: '等待图片语境生成完成' },
  partial: { label: '部分完成', description: '已有部分识别结果，仍可能有字段待补充' },
  ready: { label: '已就绪', description: '图片语境已完成生成' },
  repair_required: { label: '需要修复', description: '图片元数据需要修复后再使用' },
  unknown: { label: '状态未知', description: '暂时无法确认图片语境状态' },
})

const METADATA_ERROR_LABELS: Readonly<Record<string, string>> = Object.freeze({
  metadata_missing: '图片元数据不存在，请重新处理图片',
  metadata_invalid: '图片元数据格式无效，需要修复',
  metadata_image_mismatch: '图片文件已变化，需要修复元数据',
  metadata_path_mismatch: '图片路径已变化，需要修复元数据',
  image_unreadable: '图片文件无法读取，请检查文件后重试',
  target_changed: '图片在处理期间发生变化，请重新处理',
})

/** 判断未知值是否为可读取的对象记录，避免模板直接访问不可信响应。 */
function isRecord(value: unknown): value is MetadataRecord {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

/** 从候选值中读取首个非空字符串，用于兼容历史字段别名。 */
function firstString(...values: unknown[]): string {
  for (const value of values) {
    if (typeof value === 'string' && value.trim()) return value.trim()
  }
  return ''
}

/** 将元数据中的文本或字符串数组压缩为安全的可读文本。 */
function metadataText(value: unknown): string {
  if (typeof value === 'string') return value.trim()
  if (!Array.isArray(value)) return ''
  return value
    .filter((item): item is string => typeof item === 'string')
    .map((item) => item.trim())
    .filter(Boolean)
    .join('、')
}

/** 从元数据数组提取非空文本，供引用和不确定项使用。 */
function metadataTextList(value: unknown): string[] {
  if (Array.isArray(value)) {
    return value
      .filter((item): item is string => typeof item === 'string')
      .map((item) => item.trim())
      .filter(Boolean)
  }
  const text = metadataText(value)
  return text ? [text] : []
}

/** 判断并规范相对路径，绝对路径和越界片段不进入用户界面。 */
function safeRelativePath(value: unknown): string {
  if (typeof value !== 'string') return ''
  const normalized = value.trim().replaceAll('\\', '/')
  if (!normalized || normalized.startsWith('/') || normalized.startsWith('~/') || normalized.startsWith('//') || /^[A-Za-z]:/.test(normalized)) return ''
  if (normalized.split('/').some((segment) => segment === '..' || segment === '.')) return ''
  return normalized
}

/** 从安全相对路径中取得文件名，避免把路径结构作为标题暴露。 */
function safeFilename(value: unknown): string {
  const relative = safeRelativePath(value)
  return relative.split('/').pop() || ''
}

/** 把来源 URL 限制为带主机名的 HTTP(S) 页面，供链接和详情展示。 */
function sourceLink(value: unknown): SourceLink | null {
  const candidate = firstString(value)
  if (!candidate || !/^https?:\/\//i.test(candidate)) return null
  try {
    const parsed = new URL(candidate)
    if (!['http:', 'https:'].includes(parsed.protocol) || !parsed.hostname) return null
    return { url: candidate, label: parsed.hostname.replace(/^www\./i, '') || parsed.hostname }
  } catch {
    return null
  }
}

/** 将文件大小转换为短格式，保留 0 字节文件的有效信息。 */
function formatFileSize(value: unknown): string {
  const size = typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null
  if (size === null) return ''
  if (size < 1024) return `${size} B`
  const units = ['KB', 'MB', 'GB']
  let scaled = size
  let unitIndex = -1
  while (scaled >= 1024 && unitIndex < units.length - 1) {
    scaled /= 1024
    unitIndex += 1
  }
  return `${scaled.toFixed(scaled >= 10 ? 0 : 1)} ${units[unitIndex]}`
}

/** 仅展示 SHA-256 的短摘要，忽略格式不符合指纹约束的值。 */
function shortenSha256(value: unknown): string {
  const sha256 = firstString(value)
  return /^[0-9a-fA-F]{64}$/.test(sha256) ? `${sha256.slice(0, 12)}...` : ''
}

/** 将生产者枚举转成用户可理解的来源名称。 */
function producerLabel(value: unknown): string {
  const producer = firstString(value)
  const labels: Readonly<Record<string, string>> = {
    system: '系统',
    human: '人工',
    research: '外部研究',
    agent: 'Agent 分析',
    visual: '视觉分析',
  }
  return Object.prototype.hasOwnProperty.call(labels, producer) ? labels[producer] : (producer ? '自动分析' : '')
}

/** 隐去错误文本中的 Unix、Windows、UNC 和 file URI 绝对路径。 */
function redactAbsolutePath(value: string): string {
  const pathToken = /(?:^|[\s"'(<:=])(?:file:\/\/)?(?:\/|~\/|[A-Za-z]:[\\/]|\\\\)[^\s"'<>)]*/g
  return value.replace(pathToken, (match) => {
    const prefix = /^[\s"'(<:=]/.test(match) ? match[0] : ''
    return `${prefix}[路径已隐藏]`
  })
}

/** 将错误码或错误对象转换为不暴露内部路径的用户提示。 */
function metadataErrorText(value: unknown): string {
  const raw = isRecord(value) ? firstString(value.error, value.code, value.message) : firstString(value)
  if (!raw) return ''
  return Object.prototype.hasOwnProperty.call(METADATA_ERROR_LABELS, raw) ? METADATA_ERROR_LABELS[raw] : redactAbsolutePath(raw)
}

const metadataPayload = computed<MetadataRecord | null>(() => (isRecord(previewJson.value) ? previewJson.value : null))
const metadataContext = computed<MetadataRecord>(() => (isRecord(metadataPayload.value?.meme_context) ? metadataPayload.value.meme_context : {}))
const metadataProvenance = computed<MetadataRecord>(() => (isRecord(metadataPayload.value?.provenance) ? metadataPayload.value.provenance : {}))
const metadataStatusCode = computed(() => {
  const status = firstString(metadataPayload.value?.context_status, metadataPayload.value?.metadata_status, metadataPayload.value?.status, props.image.metadata?.status)
  return Object.prototype.hasOwnProperty.call(METADATA_STATUS_VIEWS, status) ? status : 'unknown'
})
const metadataStatusView = computed(() => METADATA_STATUS_VIEWS[metadataStatusCode.value] || METADATA_STATUS_VIEWS.unknown)
const summaryFields = computed<SummaryField[]>(() => SUMMARY_FIELD_DEFINITIONS.flatMap(({ key, label }) => {
  const value = metadataText(metadataContext.value[key])
  return value ? [{ key, label, value }] : []
}))
const metadataSourceLinks = computed<SourceLink[]>(() => {
  const values = Array.isArray(metadataContext.value.source_urls) ? metadataContext.value.source_urls : []
  return values.map(sourceLink).filter((item): item is SourceLink => item !== null)
})
const metadataReferences = computed(() => metadataTextList(metadataContext.value.references))
const metadataUncertainties = computed(() => metadataTextList(metadataContext.value.uncertainties))
const metadataUpdatedAt = computed(() => {
  const value = firstString(metadataProvenance.value.updated_at, metadataPayload.value?.updated_at)
  if (!value) return ''
  const formatted = formatTaskTime(value)
  return formatted === '—' ? '' : formatted
})
const metadataProducer = computed(() => producerLabel(metadataProvenance.value.producer))
const metadataErrors = computed(() => {
  const candidates: unknown[] = [previewError.value, metadataPayload.value?.error, metadataPayload.value?.last_error, metadataProvenance.value.last_error]
  if (Array.isArray(metadataPayload.value?.errors)) candidates.push(...metadataPayload.value.errors)
  return [...new Set(candidates.map(metadataErrorText).filter(Boolean))]
})
const metadataFile = computed(() => {
  const identity = isRecord(metadataPayload.value?.image) ? metadataPayload.value.image : {}
  const relativePath = safeRelativePath(identity.relative_path ?? metadataPayload.value?.relative_path)
  const filename = safeFilename(props.image.filename) || safeFilename(relativePath) || '未命名图片'
  const extension = firstString(identity.extension, metadataPayload.value?.extension, props.image.extension).replace(/^\./, '').toUpperCase()
  const size = formatFileSize(identity.size_bytes ?? metadataPayload.value?.size_bytes ?? props.image.size)
  const sha256 = shortenSha256(identity.sha256 ?? metadataPayload.value?.sha256)
  return { filename, relativePath, extension, size, sha256 }
})
const metadataFileDetails = computed<FileDetail[]>(() => {
  const details: FileDetail[] = [
    { label: '文件名', value: metadataFile.value.filename },
    { label: '相对路径', value: metadataFile.value.relativePath },
    { label: '格式', value: metadataFile.value.extension },
    { label: '大小', value: metadataFile.value.size },
  ]
  if (showTaskDiagnostics) details.push({ label: 'SHA-256', value: metadataFile.value.sha256 })
  return details.filter((item) => item.value)
})

useModalDialog({
  dialog,
  initialFocus: closeButton,
  returnFocus: props.returnFocus,
  close: () => emit('close'),
})

/** 读取当前图片的完整数据库元数据，供摘要、详情和原始 JSON 共用。 */
async function loadMetadata(): Promise<void> {
  try {
    previewJson.value = await api.imageMetadata(props.image.meme_id)
  } catch (reason) {
    previewError.value = metadataErrorText(reason) || '图片元数据读取失败'
  } finally {
    loading.value = false
  }
}

/** 将阶段错误压缩为优先级明确且不暴露绝对路径的可读原因。 */
function stageErrorMessage(stage: ImageProcessingStage): string {
  return metadataErrorText(stage.error?.error || stage.error?.message) || '阶段失败'
}

/** 判断当前详情中的阶段恢复按钮是否应可用，沿用图库既有的安全条件。 */
function canRetryStage(stage: ImageProcessingStage): boolean {
  return retryableStages.value.some((candidate) => candidate.stage === stage.stage)
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
          <h2 id="image-dialog-title">{{ metadataFile.filename }}</h2>
        </div>
        <button ref="closeButton" class="quiet" type="button" aria-label="关闭图片预览" @click="emit('close')">
          关闭
        </button>
      </header>
      <div class="image-dialog-content">
        <div class="image-dialog-preview">
          <img :src="image.media_url" :alt="`放大查看 ${metadataFile.filename}`" />
        </div>
        <div class="image-dialog-side">
          <section class="metadata-panel" aria-labelledby="metadata-panel-title">
            <div class="metadata-panel-head">
              <h3 id="metadata-panel-title">图片元数据</h3>
            </div>
            <div class="metadata-panel-body">
              <div class="metadata-file-summary">
                <strong>{{ metadataFile.filename }}</strong>
                <span v-if="metadataFile.extension || metadataFile.size">
                  {{ [metadataFile.extension, metadataFile.size].filter(Boolean).join(' · ') }}
                </span>
              </div>
              <div v-if="!loading || metadataStatusCode !== 'unknown'" class="metadata-context-status" :class="metadataStatusCode" role="status">
                <strong>{{ metadataStatusView.label }}</strong>
                <span>{{ metadataStatusView.description }}</span>
              </div>
              <p v-if="loading" class="metadata-loading" role="status">正在读取元数据...</p>
              <p v-else-if="previewError" class="metadata-error" role="alert">{{ previewError }}</p>
              <template v-else>
                <p v-if="metadataStatusCode === 'pending'" class="metadata-empty-state" role="status">
                  图片语境尚未生成，完成处理后会显示识别结果
                </p>
                <dl v-if="summaryFields.length" class="metadata-summary-list">
                  <div v-for="field in summaryFields" :key="field.key">
                    <dt>{{ field.label }}</dt>
                    <dd>{{ field.value }}</dd>
                  </div>
                </dl>
                <p v-else-if="metadataStatusCode !== 'pending'" class="metadata-summary-empty">暂无可展示的图片语境摘要</p>
              </template>
              <details class="metadata-details">
                <summary>更多信息</summary>
                <div class="metadata-details-body">
                  <section class="metadata-detail-group" aria-labelledby="metadata-file-details-title">
                    <h4 id="metadata-file-details-title">文件信息</h4>
                    <dl class="metadata-detail-list">
                      <div v-for="detail in metadataFileDetails" :key="detail.label">
                        <dt>{{ detail.label }}</dt>
                        <dd>{{ detail.value }}</dd>
                      </div>
                    </dl>
                  </section>
                  <section v-if="metadataProducer || metadataUpdatedAt || metadataSourceLinks.length || metadataReferences.length" class="metadata-detail-group" aria-labelledby="metadata-provenance-title">
                    <h4 id="metadata-provenance-title">来源与更新时间</h4>
                    <dl v-if="metadataProducer || metadataUpdatedAt" class="metadata-detail-list">
                      <div v-if="metadataProducer">
                        <dt>生成来源</dt>
                        <dd>{{ metadataProducer }}</dd>
                      </div>
                      <div v-if="metadataUpdatedAt">
                        <dt>更新时间</dt>
                        <dd>{{ metadataUpdatedAt }}</dd>
                      </div>
                    </dl>
                    <div v-if="metadataSourceLinks.length" class="metadata-source-list">
                      <span class="metadata-detail-label">外部来源</span>
                      <div v-for="source in metadataSourceLinks" :key="source.url" class="metadata-source-item">
                        <a :href="source.url" :title="source.url" target="_blank" rel="noopener noreferrer">{{ source.label }}</a>
                        <code class="metadata-source-url">{{ source.url }}</code>
                      </div>
                    </div>
                    <div v-if="metadataReferences.length" class="metadata-text-group">
                      <span class="metadata-detail-label">文本引用</span>
                      <ul class="metadata-text-list">
                        <li v-for="reference in metadataReferences" :key="reference">{{ reference }}</li>
                      </ul>
                    </div>
                  </section>
                  <section v-if="metadataUncertainties.length" class="metadata-detail-group" aria-labelledby="metadata-uncertainties-title">
                    <h4 id="metadata-uncertainties-title">不确定项</h4>
                    <ul class="metadata-text-list">
                      <li v-for="uncertainty in metadataUncertainties" :key="uncertainty">{{ uncertainty }}</li>
                    </ul>
                  </section>
                  <section v-if="metadataErrors.length" class="metadata-detail-group metadata-error-group" aria-labelledby="metadata-errors-title">
                    <h4 id="metadata-errors-title">错误</h4>
                    <ul class="metadata-text-list">
                      <li v-for="error in metadataErrors" :key="error">{{ error }}</li>
                    </ul>
                  </section>
                </div>
              </details>
              <details v-if="showTaskDiagnostics && !loading && previewJsonText" class="metadata-details">
                <summary>原始 JSON</summary>
                <pre class="metadata-json" tabindex="0" aria-label="原始元数据 JSON">{{ previewJsonText }}</pre>
              </details>
            </div>
          </section>
          <section class="image-processing-details" aria-labelledby="image-processing-details-title">
            <div class="image-processing-details-head">
              <div>
                <h3 id="image-processing-details-title">处理阶段</h3>
                <p v-if="showTaskDiagnostics && image.processing_job_id">完整处理 Job：{{ image.processing_job_id }}</p>
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
