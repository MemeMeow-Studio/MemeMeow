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

  it('JSON 解析错误保留明确类别、HTTP 状态和原始异常', async () => {
    const response = new Response('{invalid', { status: 503, headers: { 'Content-Type': 'application/json' } })
    const error = await imageHttpError(response)
    expect(error.message).toBe('image_http_error_invalid_json: 图片错误响应的 JSON 无法解析 (HTTP 503)')
    expect(error.cause).toBeInstanceOf(SyntaxError)
  })

  it.each(['null', '[]', '"error"'])('JSON 结构错误 %s 保留原因和 HTTP 状态', async content => {
    const response = new Response(content, { status: 502, headers: { 'Content-Type': 'application/json' } })
    expect((await imageHttpError(response)).message).toBe('image_http_error_invalid_payload: 图片错误响应缺少错误信息对象 (HTTP 502)')
  })
})
