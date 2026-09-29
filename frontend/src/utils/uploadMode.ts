/** 根据本次选择的图片总数确定上传默认方式。 */
export function defaultUploadMode(count: number): 'direct' | 'analyze' {
  return count > 10 ? 'direct' : 'analyze'
}
