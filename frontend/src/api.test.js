/** 前端请求封装和稳定图片身份契约测试。 */
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { api, request } from './api'

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
})
