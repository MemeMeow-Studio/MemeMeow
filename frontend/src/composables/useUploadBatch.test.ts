/** 上传分片与调度器契约测试，覆盖大批量、预算、背压和本地取消。 */
import { flushPromises } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'
import { useUploadBatch, splitUploadFiles } from './useUploadBatch'

const { upload } = vi.hoisted(() => ({ upload: vi.fn() }))

vi.mock('../api', () => ({ api: { upload } }))

function makeFiles(count: number, size = 1): File[] {
  return Array.from({ length: count }, (_, index) => new File([new Uint8Array(size)], `image-${index}.png`, { type: 'image/png' }))
}

function setupBatch() {
  return { batch: useUploadBatch() }
}

describe('splitUploadFiles', () => {
  it('按 20 个文件切分上千项且不使用默认总字节预算', () => {
    const chunks = splitUploadFiles(makeFiles(1001))
    expect(chunks).toHaveLength(51)
    expect(chunks.slice(0, -1).every((chunk) => chunk.length === 20)).toBe(true)
    expect(chunks.at(-1)).toHaveLength(1)
    expect(Math.max(...chunks.map((chunk) => chunk.length))).toBe(20)
  })

  it('按可选总字节预算切分，并为超预算单文件保留单独分片', () => {
    const files = makeFiles(3, 6)
    const chunks = splitUploadFiles(files, { max_files_per_request: 20, max_request_bytes: 10 })
    expect(chunks).toEqual([[files[0]], [files[1]], [files[2]]])
    const oversized = new File([new Uint8Array(11)], 'large.png', { type: 'image/png' })
    expect(splitUploadFiles([oversized], { max_request_bytes: 10 })).toEqual([[oversized]])
  })
})

describe('useUploadBatch', () => {
  it('追加文件时保留已有批次项并为新增项建立等待状态', () => {
    const { batch } = setupBatch()
    const first = makeFiles(1)[0]
    const second = makeFiles(1)[0]

    batch.setFiles([first])
    const originalItem = batch.items.value[0]
    batch.appendFiles([second])

    expect(batch.items.value).toHaveLength(2)
    expect(batch.items.value[0]).toBe(originalItem)
    expect(batch.items.value[1]).toMatchObject({ file: second, status: 'pending', retryable: true, attempts: 0 })
  })

  it('重复传入同一 File 引用时仍为每个队列项发送一次', async () => {
    upload.mockReset().mockResolvedValue({ results: [{ ok: true }, { ok: true }] })
    const { batch } = setupBatch()
    const file = makeFiles(1)[0]
    batch.setFiles([file, file])

    await batch.start([file, file], { reverse_image_policy: 'forbid', auto_name: false }, null)

    expect(upload).toHaveBeenCalledTimes(1)
    expect(upload.mock.calls[0][0]).toEqual([file, file])
    expect(batch.summary.value.succeeded).toBe(2)
  })

  it('上千文件保持最多两个活动请求并按分片完成', async () => {
    upload.mockReset()
    const pending: Array<{ files: File[]; resolve: (value: unknown) => void }> = []
    upload.mockImplementation((files: File[]) => new Promise((resolve) => pending.push({ files, resolve })))
    const { batch } = setupBatch()
    const files = makeFiles(41)
    batch.setFiles(files)
    const done = batch.start(files, { reverse_image_policy: 'forbid', auto_name: false }, null)
    await flushPromises()
    expect(upload).toHaveBeenCalledTimes(2)
    expect(Math.max(...upload.mock.calls.map(() => batch.activeRequests.value))).toBeLessThanOrEqual(2)
    pending.shift()?.resolve({ results: Array.from({ length: 20 }, () => ({ ok: true, filename: 'ok.png' })) })
    await flushPromises()
    expect(upload).toHaveBeenCalledTimes(3)
    while (pending.length) {
      pending.shift()?.resolve({ results: Array.from({ length: 20 }, () => ({ ok: true, filename: 'ok.png' })) })
      await flushPromises()
    }
    await done
    expect(batch.summary.value).toMatchObject({ total: 41, succeeded: 41, failed: 0 })
  })

  it('暂停阻止新分片，继续后恢复队列', async () => {
    upload.mockReset()
    const pending: Array<{ resolve: (value: unknown) => void }> = []
    upload.mockImplementation(() => new Promise((resolve) => pending.push({ resolve })))
    const { batch } = setupBatch()
    const files = makeFiles(41)
    batch.setFiles(files)
    const done = batch.start(files, { reverse_image_policy: 'forbid', auto_name: false }, null)
    await flushPromises()
    batch.pause()
    pending.shift()?.resolve({ results: Array.from({ length: 20 }, () => ({ ok: true })) })
    pending.shift()?.resolve({ results: Array.from({ length: 20 }, () => ({ ok: true })) })
    await flushPromises()
    expect(upload).toHaveBeenCalledTimes(2)
    batch.resume()
    await flushPromises()
    expect(upload).toHaveBeenCalledTimes(3)
    pending.shift()?.resolve({ results: [{ ok: true }] })
    await done
  })

  it('429 按 Retry-After 暂停派发并自动重试', async () => {
    upload.mockReset()
    vi.useFakeTimers()
    upload.mockRejectedValueOnce(Object.assign(new Error('busy'), { status: 429, retryAfter: 2 }))
      .mockResolvedValueOnce({ results: [{ ok: true }] })
    const { batch } = setupBatch()
    const files = makeFiles(1)
    batch.setFiles(files)
    const done = batch.start(files, { reverse_image_policy: 'forbid', auto_name: false }, null)
    await flushPromises()
    expect(upload).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(1999)
    expect(upload).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(1)
    await done
    expect(upload).toHaveBeenCalledTimes(2)
    expect(batch.summary.value.succeeded).toBe(1)
    vi.useRealTimers()
  })

  it('取消会中止活动请求并保留已完成项', async () => {
    upload.mockReset()
    let rejectPending: ((reason?: unknown) => void) | undefined
    upload.mockImplementation((_files: File[], _options: unknown, requestOptions?: { signal?: AbortSignal }) => new Promise((_resolve, reject) => {
      rejectPending = reject
      requestOptions?.signal?.addEventListener('abort', () => reject(Object.assign(new Error('aborted'), { name: 'AbortError' })))
    }))
    const { batch } = setupBatch()
    const files = makeFiles(21)
    batch.setFiles(files)
    const done = batch.start(files, { reverse_image_policy: 'forbid', auto_name: false }, null)
    await flushPromises()
    batch.cancel()
    rejectPending?.(Object.assign(new Error('aborted'), { name: 'AbortError' }))
    await done
    expect(batch.summary.value.cancelled).toBe(21)
    expect(batch.busy.value).toBe(false)
  })

  it('部分成功只把失败项标为可重试', async () => {
    upload.mockReset()
    upload.mockResolvedValue({ results: [{ ok: true, filename: 'ok.png' }, { ok: false, error: 'invalid_image', filename: 'bad.png' }] })
    const { batch } = setupBatch()
    const files = makeFiles(2)
    batch.setFiles(files)
    await batch.start(files, { reverse_image_policy: 'forbid', auto_name: false }, null)
    expect(batch.summary.value).toMatchObject({ succeeded: 1, failed: 1 })
    expect(batch.items.value.find((item) => item.file === files[1])?.retryable).toBe(false)
  })
})
