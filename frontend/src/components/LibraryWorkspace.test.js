/** 图片库完整重试、阶段恢复和共享选项对话框的行为测试。 */
import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const { images, imageMetadata, contextBatch, retryImageStagesBatch, unreadyProcessing, submitImageStage } = vi.hoisted(() => ({
  images: vi.fn(),
  imageMetadata: vi.fn(),
  contextBatch: vi.fn(),
  retryImageStagesBatch: vi.fn(),
  unreadyProcessing: vi.fn(),
  submitImageStage: vi.fn(),
}))

vi.mock('../api', () => ({
  api: { images, imageMetadata, contextBatch, retryImageStagesBatch, unreadyProcessing, submitImageStage },
}))

import { api } from '../api'
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
    imageMetadata.mockReset().mockResolvedValue({})
    contextBatch.mockReset().mockResolvedValue({ results: [{ meme_id: 'meme-1', task_id: 'job-1' }] })
    retryImageStagesBatch.mockReset().mockResolvedValue({ submitted_count: 0, failed_count: 0, results: [] })
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

  it('默认显示可选择列表并移除选择图片入口，完整重试保持旧请求契约', async () => {
    const wrapper = mount(LibraryWorkspace, {
      props: { config: { reverse_image_available: true }, cacheTask: null, cacheBusy: false, refreshToken: 0 },
    })
    await flushPromises()

    expect(wrapper.findAll('.image-check input')).toHaveLength(1)
    expect(wrapper.text()).not.toContain('选择图片')

    await wrapper.get('.image-check input').setValue(true)
    await wrapper.findAll('button').find((button) => button.text().includes('重试选中')).trigger('click')
    expect(wrapper.get('.retry-selected-dialog').text()).toContain('完整重试')
    await wrapper.get('.retry-selected-dialog form').trigger('submit')
    await flushPromises()
    expect(contextBatch).toHaveBeenCalledWith({ items: [{ meme_id: 'meme-1' }], include_unready: true })
  })

  it('指定部分允许多选三个核心阶段，并发送准确阶段载荷', async () => {
    const wrapper = mount(LibraryWorkspace, {
      props: { config: null, cacheTask: null, cacheBusy: false, refreshToken: 0 },
    })
    await flushPromises()
    await wrapper.get('.image-check input').setValue(true)
    await wrapper.findAll('button').find((button) => button.text().includes('重试选中')).trigger('click')
    await wrapper.get('.retry-selected-dialog input[value="parts"]').setValue(true)
    await wrapper.get('.retry-selected-dialog input[value="agent"]').setValue(true)
    await wrapper.get('.retry-selected-dialog input[value="text_embedding"]').setValue(true)
    await wrapper.get('.retry-selected-dialog form').trigger('submit')
    await flushPromises()

    expect(retryImageStagesBatch).toHaveBeenCalledWith({
      items: [{ meme_id: 'meme-1' }],
      stages: ['agent', 'text_embedding'],
    })
    expect(contextBatch).not.toHaveBeenCalled()
  })

  it('指定部分没有阶段时不可提交，取消后重新打开恢复完整模式', async () => {
    const wrapper = mount(LibraryWorkspace, {
      props: { config: null, cacheTask: null, cacheBusy: false, refreshToken: 0 },
    })
    await flushPromises()
    expect(wrapper.findAll('button').find((button) => button.text().includes('重试选中')).element.disabled).toBe(true)

    await wrapper.get('.image-check input').setValue(true)
    await wrapper.findAll('button').find((button) => button.text().includes('重试选中')).trigger('click')
    await wrapper.get('.retry-selected-dialog input[value="parts"]').setValue(true)
    expect(wrapper.get('.retry-selected-dialog button[type="submit"]').element.disabled).toBe(true)
    await wrapper.get('[aria-label="取消重试选中"]').trigger('click')

    await wrapper.findAll('button').find((button) => button.text().includes('重试选中')).trigger('click')
    expect(wrapper.get('.retry-selected-dialog input[value="full"]').element.checked).toBe(true)
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

  it('即使完整重试能力尚未注入，点击主按钮也先显示图片处理选项且不请求', async () => {
    const submitUnreadyProcessing = api.unreadyProcessing
    delete api.unreadyProcessing
    try {
      const wrapper = mount(LibraryWorkspace, {
        props: { config: null, cacheTask: null, cacheBusy: false, refreshToken: 0 },
      })
      await flushPromises()

      await fullRetryButton(wrapper).trigger('click')

      expect(wrapper.get('[role="dialog"][aria-labelledby="processing-options-title"]').text()).toContain('图片处理选项')
      expect(unreadyProcessing).not.toHaveBeenCalled()
      await wrapper.get('[aria-label="取消图片处理"]').trigger('click')
      expect(unreadyProcessing).not.toHaveBeenCalled()
      wrapper.unmount()
    } finally {
      api.unreadyProcessing = submitUnreadyProcessing
    }
  })

  it('提交失败时保留已选处理选项，允许用户再次确认', async () => {
    unreadyProcessing.mockRejectedValueOnce(new Error('network_failed'))
    const wrapper = mount(LibraryWorkspace, {
      props: { config: { reverse_image_available: true }, cacheTask: null, cacheBusy: false, refreshToken: 0 },
    })
    await flushPromises()

    await fullRetryButton(wrapper).trigger('click')
    await wrapper.get('.processing-options-dialog input[value="auto"]').setValue(true)
    await wrapper.get('.processing-options-dialog input[type="checkbox"]').setValue(true)
    await wrapper.get('.processing-options-dialog form').trigger('submit')
    await flushPromises()

    expect(wrapper.get('.processing-options-dialog input[value="auto"]').element.checked).toBe(true)
    expect(wrapper.get('.processing-options-dialog input[type="checkbox"]').element.checked).toBe(true)
    expect(unreadyProcessing).toHaveBeenCalledTimes(1)

    await wrapper.get('.processing-options-dialog form').trigger('submit')
    await flushPromises()
    expect(unreadyProcessing).toHaveBeenCalledTimes(2)
    wrapper.unmount()
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

  it('列表行只保留三项状态摘要，阶段明细和恢复入口迁移到详情', async () => {
    images.mockResolvedValue({
      items: [{
        ...image,
        processing_status: 'succeeded',
        processing_has_warnings: true,
        processing_stages: [
          { stage: 'visual', status: 'failed', error: { error: 'visual_failed' } },
          { stage: 'agent', status: 'blocked', error: { message: 'agent_blocked' } },
          { stage: 'text_embedding', status: 'unknown_execution' },
          { stage: 'auto_rename', status: 'warning', task_id: 'rename-1' },
        ],
      }],
    })
    const wrapper = mount(LibraryWorkspace, {
      props: { config: null, cacheTask: null, cacheBusy: false, refreshToken: 0 },
      attachTo: document.body,
    })
    await flushPromises()

    expect(wrapper.get('.image-status-summary').findAll('span')).toHaveLength(3)
    expect(wrapper.find('.image-processing-stages').exists()).toBe(false)
    expect(wrapper.find('.stage-actions').exists()).toBe(false)
    expect(wrapper.get('.image-row-actions').text()).toContain('查看详情')
    expect(wrapper.get('.image-row-actions').text()).toContain('重命名')

    const detailsTrigger = wrapper.get('.metadata-button')
    await detailsTrigger.trigger('click')
    await flushPromises()
    const details = wrapper.get('.image-processing-details')
    expect(details.text()).toContain('核心处理已完成，自动命名未完成')
    expect(details.text()).toContain('视觉向量')
    expect(details.text()).toContain('visual_failed')
    expect(details.text()).toContain('agent_blocked')
    expect(details.text()).toContain('执行状态未知，需人工确认')
    expect(details.text()).toContain('恢复自动命名')
    expect(details.findAll('.image-processing-stage-retry')).toHaveLength(4)

    for (const label of ['仅视觉', '仅 Agent', '仅文本']) {
      const button = details.findAll('.image-processing-stage-retry').find((candidate) => candidate.text().includes(label))
      expect(button).toBeDefined()
      await button.trigger('click')
      await flushPromises()
    }
    const retry = details.findAll('.image-processing-stage-retry').find((button) => button.text().includes('恢复自动命名'))
    expect(retry).toBeDefined()
    await retry.trigger('click')
    await flushPromises()

    expect(submitImageStage).toHaveBeenNthCalledWith(1, { meme_id: 'meme-1', stage: 'visual', reverse_image_policy: 'forbid' })
    expect(submitImageStage).toHaveBeenNthCalledWith(2, { meme_id: 'meme-1', stage: 'agent', reverse_image_policy: 'forbid' })
    expect(submitImageStage).toHaveBeenNthCalledWith(3, { meme_id: 'meme-1', stage: 'text_embedding', reverse_image_policy: 'forbid' })
    expect(submitImageStage).toHaveBeenCalledWith({ meme_id: 'meme-1', stage: 'auto_rename', reverse_image_policy: 'forbid' })
    await wrapper.get('[aria-label="关闭图片预览"]').trigger('click')
    expect(document.activeElement).toBe(detailsTrigger.element)
    wrapper.unmount()
  })

  it('没有阶段记录时详情仍提供可读状态，而不虚构恢复操作', async () => {
    images.mockResolvedValue({ items: [{ ...image, processing_status: 'queued' }] })
    const wrapper = mount(LibraryWorkspace, {
      props: { config: null, cacheTask: null, cacheBusy: false, refreshToken: 0 },
      attachTo: document.body,
    })
    await flushPromises()

    await wrapper.get('.metadata-button').trigger('click')
    await flushPromises()
    expect(wrapper.get('.image-processing-details').text()).toContain('排队中')
    expect(wrapper.get('.processing-details-empty').text()).toBe('暂无处理阶段记录')
    expect(wrapper.find('.image-processing-stage-retry').exists()).toBe(false)
    await wrapper.get('[aria-label="关闭图片预览"]').trigger('click')
    wrapper.unmount()
  })
})
