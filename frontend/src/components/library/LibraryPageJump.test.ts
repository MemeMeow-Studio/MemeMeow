/** 使用真实页码输入组件检验提交、范围限制和当前页同步。 */
import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import LibraryPageJump from './LibraryPageJump.vue'

describe('LibraryPageJump', () => {
  it('提交有效目标页码，并同步工作区当前页', async () => {
    const wrapper = mount(LibraryPageJump, { props: { page: 1, pageCount: 11, disabled: false } })
    const input = wrapper.get('input')
    expect(wrapper.get('button').attributes('disabled')).toBeDefined()
    await input.setValue('8')
    await wrapper.get('form').trigger('submit')
    expect(wrapper.emitted('changePage')).toEqual([[8]])
    await wrapper.setProps({ page: 8 })
    expect((input.element as HTMLInputElement).value).toBe('8')
    expect(wrapper.get('button').attributes('disabled')).toBeDefined()
    wrapper.unmount()
  })

  it.each(['', '0', '-1', '12', '1.5'])('拒绝无效页码 %s', async value => {
    const wrapper = mount(LibraryPageJump, { props: { page: 1, pageCount: 11, disabled: false } })
    await wrapper.get('input').setValue(value)
    expect(wrapper.get('button').attributes('disabled')).toBeDefined()
    await wrapper.get('form').trigger('submit')
    expect(wrapper.emitted('changePage')).toBeUndefined()
    wrapper.unmount()
  })

  it('加载期间禁止提交，并根据最新总页数检查范围', async () => {
    const wrapper = mount(LibraryPageJump, { props: { page: 1, pageCount: 11, disabled: false } })
    await wrapper.get('input').setValue('11')
    await wrapper.setProps({ disabled: true })
    expect(wrapper.get('input').attributes('disabled')).toBeDefined()
    await wrapper.get('form').trigger('submit')
    expect(wrapper.emitted('changePage')).toBeUndefined()
    await wrapper.setProps({ disabled: false, pageCount: 2 })
    expect(wrapper.get('button').attributes('disabled')).toBeDefined()
    await wrapper.get('form').trigger('submit')
    expect(wrapper.emitted('changePage')).toBeUndefined()
    await wrapper.get('input').setValue('2')
    await wrapper.get('form').trigger('submit')
    expect(wrapper.emitted('changePage')).toEqual([[2]])
    wrapper.unmount()
  })
})
