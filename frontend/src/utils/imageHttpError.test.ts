/** 验证真实 Response 的公开图片错误信息可以进入用户提示。 */
import { describe, expect, it } from 'vitest'
import { imageHttpError } from './imageHttpError'

describe('图片 HTTP 错误', () => {
  it('保留公共端点错误码、原因和 HTTP 状态', async () => {
    const response = new Response(JSON.stringify({ error: 'image_export_temp_limit_exceeded', message: '图片解码所需内存超过处理上限' }), {
      status: 409, headers: { 'Content-Type': 'application/json' },
    })
    expect((await imageHttpError(response)).message).toBe('image_export_temp_limit_exceeded: 图片解码所需内存超过处理上限 (HTTP 409)')
  })

  it('非 JSON 响应保留 HTTP 状态', async () => {
    expect((await imageHttpError(new Response('Bad Gateway', { status: 502 }))).message).toContain('HTTP 502')
  })
})
