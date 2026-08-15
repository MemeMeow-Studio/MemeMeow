/**
 * 检索结果图片剪贴板能力，封装浏览器兼容、PNG 转换和竞态提示。
 */

import { onBeforeUnmount, shallowRef } from 'vue'

const clipboardImageMime = 'image/png'

/** 将浏览器剪贴板异常转换为稳定错误码。 */
function imageClipboardWriteFailureCode(reason: unknown): string {
  if (reason instanceof DOMException && reason.name === 'NotAllowedError') {
    return window.isSecureContext ? 'image_clipboard_permission_denied' : 'image_clipboard_insecure_context'
  }
  if (reason instanceof DOMException && reason.name === 'NotSupportedError') return 'image_clipboard_unsupported'
  return 'image_clipboard_write_failed'
}

/** 检查当前浏览器是否支持写入 PNG 图片。 */
function supportsImageClipboard(): boolean {
  if (typeof navigator.clipboard?.write !== 'function' || typeof ClipboardItem !== 'function') return false
  if (typeof ClipboardItem.supports !== 'function') return true
  try {
    return ClipboardItem.supports(clipboardImageMime)
  } catch {
    return false
  }
}

/** 将浏览器可解码的图片 Blob 转成剪贴板接受的 PNG。 */
async function convertImageBlobToPng(blob: Blob): Promise<Blob> {
  let bitmap: ImageBitmap | HTMLImageElement | undefined
  let objectUrl: string | undefined
  try {
    if (typeof createImageBitmap === 'function') {
      bitmap = await createImageBitmap(blob)
    } else {
      objectUrl = URL.createObjectURL(blob)
      const image = new Image()
      image.decoding = 'async'
      image.src = objectUrl
      await image.decode()
      bitmap = image
    }
    if (!bitmap.width || !bitmap.height) throw new Error('image_decode_failed')
    const canvas = document.createElement('canvas')
    canvas.width = bitmap.width
    canvas.height = bitmap.height
    const context = canvas.getContext('2d')
    if (!context) throw new Error('image_decode_failed')
    context.drawImage(bitmap, 0, 0)
    return await new Promise((resolve, reject) => {
      canvas.toBlob((result) => result ? resolve(result) : reject(new Error('image_encode_failed')), clipboardImageMime)
    })
  } catch {
    throw new Error('image_decode_failed')
  } finally {
    if (bitmap && 'close' in bitmap) bitmap.close()
    if (objectUrl) URL.revokeObjectURL(objectUrl)
  }
}

/** 请求并准备检索结果图片，HTTP 和 MIME 异常都转换为用户可解释错误。 */
async function fetchImageForClipboard(url: string): Promise<Blob> {
  let response: Response
  try {
    response = await fetch(url, { credentials: 'same-origin' })
    if (!response.ok) throw new Error('image_fetch_failed')
  } catch {
    throw new Error('image_fetch_failed')
  }
  let blob: Blob
  try {
    blob = await response.blob()
  } catch {
    throw new Error('image_fetch_failed')
  }
  const mime = blob.type.trim().toLowerCase()
  if (!mime.startsWith('image/')) throw new Error('image_mime_unavailable')
  return mime === clipboardImageMime ? blob : convertImageBlobToPng(blob)
}

/**
 * 创建图片复制状态与动作，供检索工作区按用户点击写入图片二进制。
 * @returns 当前提示和复制动作；组件卸载时自动清理提示计时器。
 */
export function useImageClipboard() {
  const copyNotice = shallowRef('')
  let copyNoticeTimer: number | undefined
  let copyRequestId = 0

  /** 将一张检索结果图片写入系统剪贴板，禁止降级成 URL 文本。 */
  async function copyImage(url: string): Promise<void> {
    const requestId = ++copyRequestId
    window.clearTimeout(copyNoticeTimer)
    let notice = ''
    try {
      if (window.isSecureContext === false) throw new Error('image_clipboard_insecure_context')
      if (!supportsImageClipboard()) throw new Error('image_clipboard_unsupported')
      if (typeof fetch !== 'function') throw new Error('image_fetch_unavailable')

      let imageDataFailure: unknown = null
      const imageData = fetchImageForClipboard(url).catch((reason) => {
        imageDataFailure = reason
        throw reason
      })
      imageData.catch(() => {})
      let item: ClipboardItem
      try {
        item = new ClipboardItem({ [clipboardImageMime]: imageData })
      } catch {
        throw new Error('image_mime_unavailable')
      }
      try {
        await navigator.clipboard.write([item])
      } catch (reason) {
        if (imageDataFailure) throw imageDataFailure
        throw new Error(imageClipboardWriteFailureCode(reason))
      }
      notice = '图片已复制'
    } catch (reason) {
      const code = reason instanceof Error ? reason.message : ''
      notice = {
        image_clipboard_unsupported: '图片复制失败：当前浏览器不支持复制图片',
        image_fetch_unavailable: '图片复制失败：图片加载功能不可用，无法复制',
        image_fetch_failed: '图片复制失败：图片加载失败，无法复制',
        image_mime_unavailable: '图片复制失败：图片 MIME 类型不可用，无法复制',
        image_decode_failed: '图片复制失败：图片格式无法转换，图片未复制',
        image_clipboard_insecure_context: '图片复制失败：当前页面不是安全上下文，请使用 http://localhost:5275 或 HTTPS',
        image_clipboard_permission_denied: '图片复制失败：Chrome 拒绝剪贴板写入，请在网站设置中允许剪贴板后重试',
        image_clipboard_write_failed: '图片复制失败：剪贴板写入被拒绝，图片未复制',
      }[code] || '图片复制失败，图片未复制'
    }
    if (requestId !== copyRequestId) return
    copyNotice.value = notice
    copyNoticeTimer = window.setTimeout(() => { copyNotice.value = '' }, 2600)
  }

  onBeforeUnmount(() => window.clearTimeout(copyNoticeTimer))
  return { copyNotice, copyImage }
}
