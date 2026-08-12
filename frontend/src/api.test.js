/** 前端后端设置请求的凭据传递测试。 */
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { api } from './api'

describe('后端设置 API', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ saved: true }),
    }))
  })

  it('把内存中的管理凭据作为请求头发送', async () => {
    await api.updateBackendSettings({ opencode_concurrency: 2 }, 'admin-secret')

    expect(fetch).toHaveBeenCalledWith('/api/backend/settings', {
      method: 'PATCH',
      body: JSON.stringify({ opencode_concurrency: 2 }),
      headers: {
        'Content-Type': 'application/json',
        'X-Settings-Admin-Token': 'admin-secret',
      },
    })
  })

  it('没有凭据时不伪造授权请求头', async () => {
    await api.updateBackendSettings({ opencode_concurrency: 2 })

    expect(fetch).toHaveBeenCalledWith('/api/backend/settings', {
      method: 'PATCH',
      body: JSON.stringify({ opencode_concurrency: 2 }),
      headers: { 'Content-Type': 'application/json' },
    })
  })
})
