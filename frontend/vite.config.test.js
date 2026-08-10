// @vitest-environment node
/** Vite 开发代理配置测试，确保受控媒体 URL 能从前端端口正常访问。 */
import { describe, expect, it } from 'vitest'

import config from './vite.config'

describe('Vite 开发代理', () => {
  it('将媒体请求转发到 FastAPI', () => {
    expect(config.server.proxy['/media']).toMatchObject({
      target: 'http://localhost:8275',
      changeOrigin: true,
    })
  })
})
