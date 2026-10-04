/** 图片库完整重试、阶段恢复和共享选项对话框的行为测试。 */
import { flushPromises, mount } from '@vue/test-utils'
import { toast } from 'vue-sonner'
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

import LibraryWorkspace from './LibraryWorkspace.vue'

const image = {
  meme_id: 'meme-1',
  filename: 'sample.png',
  media_url: '/media/meme-1',
  metadata: { status: 'repair_required' },
  visual_embedding_status: 'pending',
  embedding_status: 'blocked',
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
      not_needed_count: 0,
      failed_count: 0,
      results: [],
    })
    submitImageStage.mockReset().mockResolvedValue({ task_id: 'stage-1' })
  })

  it('默认显示可选择列表，完整重试先确认选项再只提交原选中图片', async () => {
    const secondImage = { ...image, meme_id: 'meme-2', filename: 'other.png', media_url: '/media/meme-2' }
    images.mockResolvedValue({ items: [image, secondImage] })
    const wrapper = mount(LibraryWorkspace, {
      props: { config: { reverse_image_available: true }, cacheTask: null, cacheBusy: false, refreshToken: 0 },
    })
    await flushPromises()

    expect(wrapper.findAll('.image-check input')).toHaveLength(2)
    expect(wrapper.text()).not.toContain('选择图片')

    await wrapper.find('.image-check input').setValue(true)
    await wrapper.findAll('button').find((button) => button.text().includes('重试选中')).trigger('click')
    expect(wrapper.get('.retry-selected-dialog').text()).toContain('完整重试')
    await wrapper.get('.retry-selected-dialog form').trigger('submit')
    expect(wrapper.find('.retry-selected-dialog').exists()).toBe(false)
    expect(wrapper.get('.processing-options-dialog').text()).toContain('图片处理选项')
    expect(contextBatch).not.toHaveBeenCalled()
    await wrapper.get('.processing-options-dialog form').trigger('submit')
    await flushPromises()
    expect(contextBatch).toHaveBeenCalledWith({
      items: [{ meme_id: 'meme-1' }],
      include_unready: true,
      reverse_image_policy: 'forbid',
      auto_name: false,
    })
    expect(wrapper.findAll('.image-check input')[0].element.checked).toBe(false)
    expect(wrapper.findAll('.image-check input')[1].element.checked).toBe(false)
  })

  it('指定部分包含 Agent 时先确认选项，并发送准确阶段和选项载荷', async () => {
    const wrapper = mount(LibraryWorkspace, {
      props: { config: { reverse_image_available: true }, cacheTask: null, cacheBusy: false, refreshToken: 0 },
    })
    await flushPromises()
    await wrapper.get('.image-check input').setValue(true)
    await wrapper.findAll('button').find((button) => button.text().includes('重试选中')).trigger('click')
    await wrapper.get('.retry-selected-dialog input[value="parts"]').setValue(true)
    await wrapper.get('.retry-selected-dialog input[value="agent"]').setValue(true)
    await wrapper.get('.retry-selected-dialog input[value="text_embedding"]').setValue(true)
    await wrapper.get('.retry-selected-dialog form').trigger('submit')
    expect(wrapper.find('.retry-selected-dialog').exists()).toBe(false)
    expect(wrapper.get('.processing-options-dialog').exists()).toBe(true)
    expect(retryImageStagesBatch).not.toHaveBeenCalled()
    await wrapper.get('.processing-options-dialog input[type="checkbox"]').setValue(true)
    await wrapper.get('.processing-options-dialog form').trigger('submit')
    await flushPromises()

    expect(retryImageStagesBatch).toHaveBeenCalledWith({
      items: [{ meme_id: 'meme-1' }],
      stages: ['agent', 'text_embedding'],
      reverse_image_policy: 'forbid',
      auto_name: true,
    })
    expect(contextBatch).not.toHaveBeenCalled()
  })

  it('指定部分不包含 Agent 时保持直接提交旧载荷', async () => {
    const wrapper = mount(LibraryWorkspace, {
      props: { config: { reverse_image_available: true }, cacheTask: null, cacheBusy: false, refreshToken: 0 },
    })
    await flushPromises()
    await wrapper.get('.image-check input').setValue(true)
    await wrapper.findAll('button').find((button) => button.text().includes('重试选中')).trigger('click')
    await wrapper.get('.retry-selected-dialog input[value="parts"]').setValue(true)
    await wrapper.get('.retry-selected-dialog input[value="visual"]').setValue(true)
    await wrapper.get('.retry-selected-dialog input[value="text_embedding"]').setValue(true)
    await wrapper.get('.retry-selected-dialog form').trigger('submit')
    await flushPromises()

    expect(wrapper.find('.processing-options-dialog').exists()).toBe(false)
    expect(retryImageStagesBatch).toHaveBeenCalledWith({
      items: [{ meme_id: 'meme-1' }],
      stages: ['visual', 'text_embedding'],
    })
    expect(contextBatch).not.toHaveBeenCalled()
    expect(wrapper.get('.image-check input').element.checked).toBe(false)
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

  it('完整重试按逐图分类展示提交、冲突和失败', async () => {
    contextBatch.mockResolvedValue({
      results: [
        { meme_id: 'meme-1', category: 'submitted', processing_job_id: 'job-1' },
        { meme_id: 'meme-2', category: 'processing_active', reason: 'image_processing_active' },
        { meme_id: 'meme-3', category: 'failed', reason: 'target_changed' },
      ],
    })
    const wrapper = mount(LibraryWorkspace, {
      props: { config: { reverse_image_available: true }, cacheTask: null, cacheBusy: false, refreshToken: 0 },
    })
    await flushPromises()

    await wrapper.get('.image-check input').setValue(true)
    await wrapper.findAll('button').find((button) => button.text().includes('重试选中')).trigger('click')
    await wrapper.get('.retry-selected-dialog form').trigger('submit')
    await wrapper.get('.processing-options-dialog form').trigger('submit')
    await flushPromises()

    expect(toast.getToasts().some((item) => String(item.title).includes('提交 1 个完整任务，处理中 1，无需修复 0，失败 1') && item.type === 'warning')).toBe(true)
    expect(wrapper.get('.processing-result-details').text()).toContain('正在处理：image_processing_active')
    expect(wrapper.get('.processing-result-details').text()).toContain('提交失败：target_changed')
  })

  it('选中重试取消或失败时保留图片选择和已确认选项', async () => {
    contextBatch.mockRejectedValueOnce(new Error('selected_failed'))
    const wrapper = mount(LibraryWorkspace, {
      props: { config: { reverse_image_available: true }, cacheTask: null, cacheBusy: false, refreshToken: 0 },
    })
    await flushPromises()

    await wrapper.get('.image-check input').setValue(true)
    await wrapper.findAll('button').find((button) => button.text().includes('重试选中')).trigger('click')
    await wrapper.get('.retry-selected-dialog form').trigger('submit')
    await wrapper.get('.processing-options-dialog input[value="auto"]').setValue(true)
    await wrapper.get('.processing-options-dialog input[type="checkbox"]').setValue(true)
    await wrapper.get('.processing-options-dialog form').trigger('submit')
    await flushPromises()

    expect(wrapper.get('.processing-options-dialog input[value="auto"]').element.checked).toBe(true)
    expect(wrapper.get('.processing-options-dialog input[type="checkbox"]').element.checked).toBe(true)
    expect(wrapper.get('.image-check input').element.checked).toBe(true)
    expect(contextBatch).toHaveBeenCalledTimes(1)

    await wrapper.get('[aria-label="取消图片处理"]').trigger('click')
    expect(wrapper.find('.processing-options-dialog').exists()).toBe(false)
    expect(wrapper.get('.image-check input').element.checked).toBe(true)
    wrapper.unmount()
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

    for (const label of ['仅生成视觉向量', '仅 Agent', '仅文本']) {
      const button = details.findAll('.image-processing-stage-retry').find((candidate) => candidate.text().includes(label))
      expect(button).toBeDefined()
      await button.trigger('click')
      await flushPromises()
    }
    const retry = details.findAll('.image-processing-stage-retry').find((button) => button.text().includes('恢复自动命名'))
    expect(retry).toBeDefined()
    await retry.trigger('click')
    await flushPromises()

    expect(submitImageStage).toHaveBeenNthCalledWith(1, { meme_id: 'meme-1', stage: 'visual' })
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

  it('列表优先显示可用缩略图，缩略图失败回退原图且完整预览继续使用原图', async () => {
    images.mockResolvedValue({
      items: [{
        ...image,
        thumbnail: { status: 'available', media_url: '/media/meme-1/thumbnail', width: 320, height: 160, media_type: 'image/png' },
      }],
    })
    const wrapper = mount(LibraryWorkspace, {
      props: { config: null, cacheTask: null, cacheBusy: false, refreshToken: 0 },
      attachTo: document.body,
    })
    await flushPromises()

    const listImage = wrapper.get('.library-preview-trigger img')
    expect(listImage.attributes('src')).toBe('/media/meme-1/thumbnail')
    await listImage.trigger('error')
    expect(listImage.attributes('src')).toBe('/media/meme-1')
    await listImage.trigger('error')
    expect(wrapper.get('.resilient-image-placeholder').text()).toBe('图片暂不可用')

    await wrapper.get('.metadata-button').trigger('click')
    await flushPromises()
    expect(wrapper.get('.image-dialog-preview img').attributes('src')).toBe('/media/meme-1')
    await wrapper.get('[aria-label="关闭图片预览"]').trigger('click')
    wrapper.unmount()
  })

  it('pending、failed 和 stale 缩略图状态均直接使用原图展示', async () => {
    images.mockResolvedValue({
      items: [{ ...image, thumbnail: { status: 'failed', media_url: null } }],
    })
    const wrapper = mount(LibraryWorkspace, {
      props: { config: null, cacheTask: null, cacheBusy: false, refreshToken: 0 },
    })
    await flushPromises()
    expect(wrapper.get('.library-preview-trigger img').attributes('src')).toBe('/media/meme-1')
    wrapper.unmount()
  })

  it('总数收缩导致当前页失效时自动回到合法末页并重新读取', async () => {
    const pageOneImage = { ...image, meme_id: 'meme-1' }
    const pageTwoImage = { ...image, meme_id: 'meme-51', filename: 'page-51.png', media_url: '/media/meme-51' }
    images
      .mockResolvedValueOnce({ items: [pageOneImage], total: 51, page: 1, page_size: 50 })
      .mockResolvedValueOnce({ items: [pageTwoImage], total: 51, page: 2, page_size: 50 })
      .mockResolvedValueOnce({ items: [], total: 1, page: 2, page_size: 50 })
      .mockResolvedValueOnce({ items: [pageOneImage], total: 1, page: 1, page_size: 50 })
    const wrapper = mount(LibraryWorkspace, {
      props: { config: null, cacheTask: null, cacheBusy: false, refreshToken: 0 },
    })
    await flushPromises()

    await wrapper.get('.library-pagination button:last-child').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('第 2 / 2 页，共 51 张')

    await wrapper.get('.toolbar button').trigger('click')
    await flushPromises()

    expect(images).toHaveBeenNthCalledWith(3, { search: '', page: 2, page_size: 50, sort: 'name_asc' })
    expect(images).toHaveBeenNthCalledWith(4, { search: '', page: 1, page_size: 50, sort: 'name_asc' })
    expect(wrapper.text()).toContain('第 1 / 1 页，共 1 张')
    expect(wrapper.get('.library-preview-trigger img').attributes('src')).toBe('/media/meme-1')
    wrapper.unmount()
  })
})
