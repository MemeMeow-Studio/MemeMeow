/** 图片状态文字的直接测试：覆盖方块状态条使用的真实状态取值。 */
import { describe, expect, it } from 'vitest'
import { embeddingLabel, imageStageLabel, metadataLabel, taskTypeLabel, visualEmbeddingLabel } from '../../utils/presentation'

describe('图片库状态文字', () => {
  it.each([
    ['ready', '语境就绪'],
    ['pending', '待生成'],
    ['partial', '语境不完整'],
    ['repair_required', '需修复'],
  ])('显示语境状态 %s', (status, label) => {
    expect(metadataLabel(status)).toBe(label)
  })

  it.each([
    ['ready', '文本向量已就绪'],
    ['pending', '文本向量待生成'],
    ['waiting_for_text', '文本向量等待文本'],
    ['blocked', '文本向量需修复'],
  ])('显示文本向量状态 %s', (status, label) => {
    expect(embeddingLabel(status)).toBe(label)
  })

  it.each(['ready', 'pending'])('显示视觉向量状态 %s', status => {
    expect(visualEmbeddingLabel(status)).not.toContain('未知')
  })

  it('明确显示未识别的状态', () => {
    expect(metadataLabel()).toContain('未知')
    expect(embeddingLabel()).toBe('文本向量状态未知')
    expect(embeddingLabel('unexpected')).toBe('文本向量状态未知')
    expect(visualEmbeddingLabel('unexpected')).toContain('未知')
  })

  it('文本向量任务和阶段使用一致名称', () => {
    expect(taskTypeLabel('text_embedding_generation')).toBe('文本向量生成')
    expect(imageStageLabel('text_embedding')).toBe('文本向量生成')
  })
})
