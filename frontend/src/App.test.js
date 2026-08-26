/** Vue 核心页面切换和检索流程测试。 */
import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const { search, images, imageMetadata, contextBatch, retryImageStagesBatch, unreadyProcessing, generateCache, pollTask, tasks, task } = vi.hoisted(() => ({ search: vi.fn(), images: vi.fn(), imageMetadata: vi.fn(), contextBatch: vi.fn(), retryImageStagesBatch: vi.fn(), unreadyProcessing: vi.fn(), generateCache: vi.fn(), pollTask: vi.fn(), tasks: vi.fn(), task: vi.fn() }))
vi.mock('./api', () => ({
  api: {
    config: vi.fn(async () => ({ embedding_model: 'test-model', embedding_cache_ready: false })),
    search,
    images,
    imageMetadata,
    contextBatch,
    retryImageStagesBatch,
    unreadyProcessing,
    tasks,
    task,
    generateCache,
  },
  pollTask,
}))

import App from './App.vue'

describe('App', () => {
  beforeEach(() => {
    vi.unstubAllGlobals()
    search.mockReset()
    images.mockReset().mockResolvedValue({ items: [] })
    imageMetadata.mockReset().mockResolvedValue({})
    contextBatch.mockReset().mockResolvedValue({ results: [] })
    retryImageStagesBatch.mockReset().mockResolvedValue({ submitted_count: 0, failed_count: 0, results: [] })
    unreadyProcessing.mockReset().mockResolvedValue({ target_count: 0, submitted_count: 0, reused_count: 0, conflict_count: 0, failed_count: 0, results: [] })
    tasks.mockReset().mockResolvedValue({ items: [], next_cursor: null })
    task.mockReset().mockResolvedValue(null)
    generateCache.mockReset()
    pollTask.mockReset()
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: vi.fn() } })
    Object.defineProperty(window, 'isSecureContext', { configurable: true, value: true })
    vi.stubGlobal('fetch', vi.fn())
    delete globalThis.ClipboardItem
  })

  it('通过唯一 API 请求执行检索并显示结果', async () => {
    search.mockResolvedValue({ results: ['/media/11111111-1111-4111-8111-111111111111'] })
    const wrapper = mount(App)
    await flushPromises()
    await wrapper.get('.search-form input').setValue('开心')
    await wrapper.get('.search-form').trigger('submit')
    await flushPromises()
    expect(search).toHaveBeenCalledWith({ query: '开心', n_results: 8, llm_enhance: false })
    expect(wrapper.get('.result-item img').attributes('src')).toBe('/media/11111111-1111-4111-8111-111111111111')
  })

  it('检索失败后显示可关闭错误并恢复提交按钮', async () => {
    search.mockRejectedValue(new Error('检索服务暂不可用'))
    const wrapper = mount(App)
    await flushPromises()
    await wrapper.get('.search-form input').setValue('开心')
    await wrapper.get('.search-form').trigger('submit')
    await flushPromises()

    expect(wrapper.get('[role="alert"]').text()).toContain('检索服务暂不可用')
    expect(wrapper.get('.search-form .primary').attributes('disabled')).toBeUndefined()
    expect(wrapper.get('.search-form .primary').text()).toBe('开始检索')

    await wrapper.get('[aria-label="关闭错误"]').trigger('click')
    expect(wrapper.find('[role="alert"]').exists()).toBe(false)
  })

  it('对相同媒体路径的查询结果稳定去重', async () => {
    search.mockResolvedValue({ results: ['/media/11111111-1111-4111-8111-111111111111?cache=1', '/media/11111111-1111-4111-8111-111111111111?cache=2', '/media/22222222-2222-4222-8222-222222222222'] })
    const wrapper = mount(App)
    await flushPromises()
    await wrapper.get('.search-form input').setValue('开心')
    await wrapper.get('.search-form').trigger('submit')
    await flushPromises()
    expect(wrapper.findAll('.result-item')).toHaveLength(2)
    expect(wrapper.findAll('.result-item')[0].attributes('aria-label')).toBe('复制检索结果 1')
  })

  it('点击检索结果会复制图片二进制数据且不会复制地址', async () => {
    search.mockResolvedValue({ results: ['/media/11111111-1111-4111-8111-111111111111'] })
    const write = vi.fn().mockResolvedValue(undefined)
    const writeText = navigator.clipboard.writeText
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { write, writeText } })
    class TestClipboardItem {
      constructor(data) { this.data = data }
    }
    vi.stubGlobal('ClipboardItem', TestClipboardItem)
    const imageBlob = new Blob([Uint8Array.from([137, 80, 78, 71])], { type: 'image/png' })
    fetch.mockResolvedValue({ ok: true, blob: vi.fn().mockResolvedValue(imageBlob) })
    const wrapper = mount(App)
    await flushPromises()
    await wrapper.get('.search-form input').setValue('开心')
    await wrapper.get('.search-form').trigger('submit')
    await flushPromises()
    await wrapper.get('.result-item').trigger('click')
    await flushPromises()
    expect(fetch).toHaveBeenCalledWith('/media/11111111-1111-4111-8111-111111111111', { credentials: 'same-origin' })
    expect(write).toHaveBeenCalledTimes(1)
    const [items] = write.mock.calls[0]
    expect(items).toHaveLength(1)
    expect(items[0]).toBeInstanceOf(TestClipboardItem)
    expect(items[0].data).toEqual({ 'image/png': expect.any(Promise) })
    await expect(items[0].data['image/png']).resolves.toBe(imageBlob)
    expect(writeText).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('图片已复制')
  })

  it('浏览器不支持图片剪贴板时显示失败且不复制地址', async () => {
    search.mockResolvedValue({ results: ['/media/11111111-1111-4111-8111-111111111111'] })
    const writeText = navigator.clipboard.writeText
    const wrapper = mount(App)
    await flushPromises()
    await wrapper.get('.search-form input').setValue('开心')
    await wrapper.get('.search-form').trigger('submit')
    await flushPromises()
    await wrapper.get('.result-item').trigger('click')
    await flushPromises()
    expect(fetch).not.toHaveBeenCalled()
    expect(writeText).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('当前浏览器不支持复制图片')
  })

  it('图片加载失败时不写入剪贴板或复制地址', async () => {
    search.mockResolvedValue({ results: ['/media/11111111-1111-4111-8111-111111111111'] })
    const write = vi.fn(async ([item]) => item.data['image/png'])
    const writeText = navigator.clipboard.writeText
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { write, writeText } })
    class TestClipboardItem {
      constructor(data) { this.data = data }
    }
    vi.stubGlobal('ClipboardItem', TestClipboardItem)
    fetch.mockRejectedValue(new TypeError('network error'))
    const wrapper = mount(App)
    await flushPromises()
    await wrapper.get('.search-form input').setValue('开心')
    await wrapper.get('.search-form').trigger('submit')
    await flushPromises()
    await wrapper.get('.result-item').trigger('click')
    await flushPromises()
    expect(write).toHaveBeenCalledTimes(1)
    expect(writeText).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('图片加载失败，无法复制')
  })

  it('图片 MIME 不可用或 Chrome 拒绝剪贴板时均显示失败原因', async () => {
    search.mockResolvedValue({ results: ['/media/11111111-1111-4111-8111-111111111111'] })
    const write = vi.fn(async ([item]) => item.data['image/png'])
    const writeText = navigator.clipboard.writeText
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { write, writeText } })
    class TestClipboardItem {
      constructor(data) { this.data = data }
    }
    vi.stubGlobal('ClipboardItem', TestClipboardItem)
    fetch.mockResolvedValue({ ok: true, blob: vi.fn().mockResolvedValue(new Blob(['data'])) })
    const wrapper = mount(App)
    await flushPromises()
    await wrapper.get('.search-form input').setValue('开心')
    await wrapper.get('.search-form').trigger('submit')
    await flushPromises()
    await wrapper.get('.result-item').trigger('click')
    await flushPromises()
    expect(write).toHaveBeenCalledTimes(1)
    expect(writeText).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('图片 MIME 类型不可用，无法复制')

    fetch.mockResolvedValue({ ok: true, blob: vi.fn().mockResolvedValue(new Blob(['data'], { type: 'image/png' })) })
    write.mockRejectedValue(new DOMException('被 Chrome 拒绝', 'NotAllowedError'))
    await wrapper.get('.result-item').trigger('click')
    await flushPromises()
    expect(writeText).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('Chrome 拒绝剪贴板写入，请在网站设置中允许剪贴板后重试')
  })

  it('切换图片库时加载图片列表', async () => {
    const wrapper = mount(App)
    await flushPromises()
    const library = wrapper.findAll('.sidebar nav button').find((button) => button.text().includes('图片库'))
    await library.trigger('click')
    await flushPromises()
    expect(wrapper.get('h1').text()).toBe('图片库')
    expect(wrapper.text()).not.toContain('浏览、筛选和整理本地图片')
  })

  it('仅在图片库工具栏提供缓存生成入口', async () => {
    const wrapper = mount(App)
    await flushPromises()
    expect(wrapper.find('.sidebar .cache-button').exists()).toBe(false)
    expect(wrapper.find('.cache-action').exists()).toBe(false)
    await wrapper.findAll('.sidebar nav button').find((button) => button.text().includes('图片库')).trigger('click')
    await flushPromises()
    expect(wrapper.findAll('.cache-action')).toHaveLength(1)
    expect(wrapper.get('.cache-action').text()).toBe('生成检索缓存')
  })

  it('生成缓存期间禁用按钮并在失败后提供重试状态', async () => {
    let finishPoll
    generateCache.mockResolvedValue({ task_id: 'cache-1', status: 'queued', progress: 0 })
    pollTask.mockImplementation((taskId, onUpdate) => new Promise((resolve) => {
      finishPoll = (next) => { onUpdate(next); resolve(next) }
    }))
    const wrapper = mount(App)
    await flushPromises()
    await wrapper.findAll('.sidebar nav button').find((button) => button.text().includes('图片库')).trigger('click')
    await flushPromises()
    const button = wrapper.get('.cache-action')
    await button.trigger('click')
    await flushPromises()
    expect(button.element.disabled).toBe(true)
    expect(button.text()).toBe('排队中...')

    await wrapper.findAll('.sidebar nav button').find((item) => item.text().includes('检索')).trigger('click')
    await wrapper.findAll('.sidebar nav button').find((item) => item.text().includes('图片库')).trigger('click')
    await flushPromises()
    expect(wrapper.get('.cache-action').element.disabled).toBe(true)
    expect(wrapper.get('.cache-action').text()).toBe('排队中...')
    expect(generateCache).toHaveBeenCalledTimes(1)

    finishPoll({ task_id: 'cache-1', status: 'failed', progress: 0.4, message: '任务执行失败', error: { message: 'image_library_empty' } })
    await flushPromises()
    expect(wrapper.get('.cache-action').element.disabled).toBe(false)
    expect(wrapper.get('.cache-action').text()).toBe('重新生成检索缓存')
    expect(wrapper.get('.cache-status').text()).toContain('任务执行失败')
    expect(wrapper.get('.embedding-global').text()).toBe('Embedding 生成失败')
  })

  it('任务列表和详情展示完整的 Agent 工作回合摘要', async () => {
    const activity = {
      task_id: 'context-activity', task_type: 'meme_context_generation', status: 'running', progress: 0.1,
      message: '正在提交 Agent executor 任务', image: { filename: 'sample.png', meme_id: 'sample' },
      agent_completed_turns: 3, agent_turn_running: true, agent_last_activity_at: new Date().toISOString(),
    }
    tasks.mockResolvedValue({ items: [activity], next_cursor: null })
    task.mockResolvedValue(activity)
    const wrapper = mount(App)
    await flushPromises()
    await wrapper.findAll('.sidebar nav button').find((button) => button.text().includes('处理任务')).trigger('click')
    await flushPromises()
    expect(wrapper.get('.task-activity').text()).toContain('第 4 轮进行中')
    await wrapper.get('.task-row').trigger('click')
    await flushPromises()
    expect(wrapper.get('.task-activity-detail').text()).toContain('Agent 工作回合')
    expect(wrapper.get('.task-activity-detail').text()).toContain('第 4 轮进行中')
  })

  it('可选择未就绪图片并提交定向重试任务，同时显示文本和图片 embedding 状态', async () => {
    images.mockResolvedValue({
      items: [
        { meme_id: '33333333-3333-4333-8333-333333333333', filename: 'pending.png', size: 10, extension: '.png', media_url: '/media/33333333-3333-4333-8333-333333333333', metadata: { status: 'repair_required' }, embedding_status: 'blocked', visual_embedding_status: 'pending' },
        { meme_id: '44444444-4444-4444-8444-444444444444', filename: 'ready.png', size: 10, extension: '.png', media_url: '/media/44444444-4444-4444-8444-444444444444', metadata: { status: 'ready' }, embedding_status: 'ready', visual_embedding_status: 'ready' },
      ],

    })
    const wrapper = mount(App)
    await flushPromises()
    await wrapper.findAll('.sidebar nav button').find((button) => button.text().includes('图片库')).trigger('click')
    await flushPromises()
    const checkboxes = wrapper.findAll('.image-check input')
    expect(checkboxes).toHaveLength(2)
    expect(checkboxes[1].element.disabled).toBe(false)
    await checkboxes[0].setValue(true)
    const retrySelectedButton = wrapper.findAll('.toolbar button').find((button) => button.text().includes('重试选中'))
    expect(retrySelectedButton).toBeDefined()
    await retrySelectedButton.trigger('click')
    await wrapper.get('.retry-selected-dialog form').trigger('submit')
    await flushPromises()
    expect(contextBatch).toHaveBeenCalledWith({ items: [{ meme_id: '33333333-3333-4333-8333-333333333333' }], include_unready: true })
    expect(wrapper.text()).toContain('文本索引已就绪')
    expect(wrapper.findAll('.visual-embedding-state')[0].text()).toBe('图片向量待生成')
    expect(wrapper.findAll('.visual-embedding-state')[1].text()).toBe('图片向量已就绪')
  })

  it('图片库点击图片会打开放大预览并分层显示元数据', async () => {
    images.mockResolvedValue({
      items: [{ meme_id: '55555555-5555-4555-8555-555555555555', filename: 'pending.png', size: 10, extension: '.png', media_url: '/media/55555555-5555-4555-8555-555555555555', metadata: { status: 'pending' }, embedding_status: 'pending', visual_embedding_status: 'pending' }],
    })
    imageMetadata.mockResolvedValue({ schema_version: 1, image: { relative_path: 'work/pending.png' }, context_status: 'pending', meme_context: { summary: '等待处理' } })
    const wrapper = mount(App, { attachTo: document.body })
    await flushPromises()
    await wrapper.findAll('.sidebar nav button').find((button) => button.text().includes('图片库')).trigger('click')
    await flushPromises()
    const previewTrigger = wrapper.get('.library-preview-trigger')
    await previewTrigger.trigger('click')
    await flushPromises()
    expect(imageMetadata).toHaveBeenCalledWith('55555555-5555-4555-8555-555555555555')
    expect(wrapper.get('[role="dialog"] .image-dialog-preview img').attributes('src')).toBe('/media/55555555-5555-4555-8555-555555555555')
    expect(wrapper.get('.metadata-empty-state').text()).toBe('图片语境尚未生成，完成处理后会显示识别结果')
    const metadataDetails = wrapper.findAll('.metadata-details')
    expect(metadataDetails).toHaveLength(2)
    expect(metadataDetails.every((detail) => detail.element.open)).toBe(false)
    expect(document.activeElement).toBe(wrapper.get('[aria-label="关闭图片预览"]').element)
    expect(wrapper.findAll('button').some((button) => button.text().includes('复制元数据'))).toBe(false)
    await metadataDetails[1].get('summary').trigger('click')
    expect(wrapper.get('.metadata-json').text()).toContain('等待处理')
    await wrapper.get('[aria-label="关闭图片预览"]').trigger('click')
    expect(wrapper.find('[role="dialog"]').exists()).toBe(false)
    expect(document.activeElement).toBe(previewTrigger.element)
    wrapper.unmount()
  })

})
