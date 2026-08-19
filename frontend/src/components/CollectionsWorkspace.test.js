/** 合集列表、详情和成员维护行为测试，确保视觉整理不改变业务契约。 */
import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const { collections, createCollection, collection, renameCollection, deleteCollection, removeCollectionMember, imageMetadata } = vi.hoisted(() => ({
  collections: vi.fn(),
  createCollection: vi.fn(),
  collection: vi.fn(),
  renameCollection: vi.fn(),
  deleteCollection: vi.fn(),
  removeCollectionMember: vi.fn(),
  imageMetadata: vi.fn(),
}))

vi.mock('../api', () => ({
  api: { collections, createCollection, collection, renameCollection, deleteCollection, removeCollectionMember, imageMetadata },
}))

import CollectionsWorkspace from './CollectionsWorkspace.vue'

const member = {
  meme_id: 'meme-1',
  filename: 'a-very-long-image-name-that-stays-readable.png',
  extension: '.png',
  media_url: '/media/meme-1',
}

const summary = {
  collection_id: 'collection-1',
  name: '会议反应',
  member_count: 1,
  cover_media_url: '/media/meme-1',
}

describe('CollectionsWorkspace', () => {
  beforeEach(() => {
    collections.mockReset().mockResolvedValue({ items: [summary] })
    createCollection.mockReset().mockResolvedValue({ collection_id: 'collection-2', name: '新合集' })
    collection.mockReset().mockResolvedValue({ ...summary, members: [member] })
    renameCollection.mockReset().mockResolvedValue({ ...summary, name: '改名后的合集' })
    deleteCollection.mockReset().mockResolvedValue({})
    removeCollectionMember.mockReset().mockResolvedValue({})
    imageMetadata.mockReset().mockResolvedValue({})
    vi.stubGlobal('prompt', vi.fn())
    vi.stubGlobal('confirm', vi.fn(() => true))
  })

  it('通过内联表单创建合集并刷新列表', async () => {
    const created = { collection_id: 'collection-2', name: '新合集', member_count: 0 }
    createCollection.mockResolvedValueOnce(created)
    collections.mockResolvedValueOnce({ items: [summary] }).mockResolvedValueOnce({ items: [summary, created] })
    const wrapper = mount(CollectionsWorkspace)
    await flushPromises()

    await wrapper.get('#collection-name').setValue('新合集')
    await wrapper.get('.collection-create-form').trigger('submit')
    await flushPromises()

    expect(createCollection).toHaveBeenCalledWith({ name: '新合集' })
    expect(wrapper.findAll('.collection-row')).toHaveLength(2)
    expect(wrapper.text()).toContain('合集已创建')
  })

  it('进入详情后保留重命名、返回和删除操作', async () => {
    const wrapper = mount(CollectionsWorkspace)
    await flushPromises()

    await wrapper.get('[aria-label="打开合集 会议反应"]').trigger('click')
    await flushPromises()
    expect(collection).toHaveBeenCalledWith('collection-1')
    expect(wrapper.get('h1').text()).toBe('会议反应')
    expect(wrapper.get('.collection-gallery')).toBeTruthy()

    window.prompt = vi.fn(() => '改名后的合集')
    collections.mockResolvedValueOnce({ items: [summary] })
    await wrapper.get('.collection-detail-actions .quiet').trigger('click')
    await flushPromises()
    expect(renameCollection).toHaveBeenCalledWith('collection-1', { name: '改名后的合集' })
    expect(wrapper.get('h1').text()).toBe('改名后的合集')

    collections.mockResolvedValueOnce({ items: [summary] })
    await wrapper.get('.collection-back').trigger('click')
    await flushPromises()
    expect(wrapper.get('h1').text()).toBe('合集')

    await wrapper.get('[aria-label="打开合集 会议反应"]').trigger('click')
    await flushPromises()
    window.confirm = vi.fn(() => true)
    collections.mockResolvedValueOnce({ items: [] })
    await wrapper.get('.collection-detail-actions .collection-danger').trigger('click')
    await flushPromises()
    expect(deleteCollection).toHaveBeenCalledWith('collection-1')
    expect(wrapper.get('h1').text()).toBe('合集')
  })

  it('在资产卡片内移除成员并刷新详情', async () => {
    const wrapper = mount(CollectionsWorkspace)
    await flushPromises()
    await wrapper.get('[aria-label="打开合集 会议反应"]').trigger('click')
    await flushPromises()

    collection.mockResolvedValueOnce({ ...summary, member_count: 0, members: [] })
    await wrapper.get('[aria-label="从合集移除 a-very-long-image-name-that-stays-readable.png"]').trigger('click')
    await flushPromises()

    expect(removeCollectionMember).toHaveBeenCalledWith('collection-1', 'meme-1')
    expect(wrapper.get('.collection-empty').text()).toContain('这个合集还没有图片')
    expect(wrapper.text()).toContain('图片已从合集移除')
  })
})
