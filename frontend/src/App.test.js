/** Vue 核心页面切换和检索流程测试。 */
import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const { search, images, contextBatch } = vi.hoisted(() => ({ search: vi.fn(), images: vi.fn(), contextBatch: vi.fn() }))
vi.mock('./api', () => ({
  api: {
    config: vi.fn(async () => ({ embedding_model: 'test-model', embedding_cache_ready: false })),
    search,
    images,
    contextBatch,
    generateCache: vi.fn(),
  },
  pollTask: vi.fn(),
}))

import App from './App.vue'

describe('App', () => {
  beforeEach(() => {
    search.mockReset()
    images.mockReset().mockResolvedValue({ items: [], directories: [] })
    contextBatch.mockReset().mockResolvedValue({ results: [] })
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

  it('切换图片库时加载图片列表', async () => {
    const wrapper = mount(App)
    await flushPromises()
    const library = wrapper.findAll('.sidebar nav button').find((button) => button.text().includes('图片库'))
    await library.trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('浏览、筛选和整理本地图片')
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
})
