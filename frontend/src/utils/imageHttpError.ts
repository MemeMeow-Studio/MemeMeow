/** 将图片端点的公开错误保留到复制与相册提示，避免丢失处理原因。 */
export async function imageHttpError(response: Response): Promise<Error> {
  const status = `HTTP ${response.status}`
  if (!response.headers.get("Content-Type")?.includes("application/json")) {
    return new Error(`image_http_error: 图片请求返回 ${status}`)
  }
  let payload: { error?: string, message?: string } | null
  try {
    payload = await response.json()
  } catch (reason) {
    return new Error(`image_http_error_invalid_json: 图片错误响应的 JSON 无法解析 (${status})`, { cause: reason })
  }
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
    return new Error(`image_http_error_invalid_payload: 图片错误响应缺少错误信息对象 (${status})`)
  }
  return new Error(`${payload.error || "image_http_error"}: ${payload.message || "图片请求返回错误"} (${status})`)
}
