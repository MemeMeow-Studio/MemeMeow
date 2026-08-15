/** Agent 活跃度格式化逻辑的边界和文案测试。 */
import { describe, expect, it } from 'vitest'

import {
  formatAgentActivity,
  formatAgentLastActivity,
  formatAgentTurns,
  hasAgentActivity,
} from './agentActivity'

const now = '2026-08-13T12:30:00.000Z'

describe('Agent 活跃度格式化', () => {
  it('分别生成进行中和已结束轮次文案', () => {
    expect(formatAgentTurns({ agent_completed_turns: 18, agent_turn_running: true, agent_last_activity_at: now })).toBe('第 19 轮进行中')
    expect(formatAgentTurns({ agent_completed_turns: 18, agent_turn_running: false, agent_last_activity_at: now })).toBe('已完成 18 轮')
  })

  it('生成相对最近活动时间并处理未来时钟', () => {
    expect(formatAgentLastActivity('2026-08-13T12:29:40.000Z', now)).toBe('刚刚')
    expect(formatAgentLastActivity('2026-08-13T12:20:00.000Z', now)).toBe('10 分钟前')
    expect(formatAgentLastActivity('2026-08-12T10:30:00.000Z', now)).toBe('1 天前')
    expect(formatAgentLastActivity('2026-08-13T13:00:00.000Z', now)).toBe('刚刚')
  })

  it('缺少任一字段或时间无效时隐藏整段活动', () => {
    expect(hasAgentActivity({ agent_completed_turns: 0, agent_turn_running: false })).toBe(false)
    expect(formatAgentActivity({ agent_completed_turns: -1, agent_turn_running: false, agent_last_activity_at: now })).toBeNull()
    expect(formatAgentActivity({ agent_completed_turns: 0, agent_turn_running: false, agent_last_activity_at: 'bad-time' })).toBeNull()
  })

  it('活动视图同时提供轮次、相对时间和无障碍描述', () => {
    expect(formatAgentActivity({ agent_completed_turns: 2, agent_turn_running: true, agent_last_activity_at: '2026-08-13T12:29:30.000Z' }, now)).toEqual({
      turns: '第 3 轮进行中',
      lastActivity: '刚刚',
      ariaLabel: 'Agent 活动：第 3 轮进行中，最近活动 刚刚',
    })
  })
})
