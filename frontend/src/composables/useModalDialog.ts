/**
 * 模态对话框的共享键盘与焦点控制，保证打开、循环、关闭和焦点恢复一致。
 */

import { nextTick, onBeforeUnmount, onMounted, type Ref } from 'vue'
import { toastTarget } from './useToast'

const notificationDialogs: HTMLElement[] = []

interface ModalDialogOptions {
  dialog: Ref<HTMLElement | null>
  initialFocus: Ref<HTMLElement | null>
  returnFocus?: HTMLElement | null | (() => HTMLElement | null)
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

let modalCount = 0
let previousBodyOverflow = ''

/**
 * 为当前已挂载的模态框注册 Escape 与 Tab 约束。
 * @param options 对话框引用、初始焦点、返回焦点和关闭动作。
 * @returns 无返回值；在组件卸载时自动清理并恢复焦点。
 */
export function useModalDialog(options: ModalDialogOptions): void {
  function onKeydown(event: KeyboardEvent): void {
    if (event.key === 'Escape') {
      if (document.activeElement instanceof HTMLElement && document.activeElement.closest('[data-sonner-toaster]')) return
      event.preventDefault()
      options.close()
      return
    }
    if (event.key !== 'Tab') return
    const dialogElement = options.dialog.value
    const focusable = [...(dialogElement?.querySelectorAll<HTMLElement>(focusableSelector) || [])]
    if (!focusable.length) {
      event.preventDefault()
      dialogElement?.focus()
      return
    }
    const first = focusable[0]
    const last = focusable[focusable.length - 1]
    if (!options.dialog.value?.contains(document.activeElement)) {
      event.preventDefault()
      first.focus()
      return
    }
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault()
      last.focus()
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault()
      first.focus()
    }
  }

  onBeforeUnmount(() => {
    const index = notificationDialogs.indexOf(options.dialog.value!)
    if (index !== -1) notificationDialogs.splice(index, 1)
    toastTarget.value = notificationDialogs.at(-1) || 'body'
  })

  onMounted(async () => {
    if (modalCount === 0) {
      previousBodyOverflow = document.body.style.overflow
      document.body.style.overflow = 'hidden'
    }
    modalCount += 1
    document.addEventListener('keydown', onKeydown)
    await nextTick()
    if (options.dialog.value) {
      notificationDialogs.push(options.dialog.value)
      toastTarget.value = options.dialog.value
    }
    ;(options.initialFocus.value || options.dialog.value)?.focus()
  })

  onBeforeUnmount(() => {
    modalCount -= 1
    if (modalCount === 0) document.body.style.overflow = previousBodyOverflow
    document.removeEventListener('keydown', onKeydown)
    nextTick(() => {
      const trigger = typeof options.returnFocus === 'function' ? options.returnFocus() : options.returnFocus
      if (trigger?.isConnected) trigger.focus()
    })
  })
}
