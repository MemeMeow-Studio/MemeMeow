/** 统一封装后端请求、错误结构和任务轮询。 */
const API_BASE = import.meta.env.VITE_API_BASE ?? (import.meta.env.DEV ? '/api' : '')

export async function request(path, options = {}) {
  const response = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers: { ...(options.body instanceof FormData ? {} : { 'Content-Type': 'application/json' }), ...(options.headers || {}) },
  })
  const data = await response.json().catch(() => ({}))
  if (!response.ok) {
    const error = new Error(data.message || data.detail || '请求失败')
    error.code = data.error || 'request_failed'
    error.status = response.status
    throw error
  }
  return data
}

export const api = {
  config: () => request('/config'),
  search: (payload) => request('/search', { method: 'POST', body: JSON.stringify(payload) }),
  images: (params = {}) => request(`/images?${new URLSearchParams(params)}`),
  directories: (parent = '') => request(`/images/directories?parent=${encodeURIComponent(parent)}`),
  createDirectory: (payload) => request('/images/directories', { method: 'POST', body: JSON.stringify(payload) }),
  rename: (payload) => request('/images/rename', { method: 'POST', body: JSON.stringify(payload) }),
  upload: (directory, files, autoName) => {
    const body = new FormData()
    body.append('directory', directory)
    body.append('auto_name', autoName)
    files.forEach((file) => body.append('files', file))
    return request('/images/upload', { method: 'POST', body })
  },
  context: (payload) => request('/images/context', { method: 'POST', body: JSON.stringify(payload) }),
  contextBatch: (payload = {}) => request('/images/context/batch', { method: 'POST', body: JSON.stringify(payload) }),
  task: (id) => request(`/tasks/${id}`),
  tasks: (params = {}) => request(`/tasks?${new URLSearchParams(Object.entries(params).filter(([, value]) => value !== undefined && value !== ''))}`),
  generateCache: () => request('/generate-cache', { method: 'POST' }),
}

export async function pollTask(id, onUpdate, interval = 700) {
  while (true) {
    const task = await api.task(id)
    onUpdate(task)
    if (task.status === 'succeeded' || task.status === 'failed') return task
    await new Promise((resolve) => setTimeout(resolve, interval))
  }
}
