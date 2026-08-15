/** 任务工作区轮询终止和卸载竞态的行为测试。 */
import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const { tasks, task, context } = vi.hoisted(() => ({
  tasks: vi.fn(),
  task: vi.fn(),
  context: vi.fn(),
}))

vi.mock('../api', () => ({
  api: { tasks, task, context },
}))

import TasksWorkspace from './TasksWorkspace.vue'

describe('TasksWorkspace', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    tasks.mockReset()
    task.mockReset()
    context.mockReset()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('活跃任务进入终态后停止轮询', async () => {
    tasks
      .mockResolvedValueOnce({ items: [{ task_id: 'task-1', task_type: 'cache_generation', status: 'running' }], next_cursor: null })
      .mockResolvedValueOnce({ items: [{ task_id: 'task-1', task_type: 'cache_generation', status: 'succeeded' }], next_cursor: null })
    const wrapper = mount(TasksWorkspace)
    await flushPromises()
    expect(tasks).toHaveBeenCalledTimes(1)

    await vi.advanceTimersByTimeAsync(2500)
    await flushPromises()
    expect(tasks).toHaveBeenCalledTimes(2)

    await vi.advanceTimersByTimeAsync(5000)
    await flushPromises()
    expect(tasks).toHaveBeenCalledTimes(2)
    wrapper.unmount()
  })

  it('初始请求未完成时卸载不会重新注册轮询', async () => {
    let resolveTasks
    tasks.mockImplementationOnce(() => new Promise((resolve) => { resolveTasks = resolve }))
    const wrapper = mount(TasksWorkspace)
    wrapper.unmount()
    resolveTasks({ items: [{ task_id: 'task-1', task_type: 'cache_generation', status: 'running' }], next_cursor: null })
    await flushPromises()

    document.dispatchEvent(new Event('visibilitychange'))
    await vi.advanceTimersByTimeAsync(5000)
    expect(tasks).toHaveBeenCalledTimes(1)
  })
})
