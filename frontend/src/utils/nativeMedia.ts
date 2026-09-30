// 网页通过 Capacitor 的原生插件保存原图和选择相册文件。
import { Capacitor } from '@capacitor/core'
import { Media } from '@capacitor-community/media'
import { FilePicker } from '@capawesome/capacitor-file-picker'
import { FileTransfer, type FileTransferError } from '@capacitor/file-transfer'
import { Directory, Filesystem } from '@capacitor/filesystem'
import { errorMessage } from './presentation'

export const isAndroidApp = Capacitor.isNativePlatform() && Capacitor.getPlatform() === 'android'
const albumName = 'MemeMeow'
let pendingAlbum: Promise<string> | undefined

/** 确认插件已经安装到当前 APK，让网站和 APK 不兼容时明确报错。 */
function requirePlugin(name: string): void {
  if (!Capacitor.isPluginAvailable(name)) throw new Error(`android_plugin_missing: APK 缺少 ${name} 插件，请更新应用`)
}

/** 查询或创建应用自己的相册，返回 Android 相册标识。 */
async function resolveAlbum(): Promise<string> {
  const { path } = await Media.getAlbumsPath()
  const expectedIdentifier = `${path}/${albumName}`
  const { albums } = await Media.getAlbums()
  const existing = albums.find((album) => album.identifier === expectedIdentifier)
  if (existing) return existing.identifier
  await Media.createAlbum({ name: albumName })
  const created = await Media.getAlbums()
  const album = created.albums.find((item) => item.identifier === expectedIdentifier)
  if (!album) throw new Error('android_album_missing: 创建后未找到 MemeMeow 相册')
  return album.identifier
}

/** 合并同时发生的相册创建请求；每次保存仍检查相册是否存在。 */
function getAlbumIdentifier(): Promise<string> {
  pendingAlbum ??= resolveAlbum().finally(() => { pendingAlbum = undefined })
  return pendingAlbum
}

/** 查询原图的 Content-Type，使用网站支持的格式生成本地文件扩展名。 */
async function imageExtension(url: string): Promise<string> {
  const response = await fetch(url, {
    credentials: 'same-origin',
    headers: { Range: 'bytes=0-0' },
    redirect: 'error',
    cache: 'no-store',
  })
  await response.body?.cancel()
  if (!response.ok) throw new Error(`android_image_http_error: 读取图片返回 HTTP ${response.status}`)
  const extensions: Record<string, string> = { 'image/png': 'png', 'image/jpeg': 'jpg', 'image/gif': 'gif' }
  const extension = extensions[response.headers.get('Content-Type') || '']
  if (!extension) throw new Error('android_image_mime_invalid: 网站没有返回支持的图片类型')
  return extension
}

/** 原生下载沿用 WebView 登录会话，保存原图后删除本次缓存目录。 */
export async function saveImageToAlbum(url: string): Promise<void> {
  requirePlugin('Media')
  requirePlugin('Filesystem')
  requirePlugin('FileTransfer')
  const imageUrl = new URL(url, window.location.href)
  if (imageUrl.origin !== window.location.origin) throw new Error('android_image_origin_invalid: 图片地址必须属于当前网站')
  const extension = await imageExtension(imageUrl.href)
  const albumIdentifier = await getAlbumIdentifier()
  const directory = { directory: Directory.Cache, path: `mememeow-save-${crypto.randomUUID()}` }
  await Filesystem.mkdir(directory)
  let operationError: unknown
  try {
    const file = { directory: Directory.Cache, path: `${directory.path}/image.${extension}` }
    const { uri } = await Filesystem.getUri(file)
    try {
      await FileTransfer.downloadFile({ url: imageUrl.href, path: uri, disableRedirects: true })
    } catch (reason) {
      const details = (reason as { data?: FileTransferError })?.data
      throw new Error(`${details?.code || 'android_image_download_error'}: ${errorMessage(reason)}${details?.httpStatus ? ` (HTTP ${details.httpStatus})` : ''}`, { cause: reason })
    }
    const { size } = await Filesystem.stat(file)
    if (!size) throw new Error('android_image_empty: 网站返回了空图片')
    await Media.savePhoto({ path: uri, albumIdentifier, fileName: `MemeMeow_${crypto.randomUUID()}` })
  } catch (reason) {
    operationError = reason
    throw reason
  } finally {
    try {
      await Filesystem.rmdir({ ...directory, recursive: true })
    } catch (reason) {
      if (operationError) throw new AggregateError([operationError, reason], `${errorMessage(operationError)}；缓存清理失败：${errorMessage(reason)}`)
      throw reason
    }
  }
}

/** 系统照片选择器返回原始文件，供现有上传队列使用。 */
export async function pickAlbumImages(): Promise<File[]> {
  requirePlugin('FilePicker')
  let selected: Awaited<ReturnType<typeof FilePicker.pickImages>>
  try {
    selected = await FilePicker.pickImages({ limit: 0, readData: false })
  } catch (reason) {
    // 用户关闭选择器属于正常取消，其他错误由上传工作区显示。
    if (reason instanceof Error && reason.message === 'pickFiles canceled.') return []
    throw reason
  }
  const files: File[] = []
  for (const item of selected.files) {
    if (!item.webPath) throw new Error('android_selected_image_path_missing: 相册没有返回图片读取地址')
    const response = await fetch(item.webPath)
    if (!response.ok) throw new Error(`android_selected_image_http_error: 读取相册图片返回 HTTP ${response.status}`)
    const blob = await response.blob()
    const mime = item.mimeType || blob.type
    if (!mime.startsWith('image/')) throw new Error('android_selected_image_mime_invalid: 选择的文件没有有效图片类型')
    files.push(new File([blob], item.name, { type: mime, lastModified: item.modifiedAt ?? Date.now() }))
  }
  return files
}
