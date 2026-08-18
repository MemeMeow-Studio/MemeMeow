/** 图片库完整重试、阶段恢复和共享选项对话框的行为测试。 */
import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const { images, unreadyProcessing, submitImageStage } = vi.hoisted(() => ({
  images: vi.fn(),
  unreadyProcessing: vi.fn(),
  submitImageStage: vi.fn(),
}))

vi.mock('../api', () => ({
  api: { images, unreadyProcessing, submitImageStage },
}))

import LibraryWorkspace from './LibraryWorkspace.vue'

const image = {
  meme_id: 'meme-1',
  filename: 'sample.png',
  media_url: '/media/meme-1',
  metadata: { status: 'repair_required' },
  visual_embedding_status: 'pending',
  embedding_status: 'blocked',
}

function fullRetryButton(wrapper) {
  return wrapper.findAll('button').find((button) => button.text().includes('完整重试所有未就绪'))
}

describe('LibraryWorkspace', () => {
  beforeEach(() => {
    images.mockReset().mockResolvedValue({ items: [image] })
    unreadyProcessing.mockReset().mockResolvedValue({
      target_count: 0,
      submitted_count: 0,
      reused_count: 0,
      conflict_count: 0,
      failed_count: 0,
      results: [],
    })
    submitImageStage.mockReset().mockResolvedValue({ task_id: 'stage-1' })
  })

  it('完整重试只发送处理选项，不携带当前页筛选或 Meme 列表', async () => {
    const wrapper = mount(LibraryWorkspace, {
      props: { config: { reverse_image_available: true }, cacheTask: null, cacheBusy: false, refreshToken: 0 },
    })
    await flushPromises()
    await wrapper.get('input[aria-label="筛选文件名"]').setValue('当前页筛选')
    await wrapper.get('input[aria-label="筛选文件名"]').trigger('keyup.enter')
    await flushPromises()

    await fullRetryButton(wrapper).trigger('click')
    await wrapper.get('.processing-options-dialog input[value="auto"]').setValue(true)
    await wrapper.get('.processing-options-dialog input[type="checkbox"]').setValue(true)
    await wrapper.get('.processing-options-dialog form').trigger('submit')
    await flushPromises()

    expect(unreadyProcessing).toHaveBeenCalledTimes(1)
    expect(unreadyProcessing).toHaveBeenCalledWith({ reverse_image_policy: 'auto', auto_name: true })
    expect(unreadyProcessing.mock.calls[0][0]).not.toHaveProperty('items')
    expect(unreadyProcessing.mock.calls[0][0]).not.toHaveProperty('search')
    expect(unreadyProcessing.mock.calls[0][0]).not.toHaveProperty('cursor')
  })

  it('按提交、复用、冲突和失败分类展示 scope 级摘要及逐图结果', async () => {
    unreadyProcessing.mockResolvedValue({
      target_count: 4,
      submitted_count: 1,
      reused_count: 1,
      conflict_count: 1,
      failed_count: 1,
      results: [
        { meme_id: 'submitted', status: 'submitted', processing_job_id: 'job-1' },
        { meme_id: 'reused', status: 'reused' },
        { meme_id: 'conflict', category: 'conflict', error: 'processing_options_conflict' },
        { meme_id: 'failed', category: 'failed', error: 'submit_failed' },
      ],
    })
    const wrapper = mount(LibraryWorkspace, {
      props: { config: null, cacheTask: null, cacheBusy: false, refreshToken: 0 },
    })
    await flushPromises()
    await fullRetryButton(wrapper).trigger('click')
    await wrapper.get('.processing-options-dialog form').trigger('submit')
    await flushPromises()

    expect(wrapper.get('.inline-notice').text()).toContain('目标 4，提交 1，复用 1，冲突 1，失败 1')
    const details = wrapper.get('.processing-result-details')
    expect(details.text()).toContain('submitted已提交处理任务')
    expect(details.text()).toContain('reused已复用处理任务')
    expect(details.text()).toContain('conflict选项冲突：processing_options_conflict')
    expect(details.text()).toContain('failed提交失败：submit_failed')
    expect(details.find('li.conflict').exists()).toBe(true)
    expect(details.find('li.failed').exists()).toBe(true)
  })

  it('取消不请求，提交中阻止重复确认，失败后保留选项供安全重试', async () => {
    let resolveRetry
    unreadyProcessing.mockImplementationOnce(() => new Promise((resolve) => { resolveRetry = resolve }))
    const wrapper = mount(LibraryWorkspace, {
      props: { config: { reverse_image_available: true }, cacheTask: null, cacheBusy: false, refreshToken: 0 },
    })
    await flushPromises()

    await fullRetryButton(wrapper).trigger('click')
    await wrapper.get('.processing-options-dialog input[value="auto"]').setValue(true)
    await wrapper.get('.processing-options-dialog input[type="checkbox"]').setValue(true)
    await wrapper.get('.processing-options-dialog form').trigger('submit')
    await wrapper.get('.processing-options-dialog form').trigger('submit')
    expect(unreadyProcessing).toHaveBeenCalledTimes(1)
    expect(wrapper.get('.processing-options-dialog').text()).toContain('提交中...')

    resolveRetry({ target_count: 0, submitted_count: 0, reused_count: 0, conflict_count: 0, failed_count: 0, results: [] })
    await flushPromises()
    expect(wrapper.find('.processing-options-dialog').exists()).toBe(false)

    await fullRetryButton(wrapper).trigger('click')
    expect(wrapper.get('.processing-options-dialog input[value="forbid"]').element.checked).toBe(true)
    expect(wrapper.get('.processing-options-dialog input[value="auto"]').element.checked).toBe(false)
    expect(wrapper.get('.processing-options-dialog input[type="checkbox"]').element.checked).toBe(false)
    await wrapper.get('.processing-options-dialog input[value="forbid"]').trigger('click')
    await wrapper.get('.processing-options-dialog [aria-label="取消图片处理"]').trigger('click')
    expect(unreadyProcessing).toHaveBeenCalledTimes(1)
  })

  it('自动重命名 warning 只走受限图片阶段入口', async () => {
    images.mockResolvedValue({
      items: [{
        ...image,
        processing_status: 'succeeded',
        processing_has_warnings: true,
        processing_stages: [
          { stage: 'auto_rename', status: 'warning', task_id: 'rename-1' },
        ],
      }],
    })
    const wrapper = mount(LibraryWorkspace, {
      props: { config: null, cacheTask: null, cacheBusy: false, refreshToken: 0 },
    })
    await flushPromises()

    expect(wrapper.text()).toContain('核心处理已完成，自动命名未完成')
    const retry = wrapper.get('.stage-actions button')
    expect(retry.text()).toContain('恢复自动命名')
    await retry.trigger('click')
    await flushPromises()

    expect(submitImageStage).toHaveBeenCalledWith({ meme_id: 'meme-1', stage: 'auto_rename', reverse_image_policy: 'forbid' })
  })
})
