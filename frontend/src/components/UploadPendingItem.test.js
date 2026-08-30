/** 待上传展示行的本地预览资源生命周期测试，验证文件替换、卸载时均回收对象 URL。 */
import { mount } from '@vue/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'
import UploadPendingItem from './UploadPendingItem.vue'

/** 创建待上传行测试数据，输入本地 File，输出可替换的最小批次项。 */
function makeItem(file) {
  return { id: 'upload-item-1', file, status: 'pending', retryable: true, attempts: 0 }
}

describe('UploadPendingItem', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('文件替换时回收旧对象 URL，卸载时回收新对象 URL', async () => {
    const createObjectURL = vi.fn((file) => `blob:${file.name}`)
    const revokeObjectURL = vi.fn()
    vi.stubGlobal('URL', { createObjectURL, revokeObjectURL })
    const first = new File(['first'], 'same-name.png', { type: 'image/png' })
    const second = new File(['second'], 'same-name.png', { type: 'image/png' })
    const wrapper = mount(UploadPendingItem, { props: { item: makeItem(first), removable: true } })

    expect(createObjectURL).toHaveBeenCalledWith(first)
    await wrapper.setProps({ item: makeItem(second) })
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:same-name.png')
    expect(createObjectURL).toHaveBeenLastCalledWith(second)

    wrapper.unmount()
    expect(revokeObjectURL).toHaveBeenLastCalledWith('blob:same-name.png')
    expect(revokeObjectURL).toHaveBeenCalledTimes(2)
  })
})
