// 全应用操作提示入口；持续业务状态由所属页面保存。
import { onScopeDispose, shallowRef } from 'vue'
import { toast } from 'vue-sonner'

export type ToastKind = 'success' | 'error' | 'warning' | 'info'
export const toastTarget = shallowRef<HTMLElement | string>('body')

/** 清除当前账户的提示，供会话退出或身份变化时调用。 */
export function clearToasts(): void {
  for (const item of toast.getToasts()) toast.dismiss(item.id)
}

/** 创建作用域内的提示动作；请求所属组件卸载后停止产生新提示。 */
export function useToast() {
  let active = true
  onScopeDispose(() => { active = false })

  /** 显示操作结果并返回提示 ID；错误和警告持续显示，供用户处理。 */
  function show(message: string, kind: ToastKind = 'success'): string | number | undefined {
    if (!active) return
    return toast[kind](message, { duration: kind === 'error' || kind === 'warning' ? Infinity : 3000 })
  }

  /** 根据已显示的提示 ID 关闭对应消息，供问题修复后的操作调用。 */
  function dismiss(id: string | number): void {
    if (active) toast.dismiss(id)
  }

  return { show, dismiss, success: (message: string) => show(message), error: (message: string) => show(message, 'error'), warning: (message: string) => show(message, 'warning') }
}
