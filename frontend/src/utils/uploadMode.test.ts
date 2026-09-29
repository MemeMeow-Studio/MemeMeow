/** 上传默认方式的边界验证。 */
import { describe, expect, it } from 'vitest'
import { defaultUploadMode } from './uploadMode'

describe('上传默认方式', () => {
  it('10 张及以下使用完整分析', () => {
    expect(defaultUploadMode(1)).toBe('analyze')
    expect(defaultUploadMode(10)).toBe('analyze')
  })
  it('超过 10 张使用直接上传', () => {
    expect(defaultUploadMode(11)).toBe('direct')
    expect(defaultUploadMode(500)).toBe('direct')
  })
})
