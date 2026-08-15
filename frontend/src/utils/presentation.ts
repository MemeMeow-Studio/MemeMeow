/**
 * 工作台的纯展示转换函数，供多个组件共享且不引入 Vue 响应式状态。
 */

import { formatAgentActivity } from '../agentActivity'
import type { AgentActivityView, MemeImage, TaskItem } from '../types'

/** 读取未知异常的可展示消息，调用失败分支时用于统一降级。 */
export function errorMessage(reason: unknown, fallback = '请求失败'): string {
  return reason instanceof Error && reason.message ? reason.message : fallback
}

/** 生成图片的稳定业务键，列表选择和 Vue key 都以 meme_id 为准。 */
export function imageKey(item: Pick<MemeImage, 'meme_id'>): string {
  return item.meme_id || ''
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
  return { queued: '排队中', running: '处理中', succeeded: '已完成', failed: '失败' }[status || ''] || '未知状态'
}

/** 将后端任务类型转换为可扫描的中文名称。 */
export function taskTypeLabel(type?: string): string {
  return {
    meme_context_generation: '语境生成',
    cache_generation: '检索缓存',
    metadata_repair: '元数据修复',
    visual_embedding_generation: '图片向量生成',
  }[type || ''] || type || '未知任务'
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
    item.image?.filename || '无关联图片',
    taskActivity(item)?.ariaLabel,
  ].filter(Boolean).join('，')
}
