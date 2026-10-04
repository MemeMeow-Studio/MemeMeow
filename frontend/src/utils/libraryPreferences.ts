/** 图片库的状态类别和每页数量偏好，供两种展示方式共同使用。 */
export type ImageLibraryStatus = "all" | "ready" | "unready" | "context_unready" | "text_unready" | "visual_unready"

export const libraryPageSizes = [20, 50, 100, 200] as const
const pageSizeStorageKey = "mememeow.library.page-size"

/** 读取浏览器保存的每页数量；没有有效偏好时使用默认的 50。 */
export function loadLibraryPageSize(): number {
  const saved = Number(localStorage.getItem(pageSizeStorageKey))
  return libraryPageSizes.some(size => size === saved) ? saved : 50
}

/** 保存用户明确选择的每页数量，供刷新和重新进入图片库使用。 */
export function saveLibraryPageSize(size: number): void {
  if (!libraryPageSizes.some(candidate => candidate === size)) throw new Error("invalid_library_page_size")
  localStorage.setItem(pageSizeStorageKey, String(size))
}
