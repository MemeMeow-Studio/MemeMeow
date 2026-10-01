// 使用真实 toast 组件和计时器验证提示生命周期及模态框内的访问。
import { afterEach, describe, expect, it } from 'vitest'
import { defineComponent, h } from 'vue'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { toast } from 'vue-sonner'
import ToastHost from './ToastHost.vue'
import ImageRetryDialog from './ImageRetryDialog.vue'
import { clearToasts, toastTarget, useToast } from '../composables/useToast'

const wrappers: VueWrapper[] = []

/** 挂载真实提示宿主，并返回其组件作用域内的提示入口。 */
function mountHost() {
  let notifications!: ReturnType<typeof useToast>
  const wrapper = mount(defineComponent({
    setup() { notifications = useToast(); return () => h(ToastHost) },
  }), { attachTo: document.body })
  wrappers.push(wrapper)
  return { wrapper, notifications }
}

afterEach(async () => {
  for (const wrapper of wrappers.splice(0).reverse()) wrapper.unmount()
  clearToasts()
  await flushPromises()
})

describe('ToastHost', () => {
  it('成功提示在真实计时器到期后消失，错误及警告持续可关闭', async () => {
    const { notifications } = mountHost()
    notifications.success('图片已复制')
    notifications.error('剪贴板权限被拒绝')
    notifications.warning('部分图片未提交')
    await flushPromises()
    expect(document.querySelectorAll('[data-sonner-toast]')).toHaveLength(3)
    expect(toast.getToasts().map((item) => 'duration' in item ? item.duration : undefined)).toEqual([3000, Infinity, Infinity])
    await new Promise((resolve) => setTimeout(resolve, 3500))
    await flushPromises()
    expect(document.body.textContent).not.toContain('图片已复制')
    expect(document.body.textContent).toContain('剪贴板权限被拒绝')
    expect(document.body.textContent).toContain('部分图片未提交')
    for (const button of document.querySelectorAll<HTMLButtonElement>('[aria-label="关闭提示"]')) button.click()
    await new Promise((resolve) => setTimeout(resolve, 400))
    expect(document.querySelectorAll('[data-sonner-toast]')).toHaveLength(0)
  })

  it('同一时间的操作结果分别显示，清理账户提示后没有活动记录', async () => {
    const { notifications } = mountHost()
    for (let index = 0; index < 5; index += 1) notifications.error(`请求 ${index} 失败`)
    await flushPromises()
    expect(document.querySelectorAll('[data-sonner-toast]')).toHaveLength(5)
    clearToasts()
    expect(toast.getToasts()).toHaveLength(0)
  })

  it('卸载请求所属组件后停止产生提示', () => {
    const { wrapper, notifications } = mountHost()
    wrapper.unmount()
    notifications.error('已离开的请求')
    expect(toast.getToasts()).toHaveLength(0)
  })

  it('提示进入模态框的焦点范围，关闭模态框后返回全局容器', async () => {
    const { notifications } = mountHost()
    const dialog = mount(ImageRetryDialog, { props: { selectedCount: 1, busy: false }, attachTo: document.body })
    wrappers.push(dialog)
    await flushPromises()
    notifications.error('任务提交失败')
    await flushPromises()
    const element = document.querySelector<HTMLElement>('[role="dialog"]')!
    expect(element.contains(document.querySelector('[data-sonner-toaster]'))).toBe(true)
    const closeButton = element.querySelector<HTMLButtonElement>('[aria-label="关闭提示"]')!
    closeButton.focus()
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', code: 'Escape', bubbles: true }))
    expect(dialog.emitted('cancel')).toBeUndefined()
    dialog.unmount()
    await flushPromises()
    expect(toastTarget.value).toBe('body')
    expect(document.body.textContent).toContain('任务提交失败')
  })
})
