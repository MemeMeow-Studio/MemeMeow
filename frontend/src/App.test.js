/** Vue 核心页面切换和检索流程测试。 */
import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const { search } = vi.hoisted(() => ({ search: vi.fn() }))
vi.mock('./api', () => ({
  api: {
    config: vi.fn(async () => ({ embedding_model: 'test-model' })),
    search,
    images: vi.fn(async () => ({ items: [], directories: [] })),
    generateCache: vi.fn(),
  },
  pollTask: vi.fn(),
}))

import App from './App.vue'

describe('App', () => {
  beforeEach(() => search.mockReset())

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
})
