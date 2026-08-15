/** 任务详情对话框的用户可见内容与键盘焦点行为测试。 */
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import { afterEach, describe, expect, it } from 'vitest'
import TaskDrawer from './TaskDrawer.vue'

describe('TaskDrawer', () => {
  let wrapper

  afterEach(() => {
    wrapper?.unmount()
    wrapper = undefined
  })

  it('打开时聚焦关闭按钮，Tab 保持在模态框内且 Escape 请求关闭', async () => {
    const trigger = document.createElement('button')
    trigger.textContent = '打开任务'
    document.body.appendChild(trigger)
    trigger.focus()

    wrapper = mount(TaskDrawer, {
      attachTo: document.body,
      props: {
        returnFocus: trigger,
        task: {
          task_id: 'task-1',
          task_type: 'meme_context_generation',
          status: 'failed',
          progress: 0.5,
          message: '处理失败',
          image: { meme_id: 'meme-1', filename: 'sample.png' },
        },
      },
    })
    await nextTick()

    const closeButton = wrapper.get('[aria-label="关闭任务详情"]')
    const retryButton = wrapper.get('.task-drawer > .primary')
    expect(document.activeElement).toBe(closeButton.element)

    closeButton.element.focus()
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', shiftKey: true, bubbles: true }))
    expect(document.activeElement).toBe(retryButton.element)

    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    expect(wrapper.emitted('close')).toHaveLength(1)

    wrapper.unmount()
    wrapper = undefined
    await nextTick()
    expect(document.activeElement).toBe(trigger)
    trigger.remove()
  })
})
