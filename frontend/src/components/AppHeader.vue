<script setup lang="ts">
/** 工作台顶栏：展示品牌与服务连接状态，不持有业务状态。 */
import type { ServiceConfig } from '../types'

defineProps<{
  config: ServiceConfig | null
  embeddingState: string
  embeddingStateLabel: string
}>()
</script>

<template>
  <header class="topbar">
    <div class="brand" aria-label="MemeMeow">
      <span class="brand-mark" aria-hidden="true">M</span>
      <div><strong>MemeMeow</strong></div>
    </div>
    <div class="service-state" role="status" aria-live="polite">
      <span class="pulse" :class="`pulse-${embeddingState}`" aria-hidden="true"></span>
      <span class="service-model" :title="config?.embedding_model || '服务配置加载中'">
        {{ config?.embedding_model || '服务配置加载中' }}
      </span>
      <span class="service-divider" aria-hidden="true"></span>
      <span class="embedding-global" :class="embeddingState">{{ embeddingStateLabel }}</span>
    </div>
  </header>
</template>
