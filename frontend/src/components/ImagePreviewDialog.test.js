/** 图片预览元数据分层展示、状态映射和安全详情的行为测试。 */
import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const { imageMetadata } = vi.hoisted(() => ({ imageMetadata: vi.fn() }))

vi.mock('../api', () => ({
  api: { imageMetadata },
}))

import ImagePreviewDialog from './ImagePreviewDialog.vue'

const baseImage = {
  meme_id: 'meme-preview-1',
  filename: 'sample.png',
  media_url: '/media/meme-preview-1',
  size: 2048,
  extension: '.png',
  metadata: { status: 'pending' },
  processing_status: 'running',
  processing_stages: [{ stage: 'agent', status: 'running' }],
}

/** 构造覆盖图片身份、语境和来源字段的元数据响应。 */
function metadataPayload(status, context = {}, provenance = {}) {
  return {
    schema_version: 1,
    image: {
      relative_path: '/srv/private/sample.png',
      extension: '.png',
      size_bytes: 2048,
      sha256: 'a'.repeat(64),
    },
    context_status: status,
    meme_context: context,
    provenance,
  }
}

/** 挂载对话框并等待元数据请求结束，返回可查询的组件包装器。 */
async function mountDialog(payload, image = baseImage) {
  imageMetadata.mockResolvedValue(payload)
  const wrapper = mount(ImagePreviewDialog, { props: { image } })
  await flushPromises()
  return wrapper
}

describe('ImagePreviewDialog', () => {
  beforeEach(() => {
    imageMetadata.mockReset()
  })

  it('按固定顺序展示非空摘要字段并隐藏空字段', async () => {
    const wrapper = await mountDialog(metadataPayload('ready', {
      title: '一张标题',
      summary: '',
      subjects: ['主体一', '主体二'],
      visible_text: ['画面文字'],
      meaning: null,
      keywords: ['关键词'],
    }))

    const summary = wrapper.get('.metadata-summary-list')
    expect(summary.findAll('dt').map((item) => item.text())).toEqual(['标题', '主体', '图片文字', '关键词'])
    expect(summary.text()).toContain('一张标题')
    expect(summary.text()).toContain('主体一、主体二')
    expect(wrapper.get('.metadata-context-status').text()).toContain('已就绪')
    wrapper.unmount()
  })

  it.each([
    ['pending', '待生成'],
    ['partial', '部分完成'],
    ['ready', '已就绪'],
    ['repair_required', '需要修复'],
  ])('将 %s 映射为稳定中文状态', async (status, label) => {
    const context = status === 'partial' ? { summary: '部分摘要' } : {}
    const provenance = status === 'repair_required' ? { last_error: 'metadata_image_mismatch' } : {}
    const wrapper = await mountDialog(metadataPayload(status, context, provenance))

    expect(wrapper.get('.metadata-context-status').text()).toContain(label)
    expect(wrapper.get('.metadata-context-status').text()).not.toContain('状态未知')
    if (status === 'pending') {
      expect(wrapper.get('.metadata-empty-state').text()).toBe('图片语境尚未生成，完成处理后会显示识别结果')
    }
    if (status === 'partial') expect(wrapper.get('.metadata-summary-list').text()).toContain('部分摘要')
    if (status === 'repair_required') expect(wrapper.get('.metadata-context-status').text()).toContain('修复')
    wrapper.unmount()
  })

  it('默认折叠更多信息和原始 JSON，并安全展示文件、来源与诊断', async () => {
    const payload = metadataPayload('ready', {
      title: '标题',
      source_urls: ['https://example.com/articles/meme', 'javascript:alert(1)', '/local/source'],
      references: ['《已确认引用》'],
      uncertainties: ['出处仍待确认'],
    }, {
      producer: 'research',
      updated_at: '2026-08-26T08:30:00Z',
      last_error: 'metadata_image_mismatch',
    })
    payload.errors = ['failed at /srv/private/sample.png']
    const wrapper = await mountDialog(payload)

    const details = wrapper.findAll('.metadata-details')
    expect(details).toHaveLength(2)
    expect(details.every((item) => item.element.open)).toBe(false)
    expect(wrapper.findAll('button').some((button) => button.text().includes('复制元数据'))).toBe(false)

    await details[0].get('summary').trigger('click')
    const detailText = details[0].text()
    expect(detailText).toContain('文件名')
    expect(detailText).toContain('sample.png')
    expect(detailText).not.toContain('/srv/private/sample.png')
    expect(detailText).toContain('PNG')
    expect(detailText).toContain('2.0 KB')
    expect(detailText).toContain('aaaaaaaaaaaa...')
    expect(detailText).toContain('外部来源')
    expect(detailText).toContain('example.com')
    expect(detailText).toContain('https://example.com/articles/meme')
    expect(detailText).not.toContain('javascript:alert(1)')
    expect(detailText).not.toContain('/local/source')
    expect(detailText).toContain('文本引用')
    expect(detailText).toContain('《已确认引用》')
    expect(detailText).toContain('不确定项')
    expect(detailText).toContain('出处仍待确认')
    expect(detailText).toContain('更新时间')
    expect(detailText).toContain('错误')
    expect(detailText).toContain('图片文件已变化，需要修复元数据')
    expect(detailText).toContain('failed at [路径已隐藏]')
    expect(detailText).not.toContain('failed at /srv/private/sample.png')

    const source = wrapper.get('.metadata-source-item a')
    expect(source.text()).toBe('example.com')
    expect(source.attributes('href')).toBe('https://example.com/articles/meme')
    expect(source.attributes('title')).toBe('https://example.com/articles/meme')

    await details[1].get('summary').trigger('click')
    expect(wrapper.get('.metadata-json').text()).toContain('"context_status": "ready"')
    expect(wrapper.get('.metadata-json').attributes('tabindex')).toBe('0')
    expect(wrapper.get('.image-processing-details').text()).toContain('Agent 语境')
    wrapper.unmount()
  })

  it('读取元数据失败时仍保留文件信息和可理解错误', async () => {
    imageMetadata.mockRejectedValue(new Error('failed at /srv/private/sample.png'))
    const wrapper = mount(ImagePreviewDialog, {
      props: { image: { ...baseImage, metadata: { status: 'repair_required' } } },
    })
    await flushPromises()

    expect(wrapper.get('.metadata-file-summary').text()).toContain('sample.png')
    expect(wrapper.get('.metadata-context-status').text()).toContain('需要修复')
    expect(wrapper.get('.metadata-error').text()).toBe('failed at [路径已隐藏]')
    await wrapper.get('.metadata-details summary').trigger('click')
    expect(wrapper.get('.metadata-error-group').text()).toContain('failed at [路径已隐藏]')
    wrapper.unmount()
  })
})
