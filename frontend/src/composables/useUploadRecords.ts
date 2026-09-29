/** 本次上传的浏览器恢复、查询和操作，保存范围限于当前账户与标签页。 */
import { computed, onUnmounted, shallowRef, watch } from 'vue'
import { api } from '../api'
import type { UploadBatchItem, UploadItemStatus } from './useUploadBatch'
import { uploadErrorMessage } from '../utils/presentation'

interface UploadRecord {
  requestId: string
  filename: string
  state: UploadItemStatus
  attempted: boolean
  error?: string
}

interface UploadStatus {
  processing?: { upload_mode?: 'direct' | 'analyze'; text_input_status?: string; task_ids?: string[] }
  task_id: string
  upload: { receipt_id: string; batch_id: string; filename: string }
  status: string
  error?: { error?: string; message?: string }
  retryable: boolean
  retry_reason?: string
}

/** 从实时文件项恢复传输状态，并按服务器记录确认图片保存结果。 */
export function useUploadRecords(items: () => UploadBatchItem[], storageScope: string) {
  const storageKey = `mememeow:upload:${storageScope}`
  let serialized = sessionStorage.getItem(storageKey)
  const records = shallowRef<UploadRecord[]>(serialized ? JSON.parse(serialized) : [])
  const statuses = shallowRef(new Map<string, UploadStatus>())
  const error = shallowRef('')
  const loading = shallowRef(false)
  const acting = shallowRef(new Set<string>())
  let timer: ReturnType<typeof setTimeout> | undefined
  let disposed = false
  let generation = 0
  let refreshAgain = false

  /** 登出清除记录后，迟到的请求和传输回调不得重新保存该账户的数据。 */
  function isCurrent(): boolean {
    return !disposed && sessionStorage.getItem(storageKey) === serialized
  }

  /** 保存最小恢复信息，文件内容仍由浏览器当前页面管理。 */
  function persist(): void {
    serialized = JSON.stringify(records.value)
    sessionStorage.setItem(storageKey, serialized)
  }

  /** 新提交替换当前记录，原请求的传输重试继续保留当前组。 */
  function begin(selected: UploadBatchItem[]): void {
    generation += 1
    if (timer) clearTimeout(timer)
    const previous = new Set(records.value.map((record) => record.requestId))
    if (!selected.every((item) => previous.has(item.requestId))) {
      records.value = selected.map((item) => ({ requestId: item.requestId, filename: item.file.name, state: 'pending', attempted: false }))
      statuses.value = new Map()
    }
    error.value = ''
    persist()
  }

  const rows = computed(() => {
    const local = new Map(items().map((item) => [item.requestId, item]))
    return records.value.map((record) => {
      const item = local.get(record.requestId)
      const task = statuses.value.get(record.requestId.replaceAll('-', ''))
      let label: string
      let detail = ''
      let state = 'active'
      if (task?.status === 'succeeded') {
        label = '上传成功'
        state = 'success'
        if (task.processing?.upload_mode === 'direct') {
          detail = task.processing.text_input_status === 'waiting_for_text'
            ? '图片已保存，文本索引等待有效名称或语境。后台会自动命名。'
            : '图片已保存，检索向量独立处理。后台会自动命名。'
        }
      } else if (task?.status === 'failed') {
        const code = task.error?.error || 'task_failed'
        label = code === 'task_cancelled' ? '已取消' : '上传失败'
        detail = uploadErrorMessage(code)
        if (task.error?.message && task.error.message !== code) detail += `：${task.error.message}`
        if (task.retry_reason === 'upload_input_expired') detail += '；上传输入已过期，请重新选择文件'
        state = code === 'task_cancelled' ? 'cancelled' : 'failed'
      } else if (task) {
        label = task.status === 'queued' ? '等待校验和保存' : '正在校验和保存'
      } else if (item?.status === 'uploading') {
        label = item.progress >= 1 ? '等待接收确认' : `上传中 ${Math.floor(item.progress * 100)}%`
      } else if (record.state === 'failed') {
        label = record.error === 'request_failed' ? '接收状态待确认' : '上传失败'
        detail = uploadErrorMessage(record.error)
        state = 'failed'
      } else if (record.state === 'cancelled') {
        label = '已取消'
        state = 'cancelled'
      } else if (record.state === 'pending') {
        label = item ? '等待上传' : '尚未发送'
        if (!item) detail = '请重新选择文件'
      } else {
        label = '接收状态待确认'
        if (!item) detail = '可以刷新结果查询服务器接收情况'
      }
      return { ...record, task, label, detail, state, progress: item?.status === 'uploading' ? item.progress : null,
        errorCode: task ? task.error?.error : record.error,
        canRetryTransfer: !task && item?.status === 'failed' && item.retryable }
    })
  })
  const summary = computed(() => ({
    total: rows.value.length,
    success: rows.value.filter((row) => row.state === 'success').length,
    failed: rows.value.filter((row) => row.state === 'failed').length,
    cancelled: rows.value.filter((row) => row.state === 'cancelled').length,
  }))

  /** 合并刷新请求，查询中的结果只写入发起查询时的当前组。 */
  async function refresh(all = true): Promise<void> {
    if (!isCurrent()) return
    if (loading.value) { refreshAgain = true; return }
    if (timer) clearTimeout(timer)
    loading.value = true
    const currentGeneration = generation
    try {
      const selected = records.value.filter((record) => {
        const task = statuses.value.get(record.requestId.replaceAll('-', ''))
        return all || (record.attempted && (!task || ['queued', 'running'].includes(task.status)))
      })
      const next = new Map(statuses.value)
      for (let index = 0; index < selected.length; index += 100) {
        const response = await api.uploadStatus(selected.slice(index, index + 100).map((record) => record.requestId))
        if (!isCurrent() || currentGeneration !== generation) return
        for (const task of response.items as UploadStatus[]) next.set(task.upload.batch_id, task)
      }
      statuses.value = next
      error.value = ''
      if ([...next.values()].some((task) => ['queued', 'running'].includes(task.status))) {
        timer = setTimeout(() => { void refresh(false) }, 5000)
      }
    } catch (reason) {
      if (isCurrent() && currentGeneration === generation) {
        error.value = reason instanceof Error ? reason.message : String(reason)
        timer = setTimeout(() => { void refresh(false) }, 10000)
      }
    } finally {
      loading.value = false
      if (refreshAgain && isCurrent()) { refreshAgain = false; void refresh() }
    }
  }

  /** 对单条上传任务执行操作；服务端再次检查其重试或取消资格。 */
  async function act(task: UploadStatus, operation: 'retry' | 'cancel'): Promise<void> {
    if (acting.value.has(task.task_id)) return
    const currentGeneration = generation
    acting.value = new Set([...acting.value, task.task_id])
    try {
      if (operation === 'retry') await api.retryTask(task.task_id)
      else await api.cancelTask(task.task_id)
      if (currentGeneration === generation) await refresh()
    } catch (reason) {
      if (isCurrent() && currentGeneration === generation) error.value = reason instanceof Error ? reason.message : String(reason)
    } finally {
      acting.value = new Set([...acting.value].filter((id) => id !== task.task_id))
    }
  }

  watch(() => items().map((item) => `${item.requestId}:${item.status}:${item.attempts}:${item.error || ''}`).join('|'), () => {
    if (!isCurrent()) return
    const local = new Map(items().map((item) => [item.requestId, item]))
    records.value = records.value.map((record) => {
      const item = local.get(record.requestId)
      return item ? { ...record, state: item.status, attempted: item.attempts > 0, error: item.error } : record
    })
    persist()
    if (timer) clearTimeout(timer)
    timer = setTimeout(() => { void refresh(false) }, 300)
  }, { flush: 'sync' })

  void refresh()
  onUnmounted(() => { disposed = true; if (timer) clearTimeout(timer) })
  return { rows, summary, error, loading, acting, begin, refresh, act }
}
