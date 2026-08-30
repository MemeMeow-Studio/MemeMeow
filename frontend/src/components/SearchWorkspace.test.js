/** 检索结果缩略图、原图渐进加载和原图复制行为测试。 */
import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const { search, copyImage } = vi.hoisted(() => ({
  search: vi.fn(),
  copyImage: vi.fn(),
}))

vi.mock('../api', () => ({
  api: { search },
}))

vi.mock('../composables/useImageClipboard', () => ({
  useImageClipboard: () => ({ copyNotice: null, copyImage }),
}))

import SearchWorkspace from './SearchWorkspace.vue'

const response = {
  results: ['/media/meme-1', '/media/meme-2'],
  result_media: [
    {
      meme_id: 'meme-1',
      media_url: '/media/meme-1',
      thumbnail: { status: 'available', media_url: '/media/meme-1/thumbnail', width: 320, height: 160, media_type: 'image/png' },
    },
    {
      meme_id: 'meme-2',
      media_url: '/media/meme-2',
      thumbnail: { status: 'pending', media_url: null },
    },
  ],
}

async function runSearch(wrapper) {
  /** 提交一个有效查询并等待结果媒体旁路完成投影。 */
  await wrapper.get('#search-query').setValue('会议反应')
  await wrapper.get('.search-form').trigger('submit')
  await flushPromises()
}

describe('SearchWorkspace', () => {
  beforeEach(() => {
    search.mockReset().mockResolvedValue(response)
    copyImage.mockReset()
  })

  it('使用自然语言检索标题并移除辅助小字', () => {
    const wrapper = mount(SearchWorkspace, { props: { config: null } })

    expect(wrapper.get('h1').text()).toBe('通过自然语言检索表情包')
    expect(wrapper.find('.section-head p').exists()).toBe(false)
    wrapper.unmount()
  })

  it('按 result_media 关联缩略图，原图预加载成功后替换可见图片', async () => {
    const wrapper = mount(SearchWorkspace, { props: { config: null } })
    await runSearch(wrapper)

    const items = wrapper.findAll('.result-item')
    expect(items).toHaveLength(2)
    expect(items[0].find('img').attributes('src')).toBe('/media/meme-1/thumbnail')
    expect(items[0].find('.result-original-preload').attributes('src')).toBe('/media/meme-1')
    expect(items[1].find('img').attributes('src')).toBe('/media/meme-2')

    await items[0].find('.result-original-preload').trigger('load')
    await flushPromises()
    expect(items[0].find('img').attributes('src')).toBe('/media/meme-1')
    wrapper.unmount()
  })

  it('原图预加载失败时保留缩略图，缩略图失败时回退原图', async () => {
    const wrapper = mount(SearchWorkspace, { props: { config: null } })
    await runSearch(wrapper)
    const item = wrapper.find('.result-item')
    const visible = item.find('img')
    const preload = item.find('.result-original-preload')

    await preload.trigger('error')
    await flushPromises()
    expect(item.find('img').attributes('src')).toBe('/media/meme-1/thumbnail')
    expect(item.find('.result-original-preload').exists()).toBe(false)

    await visible.trigger('error')
    await flushPromises()
    expect(item.find('.image-load-fallback').text()).toBe('图片暂不可用')
    wrapper.unmount()
  })

  it('原图已经替换后再次失败也回退缩略图，不留下破损原图地址', async () => {
    const wrapper = mount(SearchWorkspace, { props: { config: null } })
    await runSearch(wrapper)
    const item = wrapper.find('.result-item')
    const preload = item.find('.result-original-preload')
    await preload.trigger('load')
    await flushPromises()
    const visible = item.find('img')
    expect(visible.attributes('src')).toBe('/media/meme-1')

    await visible.trigger('error')
    await flushPromises()
    expect(visible.attributes('src')).toBe('/media/meme-1/thumbnail')
    wrapper.unmount()
  })

  it('点击检索结果始终把原图媒体交给复制动作', async () => {
    const wrapper = mount(SearchWorkspace, { props: { config: null } })
    await runSearch(wrapper)
    await wrapper.find('.result-item').trigger('click')
    expect(copyImage).toHaveBeenCalledWith('/media/meme-1')
    wrapper.unmount()
  })
})
