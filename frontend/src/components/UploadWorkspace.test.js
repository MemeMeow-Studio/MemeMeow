/** 上传工作区共享处理选项、逐文件结果和重复提交边界测试。 */
import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const { upload } = vi.hoisted(() => ({ upload: vi.fn() }))
const mountedWrappers = []

vi.mock('../api', () => ({
  api: { upload },
}))

import UploadWorkspace from './UploadWorkspace.vue'

function selectFiles(wrapper, files) {
  const input = wrapper.get('input[type="file"]')
  Object.defineProperty(input.element, 'files', { configurable: true, value: files })
  return input.trigger('change')
}

function pasteFiles(files, items = files.map((file) => ({ kind: 'file', type: file.type, getAsFile: () => file }))) {
  const event = new Event('paste', { bubbles: true, cancelable: true })
  Object.defineProperty(event, 'clipboardData', { configurable: true, value: { items } })
  window.dispatchEvent(event)
  return event
}

/** 向上传区域派发带有受控文件列表的拖放事件，供交互契约测试复用。 */
function dragFiles(wrapper, type, files = []) {
  const event = new Event(type, { bubbles: true, cancelable: true })
  Object.defineProperty(event, 'dataTransfer', { configurable: true, value: { files } })
  wrapper.get('.drop-zone').element.dispatchEvent(event)
  return event
}

function mountWorkspace(options = {}) {
  const wrapper = mount(UploadWorkspace, options)
  mountedWrappers.push(wrapper)
  return wrapper
}

describe('UploadWorkspace', () => {
  beforeEach(() => {
    upload.mockReset().mockResolvedValue({ results: [] })
  })

  afterEach(() => {
    for (const wrapper of mountedWrappers.splice(0)) wrapper.unmount()
  })

  it('确认前不上传，取消不请求，确认后所有文件共享同一组选项', async () => {
    const files = [
      new File(['one'], 'one.png', { type: 'image/png' }),
      new File(['two'], 'two.jpg', { type: 'image/jpeg' }),
    ]
    upload.mockResolvedValue({ results: files.map((file, index) => ({ filename: file.name, ok: true, meme_id: `meme-${index}` })) })
    const wrapper = mountWorkspace({ props: { config: { reverse_image_available: true } } })
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
    expect(upload).toHaveBeenCalledWith(files, { reverse_image_policy: 'auto', auto_name: true }, expect.objectContaining({ signal: expect.any(AbortSignal) }))
  })

  it('联网能力不可用时保持 forbid 可选并禁用 auto', async () => {
    const wrapper = mountWorkspace({ props: { config: { reverse_image_available: false } } })
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
    const wrapper = mountWorkspace({ props: { config: { reverse_image_available: true } } })
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
    const wrapper = mountWorkspace({ props: { config: { reverse_image_available: true } } })
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
    expect(upload).toHaveBeenNthCalledWith(2, [files[1]], { reverse_image_policy: 'forbid', auto_name: false }, expect.objectContaining({ signal: expect.any(AbortSignal) }))
  })

  it('将图片预检错误码展示为具体且不泄漏内部诊断的原因', async () => {
    const file = new File(['not-an-image'], 'broken.png', { type: 'image/png' })
    upload.mockResolvedValue({ results: [{ filename: file.name, ok: false, error: 'invalid_image' }] })
    const wrapper = mountWorkspace({ props: { config: { reverse_image_available: true } } })

    await selectFiles(wrapper, [file])
    await wrapper.get('button.primary').trigger('click')
    await wrapper.get('.processing-options-dialog form').trigger('submit')
    await flushPromises()

    expect(wrapper.get('.upload-result small').text()).toContain('图片内容无法解码')
    expect(wrapper.get('.upload-result small').text()).not.toContain('invalid_image')
  })

  it('未知上传错误码使用通用提示而不回显内部标识', async () => {
    const file = new File(['unknown-error'], 'unknown.png', { type: 'image/png' })
    upload.mockResolvedValue({ results: [{ filename: file.name, ok: false, error: 'internal_storage_path_42' }] })
    const wrapper = mountWorkspace({ props: { config: { reverse_image_available: true } } })

    await selectFiles(wrapper, [file])
    await wrapper.get('button.primary').trigger('click')
    await wrapper.get('.processing-options-dialog form').trigger('submit')
    await flushPromises()

    expect(wrapper.get('.upload-result small').text()).toBe('上传失败，请稍后重试')
    expect(wrapper.get('.upload-result small').text()).not.toContain('internal_storage_path_42')
  })

  it('传输错误按公开错误码展示安全原因，不回显 detail.message', async () => {
    const file = new File(['server-error'], 'server-error.png', { type: 'image/png' })
    const transportError = Object.assign(new Error('private detail /srv/runtime/token'), { code: 'operation_grant_invalid', status: 503 })
    upload.mockRejectedValue(transportError)
    const wrapper = mountWorkspace({ props: { config: { reverse_image_available: true } } })

    await selectFiles(wrapper, [file])
    await wrapper.get('button.primary').trigger('click')
    await wrapper.get('.processing-options-dialog form').trigger('submit')
    await flushPromises()

    expect(wrapper.emitted('error')?.at(-1)).toEqual(['上传授权无效，请稍后重试'])
    expect(wrapper.text()).not.toContain('private detail')
    expect(wrapper.text()).not.toContain('/srv/runtime/token')
    expect(wrapper.find('.processing-options-dialog').exists()).toBe(true)
  })

  it('没有公开错误码的传输错误使用通用提示', async () => {
    const file = new File(['unknown-error'], 'network.png', { type: 'image/png' })
    upload.mockRejectedValue(new Error('private upstream response'))
    const wrapper = mountWorkspace({ props: { config: { reverse_image_available: true } } })

    await selectFiles(wrapper, [file])
    await wrapper.get('button.primary').trigger('click')
    await wrapper.get('.processing-options-dialog form').trigger('submit')
    await flushPromises()

    expect(wrapper.emitted('error')?.at(-1)).toEqual(['上传失败，请稍后重试'])
    expect(wrapper.text()).not.toContain('private upstream response')
  })

  it('连续粘贴图片会追加到同一待上传队列，并在确认后一次提交', async () => {
    const first = new File(['one'], 'first.png', { type: 'image/png' })
    const second = new File(['two'], 'second.jpg', { type: 'image/jpeg' })
    upload.mockResolvedValue({ results: [
      { filename: first.name, ok: true, meme_id: 'meme-1' },
      { filename: second.name, ok: true, meme_id: 'meme-2' },
    ] })
    const wrapper = mountWorkspace({ props: { config: { reverse_image_available: true } } })

    pasteFiles([first])
    pasteFiles([second])
    await wrapper.vm.$nextTick()

    expect(wrapper.text()).toContain('已选择 2 个文件，可继续添加')
    expect(wrapper.get('.upload-results').text()).toContain('first.png')
    expect(wrapper.get('.upload-results').text()).toContain('second.jpg')

    await wrapper.get('button.primary').trigger('click')
    await wrapper.get('.processing-options-dialog form').trigger('submit')
    await flushPromises()

    expect(upload).toHaveBeenCalledTimes(1)
    expect(upload.mock.calls[0][0]).toEqual([first, second])
  })

  it('拖放按顺序追加受控图片、抑制默认行为并显示激活态', async () => {
    const selected = new File(['selected'], 'selected.png', { type: 'image/png' })
    const first = new File(['first'], 'first.jpg', { type: '' })
    const ignored = new File(['text'], 'notes.txt', { type: 'text/plain' })
    const second = new File(['second'], 'second.gif', { type: 'image/gif' })
    const wrapper = mountWorkspace({ props: { config: { reverse_image_available: true } } })

    await selectFiles(wrapper, [selected])
    const enter = dragFiles(wrapper, 'dragenter')
    await wrapper.vm.$nextTick()
    expect(enter.defaultPrevented).toBe(true)
    expect(wrapper.get('.drop-zone').classes()).toContain('is-dragging')
    dragFiles(wrapper, 'dragenter')
    dragFiles(wrapper, 'dragleave')
    expect(wrapper.get('.drop-zone').classes()).toContain('is-dragging')

    const drop = dragFiles(wrapper, 'drop', [first, ignored, second])
    await wrapper.vm.$nextTick()

    expect(drop.defaultPrevented).toBe(true)
    expect(wrapper.get('.drop-zone').classes()).not.toContain('is-dragging')
    expect(wrapper.findAll('.upload-pending-item strong').map((item) => item.text())).toEqual([
      'selected.png',
      'first.jpg',
      'second.gif',
    ])
    expect(upload).not.toHaveBeenCalled()
  })

  it('上传中拖放仍阻止浏览器默认行为且不改写活动批次', async () => {
    let resolveUpload
    upload.mockImplementationOnce(() => new Promise((resolve) => { resolveUpload = resolve }))
    const first = new File(['first'], 'first.png', { type: 'image/png' })
    const second = new File(['second'], 'second.png', { type: 'image/png' })
    const wrapper = mountWorkspace({ props: { config: { reverse_image_available: true } } })

    await selectFiles(wrapper, [first])
    await wrapper.get('button.primary').trigger('click')
    await wrapper.get('.processing-options-dialog form').trigger('submit')
    expect(upload).toHaveBeenCalledTimes(1)

    const drop = dragFiles(wrapper, 'drop', [second])
    await wrapper.vm.$nextTick()
    expect(drop.defaultPrevented).toBe(true)
    expect(wrapper.get('input[type="file"]').element.disabled).toBe(true)
    expect(wrapper.text()).toContain('已选择 1 个文件，可继续添加')
    expect(wrapper.text()).not.toContain('second.png')

    resolveUpload({ results: [{ filename: first.name, ok: true, meme_id: 'meme-1' }] })
    await flushPromises()
  })

  it('上传中混合状态仍按拖放批次顺序展示每个项目', async () => {
    const requests = []
    upload.mockImplementation((files) => new Promise((resolve) => requests.push({ files, resolve })))
    const files = Array.from({ length: 21 }, (_, index) => new File([String(index)], `ordered-${index}.png`, { type: 'image/png' }))
    const wrapper = mountWorkspace({ props: { config: { reverse_image_available: true } } })

    await selectFiles(wrapper, files)
    await wrapper.get('button.primary').trigger('click')
    await wrapper.get('.processing-options-dialog form').trigger('submit')
    await flushPromises()

    expect(requests).toHaveLength(2)
    expect(wrapper.findAll('.upload-results > * strong').map((item) => item.text())).toEqual(files.map((file) => file.name))
    expect(wrapper.findAll('.upload-pending-remove')).toHaveLength(0)

    while (requests.length) {
      const request = requests.shift()
      request.resolve({ results: request.files.map((file) => ({ filename: file.name, ok: true })) })
      await flushPromises()
    }
  })

  it('暂停后显示继续并在继续后恢复暂停动作', async () => {
    let resolveUpload
    upload.mockImplementationOnce(() => new Promise((resolve) => { resolveUpload = resolve }))
    const file = new File(['one'], 'pause.png', { type: 'image/png' })
    const wrapper = mountWorkspace({ props: { config: { reverse_image_available: true } } })

    await selectFiles(wrapper, [file])
    await wrapper.get('button.primary').trigger('click')
    await wrapper.get('.processing-options-dialog form').trigger('submit')
    expect(wrapper.get('.upload-summary button.quiet').text()).toBe('暂停')

    await wrapper.get('.upload-summary button.quiet').trigger('click')
    expect(wrapper.get('.upload-summary button.quiet').text()).toBe('继续')
    await wrapper.get('.upload-summary button.quiet').trigger('click')
    expect(wrapper.get('.upload-summary button.quiet').text()).toBe('暂停')

    resolveUpload({ results: [{ filename: file.name, ok: true, meme_id: 'meme-pause' }] })
    await flushPromises()
  })

  it('待上传项使用本地预览，解码失败和移除均保留可管理性并回收对象 URL', async () => {
    const createObjectURL = vi.fn((file) => `blob:${file.name}`)
    const revokeObjectURL = vi.fn()
    vi.stubGlobal('URL', { createObjectURL, revokeObjectURL })
    const files = [
      new File(['first'], 'first.png', { type: 'image/png' }),
      new File(['broken'], 'broken.gif', { type: 'image/gif' }),
      new File(['last'], 'last.jpg', { type: 'image/jpeg' }),
    ]
    const wrapper = mountWorkspace({ props: { config: { reverse_image_available: true } } })

    try {
      await selectFiles(wrapper, files)
      expect(createObjectURL).toHaveBeenCalledTimes(3)
      expect(wrapper.findAll('.upload-pending-item img')[0].attributes('src')).toBe('blob:first.png')
      expect(wrapper.findAll('.upload-pending-item button')[0].attributes('aria-label')).toBe('移除待上传图片 first.png')

      await wrapper.findAll('.upload-pending-item button')[0].trigger('click')
      expect(revokeObjectURL).toHaveBeenCalledWith('blob:first.png')
      expect(wrapper.text()).not.toContain('first.png')

      const broken = wrapper.get('.upload-pending-item img')
      await broken.trigger('error')
      await wrapper.vm.$nextTick()
      expect(revokeObjectURL).toHaveBeenCalledWith('blob:broken.gif')
      expect(wrapper.get('.upload-pending-preview-fallback').attributes('aria-label')).toBe('无法预览 broken.gif')
      expect(wrapper.text()).toContain('broken.gif')
      expect(wrapper.get('[aria-label="移除待上传图片 broken.gif"]')).toBeTruthy()
      expect(upload).not.toHaveBeenCalled()

      wrapper.unmount()
      mountedWrappers.splice(mountedWrappers.indexOf(wrapper), 1)
      expect(revokeObjectURL).toHaveBeenCalledWith('blob:last.jpg')
    } finally {
      vi.unstubAllGlobals()
    }
  })

  it('文件选择会替换粘贴产生的本地队列', async () => {
    const pasted = new File(['pasted'], 'pasted.png', { type: 'image/png' })
    const selected = new File(['selected'], 'selected.png', { type: 'image/png' })
    upload.mockResolvedValue({ results: [{ filename: selected.name, ok: true, meme_id: 'meme-1' }] })
    const wrapper = mountWorkspace({ props: { config: { reverse_image_available: true } } })

    pasteFiles([pasted])
    await wrapper.vm.$nextTick()
    await selectFiles(wrapper, [selected])

    expect(wrapper.get('.upload-results').text()).not.toContain('pasted.png')
    expect(wrapper.get('.upload-results').text()).toContain('selected.png')
    await wrapper.get('button.primary').trigger('click')
    await wrapper.get('.processing-options-dialog form').trigger('submit')
    await flushPromises()

    expect(upload).toHaveBeenCalledTimes(1)
    expect(upload.mock.calls[0][0]).toEqual([selected])
  })

  it('非图片粘贴不会阻止默认行为或加入队列', async () => {
    const wrapper = mountWorkspace({ props: { config: { reverse_image_available: true } } })
    const event = pasteFiles([], [{ kind: 'string', type: 'text/plain', getAsFile: () => null }])
    await wrapper.vm.$nextTick()

    expect(event.defaultPrevented).toBe(false)
    expect(wrapper.get('button.primary').element.disabled).toBe(true)
    expect(wrapper.find('.upload-results').exists()).toBe(false)
  })

  it('重复粘贴同名图片会生成不冲突的文件名', async () => {
    const first = new File(['one'], 'image.png', { type: 'image/png' })
    const second = new File(['two'], 'image.png', { type: 'image/png' })
    upload.mockResolvedValue({ results: [
      { filename: 'image.png', ok: true, meme_id: 'meme-1' },
      { filename: 'image-2.png', ok: true, meme_id: 'meme-2' },
    ] })
    const wrapper = mountWorkspace({ props: { config: { reverse_image_available: true } } })

    pasteFiles([first])
    pasteFiles([second])
    await wrapper.vm.$nextTick()
    await wrapper.get('button.primary').trigger('click')
    await wrapper.get('.processing-options-dialog form').trigger('submit')
    await flushPromises()

    expect(upload.mock.calls[0][0].map((file) => file.name)).toEqual(['image.png', 'image-2.png'])
  })

  it('跨 realm 的剪贴板 File 仍会加入队列', async () => {
    const frame = document.createElement('iframe')
    document.body.appendChild(frame)
    const foreignFile = new frame.contentWindow.File(['image'], 'foreign.png', { type: 'image/png' })
    const wrapper = mountWorkspace({ props: { config: { reverse_image_available: true } } })

    const event = pasteFiles([], [{ kind: 'file', type: 'image/png', getAsFile: () => foreignFile }])
    await wrapper.vm.$nextTick()

    expect(event.defaultPrevented).toBe(true)
    expect(wrapper.get('.upload-result strong').text()).toBe('foreign.png')
    frame.remove()
  })

  it('缺少可用文件名的剪贴板图片会生成唯一的 png 文件名', async () => {
    const wrapper = mountWorkspace({ props: { config: { reverse_image_available: true } } })
    pasteFiles([
      new File(['image'], 'blob', { type: 'image/png' }),
      new File(['image'], '..', { type: 'image/png' }),
    ])
    await wrapper.vm.$nextTick()

    const names = wrapper.findAll('.upload-result strong').map((item) => item.text())
    expect(names).toHaveLength(2)
    expect(names[0]).toMatch(/^pasted-\d+-\d+\.png$/)
    expect(names[1]).toMatch(/^pasted-\d+-\d+\.png$/)
    expect(names[0]).not.toBe(names[1])
  })

  it('上传中粘贴图片不会改写活动队列', async () => {
    let resolveUpload
    upload.mockImplementationOnce(() => new Promise((resolve) => { resolveUpload = resolve }))
    const first = new File(['one'], 'first.png', { type: 'image/png' })
    const second = new File(['two'], 'second.png', { type: 'image/png' })
    const wrapper = mountWorkspace({ props: { config: { reverse_image_available: true } } })

    await selectFiles(wrapper, [first])
    await wrapper.get('button.primary').trigger('click')
    await wrapper.get('.processing-options-dialog form').trigger('submit')
    expect(upload).toHaveBeenCalledTimes(1)

    const event = pasteFiles([second])
    await wrapper.vm.$nextTick()
    expect(event.defaultPrevented).toBe(false)
    expect(wrapper.get('input[type="file"]').element.disabled).toBe(true)
    expect(wrapper.text()).toContain('已选择 1 个文件，可继续添加')
    expect(wrapper.get('.upload-results').text()).not.toContain('second.png')

    resolveUpload({ results: [{ filename: first.name, ok: true, meme_id: 'meme-1' }] })
    await flushPromises()
    expect(upload).toHaveBeenCalledTimes(1)
  })

  it('已有批次完成后粘贴图片只提交新增项并保留原结果', async () => {
    const first = new File(['one'], 'first.png', { type: 'image/png' })
    const second = new File(['two'], 'second.png', { type: 'image/png' })
    upload.mockResolvedValueOnce({ results: [{ filename: first.name, ok: true, meme_id: 'meme-1' }] })
      .mockResolvedValueOnce({ results: [{ filename: second.name, ok: true, meme_id: 'meme-2' }] })
    const wrapper = mountWorkspace({ props: { config: { reverse_image_available: true } } })

    pasteFiles([first])
    await wrapper.vm.$nextTick()
    await wrapper.get('button.primary').trigger('click')
    await wrapper.get('.processing-options-dialog form').trigger('submit')
    await flushPromises()

    pasteFiles([second])
    await wrapper.vm.$nextTick()
    expect(wrapper.get('.upload-results').text()).toContain('完成first.png')
    expect(wrapper.get('.upload-results').text()).toContain('等待中second.png')
    await wrapper.get('button.primary').trigger('click')
    await wrapper.get('.processing-options-dialog form').trigger('submit')
    await flushPromises()

    expect(upload).toHaveBeenNthCalledWith(2, [second], expect.any(Object), expect.objectContaining({ signal: expect.any(AbortSignal) }))
    expect(wrapper.get('.upload-results').text()).toContain('完成first.png')
    expect(wrapper.get('.upload-results').text()).toContain('完成second.png')
  })

  it('组件卸载后不再响应全局粘贴事件', () => {
    const wrapper = mountWorkspace({ props: { config: { reverse_image_available: true } } })
    wrapper.unmount()
    mountedWrappers.splice(mountedWrappers.indexOf(wrapper), 1)

    const event = pasteFiles([new File(['image'], 'after-unmount.png', { type: 'image/png' })])

    expect(event.defaultPrevented).toBe(false)
    expect(upload).not.toHaveBeenCalled()
  })
})
