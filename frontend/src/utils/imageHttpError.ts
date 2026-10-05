/** 将图片端点的公开错误保留到复制与相册提示，避免丢失处理原因。 */
export async function imageHttpError(response: Response): Promise<Error> {
  const status = `HTTP ${response.status}`
  if (!response.headers.get("Content-Type")?.includes("application/json")) {
    return new Error(`image_http_error: 图片请求返回 ${status}`)
  }
  const payload = await response.json() as { error?: string, message?: string }
  return new Error(`${payload.error || "image_http_error"}: ${payload.message || "图片请求返回错误"} (${status})`)
}
