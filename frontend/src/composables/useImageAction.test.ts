/** 使用实际 Vue 作用域、浏览器存储和 toast 验证图片操作设置错误与修复。 */
import { afterEach, describe, expect, it } from 'vitest'
import { effectScope } from 'vue'
import { toast } from 'vue-sonner'
import { useImageAction } from './useImageAction'
import { clearToasts } from './useToast'
import { imageExportStorageKey, loadImageExportSize } from '../utils/imageExportPreferences'

afterEach(() => {
  localStorage.removeItem(imageExportStorageKey)
  clearToasts()
})

describe('检索图片操作设置', () => {
  it('无效设置提供错误，重新选择后保存有效尺寸', async () => {
    localStorage.setItem(imageExportStorageKey, '1024')
    const scope = effectScope()
    try {
      const actions = scope.run(() => useImageAction())!
      expect(actions.imageExportSize.value).toBeNull()
      await actions.performImageAction('/media/example')
      const sizeErrors = () => toast.getToasts().filter(item => 'title' in item && String(item.title).includes('image_export_size_invalid'))
      expect(sizeErrors()).toHaveLength(1)
      actions.imageExportSize.value = 360
      actions.persistImageExportSize()
      expect(loadImageExportSize()).toBe(360)
      expect(sizeErrors()).toHaveLength(0)
    } finally {
      scope.stop()
    }
  })

  it('每次进入工作区读取浏览器保存的尺寸', () => {
    localStorage.setItem(imageExportStorageKey, '640')
    const scope = effectScope()
    try {
      expect(scope.run(() => useImageAction())!.imageExportSize.value).toBe(640)
    } finally {
      scope.stop()
    }
  })
})
