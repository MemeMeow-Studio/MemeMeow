/** 检索图片输出的浏览器偏好及受控尺寸 URL，供网页复制与相册保存共用。 */
export const imageExportSizes = [60, 120, 240, 360, 480, 640] as const
export type ImageExportSize = "original" | typeof imageExportSizes[number]
export const imageExportStorageKey = "mememeow.search.image-export-size"

/** 读取本浏览器的尺寸；首次使用保留原图，无效设置提供明确错误。 */
export function loadImageExportSize(): ImageExportSize {
  const stored = localStorage.getItem(imageExportStorageKey)
  if (stored === null || stored === "original") return "original"
  const size = Number(stored)
  if (!imageExportSizes.some(candidate => candidate === size)) {
    throw new Error("image_export_size_invalid: 浏览器保存的图片尺寸无效，请重新选择尺寸")
  }
  return size as ImageExportSize
}

/** 保存固定尺寸或原图选项，刷新和重新进入检索页面继续使用。 */
export function saveImageExportSize(size: ImageExportSize): void {
  if (size !== "original" && !imageExportSizes.some(candidate => candidate === size)) {
    throw new Error("image_export_size_invalid: 图片尺寸只允许原图或固定档位")
  }
  localStorage.setItem(imageExportStorageKey, String(size))
}

/** 根据点击时的尺寸生成同源媒体 URL；预览 URL 保持原样。 */
export function imageExportUrl(url: string, size: ImageExportSize): string {
  if (size !== "original" && !imageExportSizes.some(candidate => candidate === size)) throw new Error("image_export_size_invalid: 图片尺寸无效")
  const target = new URL(url, window.location.href)
  if (target.origin !== window.location.origin) throw new Error("image_export_origin_invalid: 图片地址必须属于当前网站")
  if (size === "original") return url
  target.searchParams.set("max_edge", String(size))
  return target.href
}
