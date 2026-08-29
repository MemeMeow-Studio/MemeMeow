/** 前端诊断开关的环境判断测试，确保生产构建不会意外打开诊断字段。 */
import { describe, expect, it } from 'vitest'
import { isTaskDiagnosticsEnabled } from './debug'

describe('任务诊断开关', () => {
  it.each([
    [{ DEV: true, VITE_SHOW_TASK_DIAGNOSTICS: 'true' }, true],
    [{ DEV: true, VITE_SHOW_TASK_DIAGNOSTICS: 'false' }, false],
    [{ DEV: false, VITE_SHOW_TASK_DIAGNOSTICS: 'true' }, false],
    [{ DEV: true }, false],
  ])('只在开发环境显式配置为 true 时启用：%o', (environment, expected) => {
    expect(isTaskDiagnosticsEnabled(environment)).toBe(expected)
  })
})
