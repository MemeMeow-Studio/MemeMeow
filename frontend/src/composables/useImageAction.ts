// 检索结果统一入口：网页复制图片，安卓应用保存原图到相册。
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
  const imageActionLabel = isAndroidApp ? '保存' : '复制'

  /** 根据当前运行平台执行图片操作，保存期间防止重复点击。 */
  async function performImageAction(url: string): Promise<void> {
    if (!isAndroidApp) return copyImage(url)
    if (saving.value) return
    saving.value = true
    try {
      await saveImageToAlbum(url)
      notifications.success('图片已保存到 MemeMeow 相册')
    } catch (reason) {
      notifications.error(`图片保存失败：${errorMessage(reason)}`)
    } finally {
      saving.value = false
    }
  }

  return { imageActionLabel, performImageAction, saving }
}
