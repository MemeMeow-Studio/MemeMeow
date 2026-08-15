/**
 * 处理任务页共享的 Agent 活跃度字段，统一轮次语义和相对时间文案。
 * 该模块只处理 API 摘要中的三个公开字段，不接触 OpenCode 原始内容。
 */

/** 将 API 时间解析为有效毫秒值；无效输入返回 null。 */
function parseActivityTime(value) {
  if (value === null || value === undefined || value === '') return null
  const timestamp = new Date(value).getTime()
  return Number.isFinite(timestamp) ? timestamp : null
}

/**
 * 判断任务是否具有完整的 Agent 活跃度摘要。
 * @param {object|null|undefined} task 任务列表或详情响应。
 * @returns {boolean} 三个字段均完整且时间可解析时返回 true。
 */
export function hasAgentActivity(task) {
  return Boolean(normalizeAgentActivity(task))
}

/**
 * 规范后端活动字段，避免缺失字段形成“零轮”或空时间占位。
 * @param {object|null|undefined} task 任务摘要或活动字段对象。
 * @returns {{completedTurns:number, turnRunning:boolean, lastActivityAt:string}|null} 完整活动值。
 */
export function normalizeAgentActivity(task) {
  if (!task || typeof task !== 'object') return null
  const completedTurns = task.agent_completed_turns
  const turnRunning = task.agent_turn_running
  const lastActivityAt = task.agent_last_activity_at
  if (!Number.isInteger(completedTurns) || completedTurns < 0) return null
  if (typeof turnRunning !== 'boolean' || typeof lastActivityAt !== 'string' || parseActivityTime(lastActivityAt) === null) return null
  return { completedTurns, turnRunning, lastActivityAt }
}

/**
 * 生成 Agent 轮次活动文案。
 * @param {object|null|undefined} task 任务摘要或活动字段对象。
 * @returns {string} 进行中返回“第 N+1 轮进行中”，否则返回已完成轮次；缺失时返回空串。
 */
export function formatAgentTurns(task) {
  const activity = normalizeAgentActivity(task)
  if (!activity) return ''
  return activity.turnRunning ? `第 ${activity.completedTurns + 1} 轮进行中` : `已完成 ${activity.completedTurns} 轮`
}

/**
 * 将 Agent 最近活动时间格式化为相对时间。
 * @param {string|number|Date} value API 返回的 UTC 时间。
 * @param {string|number|Date} now 相对时间基准，测试和轮询渲染可显式传入。
 * @returns {string} 中文相对时间；无效时间返回空串。
 */
export function formatAgentLastActivity(value, now = Date.now()) {
  const timestamp = parseActivityTime(value)
  const current = parseActivityTime(now)
  if (timestamp === null || current === null) return ''
  const elapsed = Math.max(0, current - timestamp)
  if (elapsed < 60 * 1000) return '刚刚'
  if (elapsed < 60 * 60 * 1000) return `${Math.floor(elapsed / (60 * 1000))} 分钟前`
  if (elapsed < 24 * 60 * 60 * 1000) return `${Math.floor(elapsed / (60 * 60 * 1000))} 小时前`
  return `${Math.floor(elapsed / (24 * 60 * 60 * 1000))} 天前`
}

/**
 * 生成列表和详情共用的活动视图模型。
 * @param {object|null|undefined} task 任务摘要。
 * @param {string|number|Date} now 相对时间基准。
 * @returns {{turns:string,lastActivity:string,ariaLabel:string}|null} 可展示模型或 null。
 */
export function formatAgentActivity(task, now = Date.now()) {
  const activity = normalizeAgentActivity(task)
  if (!activity) return null
  const turns = formatAgentTurns(task)
  const lastActivity = formatAgentLastActivity(activity.lastActivityAt, now)
  if (!turns || !lastActivity) return null
  return {
    turns,
    lastActivity,
    ariaLabel: `Agent 活动：${turns}，最近活动 ${lastActivity}`,
  }
}
