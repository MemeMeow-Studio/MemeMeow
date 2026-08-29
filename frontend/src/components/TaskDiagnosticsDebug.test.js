/** 任务诊断模式的可见性测试，验证显式 debug 构建会恢复技术标识。 */
import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const apiMocks = vi.hoisted(() => ({
  tasks: vi.fn(),
  processingJobs: vi.fn(),
  task: vi.fn(),
  context: vi.fn(),
  retryProcessingJob: vi.fn(),
  submitImageStage: vi.fn(),
  imageMetadata: vi.fn(),
  images: vi.fn(),
  collections: vi.fn(),
  addCollectionItems: vi.fn(),
  createCollection: vi.fn(),
  contextBatch: vi.fn(),
  retryImageStagesBatch: vi.fn(),
  unreadyProcessing: vi.fn(),
  rename: vi.fn(),
}))

vi.mock('../api', () => ({ api: apiMocks }))
vi.mock('../config/debug', () => ({ showTaskDiagnostics: true }))

import ImagePreviewDialog from './ImagePreviewDialog.vue'
import LibraryWorkspace from './LibraryWorkspace.vue'
import TaskDrawer from './TaskDrawer.vue'
import TasksWorkspace from './TasksWorkspace.vue'

const metadata = {
  schema_version: 1,
  image: { relative_path: 'sample.png', extension: '.png', size_bytes: 2048, sha256: 'a'.repeat(64) },
  context_status: 'ready',
  meme_context: { title: '标题' },
  provenance: {},
}

beforeEach(() => {
  vi.clearAllMocks()
  apiMocks.tasks.mockResolvedValue({ items: [], next_cursor: null })
  apiMocks.processingJobs.mockResolvedValue({ items: [] })
  apiMocks.imageMetadata.mockResolvedValue(metadata)
  apiMocks.images.mockResolvedValue({ items: [] })
  apiMocks.collections.mockResolvedValue({ items: [] })
  apiMocks.unreadyProcessing.mockResolvedValue({
    target_count: 1,
    submitted_count: 1,
    reused_count: 0,
    conflict_count: 0,
    failed_count: 0,
    results: [{ meme_id: 'meme-debug', processing_job_id: 'job-debug', status: 'submitted' }],
  })
})

describe('任务诊断模式', () => {
  it('任务工作区显示父 Job 和阶段标识', async () => {
    apiMocks.processingJobs.mockResolvedValue({ items: [{
      task_id: 'job-task-debug',
      task_type: 'image_processing',
      job_id: 'job-debug',
      meme_id: 'meme-debug',
      processing_job_id: 'job-debug',
      revision: 2,
      image_sha256: 'a'.repeat(64),
      reverse_image_policy: 'forbid',
      status: 'succeeded',
      stages: [{ stage: 'visual', status: 'succeeded', task_id: 'visual-debug', attempt: 1 }],
    }] })

    const wrapper = mount(TasksWorkspace)
    await flushPromises()

    expect(wrapper.text()).toContain('图片 ID meme-debug')
    expect(wrapper.text()).toContain('Job ID job-debug')
    expect(wrapper.text()).toContain('visual-debug')
    wrapper.unmount()
  })

  it('任务详情、图片详情和重试结果显示诊断标识', async () => {
    const taskWrapper = mount(TaskDrawer, {
      props: {
        task: {
          task_id: 'task-debug',
          task_type: 'visual_embedding_generation',
          processing_job_id: 'job-debug',
          status: 'succeeded',
        },
      },
    })
    expect(taskWrapper.text()).toContain('task-debug')
    expect(taskWrapper.text()).toContain('job-debug')
    taskWrapper.unmount()

    const imageWrapper = mount(ImagePreviewDialog, {
      props: {
        image: {
          meme_id: 'meme-debug',
          filename: 'sample.png',
          media_url: '/media/meme-debug',
          processing_job_id: 'job-debug',
        },
      },
    })
    await flushPromises()
    expect(imageWrapper.findAll('.metadata-details')).toHaveLength(2)
    await imageWrapper.find('.metadata-details').get('summary').trigger('click')
    expect(imageWrapper.find('.metadata-details').text()).toContain('aaaaaaaaaaaa...')
    expect(imageWrapper.get('.image-processing-details').text()).toContain('job-debug')
    imageWrapper.unmount()

    const libraryWrapper = mount(LibraryWorkspace, {
      props: { config: null, cacheTask: null, cacheBusy: false, refreshToken: 0 },
    })
    await flushPromises()
    await libraryWrapper.findAll('button').find((button) => button.text().includes('完整重试所有未就绪')).trigger('click')
    await libraryWrapper.get('.processing-options-dialog form').trigger('submit')
    await flushPromises()
    expect(libraryWrapper.get('.processing-result-details').text()).toContain('meme-debug')
    expect(libraryWrapper.get('.processing-result-details').text()).toContain('job-debug')
    libraryWrapper.unmount()
  })
})
