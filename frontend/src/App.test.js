/** Vue 核心页面切换和检索流程测试。 */
import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const { search, images, imageMetadata, contextBatch, generateCache, pollTask, backendSettings, updateBackendSettings } = vi.hoisted(() => ({ search: vi.fn(), images: vi.fn(), imageMetadata: vi.fn(), contextBatch: vi.fn(), generateCache: vi.fn(), pollTask: vi.fn(), backendSettings: vi.fn(), updateBackendSettings: vi.fn() }))
vi.mock('./api', () => ({
  api: {
    config: vi.fn(async () => ({ embedding_model: 'test-model', embedding_cache_ready: false })),
    search,
    images,
    imageMetadata,
    contextBatch,
    generateCache,
    backendSettings,
    updateBackendSettings,
  },
  pollTask,
}))

import App from './App.vue'

describe('App', () => {
  beforeEach(() => {
    vi.unstubAllGlobals()
    search.mockReset()
    images.mockReset().mockResolvedValue({ items: [], directories: [] })
    imageMetadata.mockReset().mockResolvedValue({})
    contextBatch.mockReset().mockResolvedValue({ results: [] })
    generateCache.mockReset()
    backendSettings.mockReset()
    updateBackendSettings.mockReset()
    pollTask.mockReset()
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: vi.fn() } })
    Object.defineProperty(window, 'isSecureContext', { configurable: true, value: true })
    vi.stubGlobal('fetch', vi.fn())
    delete globalThis.ClipboardItem
  })

  it('通过唯一 API 请求执行检索并显示结果', async () => {
    search.mockResolvedValue({ results: ['/media/a.png'] })
    const wrapper = mount(App)
    await flushPromises()
    await wrapper.get('.search-form input').setValue('开心')
    await wrapper.get('.search-form').trigger('submit')
    await flushPromises()
    expect(search).toHaveBeenCalledWith({ query: '开心', n_results: 8, llm_enhance: false })
    expect(wrapper.get('.result-item img').attributes('src')).toBe('/media/a.png')
  })

  it('对相同媒体路径的查询结果稳定去重', async () => {
    search.mockResolvedValue({ results: ['/media/a.png?cache=1', '/media/a.png?cache=2', '/media/b.png'] })
    const wrapper = mount(App)
    await flushPromises()
    await wrapper.get('.search-form input').setValue('开心')
    await wrapper.get('.search-form').trigger('submit')
    await flushPromises()
    expect(wrapper.findAll('.result-item')).toHaveLength(2)
    expect(wrapper.findAll('.result-item')[0].attributes('aria-label')).toBe('复制检索结果 1')
  })

  it('点击检索结果会复制图片二进制数据且不会复制地址', async () => {
    search.mockResolvedValue({ results: ['/media/a.png'] })
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
    expect(fetch).toHaveBeenCalledWith('/media/a.png', { credentials: 'same-origin' })
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
    search.mockResolvedValue({ results: ['/media/a.png'] })
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
    search.mockResolvedValue({ results: ['/media/a.png'] })
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
    search.mockResolvedValue({ results: ['/media/a.png'] })
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
    expect(wrapper.text()).toContain('浏览、筛选和整理本地图片')
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
    finishPoll({ task_id: 'cache-1', status: 'failed', progress: 0.4, message: '任务执行失败', error: { message: 'image_library_empty' } })
    await flushPromises()
    expect(button.element.disabled).toBe(false)
    expect(button.text()).toBe('重新生成检索缓存')
    expect(wrapper.get('.cache-status').text()).toContain('任务执行失败')
    expect(wrapper.get('.embedding-global').text()).toBe('Embedding 生成失败')
  })

  it('可选择未就绪图片并提交定向重试任务，同时显示 embedding 状态', async () => {
    images.mockResolvedValue({
      items: [
        { directory: '', filename: 'pending.png', size: 10, extension: '.png', media_url: '/media/pending.png', metadata: { status: 'pending' }, embedding_status: 'blocked' },
        { directory: '', filename: 'ready.png', size: 10, extension: '.png', media_url: '/media/ready.png', metadata: { status: 'ready' }, embedding_status: 'ready' },
      ],
      directories: [],
    })
    const wrapper = mount(App)
    await flushPromises()
    await wrapper.findAll('.sidebar nav button').find((button) => button.text().includes('图片库')).trigger('click')
    await flushPromises()
    await wrapper.get('.toolbar .quiet').trigger('click')
    const checkboxes = wrapper.findAll('.image-check input')
    expect(checkboxes).toHaveLength(2)
    expect(checkboxes[1].element.disabled).toBe(true)
    await checkboxes[0].setValue(true)
    await wrapper.get('.toolbar button:nth-last-child(2)').trigger('click')
    await flushPromises()
    expect(contextBatch).toHaveBeenCalledWith({ items: [{ directory: '', filename: 'pending.png' }], include_unready: true })
    expect(wrapper.text()).toContain('已索引')
  })

  it('图片库点击图片会打开放大预览并显示完整 JSON', async () => {
    images.mockResolvedValue({
      items: [{ directory: 'work', filename: 'pending.png', size: 10, extension: '.png', media_url: '/media/work/pending.png', metadata: { status: 'pending' }, embedding_status: 'pending' }],
      directories: [],
    })
    imageMetadata.mockResolvedValue({ schema_version: 1, image: { relative_path: 'work/pending.png' }, context_status: 'pending', meme_context: { summary: '等待处理' } })
    const wrapper = mount(App, { attachTo: document.body })
    await flushPromises()
    await wrapper.findAll('.sidebar nav button').find((button) => button.text().includes('图片库')).trigger('click')
    await flushPromises()
    const previewTrigger = wrapper.get('.library-preview-trigger')
    await previewTrigger.trigger('click')
    await flushPromises()
    expect(imageMetadata).toHaveBeenCalledWith({ directory: 'work', filename: 'pending.png' })
    expect(wrapper.get('[role="dialog"] .image-dialog-preview img').attributes('src')).toBe('/media/work/pending.png')
    expect(wrapper.get('.metadata-json').text()).toContain('等待处理')
    expect(document.activeElement).toBe(wrapper.get('[aria-label="关闭图片预览"]').element)
    await wrapper.get('.metadata-panel .quiet').trigger('click')
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith(expect.stringContaining('等待处理'))
    await wrapper.get('[aria-label="关闭图片预览"]').trigger('click')
    expect(wrapper.find('[role="dialog"]').exists()).toBe(false)
    expect(document.activeElement).toBe(previewTrigger.element)
    wrapper.unmount()
  })

  it('后端设置页区分只读状态并保存并发待生效值', async () => {
    const settings = {
      settings_version: 'v1',
      restart_required: false,
      effective: { opencode_concurrency: 1 },
      pending: { opencode_concurrency: null },
      readonly: { embedding_model: 'test-model', opencode_model: 'luna', runtime_ready: true, embedding_cache_ready: false, settings_admin_enabled: true },
      editable: { opencode_concurrency: { value: 1, minimum: 1, maximum: 8, environment_overridden: false } },
      deployment: { provider_url: { configured: true }, api_key: { configured: true } },
    }
    backendSettings.mockResolvedValue(settings)
    updateBackendSettings.mockResolvedValue({ ...settings, restart_required: true, pending: { opencode_concurrency: 2 } })
    const wrapper = mount(App)
    await flushPromises()
    await wrapper.findAll('.sidebar nav button').find((button) => button.text().includes('后端设置')).trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('部署环境管理')
    const tokenInput = wrapper.get('#settings-admin-token')
    expect(tokenInput.attributes('type')).toBe('password')
    expect(tokenInput.attributes('autocomplete')).toBe('off')
    await tokenInput.setValue('admin-secret')
    await wrapper.get('.settings-edit-row input[type="number"]').setValue(2)
    await wrapper.get('.settings-edit-row button').trigger('click')
    await flushPromises()
    expect(updateBackendSettings).toHaveBeenCalledWith({ opencode_concurrency: 2 }, 'admin-secret')
    expect(tokenInput.element.value).toBe('')
    expect(window.localStorage.getItem('settings-admin-token')).toBeNull()
    expect(wrapper.text()).toContain('重启服务后生效')
  })

  it('设置管理未启用时凭据输入和保存按钮保持只读', async () => {
    backendSettings.mockResolvedValue({
      settings_version: 'v1',
      restart_required: false,
      effective: { opencode_concurrency: 1 },
      pending: { opencode_concurrency: null },
      readonly: { embedding_model: 'test-model', opencode_model: 'luna', runtime_ready: false, embedding_cache_ready: false, settings_admin_enabled: false },
      editable: { opencode_concurrency: { value: 1, minimum: 1, maximum: 8, environment_overridden: false } },
      deployment: { provider_url: { configured: false }, api_key: { configured: false } },
    })
    const wrapper = mount(App)
    await flushPromises()
    await wrapper.findAll('.sidebar nav button').find((button) => button.text().includes('后端设置')).trigger('click')
    await flushPromises()
    expect(wrapper.get('#settings-admin-token').element.disabled).toBe(true)
    expect(wrapper.get('.settings-edit-row button').element.disabled).toBe(true)
  })
})
