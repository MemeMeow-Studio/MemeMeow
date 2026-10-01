/** 图片状态文字的直接测试：覆盖方块状态条使用的真实状态取值。 */
import { describe, expect, it } from 'vitest'
import { embeddingLabel, metadataLabel, visualEmbeddingLabel } from '../../utils/presentation'

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
    ['ready', '文本索引已就绪'],
    ['pending', '文本索引待生成'],
    ['waiting_for_text', '文本索引等待文本'],
    ['blocked', '文本索引需修复'],
  ])('显示文本索引状态 %s', (status, label) => {
    expect(embeddingLabel(status)).toBe(label)
  })

  it.each(['ready', 'pending'])('显示视觉向量状态 %s', status => {
    expect(visualEmbeddingLabel(status)).not.toContain('未知')
  })

  it('明确显示未识别的状态', () => {
    expect(metadataLabel()).toContain('未知')
    expect(embeddingLabel('unexpected')).toContain('未知')
    expect(visualEmbeddingLabel('unexpected')).toContain('未知')
  })
})
