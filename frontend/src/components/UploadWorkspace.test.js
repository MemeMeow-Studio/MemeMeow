/** 上传工作区共享处理选项、逐文件结果和重复提交边界测试。 */
import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const { upload } = vi.hoisted(() => ({ upload: vi.fn() }))

vi.mock('../api', () => ({
  api: { upload },
}))

import UploadWorkspace from './UploadWorkspace.vue'

function selectFiles(wrapper, files) {
  const input = wrapper.get('input[type="file"]')
  Object.defineProperty(input.element, 'files', { configurable: true, value: files })
  return input.trigger('change')
}

describe('UploadWorkspace', () => {
  beforeEach(() => {
    upload.mockReset().mockResolvedValue({ results: [] })
  })

  it('确认前不上传，取消不请求，确认后所有文件共享同一组选项', async () => {
    const files = [
      new File(['one'], 'one.png', { type: 'image/png' }),
      new File(['two'], 'two.jpg', { type: 'image/jpeg' }),
    ]
    upload.mockResolvedValue({ results: files.map((file, index) => ({ filename: file.name, ok: true, meme_id: `meme-${index}` })) })
    const wrapper = mount(UploadWorkspace, { props: { config: { reverse_image_available: true } } })
    await selectFiles(wrapper, files)
    await wrapper.get('button.primary').trigger('click')
    expect(upload).not.toHaveBeenCalled()

    await wrapper.get('[aria-label="取消图片处理"]').trigger('click')
    expect(upload).not.toHaveBeenCalled()

    await wrapper.get('button.primary').trigger('click')
    await wrapper.get('.processing-options-dialog input[value="auto"]').setValue(true)
    await wrapper.get('.processing-options-dialog input[type="checkbox"]').setValue(true)
    await wrapper.get('.processing-options-dialog form').trigger('submit')
    await flushPromises()

    expect(upload).toHaveBeenCalledTimes(1)
    expect(upload).toHaveBeenCalledWith(files, { reverse_image_policy: 'auto', auto_name: true })
  })

  it('联网能力不可用时保持 forbid 可选并禁用 auto', async () => {
    const wrapper = mount(UploadWorkspace, { props: { config: { reverse_image_available: false } } })
    await selectFiles(wrapper, [new File(['one'], 'one.png', { type: 'image/png' })])
    await wrapper.get('button.primary').trigger('click')

    const auto = wrapper.get('.processing-options-dialog input[value="auto"]')
    expect(auto.element.disabled).toBe(true)
    expect(wrapper.get('.processing-options-dialog').text()).toContain('反向图片服务不可用')
    expect(wrapper.get('.processing-options-dialog input[value="forbid"]').element.disabled).toBe(false)
  })

  it('请求失败时保留文件和选项，提交中阻止重复上传', async () => {
    let rejectUpload
    upload.mockImplementationOnce(() => new Promise((_resolve, reject) => { rejectUpload = reject }))
    const file = new File(['one'], 'one.png', { type: 'image/png' })
    const wrapper = mount(UploadWorkspace, { props: { config: { reverse_image_available: true } } })
    await selectFiles(wrapper, [file])
    await wrapper.get('button.primary').trigger('click')
    await wrapper.get('.processing-options-dialog input[value="auto"]').setValue(true)
    await wrapper.get('.processing-options-dialog form').trigger('submit')
    await wrapper.get('.processing-options-dialog form').trigger('submit')
    expect(upload).toHaveBeenCalledTimes(1)
    expect(wrapper.get('.processing-options-dialog').text()).toContain('提交中...')

    rejectUpload(new Error('上传失败'))
    await flushPromises()
    expect(wrapper.find('.processing-options-dialog').exists()).toBe(true)
    expect(wrapper.get('.processing-options-dialog input[value="auto"]').element.checked).toBe(true)
    expect(wrapper.text()).toContain('已选择 1 个文件')

    upload.mockResolvedValue({ results: [{ filename: file.name, ok: true, meme_id: 'meme-1' }] })
    await wrapper.get('.processing-options-dialog form').trigger('submit')
    await flushPromises()
    expect(upload).toHaveBeenCalledTimes(2)
    expect(wrapper.find('.processing-options-dialog').exists()).toBe(false)
  })

  it('逐文件部分成功时只保留失败文件，下一次打开回到安全默认值', async () => {
    const files = [
      new File(['one'], 'one.png', { type: 'image/png' }),
      new File(['two'], 'two.jpg', { type: 'image/jpeg' }),
    ]
    upload.mockResolvedValueOnce({ results: [
      { filename: 'one.png', ok: true, meme_id: 'meme-1' },
      { filename: 'two.jpg', ok: false, error: 'processing_failed' },
    ] }).mockResolvedValueOnce({ results: [{ filename: 'two.jpg', ok: true, meme_id: 'meme-2' }] })
    const wrapper = mount(UploadWorkspace, { props: { config: { reverse_image_available: true } } })
    await selectFiles(wrapper, files)
    await wrapper.get('button.primary').trigger('click')
    await wrapper.get('.processing-options-dialog input[type="checkbox"]').setValue(true)
    await wrapper.get('.processing-options-dialog form').trigger('submit')
    await flushPromises()

    expect(wrapper.text()).toContain('已选择 1 个文件')
    await wrapper.get('button.primary').trigger('click')
    expect(wrapper.get('.processing-options-dialog input[value="forbid"]').element.checked).toBe(true)
    expect(wrapper.get('.processing-options-dialog input[type="checkbox"]').element.checked).toBe(false)
    await wrapper.get('.processing-options-dialog form').trigger('submit')
    await flushPromises()
    expect(upload).toHaveBeenNthCalledWith(2, [files[1]], { reverse_image_policy: 'forbid', auto_name: false })
  })
})
