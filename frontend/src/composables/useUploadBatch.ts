/**
 * 大批量图片上传调度器，位于上传工作区与 API 请求封装之间。
 *
 * 它只保存当前页面的本地文件和逐项状态，不创建服务端批次事实；页面刷新后
 * 未发送文件自然需要重新选择。调度状态以浅层引用替换，避免进度事件深度代理
 * 上千个 File 对象。
 */
import { computed, getCurrentInstance, onUnmounted, shallowRef } from 'vue'
import { api } from '../api'
import type { ImageProcessingOptions, ServiceConfig, UploadResult } from '../types'

export type UploadItemStatus = 'pending' | 'uploading' | 'succeeded' | 'failed' | 'cancelled'

export interface UploadBatchItem {
  id: string
  file: File
  status: UploadItemStatus
  result?: UploadResult
  error?: string
  retryable: boolean
  attempts: number
}

export interface UploadBatchSummary {
  total: number
  succeeded: number
  failed: number
  cancelled: number
  pending: number
  uploading: number
}

export interface UploadBatchRunResult {
  transportError?: unknown
}

interface UploadChunk {
  items: UploadBatchItem[]
  retryCount: number
}

interface UploadRun {
  generation: number
  options: ImageProcessingOptions
  queue: UploadChunk[]
  controllers: Set<AbortController>
  maxConcurrent: number
  retryAt: number | null
  retryTimer: ReturnType<typeof setTimeout> | null
  cancelled: boolean
  transportError?: unknown
  resolve: (result: UploadBatchRunResult) => void
}

const MAX_FILES_PER_REQUEST = 20
// 这是客户端调度器的公开默认提示；服务端不会把它当作 scope admission semaphore。
const MAX_CONCURRENT_REQUESTS = 2
const MAX_CHUNK_RETRIES = 3
const PERMANENT_ERRORS = new Set([
  'unsupported_format',
  'invalid_image',
  'invalid_filename',
  'file_too_large',
  'file_exists',
  'upload_reconciliation_required',
  'too_many_files',
  'request_too_large',
])

/** 按服务端文件数和可选总字节预算切分文件，保证每次循环都消费一个文件。 */
export function splitUploadFiles(
  files: File[],
  limits: Pick<ServiceConfig, 'max_files_per_request' | 'max_request_bytes'> = {},
): File[][] {
  const maxFiles = Math.max(1, Math.min(MAX_FILES_PER_REQUEST, Number(limits.max_files_per_request) || MAX_FILES_PER_REQUEST))
  const rawBytes = Number(limits.max_request_bytes)
  const maxBytes = Number.isFinite(rawBytes) && rawBytes > 0 ? rawBytes : null
  const chunks: File[][] = []
  let current: File[] = []
  let currentBytes = 0

  for (const file of files) {
    const fileBytes = Math.max(0, Number(file.size) || 0)
    const exceedsBytes = maxBytes !== null && current.length > 0 && currentBytes + fileBytes > maxBytes
    if (current.length >= maxFiles || exceedsBytes) {
      chunks.push(current)
      current = []
      currentBytes = 0
    }
    // 单个文件超过预算时必须单独成片，不能等待一个永远放不下的空片。
    if (maxBytes !== null && fileBytes > maxBytes) {
      chunks.push([file])
      continue
    }
    current.push(file)
    currentBytes += fileBytes
  }
  if (current.length) chunks.push(current)
  return chunks
}

/** 将浏览器或 API 错误归一为可展示的有限错误码。 */
function errorCode(reason: any): string {
  return typeof reason?.code === 'string' && reason.code ? reason.code : 'request_failed'
}

/** 判断逐项错误是否可以由客户端再次提交。 */
function isRetryableError(reason: any): boolean {
  return !PERMANENT_ERRORS.has(errorCode(reason))
}

/** 读取 Retry-After 秒数；服务端无头或代理值非法时使用短退避。 */
function retryDelaySeconds(reason: any): number {
  const value = Number(reason?.retryAfter)
  if (Number.isFinite(value) && value >= 0) return Math.min(value, 3600)
  return reason?.status === 429 ? 1 : 0
}

export function useUploadBatch() {
  const items = shallowRef<UploadBatchItem[]>([])
  const busy = shallowRef(false)
  const paused = shallowRef(false)
  const retryAt = shallowRef<number | null>(null)
  const activeRequests = shallowRef(0)
  let nextItemSequence = 0
  let nextGeneration = 0
  let activeRun: UploadRun | null = null

  /** 为当前页面批次生成单调递增标识，避免删除后新增项复用渲染 key。 */
  function nextItemId(): string {
    nextItemSequence += 1
    return `upload-item-${nextItemSequence}`
  }

  /** 创建等待上传的批次项，保留每一次传入 File 的独立身份。 */
  function createPendingItem(file: File): UploadBatchItem {
    return { id: nextItemId(), file, status: 'pending', retryable: true, attempts: 0 }
  }

  /** 判断批次项是否仍属于下一次确认可以提交的集合。 */
  function isSubmittableItem(item: UploadBatchItem): boolean {
    return item.status === 'pending' || (item.status === 'failed' && item.retryable)
  }

  const submittableItems = computed(() => items.value.filter(isSubmittableItem))
  const submittableFiles = computed(() => submittableItems.value.map((item) => item.file))

  const summary = computed<UploadBatchSummary>(() => {
    const counts = { total: items.value.length, succeeded: 0, failed: 0, cancelled: 0, pending: 0, uploading: 0 }
    for (const item of items.value) counts[item.status] += 1
    return counts
  })

  function replaceItems(next: UploadBatchItem[]): void {
    items.value = next
  }

  function updateItems(mutator: (next: UploadBatchItem[]) => void): void {
    const next = items.value.slice()
    mutator(next)
    replaceItems(next)
  }

  /** 保存一次新的本地文件选择并清除上一次批次状态。 */
  function setFiles(files: File[]): void {
    if (busy.value) return
    replaceItems(files.map(createPendingItem))
    retryAt.value = null
  }

  /** 追加一组尚未上传的本地文件，保留当前批次的逐项结果。 */
  function appendFiles(files: File[]): void {
    if (busy.value || !files.length) return
    const additions = files.map(createPendingItem)
    replaceItems([...items.value, ...additions])
    retryAt.value = null
  }

  /** 仅在批次空闲时按项目标识移除尚未发送的 pending 项。 */
  function removePending(id: string): boolean {
    if (busy.value) return false
    const index = items.value.findIndex((item) => item.id === id && item.status === 'pending')
    if (index < 0) return false
    const next = items.value.slice()
    next.splice(index, 1)
    replaceItems(next)
    return true
  }

  function complete(run: UploadRun): void {
    if (activeRun !== run) return
    if (run.retryTimer) clearTimeout(run.retryTimer)
    run.retryTimer = null
    retryAt.value = null
    busy.value = false
    activeRequests.value = 0
    activeRun = null
    run.resolve({ transportError: run.transportError })
  }

  function schedule(run: UploadRun, delayMs: number): void {
    if (activeRun !== run || run.cancelled) return
    const nextRetryAt = Date.now() + Math.max(0, delayMs)
    // 多个并发分片可能同时收到 429；只能延后统一派发点，不能用较短的
    // Retry-After 覆盖仍在等待的较长期限，否则会提前重试对应分片。
    if (run.retryAt !== null && run.retryAt >= nextRetryAt && run.retryTimer) return
    if (run.retryTimer) clearTimeout(run.retryTimer)
    run.retryAt = nextRetryAt
    retryAt.value = run.retryAt
    run.retryTimer = setTimeout(() => {
      run.retryTimer = null
      run.retryAt = null
      retryAt.value = null
      pump(run)
    }, Math.max(0, delayMs))
  }

  function markChunkFailed(chunk: UploadChunk, reason: any): void {
    const code = errorCode(reason)
    updateItems((next) => {
      for (const item of chunk.items) {
        const current = next.find((candidate) => candidate.id === item.id)
        if (!current) continue
        current.status = 'failed'
        current.error = code
        current.retryable = isRetryableError(reason)
      }
    })
  }

  async function sendChunk(run: UploadRun, chunk: UploadChunk): Promise<void> {
    const controller = new AbortController()
    run.controllers.add(controller)
    activeRequests.value += 1
    updateItems((next) => {
      for (const item of chunk.items) {
        const current = next.find((candidate) => candidate.id === item.id)
        if (current) {
          current.status = 'uploading'
          current.attempts += 1
          current.error = undefined
        }
      }
    })
    try {
      const response = await api.upload(chunk.items.map((item) => item.file), run.options, { signal: controller.signal })
      const results = Array.isArray(response?.results) ? response.results : []
      updateItems((next) => {
        for (let index = 0; index < chunk.items.length; index += 1) {
          const item = chunk.items[index]
          const current = next.find((candidate) => candidate.id === item.id)
          if (!current) continue
          const result = results[index] as UploadResult | undefined
          current.result = result
          current.status = result?.ok === true ? 'succeeded' : 'failed'
          current.error = result?.ok === true ? undefined : (result?.error || 'request_failed')
          current.retryable = result?.ok === true ? false : !PERMANENT_ERRORS.has(current.error || '')
        }
      })
    } catch (reason: any) {
      if (run.cancelled || reason?.name === 'AbortError') {
        updateItems((next) => {
          for (const item of chunk.items) {
            const current = next.find((candidate) => candidate.id === item.id)
            if (current && current.status === 'uploading') {
              current.status = 'cancelled'
              current.error = 'upload_cancelled'
              current.retryable = false
            }
          }
        })
        return
      }
      if (reason?.status === 429 && chunk.retryCount < MAX_CHUNK_RETRIES) {
        chunk.retryCount += 1
        updateItems((next) => {
          for (const item of chunk.items) {
            const current = next.find((candidate) => candidate.id === item.id)
            if (current) {
              current.status = 'pending'
              current.error = 'rate_limited'
              current.retryable = true
            }
          }
        })
        run.queue.unshift(chunk)
        const delay = retryDelaySeconds(reason)
        if (delay > 0) schedule(run, delay * 1000)
        return
      }
      markChunkFailed(chunk, reason)
      run.transportError = reason
    } finally {
      run.controllers.delete(controller)
      activeRequests.value = Math.max(0, activeRequests.value - 1)
    }
  }

  function pump(run: UploadRun): void {
    if (activeRun !== run || run.cancelled || paused.value) return
    if (run.retryAt !== null && run.retryAt > Date.now()) return
    while (activeRequests.value < run.maxConcurrent && run.queue.length && !paused.value && !run.cancelled) {
      const chunk = run.queue.shift()
      if (!chunk) break
      void sendChunk(run, chunk).finally(() => {
        if (activeRun !== run) return
        if (run.cancelled && activeRequests.value === 0) complete(run)
        else pump(run)
      })
    }
    if (!run.queue.length && activeRequests.value === 0 && run.retryAt === null) complete(run)
  }

  /** 启动选中文件的逻辑批次；文件项只在该页面内复用，服务端不建立批次实体。 */
  function start(files: File[], options: ImageProcessingOptions, config: ServiceConfig | null): Promise<UploadBatchRunResult> {
    if (busy.value || !files.length) return Promise.resolve({})
    // 只从待提交项匹配 File，避免重复引用重试时重新打开已成功项。
    const availableItems = items.value.filter(isSubmittableItem)
    const selected = files.map((file) => {
      const existingIndex = availableItems.findIndex((item) => item.file === file)
      if (existingIndex >= 0) return availableItems.splice(existingIndex, 1)[0]
      return createPendingItem(file)
    })
    updateItems((next) => {
      for (const item of selected) {
        const current = next.find((candidate) => candidate.id === item.id)
        if (current) {
          current.status = 'pending'
          current.error = undefined
          current.result = undefined
          current.retryable = true
        } else next.push(item)
      }
    })
    const chunks = splitUploadFiles(files, config || {})
    // 同一 File 引用可能由调用方重复传入；按引用建立先进先出项队列，避免漏传或重复更新首项。
    const itemsByFile = new Map<File, UploadBatchItem[]>()
    for (const item of selected) itemsByFile.set(item.file, [...(itemsByFile.get(item.file) || []), item])
    const queue: UploadChunk[] = chunks.map((chunk) => ({
      items: chunk.map((file) => itemsByFile.get(file)?.shift() as UploadBatchItem),
      retryCount: 0,
    }))
    const maxConcurrent = Math.max(1, Math.min(MAX_CONCURRENT_REQUESTS, Number(config?.max_concurrent_upload_requests) || MAX_CONCURRENT_REQUESTS))
    const generation = ++nextGeneration
    busy.value = true
    paused.value = false
    const runPromise = new Promise<UploadBatchRunResult>((resolve) => {
      activeRun = { generation, options, queue, controllers: new Set(), maxConcurrent, retryAt: null, retryTimer: null, cancelled: false, resolve }
    })
    const run = activeRun
    if (run) pump(run)
    return runPromise
  }

  /** 暂停新分片派发；当前请求继续执行，继续操作会从队列头恢复。 */
  function pause(): void {
    if (busy.value) paused.value = true
  }

  /** 继续派发尚未开始的分片。 */
  function resume(): void {
    if (!busy.value) return
    paused.value = false
    if (activeRun) pump(activeRun)
  }

  /** 中止当前请求并取消尚未发送的本地文件，不触碰已 durable 成功的服务端事实。 */
  function cancel(): void {
    const run = activeRun
    if (!run) return
    run.cancelled = true
    if (run.retryTimer) clearTimeout(run.retryTimer)
    run.retryTimer = null
    run.retryAt = null
    retryAt.value = null
    updateItems((next) => {
      for (const current of next) {
        if (current.status === 'pending') {
          current.status = 'cancelled'
          current.error = 'upload_cancelled'
          current.retryable = false
        }
      }
    })
    for (const controller of run.controllers) controller.abort()
    if (activeRequests.value === 0) complete(run)
  }

  if (getCurrentInstance()) onUnmounted(cancel)

  return {
    items,
    busy,
    paused,
    retryAt,
    activeRequests,
    summary,
    submittableItems,
    submittableFiles,
    setFiles,
    appendFiles,
    removePending,
    start,
    pause,
    resume,
    cancel,
  }
}
