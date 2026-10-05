// 检索结果统一入口：按浏览器尺寸偏好复制图片或保存到 Android 相册。
import { imageExportSizes, imageExportUrl, loadImageExportSize, saveImageExportSize } from "../utils/imageExportPreferences"
import type { ImageExportSize } from '../utils/imageExportPreferences'
import { shallowRef } from 'vue'
import { useToast } from './useToast'
import { useImageClipboard } from './useImageClipboard'
import { isAndroidApp, saveImageToAlbum } from '../utils/nativeMedia'
import { errorMessage } from '../utils/presentation'

/** 提供检索结果的图片操作及状态提示，供卡片和图片按钮共用。 */
export function useImageAction() {
  const { copyImage } = useImageClipboard()
  const notifications = useToast()
  const saving = shallowRef(false)
  const imageExportSize = shallowRef<ImageExportSize | null>(null)
  let imageExportSizeErrorId: string | number | undefined

  /** 替换当前尺寸设置提示，保持一条需要用户处理的原因。 */
  function showImageExportSizeError(message: string): void {
    if (imageExportSizeErrorId !== undefined) notifications.dismiss(imageExportSizeErrorId)
    imageExportSizeErrorId = notifications.error(message)
  }

  try {
    imageExportSize.value = loadImageExportSize()
  } catch (reason) {
    showImageExportSizeError(`图片尺寸设置读取失败：${errorMessage(reason)}`)
  }

  /** 保存用户选择；浏览器拒绝存储时明确显示原因。 */
  function persistImageExportSize(): void {
    try {
      if (imageExportSize.value === null) throw new Error('image_export_size_invalid: 请重新选择图片尺寸')
      saveImageExportSize(imageExportSize.value)
      if (imageExportSizeErrorId !== undefined) notifications.dismiss(imageExportSizeErrorId)
      imageExportSizeErrorId = undefined
    } catch (reason) {
      showImageExportSizeError(`图片尺寸设置未保存：${errorMessage(reason)}`)
    }
  }
  const imageActionLabel = isAndroidApp ? '保存' : '复制'

  /** 根据当前运行平台执行图片操作，保存期间防止重复点击。 */
  async function performImageAction(url: string): Promise<void> {
    if (saving.value) return
    if (imageExportSize.value === null) {
      showImageExportSizeError('image_export_size_invalid: 请重新选择图片尺寸')
      return
    }
    try {
      const actionUrl = imageExportUrl(url, imageExportSize.value)
      if (!isAndroidApp) return await copyImage(actionUrl)
      saving.value = true
      await saveImageToAlbum(actionUrl)
      notifications.success('图片已保存到 MemeMeow 相册')
    } catch (reason) {
      notifications.error(`图片${imageActionLabel}失败：${errorMessage(reason)}`)
    } finally {
      saving.value = false
    }
  }

  return { imageActionLabel, performImageAction, saving, imageExportSize, imageExportSizes, persistImageExportSize }
}
