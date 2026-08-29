/** 统一封装后端请求、错误结构和任务轮询。 */
const API_BASE = import.meta.env.VITE_API_BASE ?? (import.meta.env.DEV ? '/api' : '')
const REPOSITORY_API_BASE = 'https://api.github.com/repos/MemeMeow-Studio/MemeMeow'
const REPOSITORY_REQUEST_TIMEOUT_MS = 8_000

/** 将客户端处理选项收束为服务端接受的安全形状，避免旧调用方传入非法值。 */
function normalizeImageProcessingOptions(options = {}) {
  return {
    reverse_image_policy: options?.reverse_image_policy === 'auto' ? 'auto' : 'forbid',
    auto_name: options?.auto_name === true,
  }
}

/** 请求 GitHub 公开 JSON 接口；仓库统计不经过本地业务 API 代理。 */
async function githubJson(path) {
  const controller = new AbortController()
  const timeoutId = setTimeout(() => controller.abort(), REPOSITORY_REQUEST_TIMEOUT_MS)
  try {
    const response = await fetch(`${REPOSITORY_API_BASE}${path}`, {
      headers: { Accept: 'application/vnd.github+json' },
      signal: controller.signal,
    })
    const data = await response.json().catch(() => null)
    if (!response.ok) throw new Error('repository_metadata_failed')
    return data
  } finally {
    clearTimeout(timeoutId)
  }
}

/** 判断 GitHub 返回的 star 数是否为可安全展示的非负整数。 */
function validStarCount(value) {
  return typeof value === 'number' && Number.isInteger(value) && value >= 0
}

/** 并行读取仓库 star 数和默认分支最新提交，允许单项失败时继续展示另一项。 */
async function fetchRepositoryMetadata() {
  const [repositoryResult, commitResult] = await Promise.allSettled([
    githubJson(''),
    githubJson('/commits?per_page=1'),
  ])
  const repository = repositoryResult.status === 'fulfilled' ? repositoryResult.value : null
  const commits = commitResult.status === 'fulfilled' ? commitResult.value : null
  const stars = validStarCount(repository?.stargazers_count) ? repository.stargazers_count : null
  const commitHash = typeof commits?.[0]?.sha === 'string' && commits[0].sha.trim() ? commits[0].sha.trim() : null
  if (stars === null && commitHash === null) throw new Error('repository_metadata_unavailable')
  return { stars, commitHash }
}

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
    const retryAfter = response.headers?.get?.('retry-after')
    if (retryAfter) {
      const seconds = Number.parseFloat(retryAfter)
      error.retryAfter = Number.isFinite(seconds)
        ? Math.max(0, seconds)
        : Math.max(0, (Date.parse(retryAfter) - Date.now()) / 1000)
    }
    throw error
  }
  return data
}

export const api = {
  config: () => request('/config'),
  repositoryMetadata: () => fetchRepositoryMetadata(),
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
  upload: (files, options = {}, requestOptions = {}) => {
    const normalized = typeof options === 'boolean'
      ? { reverse_image_policy: 'forbid', auto_name: options }
      : normalizeImageProcessingOptions(options)
    const body = new FormData()
    body.append('reverse_image_policy', normalized.reverse_image_policy)
    body.append('auto_name', String(normalized.auto_name))
    files.forEach((file) => body.append('files', file))
    return request('/images/upload', { method: 'POST', body, signal: requestOptions.signal })
  },
  context: (payload) => request('/images/context', { method: 'POST', body: JSON.stringify(payload) }),
  contextBatch: (payload = {}) => request('/images/context/batch', { method: 'POST', body: JSON.stringify(payload) }),
  processingJobs: (params = {}) => request(`/images/processing?${new URLSearchParams(params)}`),
  unreadyProcessing: (options = {}) => request('/images/processing/unready', { method: 'POST', body: JSON.stringify(normalizeImageProcessingOptions(options)) }),
  processingJob: (id) => request(`/images/processing/${encodeURIComponent(id)}`),
  retryProcessingJob: (id, payload = {}) => request(`/images/processing/${encodeURIComponent(id)}/retry`, { method: 'POST', body: JSON.stringify(payload) }),
  submitImageStage: (payload) => request('/images/stages', { method: 'POST', body: JSON.stringify(payload) }),
  retryImageStagesBatch: (payload) => request('/images/stages/batch', { method: 'POST', body: JSON.stringify(payload) }),
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
  // 二进制下载交给浏览器处理，保留服务端的文件名和当前会话 Cookie。
  collectionExportUrl: (id) => `${API_BASE}/collections/${encodeURIComponent(id)}/export`,
}

export async function pollTask(id, onUpdate, interval = 700) {
  const terminalStatuses = new Set(['succeeded', 'failed', 'blocked', 'unknown_execution', 'warning', 'skipped'])
  while (true) {
    const task = await api.task(id)
    onUpdate(task)
    if (terminalStatuses.has(task.status)) return task
    await new Promise((resolve) => setTimeout(resolve, interval))
  }
}
