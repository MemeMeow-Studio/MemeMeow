/** 前端请求封装和稳定图片身份契约测试。 */
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { api, pollTask, request } from './api'

describe('请求封装', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ ok: true }),
    }))
  })

  it('将 FastAPI detail 错误解包为稳定错误码', async () => {
    fetch.mockResolvedValue({
      ok: false,
      status: 400,
      json: async () => ({ detail: { error: 'invalid_request', message: '请求参数校验失败' } }),
    })

    await expect(request('/images')).rejects.toMatchObject({
      code: 'invalid_request',
      status: 400,
      message: '请求参数校验失败',
    })
  })

  it('图片元数据查询只使用稳定 meme_id', async () => {
    await api.imageMetadata('meme-1')

    expect(fetch).toHaveBeenCalledWith('/api/images/metadata?meme_id=meme-1', {
      cache: 'no-store',
      headers: { 'Content-Type': 'application/json' },
    })
  })

  it('合集成员请求编码稳定 ID 并发送 JSON', async () => {
    await api.addCollectionItems('collection/1', ['meme/1'])

    expect(fetch).toHaveBeenCalledWith('/api/collections/collection%2F1/items', {
      method: 'POST',
      body: JSON.stringify({ meme_ids: ['meme/1'] }),
      headers: { 'Content-Type': 'application/json' },
    })
  })

  it('合集列表支持分页参数', async () => {
    await api.collections({ page: 2, page_size: 10 })

    expect(fetch).toHaveBeenCalledWith('/api/collections?page=2&page_size=10', {
      headers: { 'Content-Type': 'application/json' },
    })
  })

  it('独立图片阶段请求只发送业务目标和阶段策略', async () => {
    await api.submitImageStage({ meme_id: 'meme-1', stage: 'agent', reverse_image_policy: 'forbid' })

    expect(fetch).toHaveBeenCalledWith('/api/images/stages', {
      method: 'POST',
      body: JSON.stringify({ meme_id: 'meme-1', stage: 'agent', reverse_image_policy: 'forbid' }),
      headers: { 'Content-Type': 'application/json' },
    })
  })

  it('上传和 scope 级完整重试发送冻结的处理选项', async () => {
    const file = new File(['image'], 'sample.png', { type: 'image/png' })
    await api.upload([file], { reverse_image_policy: 'auto', auto_name: true })
    const uploadRequest = fetch.mock.calls[0]
    expect(uploadRequest[0]).toBe('/api/images/upload')
    expect(uploadRequest[1].body.get('reverse_image_policy')).toBe('auto')
    expect(uploadRequest[1].body.get('auto_name')).toBe('true')

    await api.unreadyProcessing({ reverse_image_policy: 'forbid', auto_name: false })
    expect(fetch).toHaveBeenLastCalledWith('/api/images/processing/unready', {
      method: 'POST',
      body: JSON.stringify({ reverse_image_policy: 'forbid', auto_name: false }),
      headers: { 'Content-Type': 'application/json' },
    })
  })

  it('处理选项请求不会把非法值原样发送给服务端', async () => {
    const file = new File(['image'], 'sample.png', { type: 'image/png' })
    await api.upload([file], { reverse_image_policy: 'unexpected', auto_name: 'true' })
    const uploadRequest = fetch.mock.calls[0]
    expect(uploadRequest[1].body.get('reverse_image_policy')).toBe('forbid')
    expect(uploadRequest[1].body.get('auto_name')).toBe('false')

    await api.unreadyProcessing({ reverse_image_policy: 'unexpected', auto_name: 1 })
    expect(fetch).toHaveBeenLastCalledWith('/api/images/processing/unready', {
      method: 'POST',
      body: JSON.stringify({ reverse_image_policy: 'forbid', auto_name: false }),
      headers: { 'Content-Type': 'application/json' },
    })
  })

  it('任务轮询在 blocked 和 unknown_execution 终态停止', async () => {
    fetch.mockResolvedValueOnce({ ok: true, json: async () => ({ status: 'blocked' }) })
    const onUpdate = vi.fn()
    await expect(pollTask('task-1', onUpdate, 0)).resolves.toMatchObject({ status: 'blocked' })
    expect(onUpdate).toHaveBeenCalledTimes(1)
  })
})
