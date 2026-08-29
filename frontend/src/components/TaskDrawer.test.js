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

  it('核心图片阶段 blocked 时仍提供完整重试入口', async () => {
    wrapper = mount(TaskDrawer, {
      props: {
        task: {
          task_id: 'task-blocked',
          task_type: 'visual_embedding_generation',
          submission_mode: 'pipeline',
          image_stage: 'visual',
          processing_job_id: 'job-1',
          status: 'blocked',
          image: { meme_id: 'meme-1', filename: 'sample.png' },
        },
      },
    })
    await nextTick()

    const retryButton = wrapper.get('.task-drawer > .primary')
    await retryButton.trigger('click')
    expect(wrapper.emitted('retry-full')).toHaveLength(1)
  })

  it('warning 自动重命名叶子 Task 显示阶段警告和独立恢复动作', async () => {
    wrapper = mount(TaskDrawer, {
      props: {
        task: {
          task_id: 'rename-task',
          task_type: 'image_auto_rename',
          submission_mode: 'pipeline',
          image_stage: 'auto_rename',
          image_stage_status: 'warning',
          image_stage_recoverable: true,
          processing_job_id: 'job-1',
          status: 'failed',
          image: { meme_id: 'meme-1', filename: 'sample.png' },
        },
      },
    })
    await nextTick()

    expect(wrapper.text()).toContain('自动重命名：处理完成，自动重命名未完成')
    const retryButton = wrapper.findAll('.task-drawer > .quiet').find((button) => button.text().includes('恢复自动命名'))
    expect(retryButton).toBeDefined()
    expect(retryButton.text()).toBe('恢复自动命名')
    await retryButton.trigger('click')
    expect(wrapper.emitted('retry-stage')).toHaveLength(1)
    expect(wrapper.emitted('retry-full')).toBeUndefined()
    expect(wrapper.emitted('retry')).toBeUndefined()
  })

  it('自动重命名 running 时不提供恢复按钮', async () => {
    wrapper = mount(TaskDrawer, {
      props: {
        task: {
          task_id: 'rename-running',
          task_type: 'image_auto_rename',
          submission_mode: 'pipeline',
          image_stage: 'auto_rename',
          image_stage_status: 'running',
          status: 'running',
          image: { meme_id: 'meme-1', filename: 'sample.png' },
        },
      },
    })
    await nextTick()

    expect(wrapper.text()).toContain('自动重命名：处理中')
    expect(wrapper.findAll('.task-drawer > .quiet').some((button) => !button.attributes('aria-label'))).toBe(false)
    expect(wrapper.find('.task-drawer > .primary').exists()).toBe(false)
  })

  it('显示关联图片并在媒体地址缺失时安全降级', async () => {
    const task = {
      task_id: 'task-image',
      task_type: 'visual_embedding_generation',
      status: 'succeeded',
      image: { meme_id: 'meme-1', filename: 'sample.png', media_url: '/media/meme-1' },
    }
    wrapper = mount(TaskDrawer, { props: { task } })
    await nextTick()

    expect(wrapper.get('.task-image-preview img').attributes()).toMatchObject({
      src: '/media/meme-1',
      alt: '处理图片：sample.png',
    })

    await wrapper.get('.task-image-preview img').trigger('error')
    await nextTick()
    expect(wrapper.find('.task-image-preview img').exists()).toBe(false)
    expect(wrapper.get('.task-image-unavailable').text()).toContain('关联图片暂不可用')

    await wrapper.setProps({ task: { ...task, task_id: 'task-missing-url', image: { meme_id: 'meme-1', filename: 'sample.png' } } })
    await nextTick()
    expect(wrapper.find('.task-image-preview img').exists()).toBe(false)
    expect(wrapper.get('.task-image-unavailable').text()).toContain('关联图片暂不可用')

    await wrapper.setProps({ task: { ...task, task_id: 'task-no-image', image: undefined } })
    await nextTick()
    expect(wrapper.find('.task-image-preview').exists()).toBe(false)
  })
})
