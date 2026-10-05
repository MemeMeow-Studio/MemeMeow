/** 使用真实 localStorage 验证检索输出偏好和同源媒体 URL。 */
import { afterEach, describe, expect, it } from 'vitest'
import { imageExportSizes, imageExportStorageKey, imageExportUrl, loadImageExportSize, saveImageExportSize } from './imageExportPreferences'

afterEach(() => localStorage.removeItem(imageExportStorageKey))

describe('检索图片尺寸偏好', () => {
  it('首次使用选择原图，保存后重新读取保留档位', () => {
    expect(loadImageExportSize()).toBe('original')
    for (const size of imageExportSizes) {
      saveImageExportSize(size)
      expect(loadImageExportSize()).toBe(size)
      expect(localStorage.getItem(imageExportStorageKey)).toBe(String(size))
    }
    saveImageExportSize('original')
    expect(loadImageExportSize()).toBe('original')
  })

  it('原图保留 URL，固定尺寸保存其他参数', () => {
    expect(imageExportUrl('/media/example', 'original')).toBe('/media/example')
    for (const size of imageExportSizes) {
      const url = new URL(imageExportUrl('/media/example?version=2&max_edge=240', size))
      expect(url.origin).toBe(window.location.origin)
      expect(url.searchParams.get('max_edge')).toBe(String(size))
      expect(url.searchParams.get('version')).toBe('2')
    }
  })

  it('损坏设置和外部媒体地址提供明确错误', () => {
    localStorage.setItem(imageExportStorageKey, '1024')
    expect(loadImageExportSize).toThrow('image_export_size_invalid')
    expect(() => imageExportUrl('https://example.invalid/media/image', 480)).toThrow('image_export_origin_invalid')
    expect(() => imageExportUrl('https://example.invalid/media/image', 'original')).toThrow('image_export_origin_invalid')
  })

  it('重新选择档位可以修复无效设置', () => {
    localStorage.setItem(imageExportStorageKey, '1024')
    saveImageExportSize(360)
    expect(loadImageExportSize()).toBe(360)
  })
})
