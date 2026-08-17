/** 统一封装后端请求、错误结构和任务轮询。 */
const API_BASE = import.meta.env.VITE_API_BASE ?? (import.meta.env.DEV ? '/api' : '')

export async function request(path, options = {}) {
  const response = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers: { ...(options.body instanceof FormData ? {} : { 'Content-Type': 'application/json' }), ...(options.headers || {}) },
  })
  const data = await response.json().catch(() => ({}))
  if (!response.ok) {
    // FastAPI 对 HTTPException 会包一层 detail；统一解包后才能命中前端稳定错误码。
    const detail = data.detail && typeof data.detail === 'object' ? data.detail : data
    const error = new Error(detail.message || (typeof data.detail === 'string' ? data.detail : '请求失败'))
    error.code = detail.error || 'request_failed'
    error.status = response.status
    throw error
  }
  return data
}

export const api = {
  config: () => request('/config'),
  search: (payload) => request('/search', { method: 'POST', body: JSON.stringify(payload) }),
  images: (params = {}) => request(`/images?${new URLSearchParams(params)}`),
  imageMetadata: (value) => {
    // 元数据查询只接受不可变 Meme 身份，避免客户端借助可变路径访问另一条记录。
    const memeId = typeof value === 'string' ? value : value?.meme_id
    if (!memeId) {
      const error = new Error('必须提供 meme_id')
      error.code = 'meme_id_required'
      error.status = 400
      return Promise.reject(error)
    }
    return request(`/images/metadata?meme_id=${encodeURIComponent(memeId)}`, { cache: 'no-store' })
  },
  rename: (payload) => request('/images/rename', { method: 'POST', body: JSON.stringify(payload) }),
  upload: (files, autoName) => {
    const body = new FormData()
    body.append('auto_name', autoName)
    files.forEach((file) => body.append('files', file))
    return request('/images/upload', { method: 'POST', body })
  },
  context: (payload) => request('/images/context', { method: 'POST', body: JSON.stringify(payload) }),
  contextBatch: (payload = {}) => request('/images/context/batch', { method: 'POST', body: JSON.stringify(payload) }),
  processingJobs: (params = {}) => request(`/images/processing?${new URLSearchParams(params)}`),
  processingJob: (id) => request(`/images/processing/${encodeURIComponent(id)}`),
  retryProcessingJob: (id, payload = {}) => request(`/images/processing/${encodeURIComponent(id)}/retry`, { method: 'POST', body: JSON.stringify(payload) }),
  submitImageStage: (payload) => request('/images/stages', { method: 'POST', body: JSON.stringify(payload) }),
  retryImageStage: (payload) => request('/images/stages', { method: 'POST', body: JSON.stringify(payload) }),
  imageStage: (payload) => request('/images/stages', { method: 'POST', body: JSON.stringify(payload) }),
  task: (id) => request(`/tasks/${id}`),
  retryTask: (id) => request(`/tasks/${encodeURIComponent(id)}/retry`, { method: 'POST' }),
  tasks: (params = {}) => request(`/tasks?${new URLSearchParams(Object.entries(params).filter(([, value]) => value !== undefined && value !== ''))}`),
  generateCache: () => request('/generate-cache', { method: 'POST' }),
  collections: (params = {}) => request(`/collections?${new URLSearchParams(params)}`),
  createCollection: (payload) => request('/collections', { method: 'POST', body: JSON.stringify(payload) }),
  collection: (id, params = {}) => request(`/collections/${encodeURIComponent(id)}?${new URLSearchParams(params)}`),
  renameCollection: (id, payload) => request(`/collections/${encodeURIComponent(id)}`, { method: 'PATCH', body: JSON.stringify(payload) }),
  deleteCollection: (id) => request(`/collections/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  addCollectionItems: (id, memeIds) => request(`/collections/${encodeURIComponent(id)}/items`, { method: 'POST', body: JSON.stringify({ meme_ids: memeIds }) }),
  removeCollectionMember: (id, memeId) => request(`/collections/${encodeURIComponent(id)}/items/${encodeURIComponent(memeId)}`, { method: 'DELETE' }),
}

export async function pollTask(id, onUpdate, interval = 700) {
  while (true) {
    const task = await api.task(id)
    onUpdate(task)
    if (task.status === 'succeeded' || task.status === 'failed') return task
    await new Promise((resolve) => setTimeout(resolve, interval))
  }
}
