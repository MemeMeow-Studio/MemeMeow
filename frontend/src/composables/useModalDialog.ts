/**
 * 模态对话框的共享键盘与焦点控制，保证打开、循环、关闭和焦点恢复一致。
 */

import { nextTick, onBeforeUnmount, onMounted, type Ref } from 'vue'

interface ModalDialogOptions {
  dialog: Ref<HTMLElement | null>
  initialFocus: Ref<HTMLElement | null>
  returnFocus?: HTMLElement | null
  close: () => void
}

const focusableSelector = [
  'button:not([disabled])',
  '[href]',
  'input:not([disabled])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(', ')

/**
 * 为当前已挂载的模态框注册 Escape 与 Tab 约束。
 * @param options 对话框引用、初始焦点、返回焦点和关闭动作。
 * @returns 无返回值；在组件卸载时自动清理并恢复焦点。
 */
export function useModalDialog(options: ModalDialogOptions): void {
  function onKeydown(event: KeyboardEvent): void {
    if (event.key === 'Escape') {
      event.preventDefault()
      options.close()
      return
    }
    if (event.key !== 'Tab') return
    const focusable = [...(options.dialog.value?.querySelectorAll<HTMLElement>(focusableSelector) || [])]
    if (!focusable.length) return
    const first = focusable[0]
    const last = focusable[focusable.length - 1]
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault()
      last.focus()
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault()
      first.focus()
    }
  }

  onMounted(async () => {
    document.addEventListener('keydown', onKeydown)
    await nextTick()
    ;(options.initialFocus.value || options.dialog.value)?.focus()
  })

  onBeforeUnmount(() => {
    document.removeEventListener('keydown', onKeydown)
    const trigger = options.returnFocus
    nextTick(() => {
      if (trigger?.isConnected) trigger.focus()
    })
  })
}
