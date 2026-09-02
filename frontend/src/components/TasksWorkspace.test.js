/** 任务工作区轮询终止和卸载竞态的行为测试。 */
import { flushPromises, mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const { tasks, task, context, processingJobs, retryProcessingJob, submitImageStage } = vi.hoisted(() => ({
  tasks: vi.fn(),
  task: vi.fn(),
  context: vi.fn(),
  processingJobs: vi.fn(),
  retryProcessingJob: vi.fn(),
  submitImageStage: vi.fn(),
}))

vi.mock('../api', () => ({
  api: { tasks, task, context, processingJobs, retryProcessingJob, submitImageStage },
}))

import TasksWorkspace from './TasksWorkspace.vue'

describe('TasksWorkspace', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    tasks.mockReset()
    task.mockReset()
    context.mockReset()
    processingJobs.mockReset().mockResolvedValue({ items: [] })
    retryProcessingJob.mockReset().mockResolvedValue({ task_id: 'retry-task' })
    submitImageStage.mockReset().mockResolvedValue({ task_id: 'rename-retry' })
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('只为最新 revision 的 active 到 terminal 转换通知图片库，并忽略旧 Job', async () => {
    tasks.mockResolvedValue({ items: [], next_cursor: null })
    processingJobs
      .mockResolvedValueOnce({ items: [
        {
          task_id: 'old-task', task_type: 'image_processing', job_id: 'old-job', meme_id: 'meme-1', processing_job_id: 'old-job', revision: 1,
          image_sha256: 'a'.repeat(64), reverse_image_policy: 'forbid', status: 'failed', updated_at: '2026-08-26T00:00:02Z', stages: [],
        },
        {
          task_id: 'current-task', task_type: 'image_processing', job_id: 'current-job', meme_id: 'meme-1', processing_job_id: 'current-job', revision: 2,
          image_sha256: 'b'.repeat(64), reverse_image_policy: 'forbid', status: 'running', updated_at: '2026-08-26T00:00:03Z', stages: [],
        },
      ] })
      .mockResolvedValueOnce({ items: [
        {
          task_id: 'old-task', task_type: 'image_processing', job_id: 'old-job', meme_id: 'meme-1', processing_job_id: 'old-job', revision: 1,
          image_sha256: 'a'.repeat(64), reverse_image_policy: 'forbid', status: 'succeeded', updated_at: '2026-08-26T00:00:04Z', stages: [],
        },
        {
          task_id: 'current-task', task_type: 'image_processing', job_id: 'current-job', meme_id: 'meme-1', processing_job_id: 'current-job', revision: 2,
          image_sha256: 'b'.repeat(64), reverse_image_policy: 'forbid', status: 'succeeded', updated_at: '2026-08-26T00:00:05Z', stages: [],
        },
      ] })
    const wrapper = mount(TasksWorkspace)
    await flushPromises()

    await vi.advanceTimersByTimeAsync(2500)
    await flushPromises()

    expect(wrapper.emitted('imageProcessingTerminal')).toEqual([[{ meme_id: 'meme-1', job_id: 'current-job', revision: 2 }]])
    wrapper.unmount()
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

  it('普通任务和图片处理 Job 按创建时间混排，不按更新时间排序', async () => {
    tasks.mockResolvedValue({ items: [{
      task_id: 'task-old',
      task_type: 'cache_generation',
      submission_mode: 'standalone',
      status: 'succeeded',
      created_at: '2026-08-26T00:00:01Z',
      updated_at: '2026-08-26T00:10:00Z',
    }], next_cursor: null })
    processingJobs.mockResolvedValue({ items: [{
      task_id: 'job-task',
      task_type: 'image_processing',
      job_id: 'job-new',
      meme_id: 'meme-1',
      processing_job_id: 'job-new',
      submission_mode: 'pipeline',
      revision: 1,
      image_sha256: 'sha',
      reverse_image_policy: 'forbid',
      status: 'succeeded',
      created_at: '2026-08-26T00:00:02Z',
      updated_at: '2026-08-26T00:00:03Z',
      stages: [],
    }] })

    const wrapper = mount(TasksWorkspace)
    await flushPromises()

    const entries = wrapper.findAll('.processing-job, .task-row')
    expect(entries).toHaveLength(2)
    expect(entries[0].classes()).toContain('processing-job')
    expect(entries[1].classes()).toContain('task-row')
    const headers = wrapper.findAll('[role="columnheader"]')
    expect(headers[headers.length - 1].text()).toBe('创建时间')
    expect(wrapper.text()).not.toContain('最近更新')
    wrapper.unmount()
  })

  it('旧任务摘要缺少创建时间时，排序和时间显示使用同一回退值', async () => {
    tasks
      .mockResolvedValueOnce({ items: [
        { task_id: 'task-old', task_type: 'cache_generation', status: 'running', updated_at: '2026-08-26T00:00:01Z' },
        { task_id: 'task-new', task_type: 'metadata_repair', status: 'succeeded', updated_at: '2026-08-26T00:00:02Z' },
      ], next_cursor: null })
      .mockResolvedValueOnce({ items: [
        { task_id: 'task-old', task_type: 'cache_generation', status: 'succeeded', updated_at: '2026-08-26T01:00:00Z' },
        { task_id: 'task-new', task_type: 'metadata_repair', status: 'succeeded', updated_at: '2026-08-26T00:00:02Z' },
      ], next_cursor: null })
    const wrapper = mount(TasksWorkspace)
    await flushPromises()

    const rows = wrapper.findAll('.task-row')
    expect(rows).toHaveLength(2)
    const initialTypes = rows.map((row) => row.find('.task-type-cell').text())
    expect(initialTypes[0]).toContain('元数据修复')
    expect(initialTypes[1]).toContain('检索缓存')
    const initialTimes = rows.map((row) => row.find('time').text())
    expect(initialTimes.every((value) => value !== '—')).toBe(true)

    await vi.advanceTimersByTimeAsync(2500)
    await flushPromises()
    const refreshedRows = wrapper.findAll('.task-row')
    expect(refreshedRows.map((row) => row.find('.task-type-cell').text())).toEqual(initialTypes)
    expect(refreshedRows.map((row) => row.find('time').text())).toEqual(initialTimes)
    wrapper.unmount()
  })

  it('轮询更新任务的 updated_at 不改变创建顺序', async () => {
    tasks
      .mockResolvedValueOnce({ items: [
        { task_id: 'task-old', task_type: 'cache_generation', status: 'running', created_at: '2026-08-26T00:00:01Z', updated_at: '2026-08-26T00:00:02Z' },
        { task_id: 'task-new', task_type: 'metadata_repair', status: 'running', created_at: '2026-08-26T00:00:02Z', updated_at: '2026-08-26T00:00:03Z' },
      ], next_cursor: null })
      .mockResolvedValueOnce({ items: [
        { task_id: 'task-old', task_type: 'cache_generation', status: 'succeeded', created_at: '2026-08-26T00:00:01Z', updated_at: '2026-08-26T01:00:00Z' },
        { task_id: 'task-new', task_type: 'metadata_repair', status: 'running', created_at: '2026-08-26T00:00:02Z', updated_at: '2026-08-26T00:00:03Z' },
      ], next_cursor: null })
    const wrapper = mount(TasksWorkspace)
    await flushPromises()
    const before = wrapper.findAll('.task-row').map((row) => row.find('.task-type-cell').text())

    await vi.advanceTimersByTimeAsync(2500)
    await flushPromises()
    const after = wrapper.findAll('.task-row').map((row) => row.find('.task-type-cell').text())
    expect(after).toEqual(before)
    const updatedRow = wrapper.findAll('.task-row').find((row) => row.find('.task-type-cell').text().includes('检索缓存'))
    expect(updatedRow?.text()).toContain('已完成')
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

  it('展示历史三阶段 Job、skipped、running 和 warning 阶段事实', async () => {
    tasks.mockResolvedValue({ items: [], next_cursor: null })
    processingJobs.mockResolvedValue({ items: [{
      task_id: 'job-task',
      task_type: 'image_processing',
      job_id: 'job-1',
      meme_id: 'meme-1',
      processing_job_id: 'job-1',
      revision: 1,
      image_sha256: 'sha',
      reverse_image_policy: 'forbid',
      auto_name: false,
      status: 'succeeded',
      current_stage: 'agent',
      image: { meme_id: 'meme-1', filename: 'sample.png', media_url: '/media/meme-1' },
      has_warnings: false,
      warnings: [],
      stages: [
        { stage: 'visual', status: 'succeeded', task_id: 'visual-1', attempt: 1 },
        { stage: 'agent', status: 'running', task_id: 'agent-1' },
        { stage: 'auto_rename', status: 'skipped' },
        { stage: 'text_embedding', status: 'succeeded', task_id: 'text-1' },
      ],
    }] })
    const wrapper = mount(TasksWorkspace)
    await flushPromises()

    expect(wrapper.text()).toContain('四阶段流水线')
    expect(wrapper.get('.processing-job-parent .task-image').text()).toBe('sample.png')
    expect(wrapper.get('.processing-job-parent').text()).not.toContain('Agent 语境')
    expect(wrapper.text()).toContain('Agent 语境处理中')
    expect(wrapper.text()).toContain('自动重命名未启用')
    expect(wrapper.text()).toContain('未启用')
    expect(wrapper.text()).toContain('第 1 次尝试')
    expect(wrapper.text()).not.toContain('meme-1')
    expect(wrapper.text()).not.toContain('job-1')
    expect(wrapper.text()).not.toContain('visual-1')
    wrapper.unmount()
  })

  it('父 Job 默认折叠，展开后点击子任务显示图片并恢复焦点', async () => {
    tasks.mockResolvedValue({ items: [], next_cursor: null })
    processingJobs.mockResolvedValue({ items: [{
      task_id: 'job-task',
      task_type: 'image_processing',
      job_id: 'job-1',
      meme_id: 'meme-1',
      processing_job_id: 'job-1',
      revision: 1,
      image_sha256: 'sha',
      reverse_image_policy: 'forbid',
      status: 'succeeded',
      stages: [{ stage: 'visual', status: 'succeeded', task_id: 'visual-1' }],
    }] })
    task.mockResolvedValue({
      task_id: 'visual-1',
      task_type: 'visual_embedding_generation',
      submission_mode: 'pipeline',
      image_stage: 'visual',
      processing_job_id: 'job-1',
      status: 'succeeded',
      image: { meme_id: 'meme-1', filename: 'sample.png', media_url: '/media/meme-1' },
    })

    const wrapper = mount(TasksWorkspace, { attachTo: document.body })
    await flushPromises()

    const parent = wrapper.get('.processing-job')
    expect(parent.element.open).toBe(false)
    await parent.get('.processing-job-parent').trigger('click')
    expect(parent.element.open).toBe(true)

    const child = wrapper.get('.task-stage-row')
    await child.trigger('click')
    await flushPromises()

    expect(task).toHaveBeenCalledWith('visual-1')
    expect(wrapper.get('.task-image-preview img').attributes()).toMatchObject({
      src: '/media/meme-1',
      alt: '处理图片：sample.png',
    })
    expect(document.activeElement).toBe(wrapper.get('[aria-label="关闭任务详情"]').element)

    await wrapper.get('[aria-label="关闭任务详情"]').trigger('click')
    await nextTick()
    expect(document.activeElement).toBe(child.element)
    wrapper.unmount()
  })

  it('warning 自动重命名从阶段详情走 submitImageStage，不调用通用重试', async () => {
    tasks.mockResolvedValue({ items: [], next_cursor: null })
    processingJobs.mockResolvedValue({ items: [{
      task_id: 'job-task',
      task_type: 'image_processing',
      job_id: 'job-1',
      meme_id: 'meme-1',
      processing_job_id: 'job-1',
      revision: 2,
      image_sha256: 'sha',
      reverse_image_policy: 'forbid',
      auto_name: true,
      status: 'succeeded',
      has_warnings: true,
      warnings: [{ stage: 'auto_rename', error: 'name_conflict', recoverable: true }],
      stages: [{ stage: 'auto_rename', status: 'warning', task_id: 'rename-1' }],
    }] })
    task.mockResolvedValue({
      task_id: 'rename-1',
      task_type: 'image_auto_rename',
      submission_mode: 'pipeline',
      image_stage: 'auto_rename',
      image_stage_status: 'warning',
      image_stage_recoverable: true,
      processing_job_id: 'job-1',
      status: 'failed',
      image: { meme_id: 'meme-1', filename: 'sample.png' },
    })
    const wrapper = mount(TasksWorkspace)
    await flushPromises()

    expect(wrapper.text()).toContain('处理完成，自动重命名未完成')
    await wrapper.get('.task-stage-row').trigger('click')
    await flushPromises()
    expect(wrapper.get('.task-drawer').text()).toContain('恢复自动命名')
    const retryButton = wrapper.findAll('.task-drawer > .quiet').find((button) => button.text().includes('恢复自动命名'))
    expect(retryButton).toBeDefined()
    await retryButton.trigger('click')
    await flushPromises()

    expect(submitImageStage).toHaveBeenCalledWith({ meme_id: 'meme-1', stage: 'auto_rename', reverse_image_policy: 'forbid' })
    expect(retryProcessingJob).not.toHaveBeenCalled()
    expect(context).not.toHaveBeenCalled()
    wrapper.unmount()
  })
})
