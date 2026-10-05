/** 桌面 GIF 剪贴板接口，向隔离宿主传递完整图片字节。 */
export interface DesktopClipboardBridge {
  version: number
  maxGifBytes: number
  copyGif(data: ArrayBuffer): Promise<void>
}

declare global {
  interface Window {
    mememeowClipboard?: DesktopClipboardBridge
  }
}

/** 获取桌面接口；版本不匹配时提示更新应用。 */
export function desktopClipboard(): DesktopClipboardBridge | undefined {
  const bridge = window.mememeowClipboard
  if (bridge && bridge.version !== 1) {
    throw new Error("image_clipboard_version_unsupported: GIF 复制接口版本不兼容，请更新桌面应用")
  }
  return bridge
}
