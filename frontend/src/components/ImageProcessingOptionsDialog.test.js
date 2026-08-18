/** 图片处理选项对话框的安全默认值、能力变化和键盘闭环测试。 */
import { mount } from '@vue/test-utils'
import { existsSync, readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { nextTick } from 'vue'
import { afterEach, describe, expect, it } from 'vitest'
import ImageProcessingOptionsDialog from './ImageProcessingOptionsDialog.vue'

describe('ImageProcessingOptionsDialog', () => {
  let wrapper

  afterEach(() => {
    wrapper?.unmount()
    wrapper = undefined
  })

  it('联网能力打开期间失效时回落到禁止联网', async () => {
    wrapper = mount(ImageProcessingOptionsDialog, {
      props: { reverseImageAvailable: true, busy: false },
    })
    await wrapper.get('input[value="auto"]').setValue(true)
    await wrapper.setProps({ reverseImageAvailable: false })
    await nextTick()
    await wrapper.get('form').trigger('submit')

    expect(wrapper.emitted('confirm')?.[0]?.[0]).toEqual({ reverse_image_policy: 'forbid', auto_name: false })
  })

  it('打开时使用禁止联网和关闭自动命名的安全默认值', async () => {
    wrapper = mount(ImageProcessingOptionsDialog, {
      props: { reverseImageAvailable: true, busy: false },
    })
    await wrapper.get('form').trigger('submit')

    expect(wrapper.emitted('confirm')?.[0]?.[0]).toEqual({ reverse_image_policy: 'forbid', auto_name: false })
  })

  it('确认和取消都只触发对应事件，Escape 不会提交', async () => {
    wrapper = mount(ImageProcessingOptionsDialog, {
      props: { reverseImageAvailable: true, busy: false },
    })

    await wrapper.get('[aria-label="取消图片处理"]').trigger('click')
    expect(wrapper.emitted('cancel')).toHaveLength(1)
    expect(wrapper.emitted('confirm')).toBeUndefined()

    await wrapper.get('form').trigger('submit')
    expect(wrapper.emitted('confirm')).toHaveLength(1)

    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    expect(wrapper.emitted('cancel')).toHaveLength(2)
  })

  it('busy 期间忽略确认、取消和 Escape', async () => {
    wrapper = mount(ImageProcessingOptionsDialog, {
      props: { reverseImageAvailable: true, busy: true },
    })

    await wrapper.get('form').trigger('submit')
    await wrapper.get('[aria-label="取消图片处理"]').trigger('click')
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))

    expect(wrapper.emitted('confirm')).toBeUndefined()
    expect(wrapper.emitted('cancel')).toBeUndefined()
  })

  it('Tab 从首尾控件循环，关闭后恢复触发按钮焦点', async () => {
    const trigger = document.createElement('button')
    document.body.appendChild(trigger)
    trigger.focus()
    wrapper = mount(ImageProcessingOptionsDialog, {
      attachTo: document.body,
      props: { reverseImageAvailable: true, busy: false, returnFocus: trigger },
    })
    await nextTick()

    const focusable = wrapper.findAll('button, input').filter((item) => !item.element.disabled)
    const first = focusable[0].element
    const last = focusable[focusable.length - 1].element
    expect(document.activeElement).toBe(wrapper.get('input[value="forbid"]').element)

    last.focus()
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', bubbles: true }))
    expect(document.activeElement).toBe(first)

    await wrapper.get('[aria-label="取消图片处理"]').trigger('click')
    wrapper.unmount()
    await nextTick()
    expect(document.activeElement).toBe(trigger)
    trigger.remove()
    wrapper = undefined
  })

  it('重新打开时恢复安全默认值，失败重试选项可由初始值显式恢复', async () => {
    wrapper = mount(ImageProcessingOptionsDialog, {
      props: { reverseImageAvailable: true, busy: false, initialOptions: { reverse_image_policy: 'auto', auto_name: true } },
    })
    await wrapper.get('form').trigger('submit')
    expect(wrapper.emitted('confirm')?.[0]?.[0]).toEqual({ reverse_image_policy: 'auto', auto_name: true })

    wrapper.unmount()
    wrapper = mount(ImageProcessingOptionsDialog, {
      props: { reverseImageAvailable: true, busy: false },
    })
    await wrapper.get('form').trigger('submit')
    expect(wrapper.emitted('confirm')?.[0]?.[0]).toEqual({ reverse_image_policy: 'forbid', auto_name: false })

    await wrapper.setProps({ initialOptions: { reverse_image_policy: 'auto', auto_name: true } })
    await wrapper.get('form').trigger('submit')
    expect(wrapper.emitted('confirm')?.[1]?.[0]).toEqual({ reverse_image_policy: 'auto', auto_name: true })
  })

  it('窄屏规则让选项内容可收缩，操作按钮在 380px 以下单列排列', () => {
    const localStylePath = resolve(process.cwd(), 'src/style.css')
    const style = readFileSync(existsSync(localStylePath) ? localStylePath : resolve(process.cwd(), 'frontend/src/style.css'), 'utf8')
    expect(style).toContain('.option-choice span, .option-toggle span { display: grid; gap: 3px; min-width: 0; }')
    expect(style).toContain('@media (max-width: 380px)')
    expect(style).toContain('.processing-options-actions { grid-template-columns: 1fr; }')
  })
})
