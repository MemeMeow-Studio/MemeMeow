<script setup lang="ts">
/** 工作台页脚：提供仓库入口，并展示从 GitHub 动态读取的公开版本信息。 */
import { computed } from 'vue'
import type { RepositoryMetadata } from '../types'

const props = defineProps<{
  metadata: RepositoryMetadata | null
  loading: boolean
}>()

const starsLabel = computed(() => {
  if (typeof props.metadata?.stars === 'number') return new Intl.NumberFormat('zh-CN').format(props.metadata.stars)
  return props.loading ? '加载中...' : '暂不可用'
})

const commitLabel = computed(() => {
  if (props.metadata?.commitHash) return props.metadata.commitHash.slice(0, 7)
  return props.loading ? '加载中...' : '暂不可用'
})

const commitTitle = computed(() => props.metadata?.commitHash || '仓库哈希版本暂不可用')
</script>

<template>
  <footer class="app-footer">
    <div class="app-footer-inner">
      <a
        class="repository-link"
        href="https://github.com/MemeMeow-Studio/MemeMeow"
        target="_blank"
        rel="noopener noreferrer"
      >
        MemeMeow GitHub 仓库
      </a>
      <div class="repository-meta" role="status" aria-live="polite" :aria-busy="loading">
        <span class="repository-stat">
          <span class="repository-stat-label">Stars</span>
          <strong class="repository-stat-value repository-stars">{{ starsLabel }}</strong>
        </span>
        <span class="repository-separator" aria-hidden="true">/</span>
        <span class="repository-stat">
          <span class="repository-stat-label">Hash</span>
          <code class="repository-stat-value repository-hash" :title="commitTitle">{{ commitLabel }}</code>
        </span>
      </div>
    </div>
  </footer>
</template>
