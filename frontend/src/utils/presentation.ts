/**
 * 工作台的纯展示转换函数，供多个组件共享且不引入 Vue 响应式状态。
 */

import { formatAgentActivity } from '../agentActivity'
import type { AgentActivityView, MemeImage, TaskItem, ThumbnailInfo } from '../types'

/** 读取未知异常的可展示消息，调用失败分支时用于统一降级。 */
export function errorMessage(reason: unknown, fallback = '请求失败'): string {
  return reason instanceof Error && reason.message ? reason.message : fallback
}

/**
 * 将上传接口的稳定错误码映射为固定的用户提示。
 *
 * 映射只依赖服务端公开的错误码，不渲染 Pillow 异常、文件路径或其它内部诊断；
 * 未知错误统一使用通用提示，避免新错误码意外暴露敏感信息。
 */
const UPLOAD_ERROR_MESSAGES: Readonly<Record<string, string>> = Object.freeze({
  unsupported_format: '文件格式不受支持，请选择 PNG、JPG、JPEG 或 GIF 图片',
  invalid_image: '图片内容无法解码，可能已损坏或扩展名与实际格式不一致',
  image_frame_count_exceeded: '图片动画帧数超过服务端限制',
  image_frame_pixels_exceeded: '图片单帧像素超过服务端限制',
  image_total_pixels_exceeded: '图片累计帧像素超过服务端限制',
  image_preflight_timeout: '图片校验超时，请稍后重试',
  image_preflight_failed: '图片校验失败，请确认文件有效后重试',
  invalid_filename: '文件名不符合要求',
  file_too_large: '文件超过大小限制',
  file_exists: '同名图片已存在',
  upload_reconciliation_required: '图片状态需要恢复后才能重试',
  metadata_write_failed: '图片保存失败，请稍后重试',
  operation_forbidden: '当前账户暂不允许上传图片',
  operation_limit_exceeded: '已达到图片上传额度限制',
  operation_policy_unavailable: '上传服务暂不可用，请稍后重试',
  operation_grant_invalid: '上传授权无效，请稍后重试',
  operation_unknown: '上传操作类型无效',
  rate_limited: '上传请求过于频繁，请稍后重试',
  upload_cancelled: '上传已取消',
  request_failed: '上传请求失败，请检查网络后重试',
})
const GENERIC_UPLOAD_ERROR_MESSAGE = '上传失败，请稍后重试'

/** 根据错误码或带稳定 code 的异常返回安全原因，未知码不直接展示原始消息。 */
export function uploadErrorMessage(value: unknown): string {
  const code = typeof value === 'object' && value !== null
    ? (value as { code?: unknown }).code
    : value
  if (typeof code !== 'string' || !Object.prototype.hasOwnProperty.call(UPLOAD_ERROR_MESSAGES, code)) {
    return GENERIC_UPLOAD_ERROR_MESSAGE
  }
  return UPLOAD_ERROR_MESSAGES[code]
}

/** 生成图片的稳定业务键，列表选择和 Vue key 都以 meme_id 为准。 */
export function imageKey(item: Pick<MemeImage, 'meme_id'>): string {
  return item.meme_id || ''
}

/** 返回缩略图优先的展示地址；pending/failed/stale 统一回退原图。 */
export function thumbnailMediaUrl(originalUrl: string | undefined, thumbnail?: ThumbnailInfo | null): string {
  if (thumbnail?.status === 'available' && thumbnail.media_url) return thumbnail.media_url
  return originalUrl || ''
}

/** 返回图片库或合集成员的展示地址，原图地址始终由调用方单独保留。 */
export function imageDisplayUrl(item: Pick<MemeImage, 'media_url' | 'thumbnail'>): string {
  return thumbnailMediaUrl(item.media_url, item.thumbnail)
}

/** 缩略图加载失败时只切换一次原图，原图失败不会进入循环或隐藏业务条目。 */
export function fallbackImageToOriginal(event: Event, originalUrl: string): void {
  const image = event.currentTarget as HTMLImageElement | null
  if (!image || image.dataset.fallbackApplied === 'true' || !originalUrl) return
  image.dataset.fallbackApplied = 'true'
  image.src = originalUrl
}

/** 判断图片语境是否需要重新处理。 */
export function isRetryable(item: MemeImage): boolean {
  return item.metadata?.status !== 'ready'
}

/** 去掉缓存查询参数和片段，得到检索结果的稳定媒体身份。 */
export function resultIdentity(url: string): string {
  if (typeof url !== 'string' || !url.trim()) return ''
  try {
    const parsed = new URL(url, window.location.origin)
    return `${parsed.origin}${parsed.pathname}`
  } catch {
    return url.split(/[?#]/, 1)[0]
  }
}

/** 将图片语境元数据状态转换为用户可读标签。 */
export function metadataLabel(status?: string): string {
  return { ready: '语境就绪', pending: '待生成', repair_required: '需修复' }[status || ''] || '状态未知'
}

/** 将文本语义索引状态转换为用户可读标签。 */
export function embeddingLabel(status?: string): string {
  return { ready: '文本索引已就绪', pending: '文本索引待生成', blocked: '文本索引需修复' }[status || ''] || '文本索引状态未知'
}

/** 将视觉向量状态转换为用户可读标签。 */
export function visualEmbeddingLabel(status?: string): string {
  return { ready: '图片向量已就绪', pending: '图片向量待生成' }[status || ''] || '图片向量状态未知'
}

/** 将后端任务状态转换为用户可读标签。 */
export function taskStatusLabel(status?: string): string {
  return {
    queued: '排队中',
    running: '处理中',
    succeeded: '已完成',
    failed: '失败',
    blocked: '已阻止',
    unknown_execution: '执行状态未知',
    skipped: '未启用',
    warning: '处理完成，自动重命名未完成',
  }[status || ''] || '未知状态'
}

/** 将后端任务类型转换为可扫描的中文名称。 */
export function taskTypeLabel(type?: string): string {
  return {
    meme_context_generation: '语境生成',
    cache_generation: '检索缓存',
    metadata_repair: '元数据修复',
    derived_thumbnail_generation: '缩略图生成',
    visual_embedding_generation: '图片向量生成',
    image_auto_rename: '自动重命名',
    text_embedding_generation: '文本语义检索',
    image_processing: '完整图片处理',
  }[type || ''] || type || '未知任务'
}

/** 将持久化图片来源转换为稳定展示标签。 */
export function submissionModeLabel(mode?: string | null): string {
  return { pipeline: '完整 Job', standalone: '独立阶段', unclassified: '未归类历史' }[mode || 'unclassified'] || '未归类历史'
}

/** 将图片阶段转换为工作台标签。 */
export function imageStageLabel(stage?: string | null): string {
  return { visual: '视觉向量生成', agent: '图片语境分析', auto_rename: '自动重命名', text_embedding: '文本语义检索' }[stage || ''] || '图片阶段'
}

/** 将图片处理 Job 阶段状态转换为短标签。 */
export function imageStageStatusLabel(status?: string): string {
  return {
    skipped: '未启用',
    warning: '处理完成，自动重命名未完成',
    failed: '失败，处理已停止',
    blocked: '已阻止，等待恢复',
    unknown_execution: '执行状态未知，需人工确认',
  }[status || ''] || taskStatusLabel(status)
}

/** 将任务时间压缩为桌面和移动端都能容纳的本地格式。 */
export function formatTaskTime(value?: string | number | Date): string {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '—'
  return date.toLocaleString('zh-CN', {
    year: 'numeric', month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit',
  })
}

/** 将任务 API 的可选 Agent 活跃度转换为展示视图模型。 */
export function taskActivity(item?: TaskItem | null): AgentActivityView | null {
  return formatAgentActivity(item) as AgentActivityView | null
}

/** 生成处理任务行的完整无障碍名称。 */
export function taskRowAriaLabel(item: TaskItem): string {
  return [
    taskStatusLabel(item.status),
    taskTypeLabel(item.task_type),
    submissionModeLabel(item.historical_unclassified ? 'unclassified' : item.submission_mode),
    item.image?.filename || '无关联图片',
    taskActivity(item)?.ariaLabel,
  ].filter(Boolean).join('，')
}
