/**
 * 前端开发诊断开关，集中控制仅供维护者使用的标识渲染。
 *
 * 该模块只参与页面展示，不改变 API 数据、任务轮询或重试行为。
 */

/** 诊断开关依赖的最小 Vite 环境字段，便于在单元测试中验证边界。 */
interface DebugEnvironment {
  readonly DEV?: boolean
  readonly VITE_SHOW_TASK_DIAGNOSTICS?: string
}

/** 判断给定 Vite 环境是否允许普通页面展示任务诊断字段。 */
export function isTaskDiagnosticsEnabled(environment: DebugEnvironment = import.meta.env): boolean {
  return environment.DEV === true && environment.VITE_SHOW_TASK_DIAGNOSTICS === 'true'
}

/** 当前构建是否启用任务诊断字段；生产构建始终关闭。 */
export const showTaskDiagnostics = isTaskDiagnosticsEnabled()
